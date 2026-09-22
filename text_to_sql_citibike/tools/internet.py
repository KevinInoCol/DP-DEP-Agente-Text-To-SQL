"""
Tool de búsqueda en internet (Tavily) para COMPLEMENTAR las respuestas del agente.

Envuelve TavilySearch (paquete partner langchain-tavily) en el patrón del proyecto:
función imperativa testeable + factory que devuelve la tool. Devuelve un JSON
acotado (título, URL, extracto, puntuación) para que el agente cite fuentes.

Regla de negocio (reforzada en el system prompt): la web solo añade contexto
—por qué una ruta es popular, qué hay en una estación, eventos de una fecha—.
Nunca sustituye ni contradice las cifras que vienen de BigQuery.

Si TAVILY_API_KEY no está definida, esta_disponible() devuelve False y agent.py
no registra la tool: el agente sigue funcionando sin búsqueda web.

Ejecutar (prueba directa sin LLM):
    python -m tools.internet
"""

import json
import os

from langchain.tools import tool
from langchain_tavily import TavilySearch

from dotenv import load_dotenv

load_dotenv()

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
TAVILY_MAX_RESULTADOS = int(os.getenv("TAVILY_MAX_RESULTADOS", "4"))
TAVILY_SEARCH_DEPTH = os.getenv("TAVILY_SEARCH_DEPTH", "basic")  # basic | advanced (advanced cuesta 2 créditos)

MAX_CARACTERES_EXTRACTO = 600

_buscador: TavilySearch | None = None


def esta_disponible() -> bool:
    """True si hay API key: agent.py solo registra la tool en ese caso."""
    return bool(TAVILY_API_KEY)


def get_buscador() -> TavilySearch:
    """Cliente de Tavily como singleton de módulo."""
    global _buscador
    if _buscador is None:
        _buscador = TavilySearch(
            max_results=TAVILY_MAX_RESULTADOS,
            search_depth=TAVILY_SEARCH_DEPTH,
            topic="general",
            include_answer=False,
            include_raw_content=False,
            tavily_api_key=TAVILY_API_KEY,
        )
    return _buscador


def _formatear_resultados(bruto: dict) -> list[dict]:
    fuentes = []
    for r in bruto.get("results", []):
        extracto = (r.get("content") or "").strip()
        if len(extracto) > MAX_CARACTERES_EXTRACTO:
            extracto = extracto[:MAX_CARACTERES_EXTRACTO].rsplit(" ", 1)[0] + "…"
        fuentes.append(
            {
                "titulo": r.get("title") or "(sin título)",
                "url": r.get("url"),
                "extracto": extracto,
                "puntuacion": round(r.get("score") or 0, 3),
            }
        )
    return fuentes


def buscar_en_internet(consulta: str) -> dict:
    """
    Función imperativa: consulta → {"ok", "consulta", "fuentes": [...]}.
    Nunca lanza: los errores vuelven en 'error' para que el LLM siga sin web.
    """
    consulta = (consulta or "").strip()
    if not consulta:
        return {"ok": False, "consulta": consulta, "error": "La consulta de búsqueda está vacía."}
    if not esta_disponible():
        return {"ok": False, "consulta": consulta, "error": "Búsqueda web no configurada (falta TAVILY_API_KEY)."}
    try:
        bruto = get_buscador().invoke({"query": consulta})
        if isinstance(bruto, str):  # TavilySearch devuelve str cuando la API falla
            return {"ok": False, "consulta": consulta, "error": bruto}
        fuentes = _formatear_resultados(bruto)
        if not fuentes:
            return {"ok": False, "consulta": consulta, "error": "La búsqueda no devolvió resultados."}
        return {"ok": True, "consulta": consulta, "fuentes": fuentes}
    except Exception as e:  # noqa: BLE001 — la tool nunca debe tumbar al agente
        return {"ok": False, "consulta": consulta, "error": f"Error en Tavily: {type(e).__name__}: {e}"}


def get_buscar_en_internet_tool():
    """Factory: devuelve la tool que el agente principal registra en create_agent."""

    @tool
    def buscar_en_internet_tool(consulta: str) -> str:
        """Busca en internet contexto COMPLEMENTARIO para enriquecer una respuesta que
        ya está respaldada por datos de BigQuery: por qué una ruta o estación es
        popular (parques, atracciones, oficinas, transbordos), qué ocurrió en una
        fecha con muchos o pocos viajes, qué es un programa o tarifa de CitiBike.

        Úsala SOLO después de consultar_bigquery_tool y solo si el contexto externo
        aporta valor a la pregunta. No la uses para obtener cifras de viajes,
        duraciones ni rankings: esas vienen exclusivamente de la base de datos.
        Máximo 2 búsquedas por respuesta. Devuelve título, URL y extracto de cada
        fuente; cita las URLs que uses.

        Args:
            consulta: búsqueda concreta y específica, preferiblemente en inglés y
                      con el contexto de Nueva York. Ejemplo:
                      "Central Park S & 6 Ave Citi Bike station popularity tourists"
        """
        return json.dumps(buscar_en_internet(consulta), ensure_ascii=False)

    return buscar_en_internet_tool


if __name__ == "__main__":
    print("disponible:", esta_disponible())
    print(json.dumps(buscar_en_internet("Why is Central Park S & 6 Ave the busiest Citi Bike station"), indent=2, ensure_ascii=False))
