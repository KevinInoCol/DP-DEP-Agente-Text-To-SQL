"""
Traza de las tools que el agente elige y ejecuta en un turno.

Es un callback handler de LangChain: se pasa en el config de cada invoke y
registra, en orden, qué tool se llamó, con qué argumentos, cuánto tardó y si
salió bien. Se usa un trazador NUEVO por turno, así la traza nunca mezcla
llamadas de mensajes distintos.

Por qué callbacks y no leer los mensajes: los ToolMessage dicen QUÉ devolvió
cada tool, pero no cuándo empezó ni cuánto tardó. La duración es justo lo que
hace útil la traza para diagnosticar (una consulta lenta, un subagente que se
atasca).

Ejecutar (prueba sin LLM):
    python -m observabilidad.tool_tracer
"""

import json
import time
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

MAX_CARACTERES_ARGUMENTO = 400

# Cómo resumir en una línea la salida de cada tool del proyecto.
# Clave: nombre de la tool. Valor: función datos(dict) -> str.
RESUMIDORES = {
    "consultar_bigquery_tool": lambda d: (
        f"{d.get('filas_devueltas', '?')} fila(s) · {d.get('gb_procesados', '?')} GB procesados"
    ),
    "generar_grafico_tool": lambda d: (
        f"{(d.get('grafico') or {}).get('tipo', '?')} · {(d.get('grafico') or {}).get('titulo', '')}"
    ),
    "buscar_en_internet_tool": lambda d: f"{len(d.get('fuentes') or [])} fuente(s) web",
}

ETIQUETAS = {
    "consultar_bigquery_tool": ("🗄️", "Consulta a BigQuery"),
    "generar_grafico_tool": ("📊", "Subagente de gráficos"),
    "buscar_en_internet_tool": ("🌐", "Búsqueda web (Tavily)"),
}


def etiqueta_tool(nombre: str) -> tuple[str, str]:
    """Icono y nombre legible de una tool; genérico si no está registrada."""
    return ETIQUETAS.get(nombre, ("🔧", nombre))


def _recortar(valor: Any) -> Any:
    if isinstance(valor, str) and len(valor) > MAX_CARACTERES_ARGUMENTO:
        return valor[:MAX_CARACTERES_ARGUMENTO].rstrip() + "…"
    return valor


def _resumir(nombre: str, salida: Any) -> tuple[bool, str]:
    """(ok, resumen legible) a partir de la salida cruda de la tool."""
    texto = getattr(salida, "content", salida)
    if not isinstance(texto, str):
        texto = str(texto)
    try:
        datos = json.loads(texto)
    except (json.JSONDecodeError, TypeError):
        return True, _recortar(texto)
    if not isinstance(datos, dict):
        return True, _recortar(texto)
    if datos.get("ok") is False:
        return False, str(datos.get("error", "error sin detalle"))
    resumidor = RESUMIDORES.get(nombre)
    return True, (resumidor(datos) if resumidor else "ok")


class TrazadorDeTools(BaseCallbackHandler):
    """Acumula una entrada por llamada a tool. Un trazador por turno."""

    def __init__(self) -> None:
        self.llamadas: list[dict] = []
        self._por_run: dict[UUID, dict] = {}

    # --- callbacks de LangChain ---

    def on_tool_start(
        self,
        serialized: dict,
        input_str: str,
        *,
        run_id: UUID,
        inputs: dict | None = None,
        **kwargs: Any,
    ) -> None:
        nombre = (serialized or {}).get("name") or kwargs.get("name") or "tool"
        argumentos = {k: _recortar(v) for k, v in (inputs or {}).items()} if inputs else {"input": _recortar(input_str)}
        entrada = {
            "orden": len(self.llamadas) + 1,
            "tool": nombre,
            "argumentos": argumentos,
            "ok": None,
            "resumen": "",
            "duracion_s": None,
        }
        self.llamadas.append(entrada)
        self._por_run[run_id] = {"entrada": entrada, "inicio": time.perf_counter()}

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        registro = self._por_run.pop(run_id, None)
        if registro is None:
            return
        entrada = registro["entrada"]
        entrada["ok"], entrada["resumen"] = _resumir(entrada["tool"], output)
        entrada["duracion_s"] = round(time.perf_counter() - registro["inicio"], 2)

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        registro = self._por_run.pop(run_id, None)
        if registro is None:
            return
        entrada = registro["entrada"]
        entrada["ok"] = False
        entrada["resumen"] = f"Excepción: {type(error).__name__}: {error}"
        entrada["duracion_s"] = round(time.perf_counter() - registro["inicio"], 2)

    # --- lectura ---

    def traza(self) -> list[dict]:
        """Las llamadas en el orden en que el agente las decidió."""
        return self.llamadas

    def resumen(self) -> str:
        """Una línea: '🗄️ BigQuery → 📊 Gráfico' para logs y para el título del desplegable."""
        return " → ".join(f"{etiqueta_tool(l['tool'])[0]} {etiqueta_tool(l['tool'])[1]}" for l in self.llamadas)


if __name__ == "__main__":
    from uuid import uuid4

    t = TrazadorDeTools()
    r1, r2 = uuid4(), uuid4()
    t.on_tool_start({"name": "consultar_bigquery_tool"}, "", run_id=r1, inputs={"sql": "SELECT 1 " * 80})
    t.on_tool_end(json.dumps({"ok": True, "filas_devueltas": 3, "gb_procesados": 0.59}), run_id=r1)
    t.on_tool_start({"name": "generar_grafico_tool"}, "", run_id=r2, inputs={"consulta_id": "ab12", "pregunta": "¿rutas?"})
    t.on_tool_error(TimeoutError("se agotó el tiempo"), run_id=r2)
    print(json.dumps(t.traza(), indent=2, ensure_ascii=False))
    print("resumen:", t.resumen())
