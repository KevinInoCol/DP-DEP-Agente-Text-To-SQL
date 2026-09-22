---
name: agent-project-structure
description: Modular folder convention for production LangChain/LangGraph (or LlamaIndex) agent projects with RAG, persistent memory, tools, and optional CRM/messaging integration. Use when scaffolding a new agent project, refactoring a monolithic agent into modular folders, deciding where new code belongs, or when the user mentions building a chatbot/agent/vendedor digital with RAG + memoria + tools. Apply this layout by default unless the user explicitly says otherwise.
---

# Agent Project Structure Convention

Layout probado en producción para un agente que combina LLM + RAG + memoria persistente + tools, opcionalmente integrado con un CRM o canal de mensajería (Kommo, Chatwoot, WhatsApp, Telegram).

Es **un solo agente**, no multi-agente. Esa es la forma que cubre la mayoría de los casos reales: atención y ventas sobre una base de conocimiento propia, con acciones sobre un sistema externo.

## Layout

```
<project-root>/
├── model_config/model.yaml        ← parámetros del LLM y de la memoria
├── prompt/system_prompt.yaml      ← system prompt (formato XML-tag)
├── chat_history/                  ← persistencia de la conversación
│   ├── __init__.py
│   ├── postgres_store.py            checkpointer (estado de ejecución)
│   └── conversation_log.py          log plano legible con SQL
├── RAG/rag.py                     ← pipeline de INGESTA (reindexado)
├── tools/                         ← una tool por archivo
│   ├── __init__.py
│   ├── retrieval.py                 RAG como tool
│   └── <accion>.py                  acciones sobre el sistema externo
├── vector_store.py                ← config del vector store (FUENTE ÚNICA)
├── <Integracion>/                 ← cliente del CRM/canal (opcional)
│   ├── __init__.py
│   └── <servicio>.py                parseo de webhook, llamadas a su API
├── <Integracion>_support_tools/    ← scripts de consulta de metadatos (opcional)
├── migraciones_SQL/               ← DDL y RLS, numeradas e idempotentes
├── agent.py                       ← orquestador: ensambla las piezas
├── app.py                         ← entrypoint (FastAPI/Streamlit/CLI)
├── requirements.txt
├── .env                           ← secretos
├── .gitignore                     ← DEBE incluir .env y credenciales JSON
└── .claude/rules/                 ← reglas del proyecto, importadas desde CLAUDE.md
```

## Regla para decidir dónde va el código

| Si cambio X, ¿qué toco? | Vive en |
|---|---|
| Proveedor de LLM, temperatura, límites de memoria | `model_config/` |
| Prompt o persona del agente | `prompt/` |
| Backend de memoria (Postgres → Redis) | `chat_history/` |
| Fuente de datos o estrategia de chunking | `RAG/` |
| Vector DB, embedding model, nombre de colección | `vector_store.py` |
| Agrego o modifico una acción del agente | `tools/` |
| API del CRM/canal | `<Integracion>/` |
| Esquema de la base | `migraciones_SQL/` |
| Canal de entrada (webhook → CLI) | entrypoint |
| Cómo se ensamblan las piezas | `agent.py` |

Si la respuesta natural es "agent.py" para cualquier otra cosa, **algo está mal**. `agent.py` solo cambia para orquestación.

## Las dos reglas que evitan los bugs caros

**1. `vector_store.py` es la fuente única del par (embedding model, colección).**

La ingesta (`RAG/rag.py`) y la consulta (`tools/retrieval.py`) importan de ahí. Sin esto, el bug clásico es indexar en una colección y consultar en otra, o cambiar el embedding model en un lado y no en el otro: el agente responde "no encontré información" sobre documentos que sí están indexados, sin ningún error visible.

**2. Recursos pesados se inicializan UNA vez; el agente se reconstruye por mensaje.**

```python
# agent.py
def init_resources(bot_name: str) -> None:
    """Una vez al arrancar. Deja singletons de módulo."""
    global _llm, _retrieval_tool, _system_prompt, _checkpointer
    ...

def build_agent_for_lead(entity_id: int, **contexto):
    """Por cada mensaje. Barato: cierra las tools sobre el entity_id correcto."""
    tools = [_retrieval_tool, get_mi_accion_tool(entity_id), ...]
    return create_agent(model=_llm, tools=tools,
                        system_prompt=_system_prompt,
                        checkpointer=_checkpointer,
                        middleware=[...])
```

Cargar el índice o el vector store por mensaje mata el tiempo de respuesta. Reconstruir el agente es trivial y es lo que permite que cada tool tenga el ID de la conversación en clausura.

## Responsabilidad por carpeta

### `model_config/model.yaml`
Config pura, sin código y sin prompt.

