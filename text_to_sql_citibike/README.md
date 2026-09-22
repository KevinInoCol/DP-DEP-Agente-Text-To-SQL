# Agente Text-to-SQL · CitiBike (BigQuery)

Agente en **LangChain 1.x** que convierte preguntas en lenguaje natural en SQL de
BigQuery, lo ejecuta contra la tabla pública
`bigquery-public-data.new_york_citibike.citibike_trips` (~59 M viajes) y devuelve
el SQL generado, el resultado, una interpretación y, cuando el resultado es una tabla,
**un gráfico** elegido por un segundo agente especialista.

Dos agentes, tres tools:

- La conexión a BigQuery es una **tool** del agente principal (`tools/bigquery.py`). El LLM
  decide cuándo llamarla y corrige el SQL si BigQuery devuelve un error.
- El **subagente de gráficos** (`subagents/grafico.py`) es un segundo `create_agent` con
  salida estructurada (`response_format=EspecificacionGrafico`) y modelo propio
  (`gpt-4.1-mini`). Se expone al principal como tool (`tools/grafico.py`): decide el tipo de
  gráfico y las columnas; la interfaz lo dibuja con Plotly.
- La **búsqueda en internet** con Tavily (`tools/internet.py`) es una tool opcional y
  subordinada: solo añade contexto cualitativo (por qué una ruta es popular, qué explica un
  pico) después de que BigQuery ya respondió. Jerarquía fijada en el prompt: BigQuery >
  conocimiento general > internet. Sin `TAVILY_API_KEY` la tool no se registra y el agente
  funciona igual.

## Estructura

Sigue la skill `agent-project-structure` (mirroreada en `../.claude/skills/`),
recortada a lo que un agente básico necesita (sin RAG ni CRM):

```
text_to_sql_citibike/
├── model_config/model.yaml      ← llm (gpt-4.1) del agente principal, llm_grafico (gpt-4.1-mini) del subagente
├── prompt/
│   ├── system_prompt.yaml       ← prompt del agente principal (YAML + tags XML)
│   └── grafico_prompt.yaml      ← prompt del subagente de gráficos
├── subagents/
│   └── grafico.py               ← subagente: create_agent + response_format=EspecificacionGrafico
├── tools/
│   ├── bigquery.py              ← tool: valida solo-lectura, dry run, ejecuta, devuelve JSON + consulta_id
│   ├── grafico.py               ← tool: envuelve al subagente, valida columnas, prepara datos del gráfico
│   ├── internet.py              ← tool: búsqueda web con Tavily, solo contexto complementario (opcional)
│   └── resultados_cache.py      ← caché por consulta_id (la tool de gráficos lee de aquí, no del LLM)
├── ui/
│   └── graficos.py              ← dibuja la especificación con Plotly (capa de presentación)
├── observabilidad/
│   └── tool_tracer.py           ← callback handler: qué tools usó el agente, en qué orden y cuánto tardaron
├── chat_history/
│   ├── memory_store.py          ← checkpointer InMemorySaver (memoria de la sesión)
│   └── feedback_store.py        ← log .jsonl con las calificaciones y comentarios del usuario
├── agent.py                     ← orquestador: create_agent(model, tools=[bigquery, grafico], ...)
├── app.py                       ← entrypoint web: chat en Streamlit con gráficos
├── requirements.txt
├── credentials/                 ← clave JSON de la cuenta de servicio (ignorada por git)
├── .env.example
└── .gitignore
```

| Si cambio…                          | Toco…                    |
|-------------------------------------|--------------------------|
| Modelo o temperatura                | `model_config/model.yaml`|
| Cómo razona / formato de respuesta  | `prompt/system_prompt.yaml` |
| Límites de GB, filas, validación SQL| `tools/bigquery.py`      |
| Criterios para elegir el gráfico    | `prompt/grafico_prompt.yaml` |
| Cuándo y cómo usar internet         | `prompt/system_prompt.yaml` (Reglas_De_Busqueda_Web) |
| Resultados, profundidad de Tavily   | `tools/internet.py` / `.env` |
| Campos de la especificación         | `subagents/grafico.py`   |
| Colores, tamaños, estilo del gráfico| `ui/graficos.py`         |
| Cómo se resume cada tool en la traza| `observabilidad/tool_tracer.py` |
| Memoria persistente (Postgres)      | `chat_history/memory_store.py` |
| Destino del feedback (Postgres, LangSmith) | `chat_history/feedback_store.py` |
| Canal (Streamlit → FastAPI/CLI)     | `app.py`                 |

