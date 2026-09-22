"""
Entrypoint web del agente Text-to-SQL sobre CitiBike: chat en Streamlit.

Capa de I/O: recibe la pregunta del usuario, la pasa a agent.preguntar_detallado()
y muestra la respuesta (SQL generado + resultado + interpretación). Si el agente
llamó al subagente de gráficos, dibuja la figura con Plotly debajo de la respuesta.
Un desplegable enseña la traza de tools: qué eligió el agente, en qué orden, con
qué argumentos, cuánto tardó cada una y con qué resultado. Debajo de cada respuesta hay una calificación (pulgar arriba/abajo)
y un campo de comentario; cada envío se guarda en chat_history/feedback_store. Mantiene un thread_id por sesión de navegador para que el
agente recuerde el contexto de la conversación.

Requiere OPENAI_API_KEY y GOOGLE_CLOUD_PROJECT en .env, y credenciales de GCP
(gcloud auth application-default login).

Ejecutar:
    streamlit run app.py
"""

import os
import uuid

import streamlit as st

from dotenv import load_dotenv

import agent
from chat_history import guardar_feedback, resumen_feedback
from observabilidad import etiqueta_tool
from tools import BQ_TABLE, internet_disponible
from ui import construir_figura

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT")

TITULO = "Agente Text-to-SQL · CitiBike"
PREGUNTAS_EJEMPLO = [
    "¿Cuántos viajes hicieron los Subscribers frente a los Customers?",
    "¿Cuál es la duración promedio de los viajes en minutos, por género?",
    "Top 10 estaciones de salida con más viajes en 2017",
    "¿En qué mes del año se registran más viajes?",
]


def verificar_configuracion() -> list[str]:
    """Devuelve la lista de variables de entorno obligatorias que faltan."""
    return [
        nombre
        for nombre, valor in {
            "OPENAI_API_KEY": OPENAI_API_KEY,
            "GOOGLE_CLOUD_PROJECT": GOOGLE_CLOUD_PROJECT,
        }.items()
        if not valor
    ]


@st.cache_resource(show_spinner="Inicializando agente (LLM + esquema de BigQuery)...")
def inicializar_agente() -> bool:
    """Carga LLM, prompt, tool y checkpointer UNA vez por proceso de Streamlit."""
    agent.init_resources()
    return True


def iniciar_sesion() -> None:
    if "thread_id" not in st.session_state:
        st.session_state.thread_id = str(uuid.uuid4())
    if "mensajes" not in st.session_state:
        st.session_state.mensajes = []  # [{"rol", "contenido", "consultas"}]


def nueva_conversacion() -> None:
    st.session_state.thread_id = str(uuid.uuid4())
    st.session_state.mensajes = []


def _titulo_traza(traza: list[dict]) -> str:
    """Resumen de una línea para el encabezado: pasos consecutivos iguales se agrupan (×2)."""
    pasos: list[list] = []
    for t in traza:
        icono, nombre = etiqueta_tool(t["tool"])
        etiqueta = f"{icono} {nombre}"
        if pasos and pasos[-1][0] == etiqueta:
            pasos[-1][1] += 1
        else:
            pasos.append([etiqueta, 1])
    cadena = " → ".join(e if n == 1 else f"{e} ×{n}" for e, n in pasos)
    total = sum(t.get("duracion_s") or 0 for t in traza)
    return f"🧭 {len(traza)} tool(s): {cadena} · {total:.1f} s"


