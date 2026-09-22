"""
Log plano de feedback del usuario sobre las respuestas del agente.

Cada calificación (pulgar arriba/abajo + comentario opcional) se guarda como una
línea JSON en un archivo .jsonl junto con la pregunta, la respuesta, el SQL que se
ejecutó y el thread_id. Es la pieza "legible" de la memoria (ver references/memoria.md
de la skill agent-project-structure): sirve para QA, para detectar preguntas que el
agente responde mal y, más adelante, para construir un dataset de evaluación.

Cambiar el destino (Postgres, LangSmith, BigQuery) solo toca este archivo.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

RUTA_FEEDBACK = Path(os.getenv("FEEDBACK_PATH", "feedback/feedback.jsonl"))

CALIFICACIONES = {"positiva", "negativa"}


def guardar_feedback(
    thread_id: str,
    pregunta: str,
    respuesta: str,
    calificacion: str | None,
    comentario: str = "",
    consultas: list[dict] | None = None,
    graficos: list[dict] | None = None,
    traza: list[dict] | None = None,
) -> dict:
    """Añade una línea al .jsonl y devuelve el registro guardado."""
    if calificacion is not None and calificacion not in CALIFICACIONES:
        raise ValueError(f"calificacion debe ser una de {CALIFICACIONES} o None")
    registro = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "thread_id": thread_id,
        "calificacion": calificacion,
        "comentario": comentario.strip(),
        "pregunta": pregunta,
        "respuesta": respuesta,
        "sql": [c.get("sql") for c in (consultas or []) if c.get("ok")],
        "graficos": [{"tipo": g.get("tipo"), "titulo": g.get("titulo")} for g in (graficos or [])],
        "traza": [
            {"orden": t.get("orden"), "tool": t.get("tool"), "ok": t.get("ok"), "duracion_s": t.get("duracion_s")}
            for t in (traza or [])
        ],
    }
    RUTA_FEEDBACK.parent.mkdir(parents=True, exist_ok=True)
    with open(RUTA_FEEDBACK, "a", encoding="utf-8") as f:
        f.write(json.dumps(registro, ensure_ascii=False) + "\n")
    return registro


def cargar_feedback() -> list[dict]:
    """Lee todos los registros. Lista vacía si aún no hay archivo."""
    if not RUTA_FEEDBACK.exists():
        return []
    with open(RUTA_FEEDBACK, encoding="utf-8") as f:
        return [json.loads(linea) for linea in f if linea.strip()]


def resumen_feedback() -> dict:
    """Conteo de positivas, negativas y comentarios, para mostrar en la UI."""
    registros = cargar_feedback()
    return {
        "total": len(registros),
        "positivas": sum(r["calificacion"] == "positiva" for r in registros),
        "negativas": sum(r["calificacion"] == "negativa" for r in registros),
        "con_comentario": sum(bool(r["comentario"]) for r in registros),
    }