```yaml
llm:
  provider: openai
  model: gpt-4.1          # default del usuario (skill default-llm-model)
  temperature: 0

memory:
  retention_days: 30      # descarta mensajes más viejos que esto
  token_limit: 3000       # umbral que dispara el resumen
  keep_messages: 20       # mensajes recientes que sobreviven al resumen
  reengagement_hours: 6   # hueco a partir del cual el cliente "vuelve"
```

No dejes claves muertas aquí. Si `model.yaml` declara algo que ningún módulo lee, es una mentira que alguien va a creer.

### `prompt/system_prompt.yaml`
Prompt puro en formato XML-tag (ver skill `agent-prompt-yaml-format`). Sin config de modelo ni de retrieval. Los placeholders se inyectan en `agent.py` con `.replace()`.

### `chat_history/` — dos piezas, no una
Ver `references/memoria.md`. Resumen:

- `postgres_store.py` → checkpointer de LangGraph. Estado de ejecución, `thread_id` = ID de la conversación. Ilegible con SQL.
- `conversation_log.py` → tabla plana, una fila por mensaje. Para reportes, QA y auditoría.

Los dos, no uno. Cumplen funciones distintas.

### `RAG/`
Solo **ingesta**. Loader → splitter → embeddings → vector store. Ejecutable directo (`python RAG/rag.py`) para reindexar. La config de retrieval (`top_k`, reranker) NO vive aquí: vive en `tools/retrieval.py`.

### `tools/`
Un archivo por tool, cada uno con función pública + factory. Ver `references/tools.md`.

```python
def hacer_algo(entity_id: int, dato: str) -> bool:
    """Imperativa y testeable sin el LLM."""
    ...

def get_hacer_algo_tool(entity_id: int):
    """Factory: cierra el entity_id en clausura. El LLM no lo ve."""
    @tool
    def hacer_algo_tool(dato: str) -> str:
        """Docstring = lo que el LLM lee para decidir. Sé específico."""
        ...
    return hacer_algo_tool
```

### `migraciones_SQL/`
Archivos numerados, SQL puro, idempotente (`IF NOT EXISTS`), con un `README.md` que indique el orden. Sin comentarios explicativos dentro: se pegan tal cual en el editor SQL.

Que exista esta carpeta es lo que hace el proyecto replicable: al montarlo para otro cliente no hay que recordar qué SQL correr.

### `agent.py`
Orquestador. Debería ser corto. Si crece, algo pertenece a otra carpeta.

### Entrypoint
Capa de I/O. No sabe nada de LLMs, prompts ni storage. Conoce solo la API pública de `agent.py` y `chat_history/`.

## Aplicación

**Proyecto nuevo:** crea las carpetas desde el día 1, aunque queden con stubs. Incluye `.gitignore` con `.env` **antes** del primer commit.

**Refactor de un monolito**, en este orden:
1. Config LLM/memoria → `model_config/`
2. System prompt → `prompt/`
3. Vector store → `vector_store.py` (primero esto: desbloquea 4 y 5)
4. Ingesta → `RAG/`
5. Retrieval y acciones → `tools/`
6. Memoria → `chat_history/`
7. Cliente del CRM/canal → `<Integracion>/`
8. `agent.py` queda como orquestador

## Cuándo NO aplicar

- Demos de un archivo o experimentos rápidos.
- Scripts one-shot.
- RAG puro sin agente (sin tools ni memoria): omite `chat_history/` y `tools/`; el resto aplica.
- Sistemas multi-agente con handoffs: el layout sirve de base pero necesita una capa de routing que esta convención no cubre.

## Referencias

- `references/memoria.md` — checkpointer vs tabla plana, las tres capas de memoria, pool de conexiones, RLS. **Léelo antes de tocar memoria.**
- `references/tools.md` — patrón factory, docstrings efectivos, errores comunes.

## Skills relacionadas

Incluidas en este proyecto (`.claude/skills/`):

- `agent-prompt-yaml-format` — formato del `system_prompt.yaml`.
- `agent-domain-swap` — repuntar el agente a otro negocio.
- `kommo-setup` — descubrir y poblar la configuración de Kommo.
- `debounce` — concatenar mensajes en ráfaga (WhatsApp) en una sola inferencia.

Solo a nivel global (no se distribuyen con el proyecto): `default-llm-model` para el
modelo por defecto, `skill-mirror` para symlinkear skills globales al proyecto actual.

Y la regla `.claude/rules/langchain-mcp-first.md`, importada desde `CLAUDE.md`: obliga a consultar la MCP de LangChain antes de escribir código LangChain. No es opcional — la API 1.x rompió con 0.x y el conocimiento previo del modelo mezcla ambas.
