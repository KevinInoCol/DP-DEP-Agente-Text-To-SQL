"""
Tool de conexión a BigQuery para el agente Text-to-SQL.

Expone dos cosas, siguiendo el patrón de la skill agent-project-structure:
- consultar_bigquery(sql): función imperativa, testeable sin LLM.
- get_consultar_bigquery_tool(): factory que devuelve la tool para create_agent.

Seguridad: solo se admiten sentencias SELECT / WITH. Antes de ejecutar se hace
un dry run para validar sintaxis y estimar bytes, y se rechaza la consulta si
supera BQ_MAX_BYTES_BILLED. La autenticación usa Application Default
Credentials (gcloud auth application-default login) o GOOGLE_APPLICATION_CREDENTIALS.

Ejecutar (prueba directa sin LLM):
    python -m tools.bigquery
"""

import datetime as dt
import decimal
import json
import os
import re

from google.api_core import exceptions as gcp_exceptions
from google.cloud import bigquery
from langchain.tools import tool

from dotenv import load_dotenv

from tools.resultados_cache import guardar_resultado

load_dotenv()

GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT")
BQ_MAX_BYTES_BILLED = int(os.getenv("BQ_MAX_BYTES_BILLED", str(5 * 1024**3)))  # 5 GB; la tabla completa pesa ~7.5 GB
BQ_MAX_ROWS = int(os.getenv("BQ_MAX_ROWS", "100"))
BQ_TIMEOUT_SECONDS = int(os.getenv("BQ_TIMEOUT_SECONDS", "60"))

BQ_TABLE = "bigquery-public-data.new_york_citibike.citibike_trips"

# Palabras que convierten una consulta en algo distinto de una lectura.
PALABRAS_PROHIBIDAS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|"
    r"CALL|EXECUTE|BEGIN|COMMIT|ROLLBACK|DECLARE|SET|EXPORT|LOAD)\b",
    re.IGNORECASE,
)

_client: bigquery.Client | None = None


def get_client() -> bigquery.Client:
    """Cliente de BigQuery como singleton de módulo (se crea una sola vez)."""
    global _client
    if _client is None:
        _client = bigquery.Client(project=GOOGLE_CLOUD_PROJECT)
    return _client


def _limpiar_sql(sql: str) -> str:
    """Quita comentarios, fences de markdown y el ';' final."""
    sql = re.sub(r"```(?:sql)?", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"--[^\n]*", "", sql)
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return sql.strip().rstrip(";").strip()


def validar_solo_lectura(sql: str) -> str | None:
    """Devuelve un mensaje de error si el SQL no es una lectura simple, o None si es válido."""
    if not sql:
        return "La consulta está vacía."
    if ";" in sql:
        return "Solo se admite una sentencia por consulta (no uses ';')."
    if not re.match(r"^\s*(SELECT|WITH)\b", sql, re.IGNORECASE):
        return "Solo se admiten consultas SELECT o WITH ... SELECT."
    prohibida = PALABRAS_PROHIBIDAS.search(sql)
    if prohibida:
        return f"Palabra no permitida en una consulta de solo lectura: {prohibida.group(0).upper()}."
    return None


def _mensaje_error(e: Exception) -> str:
    """Quita el prefijo 'POST https://bigquery.googleapis.com/...: ' para que el LLM lea solo la causa."""
    msg = getattr(e, "message", str(e))
    return re.sub(r"^(POST|GET) https://\S+:\s*", "", msg).strip()


def _serializar(valor):
    """Convierte tipos de BigQuery a algo que json.dumps acepte."""
    if isinstance(valor, (dt.datetime, dt.date, dt.time)):
        return valor.isoformat()
    if isinstance(valor, decimal.Decimal):
        return float(valor)
    if isinstance(valor, bytes):
        return valor.decode("utf-8", errors="replace")
    return valor


