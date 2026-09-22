"""
Memoria de corto plazo del agente: checkpointer en memoria de LangGraph.

Guarda el estado de cada conversación (thread_id) mientras el proceso vive.
Para un agente básico es suficiente. Si hace falta persistencia entre reinicios,
este es el único archivo que cambia: se reemplaza InMemorySaver por PostgresSaver
(ver references/memoria.md de la skill agent-project-structure).
"""

from langgraph.checkpoint.memory import InMemorySaver


def get_checkpointer() -> InMemorySaver:
    """Devuelve el checkpointer. Se llama UNA vez en init_resources()."""
    return InMemorySaver()
