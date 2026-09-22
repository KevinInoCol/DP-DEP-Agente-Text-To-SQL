"""
Middleware que EXIGE las tools de enriquecimiento antes de cerrar una respuesta.

El prompt pide que toda tabla lleve gráfico y que toda explicación causal se
fundamente con una búsqueda web, pero el modelo lo incumple con frecuencia
cuando la pregunta no dice literalmente "por qué". Este middleware lo vuelve
determinista: cuando el modelo va a dar la respuesta final, revisa qué hizo en
el turno y, si falta el gráfico o la búsqueda, inyecta un aviso y lo devuelve al
nodo del modelo (jump_to="model") para que llame la tool que falta.

Tope de MAX_AVISOS_POR_TURNO avisos por turno, para que nunca entre en bucle:
si el modelo insiste en no llamarla, la respuesta sale igual.

Ejecutar (prueba de la lógica pura, sin LLM):
    python -m middlewares.enriquecimiento
"""

import json
import re
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.runtime import Runtime

NOMBRE_TOOL_SQL = "consultar_bigquery_tool"
NOMBRE_TOOL_GRAFICO = "generar_grafico_tool"
NOMBRE_TOOL_WEB = "buscar_en_internet_tool"

MARCA_AVISO = "[control de calidad]"
MAX_AVISOS_POR_TURNO = 2
MIN_FILAS_PARA_GRAFICO = 2

# Frases con las que el modelo explica o conjetura: si aparecen, la explicación
# debe apoyarse en fuentes, no en memoria.
MARCADORES_CAUSALES = re.compile(
    r"puede deberse|puede estar|se debe a|debido a|probablemente|posiblemente|"
    r"esto indica|esto sugiere|sugiere que|refleja|explicad?[oa]|lo que explica|"
    r"suele|tiende a|por su ubicaci|a qué se debe",
    re.IGNORECASE,
)
# SQL que busca un extremo o un ranking: su resultado siempre pide contexto.
PATRON_EXTREMO = re.compile(r"\border\s+by\b[\s\S]*\blimit\b|\bmax\s*\(|\bmin\s*\(", re.IGNORECASE)


def _json_de(mensaje: ToolMessage) -> dict | None:
    try:
        datos = json.loads(mensaje.content)
    except (json.JSONDecodeError, TypeError):
        return None
    return datos if isinstance(datos, dict) else None


def merece_busqueda_web(consultas_ok: list[dict], texto_respuesta: str) -> bool:
    """True si el resultado o la redacción piden contexto externo."""
    if not consultas_ok:
        return False
    if MARCADORES_CAUSALES.search(texto_respuesta or ""):
        return True
    for c in consultas_ok:
        if (c.get("filas_devueltas") or 0) >= 2:
            return True  # tabla comparativa: ranking o grupos
        if PATRON_EXTREMO.search(c.get("sql") or ""):
            return True  # un máximo o un mínimo
    return False


