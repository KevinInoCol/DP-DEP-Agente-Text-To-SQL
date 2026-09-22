# Agente Text-to-SQL · CitiBike (BigQuery)

Agente básico en **LangChain 1.x** que convierte preguntas en lenguaje natural en SQL
de BigQuery, lo ejecuta contra la tabla pública
`bigquery-public-data.new_york_citibike.citibike_trips` (~59 M viajes) y devuelve
el SQL generado, el resultado y una interpretación.

La conexión a BigQuery es una **tool** del agente (`tools/bigquery.py`), no código
del orquestador: el LLM decide cuándo llamarla y puede corregir el SQL si BigQuery
devuelve un error.

## Estructura

Sigue la skill `agent-project-structure` (mirroreada en `../.claude/skills/`),
recortada a lo que un agente básico necesita (sin RAG ni CRM):

```
text_to_sql_citibike/
├── model_config/model.yaml      ← proveedor, modelo (gpt-4.1), temperatura
├── prompt/system_prompt.yaml    ← system prompt en YAML + tags XML (skill agent-prompt-yaml-format)
├── tools/
│   └── bigquery.py              ← tool: valida solo-lectura, dry run, ejecuta, devuelve JSON
├── chat_history/
│   └── memory_store.py          ← checkpointer InMemorySaver (memoria de la sesión)
├── agent.py                     ← orquestador: create_agent(model, tools, system_prompt, checkpointer)
├── app.py                       ← entrypoint web: chat en Streamlit
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
| Memoria persistente (Postgres)      | `chat_history/`          |
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
   (5 GB por defecto, así un `SELECT *` sobre la tabla completa de ~7.5 GB queda bloqueado). Devuelve como máximo `BQ_MAX_ROWS` filas.

## Instalación

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env   # y completa OPENAI_API_KEY y GOOGLE_CLOUD_PROJECT
```

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

# Probar la tool sin LLM
.venv/bin/python tools/bigquery.py
```

La interfaz muestra cada respuesta del agente (SQL, resultado, interpretación) y, en un
desplegable, las consultas que la tool ejecutó realmente en BigQuery con los GB
procesados, incluidos los intentos fallidos que el agente corrigió. El botón
"Nueva conversación" genera un `thread_id` nuevo y vacía la memoria.

Ejemplos de preguntas:

- ¿Cuántos viajes hicieron los Subscribers frente a los Customers?
- ¿Cuál es la duración promedio de los viajes por género?
- Top 10 estaciones de salida con más viajes en 2017.
- ¿En qué mes del año hay más viajes?

## Decisiones

- **LangChain 1.x idiomático**: `create_agent` + `system_prompt=` string + `checkpointer`
  con `thread_id`. Nada de `create_react_agent`, `AgentExecutor` ni bucles manuales de
  `tool_calls` (skill `langchain-v1-idioms`).
- **Una sola tool**. El esquema va en el prompt porque es estático y pequeño; si el agente
  creciera a varias tablas, el esquema pasaría a ser una segunda tool.
- **La tool nunca lanza**: todo error vuelve como `{"ok": false, "error": ...}` para que
  el LLM lo lea y reintente (máximo 3 intentos, fijado en el prompt).
- **Memoria en RAM**: suficiente para un agente básico de consola. Cambiar a
  `PostgresSaver` solo toca `chat_history/memory_store.py`.
