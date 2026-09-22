# Memoria del agente

Guía de la capa de persistencia. Léela antes de tocar `chat_history/`, porque casi todas las decisiones aquí son contraintuitivas.

Contexto: LangChain 1.x / LangGraph 1.x. En esta versión `RunnableWithMessageHistory` y `PostgresChatMessageHistory` (el patrón de "tabla `chat_history` gestionada por el framework") están fuera del canon: no aparecen en la documentación v1 salvo en la guía de migración.

## Las dos piezas y por qué van las dos

|  | Checkpointer | Tabla plana |
|---|---|---|
| Tablas | `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations` | una propia (ej. `chat_history`) |
| Guarda | snapshots del estado del grafo | una fila por mensaje |
| Legible con SQL | **No** — los mensajes van en `BYTEA` por versión de canal | Sí |
| Sirve para | continuar la conversación, tolerar fallos a mitad de turno | reportes, QA, auditoría, LGPD/GDPR |
| Quién la crea | `PostgresSaver.setup()` | tu código o `migraciones_SQL/` |

**El checkpointer no reemplaza la tabla plana.** Tres razones:

1. Los mensajes viven en blobs binarios. No hay SQL que devuelva una conversación legible. Para leerla hace falta código: `agent.get_state(config).values["messages"]`.
2. Si hay resumen o retención (más abajo), el texto original **se destruye**. La tabla plana pasa a ser el único registro de qué se dijo realmente.
3. Escribe varias filas por turno, así que crece rápido y no está pensada para consultas analíticas.

**Y la tabla plana no reemplaza al checkpointer**, porque una tabla de `role` + `content` no representa `tool_calls` ni `tool_call_id`. Si la serializas mal, al recargar la conversación el LLM ve tool calls huérfanas y el proveedor devuelve 400.

## `thread_id` es identidad, no sesión

```python
config = {"configurable": {"thread_id": str(entity_id)}}
agent.invoke({"messages": [mensaje]}, config)
```

No expira. Si el usuario vuelve en tres semanas, se carga toda su conversación. No hay que hacer nada especial para "recordar".

**El riesgo real está en la elección de la clave.** Si el CRM crea una entidad nueva cada vez que el mismo contacto escribe (muy común: cada consulta abre un lead nuevo), el `thread_id` cambia y la memoria se pierde aunque sea la misma persona. Verifica esto en el CRM antes de dar la memoria por funcionando; si pasa, la clave debe ser el **contacto**, no la conversación:

```python
config = {"configurable": {"thread_id": f"contact-{contact_id}"}}
```

Y las tools siguen apuntando al ID de la entidad del momento.

## Las tres capas de control del historial

Resuelven problemas distintos. No son alternativas.

### 1. Retención por edad — "solo el último mes"

**No se hace con un DELETE en la base.** En `checkpoints` una fila es un snapshot de la conversación completa, no un mensaje: el snapshot más reciente contiene todos los mensajes, incluidos los de hace meses. Borrar filas viejas libera disco y **no reduce ni un token**.

Se hace en el agente, y requiere estampar la fecha porque los mensajes no traen timestamp propio:

```python
# entrypoint
mensaje = HumanMessage(
    content=texto,
    additional_kwargs={"ts": datetime.now(timezone.utc).isoformat()},
)
```

```python
# agent.py
@before_model
def descartar_vencidos(state, runtime):
    limite = datetime.now(timezone.utc) - timedelta(days=retention_days)
    corte = None
    for i, m in enumerate(state["messages"]):
        if m.type != "human" or _es_resumen(m):
            continue
        ts = (m.additional_kwargs or {}).get("ts")
        if ts is None or datetime.fromisoformat(ts) >= limite:
            corte = i
            break
    if corte is None or corte == 0:
        return None
    resumenes = [m for m in state["messages"][:corte] if _es_resumen(m)]
    return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES),
                         *resumenes, *state["messages"][corte:]]}
```

Tres detalles que no son opcionales:

- **Corta en frontera de mensaje humano.** Si cortas a media secuencia de tool-calling dejas un `ToolMessage` sin el `AIMessage` que lo originó → 400 del proveedor.
- **Conserva el mensaje-resumen.** `SummarizationMiddleware` lo marca con `additional_kwargs={"lc_source": "summarization"}`. Úsalo para reinyectarlo.
- **Los mensajes sin `ts` trátalos como vigentes.** Al desplegar esto sobre conversaciones existentes, si los das por vencidos destruyes el historial de todos tus usuarios de golpe.

### 2. Resumen — que un usuario viejo no cueste una fortuna

```python
SummarizationMiddleware(
    model=_llm,
    trigger=("tokens", token_limit),
    keep=("messages", keep_messages),
    summary_prompt=_MI_PROMPT_DE_RESUMEN,
)
```

**Escribe siempre tu propio `summary_prompt`.** El default de LangChain está redactado para agentes de código: pide secciones `ARTIFACTS` y rutas de archivos. En un agente de ventas produce resúmenes inútiles. Extrae lo de tu dominio: perfil del cliente, qué se le ofreció y qué descartó, qué información ya recibió, compromisos pendientes.

**`{messages}` es el único placeholder permitido.** El middleware hace `.format(messages=...)`, así que cualquier otra llave `{}` en el prompt lanza `KeyError` en producción.

Nota: el resumen se dispara por **tokens**, no por edad. Una conversación corta y vieja se la lleva la retención sin dejar resumen. Si necesitas garantía, baja el `token_limit` o resume al expirar.

