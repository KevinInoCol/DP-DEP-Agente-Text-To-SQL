"""
Subagente especialista en gráficos.

Recibe la pregunta del usuario, las columnas del resultado y una muestra de filas,
y decide QUÉ gráfico conviene (tipo, columnas, título). No dibuja: devuelve una
EspecificacionGrafico validada con Pydantic mediante response_format de
create_agent. El dibujo ocurre en la capa de UI (ui/graficos.py) a partir de esa
especificación más los datos completos, que la tool toma de la caché.

Se expone al agente principal como tool en tools/grafico.py (patrón
"subagente como tool").
"""

import json
from pathlib import Path
from typing import Literal

import yaml
from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent.parent
RUTA_MODEL_CONFIG = BASE_DIR / "model_config" / "model.yaml"
RUTA_PROMPT = BASE_DIR / "prompt" / "grafico_prompt.yaml"

FILAS_MUESTRA = 15  # filas que ve el subagente; los datos completos los añade la tool

_grafico_agent = None


class EspecificacionGrafico(BaseModel):
    """Qué gráfico dibujar a partir de un resultado tabular."""

    tipo: Literal["barras", "barras_horizontales", "lineas", "pastel", "dispersion"] = Field(
        description=(
            "barras: ranking o comparación de magnitudes con etiquetas cortas (<= 12 categorías). "
            "barras_horizontales: lo mismo con etiquetas largas (nombres de estaciones, rutas). "
            "lineas: evolución en el tiempo (años, meses, días, horas). "
            "pastel: participación sobre un total con <= 6 categorías. "
            "dispersion: relación entre dos columnas numéricas."
        )
    )
    columnas_categoria: list[str] = Field(
        description=(
            "Una o más columnas que identifican cada fila en el eje de categorías. Si son varias "
            "(p. ej. estación de salida y de llegada) se concatenan con ' → '. "
            "En dispersion, la única columna numérica del eje X."
        )
    )
    columna_valor: str = Field(description="Columna numérica que se grafica (eje Y o tamaño del sector).")
    columna_serie: str | None = Field(
        default=None,
        description="Columna categórica para desglosar en varias series (barras agrupadas o varias líneas). null si no aplica.",
    )
    titulo: str = Field(description="Título corto en español que responde a la pregunta del usuario.")
    etiqueta_x: str = Field(description="Etiqueta legible del eje X (o de las categorías).")
    etiqueta_y: str = Field(description="Etiqueta legible del eje Y, con la unidad (viajes, minutos, %).")
    formato_valor: Literal["entero", "decimal", "porcentaje"] = Field(
        default="entero", description="Cómo formatear los valores en etiquetas y tooltips."
    )
    justificacion: str = Field(description="Una frase: por qué este tipo de gráfico es el adecuado.")


def _cargar_yaml(ruta: Path) -> dict:
    with open(ruta, encoding="utf-8") as f:
        return yaml.safe_load(f)


def init_grafico_agent() -> None:
    """Construye el subagente UNA vez (modelo propio, sin tools, salida estructurada)."""
    global _grafico_agent
    if _grafico_agent is not None:
        return
    cfg = _cargar_yaml(RUTA_MODEL_CONFIG)["llm_grafico"]
    modelo = init_chat_model(f"{cfg['provider']}:{cfg['model']}", temperature=cfg.get("temperature", 0))
    system_prompt = _cargar_yaml(RUTA_PROMPT)["system_prompt"]
    _grafico_agent = create_agent(
        model=modelo,
        tools=[],
        system_prompt=system_prompt,
        response_format=EspecificacionGrafico,
    )


def disenar_grafico(pregunta: str, columnas: dict[str, str], filas: list[dict]) -> EspecificacionGrafico:
    """
    Pide al subagente la especificación del gráfico.

    columnas: {nombre: tipo_python} inferido de las filas.
    filas: resultado completo; solo se envía una muestra al modelo.
    """
    init_grafico_agent()
    contenido = (
        f"Pregunta original del usuario: {pregunta}\n\n"
        f"Total de filas del resultado: {len(filas)}\n"
        f"Columnas y tipos: {json.dumps(columnas, ensure_ascii=False)}\n\n"
        f"Muestra de filas (máximo {FILAS_MUESTRA}):\n"
        f"{json.dumps(filas[:FILAS_MUESTRA], ensure_ascii=False, default=str)}"
    )
    resultado = _grafico_agent.invoke({"messages": [{"role": "user", "content": contenido}]})
    return resultado["structured_response"]
