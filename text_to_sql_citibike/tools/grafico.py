"""
Tool de gráficos: envuelve al subagente de visualización (subagents/grafico.py).

Recupera las filas de la caché por consulta_id, pide al subagente la
especificación del gráfico, la valida contra las columnas reales y devuelve un
JSON con la especificación MÁS los datos ya preparados (categorías, valores,
series). La UI (ui/graficos.py) dibuja a partir de ese JSON; el agente principal
solo lee el tipo de gráfico y el título para mencionarlo en su respuesta.

Ejecutar (prueba directa sin LLM principal):
    python -m tools.grafico
"""

import json
import numbers

from langchain.tools import tool

from subagents.grafico import EspecificacionGrafico, disenar_grafico
from tools.resultados_cache import obtener_resultado

MAX_SECTORES_PASTEL = 6
MAX_SERIES = 6
SEPARADOR_CATEGORIA = " → "


def _tipo_columna(filas: list[dict], col: str) -> str:
    for f in filas:
        v = f.get(col)
        if v is None:
            continue
        if isinstance(v, bool):
            return "bool"
        if isinstance(v, numbers.Number):
            return "numero"
        return "texto"
    return "nulo"


def _inferir_columnas(filas: list[dict]) -> dict[str, str]:
    return {col: _tipo_columna(filas, col) for col in filas[0].keys()}


def _etiqueta(fila: dict, columnas: list[str]) -> str:
    return SEPARADOR_CATEGORIA.join(str(fila.get(c, "")) for c in columnas)


def _preparar_datos(spec: EspecificacionGrafico, filas: list[dict]) -> dict:
    """Convierte spec + filas en las listas que necesita la UI."""
    if spec.columna_serie:
        # Varias series: categorías únicas en orden de aparición, una lista de valores por serie.
        categorias = list(dict.fromkeys(_etiqueta(f, spec.columnas_categoria) for f in filas))
        series: dict[str, list] = {}
        for f in filas:
            nombre = str(f.get(spec.columna_serie))
            series.setdefault(nombre, [None] * len(categorias))
            series[nombre][categorias.index(_etiqueta(f, spec.columnas_categoria))] = f.get(spec.columna_valor)
        if len(series) > MAX_SERIES:
            series = dict(list(series.items())[:MAX_SERIES])
        return {"categorias": categorias, "valores": None, "series": series}

    if spec.tipo == "dispersion":
        return {
            "categorias": [f.get(spec.columnas_categoria[0]) for f in filas],
            "valores": [f.get(spec.columna_valor) for f in filas],
            "series": None,
        }

    return {
        "categorias": [_etiqueta(f, spec.columnas_categoria) for f in filas],
        "valores": [f.get(spec.columna_valor) for f in filas],
        "series": None,
    }


def generar_grafico(consulta_id: str, pregunta: str) -> dict:
    """
    Función imperativa: consulta_id + pregunta → dict con la especificación y los datos.
    Nunca lanza; los errores vuelven en 'error' para que el LLM principal los lea.
    """
    resultado = obtener_resultado(consulta_id)
    if resultado is None:
        return {"ok": False, "error": f"No existe ningún resultado con consulta_id '{consulta_id}'. Ejecuta primero consultar_bigquery_tool."}
    filas = resultado["filas"]
    if len(filas) < 2:
        return {"ok": False, "error": "El resultado tiene menos de 2 filas: no tiene sentido graficarlo. Responde solo con el valor."}

    columnas = _inferir_columnas(filas)
    if not any(t == "numero" for t in columnas.values()):
        return {"ok": False, "error": "El resultado no tiene ninguna columna numérica que graficar."}

    try:
        spec = disenar_grafico(pregunta, columnas, filas)
    except Exception as e:  # noqa: BLE001 — la tool nunca debe tumbar al agente
        return {"ok": False, "error": f"El subagente de gráficos falló: {type(e).__name__}: {e}"}

    # Validaciones contra las columnas reales.
    faltantes = [c for c in [*spec.columnas_categoria, spec.columna_valor, spec.columna_serie] if c and c not in columnas]
    if faltantes:
        return {"ok": False, "error": f"El subagente propuso columnas inexistentes: {faltantes}. Columnas disponibles: {list(columnas)}."}
    if columnas[spec.columna_valor] != "numero":
        return {"ok": False, "error": f"La columna de valor '{spec.columna_valor}' no es numérica."}

    ajuste = None
    if spec.tipo == "pastel" and len(filas) > MAX_SECTORES_PASTEL:
        spec.tipo = "barras_horizontales"
        ajuste = f"Se cambió pastel por barras horizontales: {len(filas)} categorías son demasiadas para un pastel."

    datos = _preparar_datos(spec, filas)
    return {
        "ok": True,
        "grafico": {
            **spec.model_dump(),
            **datos,
            "consulta_id": consulta_id,
            "ajuste": ajuste,
        },
    }


def get_generar_grafico_tool():
    """Factory: devuelve la tool que el agente principal registra en create_agent."""

    @tool
    def generar_grafico_tool(consulta_id: str, pregunta: str) -> str:
        """Genera un gráfico (barras, barras horizontales, líneas, pastel o dispersión)
        a partir del resultado de una consulta ya ejecutada. Un subagente especialista
        elige el tipo de gráfico y las columnas; la interfaz lo dibuja automáticamente
        debajo de tu respuesta.

        Úsala SIEMPRE después de consultar_bigquery_tool cuando el resultado tenga 2 o
        más filas y al menos una columna numérica (rankings, comparaciones, series
        temporales, participaciones). No la uses para resultados de un solo valor.

        Si la respuesta trae "ok": false, lee "error": si dice que el resultado no es
        graficable, sigue sin gráfico; no reintentes más de una vez.

        Args:
            consulta_id: el "consulta_id" devuelto por consultar_bigquery_tool.
            pregunta: la pregunta original del usuario, tal como la escribió, para que
                      el gráfico responda a lo que pidió.
        """
        return json.dumps(generar_grafico(consulta_id, pregunta), ensure_ascii=False, default=str)

    return generar_grafico_tool


if __name__ == "__main__":
    from tools.resultados_cache import guardar_resultado

    filas_demo = [
        {"start_station_name": "Central Park S & 6 Ave", "end_station_name": "Central Park S & 6 Ave", "total_viajes": 55703},
        {"start_station_name": "Grand Army Plaza & Central Park S", "end_station_name": "Grand Army Plaza & Central Park S", "total_viajes": 25573},
        {"start_station_name": "12 Ave & W 40 St", "end_station_name": "West St & Chambers St", "total_viajes": 18667},
    ]
    cid = guardar_resultado("SELECT ...", filas_demo)
    print(json.dumps(generar_grafico(cid, "¿Cuáles son las rutas más famosas?"), indent=2, ensure_ascii=False))