### 3. Retoma — que reconozca al que vuelve

Los mensajes no llevan fecha legible por el LLM, así que el modelo no distingue entre "hace 30 segundos" y "hace 8 días": responde *"como te decía..."* después de una semana.

Mide el hueco leyendo el timestamp del último checkpoint:

```python
tupla = checkpointer.get_tuple({"configurable": {"thread_id": str(entity_id)}})
ts = tupla.checkpoint["ts"] if tupla else None   # ISO 8601; None = primera vez
```

Y añade el aviso al prompt **de ese turno** con `dynamic_prompt`, que reemplaza el system prompt sin persistir nada:

```python
@dynamic_prompt
def _prompt_de_retoma(request) -> str:
    return _system_prompt + bloque_de_retoma
```

Que sea transitorio es el punto: si lo inyectas como mensaje normal, se acumulan avisos viejos de "han pasado 8 días" en el historial para siempre.

En el bloque, instruye **reverificar con las tools** antes de reafirmar precios o disponibilidad. El inventario cambió desde la última conversación.

Y revisa el system prompt: si tiene una regla tipo "no saludes de nuevo si ya lo hiciste", contradice la retoma. Necesita excepción explícita.

**Orden del middleware:** retención → resumen → retoma. Resumir antes de descartar gasta una llamada al LLM en mensajes que están por borrarse.

## Conexiones

`PostgresSaver.from_conn_string()` abre **una sola** conexión. Si la mantienes viva con `__enter__()` sin `__exit__()`, funciona hasta que el proveedor la cierra por inactividad — y psycopg **no reconecta**. A partir de ahí toda invocación falla con `OperationalError: the connection is closed` y, si el entrypoint traga excepciones, **el bot deja de responder en silencio** hasta reiniciar el proceso.

Usa un pool que valide antes de entregar:

```python
_pool = ConnectionPool(
    build_database_url(),
    min_size=1, max_size=10,
    kwargs={"autocommit": True, "prepare_threshold": 0, "connect_timeout": 5},
    timeout=5,
    check=ConnectionPool.check_connection,
    open=True,
)
```

Requiere `psycopg[binary,pool]` — el extra `pool` es fácil de olvidar.

- `prepare_threshold: 0` es obligatorio detrás de un pooler en modo transacción (Supabase puerto 6543, PgBouncer): sin él, `prepared statement already exists`.
- `connect_timeout` y `timeout` evitan que una base inalcanzable cuelgue el arranque ~30s.
- El checkpointer usa un `threading.Lock` interno, así que compartirlo no corrompe datos; pero serializa todo el tráfico. Con carga real, `AsyncPostgresSaver` + `ainvoke()`.

## Escritura del log plano

Fire-and-forget, siempre:

```python
def log_mensaje(entity_id, rol, contenido, tools_usadas=None):
    if _pool is None:
        return
    try:
        ...
    except Exception as e:
        print(f"⚠️ no se pudo registrar: {e}")
```

Un fallo escribiendo telemetría no debe impedir que el usuario reciba su respuesta.

Registra el mensaje entrante **antes** de invocar al agente: si el turno falla, al menos queda constancia de qué preguntó. Y guarda las tools invocadas en el turno — es lo que permite auditar después por qué el agente cambió una etapa o un campo.

## Seguridad

En Supabase, el badge `UNRESTRICTED` significa **RLS desactivado**, y las tablas del schema `public` se exponen por PostgREST: con la anon key se leen todas las conversaciones. Las tablas del checkpointer nacen así.

```sql
ALTER TABLE checkpoints           ENABLE ROW LEVEL SECURITY;
ALTER TABLE checkpoint_blobs      ENABLE ROW LEVEL SECURITY;
ALTER TABLE checkpoint_writes     ENABLE ROW LEVEL SECURITY;
ALTER TABLE checkpoint_migrations ENABLE ROW LEVEL SECURITY;
```

No rompe el agente: se conecta con el rol dueño de las tablas, que ignora RLS. Sin políticas, RLS activado = nadie entra por PostgREST.

Si las conversaciones incluyen documentos de identidad o datos financieros, considera cifrar el estado con `EncryptedSerializer.from_pycryptodome_aes()` (lee `LANGGRAPH_AES_KEY`).

## Retención de filas

Ninguna de las tres capas borra filas: el estado vivo se recorta, pero `checkpoints` y compañía siguen creciendo, y los hilos de usuarios inactivos quedan enteros para siempre. Eso necesita un limpiador aparte.

Al construirlo: **borra `thread_id` completos, nunca filas parciales de un hilo vivo.** LangGraph reconstruye algunos canales por deltas encadenados; si le quitas eslabones intermedios, el estado puede reconstruirse vacío. Y consulta al sistema externo qué entidades siguen activas antes de borrar.

## Memoria de largo plazo (cross-thread)

El checkpointer es memoria de **corto plazo**, aunque persista en disco: su alcance es un `thread_id`. Para hechos que cruzan conversaciones distintas (preferencias, presupuesto declarado hace meses) la pieza es el **Store**:

```python
graph = builder.compile(checkpointer=checkpointer, store=store)
```

Tablas propias, acceso por `namespace` en vez de por hilo. No lo montes si tu clave de conversación ya es estable por persona: con `thread_id` = contacto, el checkpointer ya cubre "que me recuerde".