class ExigirEnriquecimiento(AgentMiddleware):
    """Revisa el turno antes de cerrarlo y reclama el gráfico o la búsqueda que falte."""

    def __init__(self, con_web: bool = True) -> None:
        super().__init__()
        self.con_web = con_web

    @staticmethod
    def _mensajes_del_turno(mensajes: list) -> list:
        indices = [i for i, m in enumerate(mensajes) if isinstance(m, HumanMessage)]
        return mensajes[indices[-1]:] if indices else mensajes

    def _diagnostico(self, turno: list, texto_respuesta: str) -> list[str]:
        """Lista de avisos: qué tool falta y por qué."""
        consultas_ok, hubo_grafico, hubo_web = [], False, False
        for m in turno:
            if not isinstance(m, ToolMessage):
                continue
            if m.name == NOMBRE_TOOL_SQL:
                datos = _json_de(m)
                if datos and datos.get("ok"):
                    consultas_ok.append(datos)
            elif m.name == NOMBRE_TOOL_GRAFICO:
                hubo_grafico = True   # si falló, no insistimos
            elif m.name == NOMBRE_TOOL_WEB:
                hubo_web = True

        avisos = []
        graficables = [c for c in consultas_ok if (c.get("filas_devueltas") or 0) >= MIN_FILAS_PARA_GRAFICO]
        if graficables and not hubo_grafico:
            cid = graficables[-1].get("consulta_id")
            avisos.append(
                f"El resultado tiene {graficables[-1].get('filas_devueltas')} filas y debe ir acompañado de un "
                f"gráfico: llama a {NOMBRE_TOOL_GRAFICO} con consulta_id='{cid}' y la pregunta del usuario."
            )
        if self.con_web and not hubo_web and merece_busqueda_web(consultas_ok, texto_respuesta):
            avisos.append(
                f"Tu respuesta presenta un extremo, un ranking o una explicación causal, así que debe apoyarse en "
                f"fuentes: llama a {NOMBRE_TOOL_WEB} con una búsqueda en inglés sobre CitiBike y Nueva York "
                f"relacionada con ese resultado concreto, y añade después la sección "
                f'"Contexto adicional (fuentes web)" citando las URLs que devuelva.'
            )
        return avisos

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        mensajes = state["messages"]
        ultimo = mensajes[-1]
        if not isinstance(ultimo, AIMessage) or getattr(ultimo, "tool_calls", None):
            return None  # el modelo aún está llamando tools

        turno = self._mensajes_del_turno(mensajes)
        ya_avisado = sum(
            1 for m in turno if isinstance(m, SystemMessage) and MARCA_AVISO in str(m.content)
        )
        if ya_avisado >= MAX_AVISOS_POR_TURNO:
            return None  # no insistir más: se responde con lo que haya

        avisos = self._diagnostico(turno, ultimo.text)
        if not avisos:
            return None

        texto = (
            f"{MARCA_AVISO} Antes de dar la respuesta final te faltó una tool obligatoria:\n"
            + "\n".join(f"- {a}" for a in avisos)
            + "\nLlámala ahora y luego reescribe la respuesta completa con el formato habitual."
        )
        return {"messages": [SystemMessage(texto)], "jump_to": "model"}


if __name__ == "__main__":
    mw = ExigirEnriquecimiento(con_web=True)

    def tm(nombre: str, datos: dict) -> ToolMessage:
        return ToolMessage(json.dumps(datos), tool_call_id="1", name=nombre)

    casos = {
        "tabla sin gráfico ni web": [
            HumanMessage("¿duración por género?"),
            tm(NOMBRE_TOOL_SQL, {"ok": True, "filas_devueltas": 3, "consulta_id": "ab12", "sql": "SELECT gender..."}),
        ],
        "extremo de 1 fila, texto causal": [
            HumanMessage("¿mes con más viajes?"),
            tm(NOMBRE_TOOL_SQL, {"ok": True, "filas_devueltas": 1, "consulta_id": "cd34", "sql": "SELECT ... ORDER BY total DESC LIMIT 1"}),
        ],
        "conteo simple": [
            HumanMessage("¿cuántos viajes hay?"),
            tm(NOMBRE_TOOL_SQL, {"ok": True, "filas_devueltas": 1, "consulta_id": "ef56", "sql": "SELECT COUNT(*) FROM t"}),
        ],
        "todo hecho": [
            HumanMessage("¿top 5?"),
            tm(NOMBRE_TOOL_SQL, {"ok": True, "filas_devueltas": 5, "consulta_id": "gh78", "sql": "SELECT ... LIMIT 5"}),
            tm(NOMBRE_TOOL_GRAFICO, {"ok": True}),
            tm(NOMBRE_TOOL_WEB, {"ok": True}),
        ],
    }
    respuestas = {
        "tabla sin gráfico ni web": "Los hombres promedian 13 min. Esto puede deberse al tipo de usuario.",
        "extremo de 1 fila, texto causal": "El mes con más viajes fue octubre de 2017.",
        "conteo simple": "Hay 53 millones de viajes en total.",
        "todo hecho": "Listo.",
    }
    for nombre, turno in casos.items():
        avisos = mw._diagnostico(turno, respuestas[nombre])
        print(f"\n{nombre}: {len(avisos)} aviso(s)")
        for a in avisos:
            print("   -", a[:110])
