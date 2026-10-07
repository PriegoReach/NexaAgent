"""Tests del eval de enrutado a herramientas (app/eval/tool_routing.py).

Igual que el de recuperación, en dos niveles:

  1. Deterministas, en la suite estándar: la aritmética del acierto con el modelo
     sustituido, y que el dataset solo nombra herramientas que existen (una errata
     en `esperado` sería un fallo falso para siempre).

  2. test_tool_routing_floor_real — PISO sobre el modelo real. Skippeado por defecto
     (la suite no depende de Ollama); con Ollama levantado, el servicio `tests` lo
     alcanza por la red de compose:
         docker compose run --rm -e EVAL_REAL=1 tests pytest tests/test_tool_routing_eval.py
"""
import os

import pytest
from langchain_core.messages import AIMessage

from app.agent.tools import get_tools
from app.eval import tool_routing

# Medido entre 0.84 y 0.90 según la sesión (ver el docstring del test): el piso deja
# margen para esa deriva y salta con una regresión amplia, como la variante del
# prompt que bajó a 0.737.
FLOOR = 0.75


async def test_routing_accuracy_math(monkeypatch):
    cases = [
        {"mensaje": "a", "esperado": "list_tasks"},
        {"mensaje": "b", "esperado": "list_tasks"},
        {"mensaje": "c", "esperado": "none"},
        {"mensaje": "d", "esperado": ["send_email", "none"]},   # varias respuestas válidas
    ]
    chosen = {"a": "list_tasks", "b": "update_task", "c": "none", "d": "none"}

    async def fake_first_tool(message: str) -> str:
        return chosen[message]

    monkeypatch.setattr(tool_routing, "first_tool", fake_first_tool)

    accuracy, per_tool, results = await tool_routing.evaluate_cases(cases)

    assert accuracy == pytest.approx(3 / 4)
    assert per_tool == {"list_tasks": (1, 2), "none": (1, 1), "send_email | none": (1, 1)}
    assert [(case["mensaje"], tool, ok) for case, tool, ok in results] == [
        ("a", "list_tasks", True),
        ("b", "update_task", False),
        ("c", "none", True),
        ("d", "none", True),
    ]


async def test_first_tool_is_the_first_tool_call_or_none(monkeypatch):
    replies = iter([
        AIMessage(content="", tool_calls=[
            {"name": "list_tasks", "args": {}, "id": "1"},
            {"name": "send_email", "args": {}, "id": "2"},
        ]),
        AIMessage(content="¡Hola! ¿En qué te ayudo?"),
    ])

    class FakeModel:
        async def ainvoke(self, messages):
            assert messages[0].content == tool_routing.SYSTEM_PROMPT   # el prompt del agente
            return next(replies)

    monkeypatch.setattr(tool_routing, "_model", FakeModel())

    assert await tool_routing.first_tool("borra la tarea de Rubí") == "list_tasks"
    assert await tool_routing.first_tool("hola") == tool_routing.NONE


def test_the_dataset_only_names_real_tools():
    valid = {t.name for t in get_tools()} | {tool_routing.NONE}
    cases = tool_routing.load_cases(tool_routing.DEFAULT_DATASET)

    unknown = {name for case in cases for name in tool_routing.expected_tools(case)} - valid

    assert not unknown, f"herramientas que no existen en el dataset: {unknown}"
    assert len(cases) >= 30


@pytest.mark.skipif(
    not os.getenv("EVAL_REAL"),
    reason="piso de regresión sobre el modelo real; requiere Ollama. "
    "Activar con EVAL_REAL=1 (docker compose run --rm -e EVAL_REAL=1 tests ...).",
)
async def test_tool_routing_floor_real():
    """Piso sobre el modelo real (qwen2.5 7B, temperature 0).

    Medición del 2026-10-07, 38 casos: 0.816 con el prompt inicial; 0.842-0.895 con el
    actual (de una sesión a otra cambian 1-2 casos, aun con temperature 0). Fallan
    siempre los mismos tipos: pedir el id antes de mover/borrar por descripción (el
    modelo pregunta en vez de llamar a list_tasks/list_calendar_events) y alguna
    llamada escrita como texto, que el orquestador sustituye por un aviso.
    """
    accuracy, _, results = await tool_routing.evaluate(tool_routing.DEFAULT_DATASET)
    failures = [
        f"{case['mensaje']!r}: {tool}" for case, tool, ok in results if not ok
    ]
    assert accuracy >= FLOOR, f"acierto {accuracy:.3f} < {FLOOR}; fallos: {failures}"
