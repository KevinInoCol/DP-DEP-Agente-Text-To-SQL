# Tools del agente

Un archivo por tool en `tools/`. Cada archivo expone dos cosas.

## El patrón factory

```python
# tools/mover_a_calificado.py
import os
import requests
from langchain.tools import tool


def mover_a_calificado(entity_id: int) -> bool:
    """
    Función imperativa. Testeable sin LLM y reutilizable desde scripts.
    Devuelve True/False, no lanza.
    """
    url = f"https://{os.getenv('CRM_SUBDOMAIN')}.crm.com/api/v4/leads/{entity_id}"
    r = requests.patch(url, headers=..., json={"status_id": ...})
    if r.status_code == 200:
        return True
    print(f"❌ error {r.status_code}: {r.text}")
    return False


def get_mover_a_calificado_tool(entity_id: int):
    """
    Factory: devuelve la tool con el entity_id cerrado en clausura.
    El LLM nunca ve el ID, así que no puede alucinarlo ni escribir en otro.
    """
    @tool
    def mover_a_calificado_tool() -> str:
        """Mueve el lead actual a la etapa 'Calificado'.
        Úsala cuando el cliente muestra intención clara de comprar: pide
        requisitos, pregunta por documentación o quiere agendar. No la uses
        para preguntas informativas. No requiere argumentos."""
        ok = mover_a_calificado(entity_id)
        return "Lead movido a Calificado." if ok else "Error al mover el lead."
    return mover_a_calificado_tool
```

**Por qué la clausura y no un parámetro `entity_id`:** si el ID fuera argumento, el LLM tendría que producirlo, y puede equivocarse o inventarlo — escribiendo en el registro de otro cliente. Con clausura es imposible por construcción.

## El docstring es interfaz, no documentación

El LLM decide qué invocar leyendo solo el nombre y el docstring. Ahí van:

- **Cuándo sí** usarla, con señales concretas del mensaje del usuario.
- **Cuándo no**, especialmente frente a tools parecidas.
- **Qué significa cada argumento**, con ejemplos de valores reales.

```python
@tool
def buscar_inmuebles(tipo: str = "", barrio: str = "", alquiler_max: float = 0) -> str:
    """Consulta el inventario de inmuebles disponibles. ÚNICA fuente de verdad
    del inventario. Todos los argumentos son OPCIONALES: omite los que el
    cliente no mencione.

    Args:
        tipo: Casa, Departamento o Kitnet.
        barrio: barrio o zona (ej: 'Barão Geraldo').
        alquiler_max: alquiler mensual máximo en reales. 0 = sin tope.
    """
```

Si dos tools se confunden entre sí, el arreglo va en los docstrings antes que en el system prompt: está más cerca de la decisión.

## Tools compartidas vs por entidad

- **Compartidas** (retrieval, consulta de catálogo): no dependen del ID. Constrúyelas **una vez** en `init_resources()`. Cargar un índice o abrir un Google Sheet por mensaje es latencia regalada.
- **Por entidad** (acciones sobre el CRM): factory por mensaje en `build_agent_for_lead()`.

## Errores comunes

**Validación de conexión en la factory.** Si la factory abre la conexión (Sheet, API), un fallo de credenciales tumba el arranque entero de la app con un `FileNotFoundError` en el import. Es deseable como fail-fast, pero documenta el requisito en el README o alguien pasará una hora sin entender por qué `uvicorn` no levanta.

**`int(os.getenv(...))` a nivel de módulo o dentro de la tool.** Si falta la variable, revienta con `TypeError: int() argument must be ... not 'NoneType'` en pleno request en vez de al arrancar. Valida todas las env vars en el entrypoint, incluidas las que solo usan las tools.

**Devolver excepciones al LLM.** Si la tool lanza, el error sube al agente y el usuario se queda sin respuesta. Captura y devuelve un string que el LLM pueda usar: `"Error al consultar el inventario. Ofrece conectar con un asesor."`

**Campos de tipo select con texto libre.** Si el CRM tiene un campo enum y le mandas el texto que produjo el LLM, la API devuelve 400 salvo coincidencia exacta. Consulta los valores válidos con un script en `<Integracion>_support_tools/` y restringe el argumento en el docstring.

**Nombres heredados.** Una tool que se llamaba `update_marca_de_interes` y ahora guarda IDs de inmueble desorienta al LLM y a quien mantenga el código. Renombra cuando cambie el dominio.

## Exclusión mutua

Cuando varias tools son mutuamente excluyentes por turno (mover a una sola etapa), esa regla va en el **system prompt**, no en el código: el prompt es lo único que ve el conjunto completo.

```
- Las 3 tools de etapa son MUTUAMENTE EXCLUYENTES: invoca máximo UNA por turno,
  la de la etapa más avanzada que aplique.
- Las tools de update SÍ pueden acompañar a una de etapa.
```

Si el LLM las combina mal de forma persistente, el problema suele estar en los docstrings, que se solapan.

## `tools/__init__.py`

Exporta ambas formas: la imperativa (para tests) y la factory (para el agente).

```python
from .retrieval import get_index, get_retrieval_tool
from .mover_a_calificado import mover_a_calificado, get_mover_a_calificado_tool

__all__ = ["get_index", "get_retrieval_tool",
           "mover_a_calificado", "get_mover_a_calificado_tool"]
```

Ojo al renombrar archivos: este `__init__.py` rompe silenciosamente el arranque si queda un import obsoleto.