## Cómo funciona

1. `agent.init_resources()` carga el modelo, lee desde BigQuery el **esquema real** de la
   tabla con la descripción oficial de cada columna y su tamaño, y lo inyecta en el prompt
   (placeholders `{tabla_completa}`, `{esquema_tabla}`, `{total_filas}`, `{gb_tabla}`).
   El prompt añade una `<Guia_De_Columnas>` con la semántica y los valores reales de cada
   campo: unidades, cobertura temporal (jul 2013 – may 2018), registros incompletos,
   outliers de `birth_year` y `tripduration`, columna `customer_plan` vacía, cómo calcular
   edad y distancia.
2. El usuario pregunta. `create_agent` ejecuta el bucle: el LLM escribe SQL → llama a
   `consultar_bigquery_tool` → lee el JSON → responde (o corrige y reintenta si `ok: false`).
3. La tool solo acepta `SELECT` / `WITH`, rechaza DML/DDL por regex, hace un **dry run**
   gratuito para validar sintaxis y estimar bytes, y aborta si supera `BQ_MAX_BYTES_BILLED`
   (5 GB por defecto, así un `SELECT *` sobre la tabla completa de ~7.5 GB queda bloqueado). Devuelve como máximo `BQ_MAX_ROWS` filas
   y guarda el resultado en una caché con un `consulta_id`.
4. Si el resultado tiene 2+ filas y una columna numérica, el prompt obliga a llamar a
   `generar_grafico_tool(consulta_id, pregunta)`. La tool recupera las filas de la caché
   (el LLM no las recopia), se las pasa al **subagente** junto con la pregunta, y este devuelve
   una `EspecificacionGrafico` validada: tipo (`barras`, `barras_horizontales`, `lineas`,
   `pastel`, `dispersion`), columnas, título, etiquetas. La tool valida que las columnas existan,
   degrada un pastel de más de 6 sectores a barras horizontales y devuelve la especificación con
   los datos ya preparados.
5. Si la pregunta pide un "por qué" o el contexto externo aporta valor, y hay `TAVILY_API_KEY`,
   el agente hace 1 o 2 búsquedas con `buscar_en_internet_tool` **al final**, y añade una
   sección "Contexto adicional (fuentes web)" en tono de hipótesis, con las URLs devueltas por la
   tool. Solo puede citar URLs que la tool devolvió; sin tool, sin enlaces.
6. `agent.preguntar_detallado()` extrae de los `ToolMessage` del turno las consultas SQL, los
   gráficos y las fuentes web; `app.py` dibuja los gráficos con `ui.construir_figura()` y lista
   las fuentes en un desplegable debajo de la respuesta.
7. En paralelo, un `TrazadorDeTools` (callback handler de LangChain, uno nuevo por turno) registra
   **qué tools eligió el agente**, en qué orden, con qué argumentos, cuánto tardó cada una y con
   qué resultado. La interfaz lo muestra como línea de tiempo desplegable, p. ej.
   `🧭 2 tool(s): 🗄️ Consulta a BigQuery → 📊 Subagente de gráficos · 3.6 s`, incluidos los
   intentos fallidos que el agente corrigió.

## Instalación

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env   # y completa OPENAI_API_KEY y GOOGLE_CLOUD_PROJECT
```

### Búsqueda web (opcional)

Crea una key gratuita en [tavily.com](https://tavily.com) y ponla en `.env` como
`TAVILY_API_KEY`. `TAVILY_MAX_RESULTADOS` (4) y `TAVILY_SEARCH_DEPTH` (`basic`; `advanced`
cuesta el doble de créditos) son opcionales. La barra lateral indica si está activa.

### Credenciales de Google Cloud

La tabla es pública, pero **las consultas se facturan al proyecto de `GOOGLE_CLOUD_PROJECT`**,
así que la cuenta autenticada necesita el rol *BigQuery Job User* (permiso
`bigquery.jobs.create`) en ese proyecto. Las primeras 1 TB/mes son gratis.

Opción A (recomendada, local):

```bash
gcloud auth application-default login
# elige en el navegador la cuenta que es dueña del proyecto de GOOGLE_CLOUD_PROJECT
```

Opción B (recomendada si tu gcloud usa otra cuenta, o en servidores / CI): una cuenta de
servicio con el rol **BigQuery Job User** en el proyecto. Guarda su clave JSON en
`credentials/` (carpeta ignorada por git) y apunta a ella en `.env`:

```
GOOGLE_APPLICATION_CREDENTIALS=credentials/<archivo-de-clave>.json
```

La librería de Google la toma automáticamente; no hace falta código extra.

## Uso

```bash
# Chat web en Streamlit (abre http://localhost:8501)
.venv/bin/streamlit run app.py

