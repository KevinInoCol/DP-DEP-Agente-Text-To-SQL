"""
Caché en memoria de los resultados de consultas a BigQuery.

Cada consulta exitosa se guarda con un consulta_id (UUID corto). La tool de
gráficos recupera las filas por ese id, de modo que el LLM nunca tiene que
recopiar los datos de una tool a otra (evita errores de transcripción y ahorra
tokens). Acotada a los últimos MAX_RESULTADOS resultados del proceso.
"""

import threading
import uuid
from collections import OrderedDict

MAX_RESULTADOS = 50

_cache: "OrderedDict[str, dict]" = OrderedDict()
_lock = threading.Lock()


def guardar_resultado(sql: str, filas: list[dict]) -> str:
    """Guarda un resultado y devuelve su consulta_id."""
    consulta_id = uuid.uuid4().hex[:8]
    with _lock:
        _cache[consulta_id] = {"sql": sql, "filas": filas}
        while len(_cache) > MAX_RESULTADOS:
            _cache.popitem(last=False)
    return consulta_id


def obtener_resultado(consulta_id: str) -> dict | None:
    """Devuelve {"sql", "filas"} o None si el id no existe (o ya expiró)."""
    with _lock:
        return _cache.get(consulta_id)
