"""
Entrypoint web del agente Text-to-SQL sobre CitiBike: chat en Streamlit.

Capa de I/O: recibe la pregunta del usuario, la pasa a agent.preguntar_detallado()
y muestra la respuesta (SQL generado + resultado + interpretación). Debajo de cada
respuesta, un desplegable enseña las consultas que la tool ejecutó en BigQuery y
los GB procesados. Mantiene un thread_id por sesión de navegador para que el
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
from tools import BQ_TABLE

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


def render_consultas(consultas: list[dict]) -> None:
    """Desplegable con el SQL ejecutado por la tool y sus métricas."""
    if not consultas:
        return
    with st.expander(f"🔍 {len(consultas)} consulta(s) ejecutada(s) en BigQuery"):
        for i, c in enumerate(consultas, 1):
            estado = "✅" if c["ok"] else "❌"
            st.markdown(f"**Intento {i}** {estado}")
            st.code(c["sql"] or "", language="sql")
            if c["ok"]:
                st.caption(
                    f"{c['filas_devueltas']} fila(s) devuelta(s) · "
                    f"{c['gb_procesados']} GB procesados"
                )
            else:
                st.caption(f"Error: {c['error']}")


def render_mensaje(m: dict) -> None:
    with st.chat_message(m["rol"]):
        st.markdown(m["contenido"])
        if m["rol"] == "assistant":
            render_consultas(m.get("consultas", []))


def responder(pregunta: str) -> None:
    st.session_state.mensajes.append({"rol": "user", "contenido": pregunta})
    render_mensaje(st.session_state.mensajes[-1])

    with st.chat_message("assistant"):
        with st.spinner("Generando SQL y consultando BigQuery..."):
            try:
                salida = agent.preguntar_detallado(pregunta, thread_id=st.session_state.thread_id)
            except Exception as e:  # noqa: BLE001 — la UI no debe caerse por un error del agente
                salida = {
                    "respuesta": f"❌ Ocurrió un error al procesar la pregunta: `{type(e).__name__}: {e}`",
                    "consultas": [],
                }
        st.markdown(salida["respuesta"])
        render_consultas(salida["consultas"])

    st.session_state.mensajes.append(
        {"rol": "assistant", "contenido": salida["respuesta"], "consultas": salida["consultas"]}
    )


def main() -> None:
    st.set_page_config(page_title=TITULO, page_icon="🚲", layout="wide")
    st.title(f"🚲 {TITULO}")
    st.caption(f"Pregunta en lenguaje natural sobre `{BQ_TABLE}`. El agente genera el SQL, lo ejecuta y lo explica.")

    faltantes = verificar_configuracion()
    if faltantes:
        st.error(f"Faltan variables en `.env`: {', '.join(faltantes)}. Complétalas y recarga la página.")
        st.stop()

    inicializar_agente()
    iniciar_sesion()

    with st.sidebar:
        st.header("Sesión")
        st.button("🗑️ Nueva conversación", on_click=nueva_conversacion, use_container_width=True)
        st.caption(f"thread_id: `{st.session_state.thread_id[:8]}…`")
        st.divider()
        st.header("Ejemplos")
        for p in PREGUNTAS_EJEMPLO:
            if st.button(p, use_container_width=True):
                st.session_state.pregunta_pendiente = p
        st.divider()
        st.caption(f"Proyecto GCP: `{GOOGLE_CLOUD_PROJECT}`")

    for m in st.session_state.mensajes:
        render_mensaje(m)

    pregunta = st.chat_input("Ej: ¿Cuál es la estación de salida más usada?")
    if not pregunta and "pregunta_pendiente" in st.session_state:
        pregunta = st.session_state.pop("pregunta_pendiente")
    if pregunta:
        responder(pregunta)


main()