def render_traza(traza: list[dict]) -> None:
    """Línea de tiempo de las tools que el agente decidió usar en este turno."""
    if not traza:
        st.caption("🧭 El agente respondió sin usar ninguna tool.")
        return
    with st.expander(_titulo_traza(traza)):
        for paso in traza:
            icono, nombre = etiqueta_tool(paso["tool"])
            estado = "✅" if paso.get("ok") else ("❌" if paso.get("ok") is False else "⏳")
            duracion = f"{paso['duracion_s']:.2f} s" if paso.get("duracion_s") is not None else "—"
            st.markdown(f"**{paso['orden']}. {icono} {nombre}** · {estado} · {duracion}")
            if paso.get("resumen"):
                st.caption(paso["resumen"])
            argumentos = paso.get("argumentos") or {}
            if "sql" in argumentos:
                st.code(argumentos["sql"], language="sql")
            otros = {k: v for k, v in argumentos.items() if k != "sql"}
            if otros:
                st.caption(" · ".join(f"`{k}`: {v}" for k, v in otros.items()))
            st.caption(f"tool: `{paso['tool']}`")


def render_graficos(graficos: list[dict], clave: str) -> None:
    """Dibuja cada especificación devuelta por el subagente de gráficos."""
    for i, spec in enumerate(graficos):
        try:
            st.plotly_chart(construir_figura(spec), width="stretch", key=f"{clave}-{i}")
        except Exception as e:  # noqa: BLE001 — un gráfico roto no debe ocultar la respuesta
            st.warning(f"No se pudo dibujar el gráfico: {type(e).__name__}: {e}")
            continue
        pie = spec.get("justificacion", "")
        if spec.get("ajuste"):
            pie = f"{pie} {spec['ajuste']}"
        if pie:
            st.caption(f"📊 {pie}")


def _pregunta_previa(indice: int) -> str:
    """La pregunta del usuario que originó la respuesta en la posición `indice`."""
    for m in reversed(st.session_state.mensajes[:indice]):
        if m["rol"] == "user":
            return m["contenido"]
    return ""


def render_feedback(m: dict, indice: int) -> None:
    """Pulgar arriba/abajo + comentario. Una vez enviado, muestra el resumen."""
    fb = m.get("feedback")
    if fb:
        icono = {"positiva": "👍", "negativa": "👎"}.get(fb["calificacion"], "💬")
        texto = f"{icono} Gracias por tu feedback."
        if fb["comentario"]:
            texto += f' Comentario: "{fb["comentario"]}"'
        st.caption(texto)
        return

    with st.form(key=f"fb-form-{indice}", border=False):
        col_voto, col_texto, col_boton = st.columns([1, 5, 1], vertical_alignment="bottom")
        with col_voto:
            voto = st.feedback("thumbs", key=f"fb-voto-{indice}")
        with col_texto:
            comentario = st.text_input(
                "Comentario",
                key=f"fb-texto-{indice}",
                placeholder="¿Qué estuvo bien o mal en esta respuesta? (opcional)",
                label_visibility="collapsed",
            )
        with col_boton:
            enviado = st.form_submit_button("Enviar", width="stretch")

    if enviado:
        if voto is None and not comentario.strip():
            st.warning("Elige 👍 o 👎, o escribe un comentario, antes de enviar.")
            return
        calificacion = None if voto is None else ("positiva" if voto == 1 else "negativa")
        guardar_feedback(
            thread_id=st.session_state.thread_id,
            pregunta=_pregunta_previa(indice),
            respuesta=m["contenido"],
            calificacion=calificacion,
            comentario=comentario,
            consultas=m.get("consultas"),
            graficos=m.get("graficos"),
            traza=m.get("traza"),
        )
        m["feedback"] = {"calificacion": calificacion, "comentario": comentario.strip()}
        st.rerun()


def render_fuentes_web(fuentes: list[dict]) -> None:
    """Desplegable con las fuentes de internet que el agente consultó (Tavily)."""
    if not fuentes:
        return
    with st.expander(f"🌐 {len(fuentes)} fuente(s) web consultada(s) como contexto complementario"):
        st.caption("La búsqueda web solo aporta contexto; las cifras provienen siempre de BigQuery.")
        for f in fuentes:
            st.markdown(f"**[{f['titulo']}]({f['url']})**  \n{f['extracto']}")
            st.caption(f"Búsqueda: \"{f['consulta']}\" · relevancia {f['puntuacion']}")


