"""Evaluación del enrutado a herramientas: ¿llama el agente a la tool correcta?

Para cada mensaje del dataset se pregunta al modelo, con el MISMO system prompt y
las MISMAS tools que usa el agente, y se mira la PRIMERA herramienta que decide
llamar ("none" si contesta sin herramientas, "texto" si escribe la llamada en vez de
hacerla). Mide solo esa decisión: no ejecuta la tool ni sigue el bucle del agente,
así que no toca Google, la BD ni el correo.

El modelo no es del todo determinista ni con temperature 0 (GPU): de una pasada a
otra cambian uno o dos casos. Compara varias pasadas antes de sacar conclusiones.

Necesita Ollama vivo, así que NO va en la suite pytest determinista; se corre a mano:

    docker compose exec api python -m app.eval.tool_routing
    # o con otro dataset:
    docker compose exec api python -m app.eval.tool_routing app/eval/datasets/otro.yaml
"""
import asyncio
import sys
import time

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama

from app.agent.orchestrator import SYSTEM_PROMPT, is_tool_call_as_text
from app.agent.tools import get_tools
from app.core.config import settings
from app.eval.retrieval import load_cases

DEFAULT_DATASET = "app/eval/datasets/tool_routing.yaml"
NONE = "none"     # el modelo contesta directamente, sin llamar a ninguna herramienta
AS_TEXT = "texto"  # escribe la llamada en la respuesta en vez de hacerla (no se ejecuta)

_model = None


def _bound_model():
    """El modelo del agente (misma config que orchestrator._build_agent) con las tools."""
    global _model
    if _model is None:
        llm = ChatOllama(
            model=settings.ollama_model,
            base_url=settings.ollama_base_url,
            temperature=0,
            keep_alive="30m",
            timeout=120,
            client_kwargs={"timeout": 120},
            async_client_kwargs={"timeout": 120},
        )
        _model = llm.bind_tools(get_tools())
    return _model


async def first_tool(message: str) -> str:
    """Nombre de la primera herramienta que el modelo decide llamar ante `message`."""
    reply = await _bound_model().ainvoke(
        [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=message)]
    )
    if reply.tool_calls:
        return reply.tool_calls[0]["name"]
    return AS_TEXT if is_tool_call_as_text(reply.content) else NONE


def expected_tools(case: dict) -> list[str]:
    """`esperado` admite un nombre o una lista de nombres válidos."""
    expected = case["esperado"]
    return expected if isinstance(expected, list) else [expected]


async def evaluate_cases(cases: list[dict]):
    """Acierto global y por herramienta esperada sobre una lista de casos ya cargada.

    Devuelve (accuracy, por_tool, results): por_tool[esperado] = (aciertos, casos) y
    results = [(caso, herramienta_elegida, acierto), ...] en el orden del dataset.
    """
    results = []
    for case in cases:
        chosen = await first_tool(case["mensaje"])
        results.append((case, chosen, chosen in expected_tools(case)))

    accuracy = sum(1 for *_, ok in results if ok) / len(results)
    per_tool: dict[str, tuple[int, int]] = {}
    for case, _, ok in results:
        key = " | ".join(expected_tools(case))
        hits, total = per_tool.get(key, (0, 0))
        per_tool[key] = (hits + ok, total + 1)
    return accuracy, per_tool, results


async def evaluate(dataset_path: str):
    return await evaluate_cases(load_cases(dataset_path))


def _format(accuracy, per_tool, results, seconds: float) -> str:
    hits = sum(1 for *_, ok in results if ok)
    lines = [
        f"Modelo: {settings.ollama_model}",
        f"Casos: {len(results)}   Acierto: {accuracy:.3f} ({hits}/{len(results)})   "
        f"Tiempo: {seconds:.0f} s ({seconds / len(results):.1f} s por caso)",
        "",
        "Por herramienta esperada (aciertos/casos):",
    ]
    width = max(len(k) for k in per_tool)
    for tool_name, (tool_hits, total) in sorted(per_tool.items()):
        lines.append(f"  {tool_name:{width}}  {tool_hits}/{total}")
    as_text = sum(1 for _, chosen, _ in results if chosen == AS_TEXT)
    lines += [
        "",
        f"Llamadas escritas como texto: {as_text} (el agente las sustituye por un aviso)",
    ]
    failures = [(case, chosen) for case, chosen, ok in results if not ok]
    lines += ["", "Fallos (esperado -> eligió):" if failures else "Sin fallos."]
    for case, chosen in failures:
        lines.append(
            f"  {' | '.join(expected_tools(case))} -> {chosen}   «{case['mensaje'][:60]}»"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    ds = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DATASET
    start = time.perf_counter()
    accuracy, per_tool, results = asyncio.run(evaluate(ds))
    print(_format(accuracy, per_tool, results, time.perf_counter() - start))
