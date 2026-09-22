"""
Orquestador del agente Text-to-SQL sobre CitiBike (BigQuery).

Ensambla las piezas: LLM (model_config/), system prompt (prompt/), tool de
BigQuery (tools/) y memoria (chat_history/). No contiene lógica de negocio.

Patrón: init_resources() se llama UNA vez al arrancar y deja singletons de
módulo; build_agent() es barato y se puede llamar por mensaje.

Ejecutar (una pregunta suelta):
    python agent.py "¿Cuántos viajes hicieron los Subscribers en 2016?"
"""

import json
import sys
from pathlib import Path

import yaml
from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
from langchain.messages import ToolMessage

from dotenv import load_dotenv

from chat_history import get_checkpointer
from tools import BQ_TABLE, get_consultar_bigquery_tool, obtener_esquema_tabla

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
RUTA_MODEL_CONFIG = BASE_DIR / "model_config" / "model.yaml"
RUTA_SYSTEM_PROMPT = BASE_DIR / "prompt" / "system_prompt.yaml"

_llm = None
_system_prompt: str | None = None
_bigquery_tool = None
_checkpointer = None


def _cargar_yaml(ruta: Path) -> dict:
    with open(ruta, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _render_system_prompt() -> str:
    """Carga el YAML del prompt e inyecta los placeholders con .replace()."""
    prompt_cfg = _cargar_yaml(RUTA_SYSTEM_PROMPT)
    esquema = obtener_esquema_tabla()
    esquema_txt = "\n".join(f"  - {nombre}: {tipo}" for nombre, tipo in esquema)
    return (
        prompt_cfg["system_prompt"]
        .replace("{tabla_completa}", BQ_TABLE)
        .replace("{esquema_tabla}", esquema_txt)
    )


def init_resources() -> None:
    """Una vez al arrancar: LLM, prompt renderizado, tool y checkpointer."""
    global _llm, _system_prompt, _bigquery_tool, _checkpointer
    if _llm is not None:
        return
    model_cfg = _cargar_yaml(RUTA_MODEL_CONFIG)["llm"]
    _llm = init_chat_model(
        f"{model_cfg['provider']}:{model_cfg['model']}",
        temperature=model_cfg.get("temperature", 0),
    )
    _system_prompt = _render_system_prompt()
    _bigquery_tool = get_consultar_bigquery_tool()
    _checkpointer = get_checkpointer()


def build_agent():
    """Por cada mensaje (barato). Devuelve el agente compilado listo para invoke."""
    init_resources()
    return create_agent(
        model=_llm,
        tools=[_bigquery_tool],
        system_prompt=_system_prompt,
        checkpointer=_checkpointer,
    )


def preguntar_detallado(pregunta: str, thread_id: str = "default") -> dict:
    """
    Pregunta en lenguaje natural → dict con la respuesta y las consultas ejecutadas.

    Devuelve {"respuesta": str, "consultas": [{"sql", "ok", "gb_procesados",
    "filas_devueltas", "error"}]}. Las consultas se extraen de los ToolMessage
    generados en ESTE turno (los anteriores ya están en el checkpointer).
    """
    agent = build_agent()
    resultado = agent.invoke(
        {"messages": [{"role": "user", "content": pregunta}]},
        {"configurable": {"thread_id": thread_id}},
    )
    mensajes = resultado["messages"]
    # Índice del último mensaje humano: todo lo posterior pertenece a este turno.
    inicio = max(i for i, m in enumerate(mensajes) if m.type == "human")
    consultas = []
    for m in mensajes[inicio:]:
        if isinstance(m, ToolMessage):
            try:
                datos = json.loads(m.content)
            except (json.JSONDecodeError, TypeError):
                continue
            consultas.append(
                {
                    "sql": datos.get("sql"),
                    "ok": datos.get("ok"),
                    "gb_procesados": datos.get("gb_procesados"),
                    "filas_devueltas": datos.get("filas_devueltas"),
                    "error": datos.get("error"),
                }
            )
    return {"respuesta": mensajes[-1].text, "consultas": consultas}


def preguntar(pregunta: str, thread_id: str = "default") -> str:
    """API pública simple: pregunta en lenguaje natural → respuesta en texto."""
    return preguntar_detallado(pregunta, thread_id)["respuesta"]


if __name__ == "__main__":
    pregunta = " ".join(sys.argv[1:]) or "¿Cuántos viajes hay en total por tipo de usuario?"
    print(preguntar(pregunta))