def render_mensaje(m: dict, indice: int) -> None:
    with st.chat_message(m["rol"]):
        st.markdown(m["contenido"])
        if m["rol"] == "assistant":
            render_graficos(m.get("graficos", []), clave=f"hist-{indice}")
            render_traza(m.get("traza", []))
            render_fuentes_web(m.get("fuentes_web", []))
            render_feedback(m, indice)


def responder(pregunta: str) -> None:
    """Muestra la pregunta, llama al agente y guarda ambos mensajes en la sesión.

    No dibuja la respuesta aquí: tras guardarla hace st.rerun() y el historial la
    renderiza con claves estables, necesarias para los widgets de feedback.
    """
    st.session_state.mensajes.append({"rol": "user", "contenido": pregunta})
    with st.chat_message("user"):
        st.markdown(pregunta)

    with st.chat_message("assistant"):
        with st.spinner("Generando SQL, consultando BigQuery y eligiendo el gráfico..."):
            try:
                salida = agent.preguntar_detallado(pregunta, thread_id=st.session_state.thread_id)
            except Exception as e:  # noqa: BLE001 — la UI no debe caerse por un error del agente
                salida = {
                    "respuesta": f"❌ Ocurrió un error al procesar la pregunta: `{type(e).__name__}: {e}`",
                    "consultas": [],
                    "graficos": [],
                    "fuentes_web": [],
                    "traza": [],
                }

    st.session_state.mensajes.append(
        {
            "rol": "assistant",
            "contenido": salida["respuesta"],
            "consultas": salida["consultas"],
            "graficos": salida["graficos"],
            "fuentes_web": salida.get("fuentes_web", []),
            "traza": salida.get("traza", []),
            "feedback": None,
        }
    )
    st.rerun()


def main() -> None:
    st.set_page_config(page_title=TITULO, page_icon="🚲", layout="wide")
    st.title(f"🚲 {TITULO}")
    st.caption(f"Pregunta en lenguaje natural sobre `{BQ_TABLE}`. El agente genera el SQL, lo ejecuta, lo explica, un subagente elige el gráfico y, si aporta, complementa con contexto de internet.")

    faltantes = verificar_configuracion()
    if faltantes:
        st.error(f"Faltan variables en `.env`: {', '.join(faltantes)}. Complétalas y recarga la página.")
        st.stop()

    inicializar_agente()
    iniciar_sesion()

    with st.sidebar:
        st.header("Sesión")
        st.button("🗑️ Nueva conversación", on_click=nueva_conversacion, width="stretch")
        st.caption(f"thread_id: `{st.session_state.thread_id[:8]}…`")
        st.divider()
        st.header("Ejemplos")
        for p in PREGUNTAS_EJEMPLO:
            if st.button(p, width="stretch"):
                st.session_state.pregunta_pendiente = p
        st.divider()
        st.header("Feedback recibido")
        resumen = resumen_feedback()
        c1, c2, c3 = st.columns(3)
        c1.metric("👍", resumen["positivas"])
        c2.metric("👎", resumen["negativas"])
        c3.metric("💬", resumen["con_comentario"])
        st.caption("Se guarda en el archivo indicado por FEEDBACK_PATH.")
        st.divider()
        st.caption(f"Proyecto GCP: `{GOOGLE_CLOUD_PROJECT}`")
        st.caption("🌐 Búsqueda web: " + ("activa (Tavily)" if internet_disponible() else "no configurada (falta TAVILY_API_KEY)"))

    for i, m in enumerate(st.session_state.mensajes):
        render_mensaje(m, indice=i)

    pregunta = st.chat_input("Ej: ¿Cuál es la estación de salida más usada?")
    if not pregunta and "pregunta_pendiente" in st.session_state:
        pregunta = st.session_state.pop("pregunta_pendiente")
    if pregunta:
        responder(pregunta)


main()