def consultar_bigquery(sql: str) -> dict:
    """
    Ejecuta una consulta de solo lectura en BigQuery.

    Devuelve un dict serializable con las claves:
      ok, sql, filas, total_filas, bytes_procesados, gb_procesados, error.
    Nunca lanza: los errores vuelven en la clave 'error' para que el LLM pueda corregir.
    """
    sql = _limpiar_sql(sql)
    error = validar_solo_lectura(sql)
    if error:
        return {"ok": False, "sql": sql, "error": error}

    client = get_client()
    try:
        # 1) Dry run: valida sintaxis y estima bytes sin cobrar nada.
        dry = client.query(
            sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        )
        if dry.total_bytes_processed > BQ_MAX_BYTES_BILLED:
            return {
                "ok": False,
                "sql": sql,
                "error": (
                    f"La consulta procesaría {dry.total_bytes_processed / 1024**3:.2f} GB, "
                    f"por encima del límite de {BQ_MAX_BYTES_BILLED / 1024**3:.2f} GB. "
                    "Reduce columnas o añade filtros."
                ),
            }

        # 2) Ejecución real con tope de bytes facturables.
        job = client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                maximum_bytes_billed=BQ_MAX_BYTES_BILLED, use_query_cache=True
            ),
        )
        resultado = job.result(timeout=BQ_TIMEOUT_SECONDS, max_results=BQ_MAX_ROWS)
        filas = [
            {k: _serializar(v) for k, v in dict(fila).items()} for fila in resultado
        ]
        consulta_id = guardar_resultado(sql, filas)
        return {
            "ok": True,
            "consulta_id": consulta_id,
            "sql": sql,
            "filas": filas,
            "total_filas": resultado.total_rows,
            "filas_devueltas": len(filas),
            "bytes_procesados": job.total_bytes_processed,
            "gb_procesados": round((job.total_bytes_processed or 0) / 1024**3, 4),
        }
    except gcp_exceptions.BadRequest as e:
        return {"ok": False, "sql": sql, "error": f"Error de sintaxis o semántica en BigQuery: {_mensaje_error(e)}"}
    except gcp_exceptions.Forbidden as e:
        return {"ok": False, "sql": sql, "error": f"Permisos insuficientes o proyecto sin facturación: {_mensaje_error(e)}"}
    except gcp_exceptions.GoogleAPICallError as e:
        return {"ok": False, "sql": sql, "error": f"Error de BigQuery: {_mensaje_error(e)}"}
    except Exception as e:  # noqa: BLE001 — la tool nunca debe tumbar al agente
        return {"ok": False, "sql": sql, "error": f"Error inesperado: {type(e).__name__}: {e}"}


def obtener_esquema_tabla() -> list[dict]:
    """
    Lee el esquema real de la tabla desde BigQuery, incluida la descripción de cada
    columna que mantiene el dataset público. Se usa para inyectarlo en el prompt.

    Devuelve una lista de dicts: {"nombre", "tipo", "modo", "descripcion"}.
    """
    tabla = get_client().get_table(BQ_TABLE)
    return [
        {
            "nombre": campo.name,
            "tipo": campo.field_type,
            "modo": campo.mode,
            "descripcion": campo.description or "",
        }
        for campo in tabla.schema
    ]


def obtener_metadatos_tabla() -> dict:
    """Tamaño de la tabla, para que el prompt sepa cuánto cuesta leerla entera."""
    tabla = get_client().get_table(BQ_TABLE)
    return {
        "total_filas": tabla.num_rows,
        "gb_tabla": round((tabla.num_bytes or 0) / 1024**3, 2),
    }


def get_consultar_bigquery_tool():
    """Factory: devuelve la tool que el agente registra en create_agent."""

    @tool
    def consultar_bigquery_tool(sql: str) -> str:
        """Ejecuta una consulta SQL de SOLO LECTURA (SELECT o WITH) en Google BigQuery
        sobre la tabla `bigquery-public-data.new_york_citibike.citibike_trips` y
        devuelve las filas en JSON.

        Úsala cada vez que necesites datos reales para responder al usuario.
        Es la única fuente de datos: nunca respondas cifras sin llamarla.

        Si la respuesta trae "ok": false, lee "error", corrige el SQL y vuelve a
        llamarla. Errores típicos: columna inexistente, función TIMESTAMP sobre una
        columna DATETIME, tabla sin backticks, consulta que supera el límite de GB.

        Args:
            sql: consulta completa en BigQuery Standard SQL. Debe referenciar la
                 tabla con su nombre completo entre backticks. Máximo una sentencia.
                 Ejemplo: SELECT usertype, COUNT(*) AS total_viajes
                          FROM `bigquery-public-data.new_york_citibike.citibike_trips`
                          GROUP BY usertype ORDER BY total_viajes DESC LIMIT 10
        """
        return json.dumps(consultar_bigquery(sql), ensure_ascii=False, default=str)

    return consultar_bigquery_tool


if __name__ == "__main__":
    print("Esquema:", obtener_esquema_tabla())
    demo = f"SELECT usertype, COUNT(*) AS total_viajes FROM `{BQ_TABLE}` GROUP BY usertype ORDER BY total_viajes DESC LIMIT 5"
    print(json.dumps(consultar_bigquery(demo), indent=2, ensure_ascii=False))
    print(json.dumps(consultar_bigquery(f"DELETE FROM `{BQ_TABLE}` WHERE TRUE"), indent=2, ensure_ascii=False))
