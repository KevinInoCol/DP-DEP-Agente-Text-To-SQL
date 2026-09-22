"""
Dibuja con Plotly la especificación de gráfico que devuelve generar_grafico_tool.

Capa de presentación pura: no conoce LLMs ni BigQuery. Recibe el dict
"grafico" (tipo, título, categorías, valores, series...) y devuelve una figura
de Plotly lista para st.plotly_chart. Sigue las reglas de la skill dataviz:
un solo eje Y, un hue para magnitud, paleta categórica en orden fijo (validada
para daltonismo), marcas finas, etiquetas directas solo cuando hay pocas barras.
"""

import plotly.graph_objects as go

COLOR_MAGNITUD = "#2a78d6"  # un solo hue para una sola serie
PALETA_CATEGORICA = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
MAX_BARRAS_CON_ETIQUETA = 12
ALTO_BASE = 420
ALTO_POR_BARRA_HORIZONTAL = 28

FORMATOS = {"entero": ",.0f", "decimal": ",.2f", "porcentaje": ".1f"}


def _formato(spec: dict) -> str:
    return FORMATOS.get(spec.get("formato_valor", "entero"), ",.0f")


def _texto_valores(valores: list, fmt: str) -> list[str]:
    return [format(v, fmt) if isinstance(v, (int, float)) else "" for v in valores]


def _layout_base(fig: go.Figure, spec: dict, alto: int = ALTO_BASE) -> go.Figure:
    fig.update_layout(
        title={"text": spec["titulo"], "x": 0, "xanchor": "left"},
        template="plotly_white",
        height=alto,
        margin={"l": 16, "r": 16, "t": 56, "b": 16},
        legend={"orientation": "h", "y": -0.18},
        hoverlabel={"namelength": -1},
    )
    fig.update_xaxes(title_text=spec.get("etiqueta_x", ""), showgrid=False)
    fig.update_yaxes(title_text=spec.get("etiqueta_y", ""), gridcolor="#eeeeea", zeroline=False)
    return fig


def _barras(spec: dict, horizontal: bool) -> go.Figure:
    fmt = _formato(spec)
    fig = go.Figure()
    if spec.get("series"):
        for i, (nombre, valores) in enumerate(spec["series"].items()):
            color = PALETA_CATEGORICA[i % len(PALETA_CATEGORICA)]
            fig.add_bar(
                name=nombre,
                x=valores if horizontal else spec["categorias"],
                y=spec["categorias"] if horizontal else valores,
                orientation="h" if horizontal else "v",
                marker={"color": color, "line": {"width": 0}},
                hovertemplate=f"%{{x}}<br>{nombre}: %{{y:{fmt}}}<extra></extra>" if not horizontal
                else f"%{{y}}<br>{nombre}: %{{x:{fmt}}}<extra></extra>",
            )
        fig.update_layout(barmode="group", bargap=0.25)
    else:
        valores = spec["valores"]
        pocas = len(valores) <= MAX_BARRAS_CON_ETIQUETA
        fig.add_bar(
            x=valores if horizontal else spec["categorias"],
            y=spec["categorias"] if horizontal else valores,
            orientation="h" if horizontal else "v",
            marker={"color": COLOR_MAGNITUD, "line": {"width": 0}},
            text=_texto_valores(valores, fmt) if pocas else None,
            textposition="outside" if pocas else None,
            cliponaxis=False,
            hovertemplate=f"%{{y}}<br>%{{x:{fmt}}}<extra></extra>" if horizontal else f"%{{x}}<br>%{{y:{fmt}}}<extra></extra>",
        )
        fig.update_layout(bargap=0.35)

    alto = ALTO_BASE
    if horizontal:
        alto = max(ALTO_BASE, 120 + ALTO_POR_BARRA_HORIZONTAL * len(spec["categorias"]))
        fig.update_yaxes(autorange="reversed", title_text="")  # la barra mayor arriba
        fig.update_xaxes(title_text=spec.get("etiqueta_y", ""), gridcolor="#eeeeea")
        _layout_base(fig, spec, alto)
        fig.update_yaxes(title_text="", showgrid=False)
        fig.update_xaxes(title_text=spec.get("etiqueta_y", ""), showgrid=True, gridcolor="#eeeeea")
        return fig
    return _layout_base(fig, spec, alto)


def _lineas(spec: dict) -> go.Figure:
    fmt = _formato(spec)
    fig = go.Figure()
    series = spec.get("series") or {"": spec["valores"]}
    for i, (nombre, valores) in enumerate(series.items()):
        color = PALETA_CATEGORICA[i % len(PALETA_CATEGORICA)] if spec.get("series") else COLOR_MAGNITUD
        fig.add_scatter(
            name=nombre or spec.get("etiqueta_y", ""),
            x=spec["categorias"],
            y=valores,
            mode="lines+markers",
            line={"width": 2, "color": color},
            marker={"size": 8, "color": color},
            hovertemplate=f"%{{x}}<br>%{{y:{fmt}}}<extra>{nombre}</extra>",
        )
    fig = _layout_base(fig, spec)
    fig.update_layout(showlegend=bool(spec.get("series")), hovermode="x unified")
    return fig


def _pastel(spec: dict) -> go.Figure:
    fmt = _formato(spec)
    fig = go.Figure(
        go.Pie(
            labels=spec["categorias"],
            values=spec["valores"],
            hole=0.45,
            sort=False,
            marker={"colors": PALETA_CATEGORICA[: len(spec["categorias"])], "line": {"color": "#ffffff", "width": 2}},
            textinfo="label+percent",
            hovertemplate=f"%{{label}}<br>%{{value:{fmt}}} (%{{percent}})<extra></extra>",
        )
    )
    fig.update_layout(
        title={"text": spec["titulo"], "x": 0, "xanchor": "left"},
        template="plotly_white",
        height=ALTO_BASE,
        margin={"l": 16, "r": 16, "t": 56, "b": 16},
        showlegend=False,
    )
    return fig


def _dispersion(spec: dict) -> go.Figure:
    fmt = _formato(spec)
    fig = go.Figure(
        go.Scatter(
            x=spec["categorias"],
            y=spec["valores"],
            mode="markers",
            marker={"size": 9, "color": COLOR_MAGNITUD, "opacity": 0.8, "line": {"color": "#ffffff", "width": 1}},
            hovertemplate=f"%{{x}}<br>%{{y:{fmt}}}<extra></extra>",
        )
    )
    fig = _layout_base(fig, spec)
    fig.update_xaxes(showgrid=True, gridcolor="#eeeeea")
    return fig


def construir_figura(spec: dict) -> go.Figure:
    """Punto de entrada: dict 'grafico' de la tool → figura de Plotly."""
    tipo = spec.get("tipo")
    if tipo == "barras":
        return _barras(spec, horizontal=False)
    if tipo == "barras_horizontales":
        return _barras(spec, horizontal=True)
    if tipo == "lineas":
        return _lineas(spec)
    if tipo == "pastel":
        return _pastel(spec)
    if tipo == "dispersion":
        return _dispersion(spec)
    raise ValueError(f"Tipo de gráfico no soportado: {tipo}")