# Una pregunta suelta
.venv/bin/python agent.py "¿Cuál es la estación de salida más usada?"

# Probar la tool de BigQuery sin LLM
.venv/bin/python -m tools.bigquery

# Probar el subagente de gráficos con datos de ejemplo
.venv/bin/python -m tools.grafico

# Probar la búsqueda web (requiere TAVILY_API_KEY)
.venv/bin/python -m tools.internet

# Probar el trazador de tools con eventos simulados
.venv/bin/python -m observabilidad.tool_tracer
```

La interfaz muestra cada respuesta del agente (SQL, resultado, interpretación), el gráfico
elegido por el subagente y, en un desplegable, las consultas que la tool ejecutó realmente en
BigQuery con los GB procesados, incluidos los intentos fallidos que el agente corrigió. El botón
"Nueva conversación" genera un `thread_id` nuevo y vacía la memoria.

Debajo de cada respuesta hay una **calificación** (👍 / 👎) y un campo de comentario. Cada envío
se guarda como una línea JSON en `FEEDBACK_PATH` (por defecto `feedback/feedback.jsonl`,
ignorado por git) con la pregunta, la respuesta, el SQL ejecutado, los gráficos, la traza de
tools (nombre, éxito y duración de cada una) y el `thread_id`. Así un 👎 llega acompañado de qué
hizo el agente para producir esa respuesta.
La barra lateral muestra el acumulado. Ese archivo es la materia prima para revisar qué
preguntas responde mal el agente y construir un dataset de evaluación.

Ejemplos de preguntas:

- ¿Cuántos viajes hicieron los Subscribers frente a los Customers?
- ¿Cuál es la duración promedio de los viajes por género?
- Top 10 estaciones de salida con más viajes en 2017.
- ¿En qué mes del año hay más viajes?

## Decisiones

- **LangChain 1.x idiomático**: `create_agent` + `system_prompt=` string + `checkpointer`
  con `thread_id`. Nada de `create_react_agent`, `AgentExecutor` ni bucles manuales de
  `tool_calls` (skill `langchain-v1-idioms`).
- **El esquema va en el prompt** porque es estático y pequeño; si el agente creciera a varias
  tablas, pasaría a ser otra tool.
- **Subagente como tool, no handoff.** El agente principal conserva el control de la
  conversación; el subagente solo decide el gráfico y devuelve JSON validado con Pydantic.
  Así el principal nunca recibe texto libre que tenga que parsear.
- **El subagente no dibuja.** Devuelve una especificación; la UI la renderiza. Separar decisión
  de renderizado permite cambiar de Plotly a otra librería sin tocar ningún prompt, y testear el
  renderer sin LLM.
- **Los datos no pasan por el LLM.** La tool de gráficos lee las filas de la caché por
  `consulta_id`; el modelo solo ve una muestra de 15 filas para decidir. Evita errores de
  transcripción de cifras y ahorra tokens.
- **Un solo eje Y, un hue para magnitud, paleta categórica fija** (skill `dataviz`): sin
  ejes dobles ni pasteles de más de 6 sectores.
- **La traza se captura con callbacks, no leyendo los mensajes.** Los `ToolMessage` dicen qué
  devolvió cada tool, pero no cuándo empezó ni cuánto tardó; la duración es justo lo que permite
  ver si la lentitud viene de BigQuery, del subagente o de la web.
- **Internet es la última fuente y nunca manda.** La tool se llama después de BigQuery y del
  gráfico, máximo dos veces por respuesta, y el prompt prohíbe corregir cifras de la base con lo
  leído en la web. Además prohíbe inventar URLs: en pruebas sin la tool el modelo fabricaba
  enlaces plausibles, así que el prompt exige que toda URL provenga literalmente de un resultado
  de la tool, y sin tool no hay sección de fuentes.
- **La tool nunca lanza**: todo error vuelve como `{"ok": false, "error": ...}` para que
  el LLM lo lea y reintente (máximo 3 intentos, fijado en el prompt).
- **Memoria en RAM**: suficiente para un agente básico de consola. Cambiar a
  `PostgresSaver` solo toca `chat_history/memory_store.py`.
