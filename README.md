# DP-DEP · Agente Text-to-SQL (CitiBike · BigQuery · LangChain)

Repositorio del módulo de Agentes del Programa de AI Data Engineer (Datapath).

- [`text_to_sql_citibike/`](text_to_sql_citibike/) — agente Text-to-SQL en LangChain 1.x con
  interfaz de chat en Streamlit. Convierte preguntas en lenguaje natural en SQL de BigQuery,
  lo ejecuta sobre la tabla pública `bigquery-public-data.new_york_citibike.citibike_trips`
  mediante una tool y explica el resultado. Instalación y uso en su README.
- [`.claude/skills/`](.claude/skills/) — skills de Claude Code que definen la arquitectura del
  agente (`agent-project-structure`) y el formato del system prompt (`agent-prompt-yaml-format`).
