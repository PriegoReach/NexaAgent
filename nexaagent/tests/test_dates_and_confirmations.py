"""Tests de las funciones puras que deciden qué hace el agente:
  - _resolve_due / _resolve_time: fechas y horas en lenguaje natural ("el viernes",
    "3pm") a valores reales. Las calcula el código, no el modelo.
  - _handle_confirmation: la heurística sí/no de las acciones confirmables. Ante la
    duda NO ejecuta y vuelve a preguntar.

Con una fecha imposible ("31 de febrero"), _resolve_due lanzaba ValueError y la tool
fallaba a mitad del turno; ahora devuelve None, como con un ISO inválido.
"""
from datetime import date

import pytest

from app.agent import orchestrator, pending
from app.agent.confirmable import CONFIRMABLE_ACTIONS
from app.agent.tools.calendar import _resolve_time
from app.agent.tools.create_task import _resolve_due

_MONDAY = date(2026, 10, 5)


@pytest.mark.parametrize(
    "phrase, expected",
    [
        ("hoy", date(2026, 10, 5)),
        ("Mañana", date(2026, 10, 6)),
        ("pasado mañana", date(2026, 10, 7)),
        ("el viernes", date(2026, 10, 9)),
        ("el próximo martes", date(2026, 10, 6)),
        ("sábado", date(2026, 10, 10)),
        ("lunes", date(2026, 10, 12)),            # nunca hoy: el próximo
        ("5 de octubre", date(2026, 10, 5)),
        ("31 de diciembre", date(2026, 12, 31)),
        ("15 de junio", date(2027, 6, 15)),       # ya pasó este año: el siguiente
        ("1 de setiembre", date(2027, 9, 1)),
        ("2026-10-20", date(2026, 10, 20)),
    ],
)
def test_resolve_due(phrase, expected):
    assert _resolve_due(phrase, _MONDAY) == expected


@pytest.mark.parametrize(
    "phrase",
    ["", "2026-10-04", "2026-02-30", "31 de febrero", "29 de febrero", "la semana que viene", "13 de mes"],
    ids=["vacía", "iso-pasada", "iso-inválida", "31-feb", "29-feb-no-bisiesto", "sin-fecha", "mes-inventado"],
)
def test_resolve_due_rejects_what_it_cannot_place(phrase):
    assert _resolve_due(phrase, _MONDAY) is None


@pytest.mark.parametrize(
    "phrase, expected",
    [
        ("3pm", (15, 0)), ("3 pm", (15, 0)), ("3 p.m.", (15, 0)), ("15:00", (15, 0)),
        ("8:30am", (8, 30)), ("12am", (0, 0)), ("12pm", (12, 0)), ("9", (9, 0)),
        ("15h", (15, 0)), ("15 hrs", (15, 0)), ("23:59", (23, 59)),
    ],
)
def test_resolve_time(phrase, expected):
    assert _resolve_time(phrase) == expected


@pytest.mark.parametrize("phrase", ["", "25:00", "3:75", "13pm", "mediodía", "a las 3"])
def test_resolve_time_rejects_what_it_cannot_read(phrase):
    assert _resolve_time(phrase) is None


# --- Heurística sí/no -------------------------------------------------------
_INTENT = {"action": "accion_de_prueba", "args": {"x": 1}, "description": "#7 'comprar pan'"}


@pytest.fixture
def confirm(monkeypatch):
    """Registra una acción de prueba y sustituye Redis. Devuelve las llamadas."""
    calls = {"performed": [], "cleared": 0}

    async def perform(args: dict) -> str:
        calls["performed"].append(args)
        return "Hecho."

    async def clear_pending(conversation_id: int) -> None:
        calls["cleared"] += 1

    monkeypatch.setitem(CONFIRMABLE_ACTIONS, "accion_de_prueba", perform)
    monkeypatch.setattr(pending, "clear_pending", clear_pending)
    return calls


@pytest.mark.parametrize(
    "reply",
    ["sí", "Sí.", "¡Sí!", "si", "SÍ", "Claro que sí", "dale", "ok", "Vale.", "sí, bórrala", "Sí, quiero"],
)
async def test_yes_executes_the_pending_action(confirm, reply):
    answer = await orchestrator._handle_confirmation(1, reply, _INTENT)

    assert answer == "Hecho."
    assert confirm["performed"] == [{"x": 1}]
    assert confirm["cleared"] == 1


@pytest.mark.parametrize("reply", ["no", "No.", "¡No!", "cancela", "mejor no", "déjalo", "olvídalo"])
async def test_no_cancels_without_executing(confirm, reply):
    answer = await orchestrator._handle_confirmation(1, reply, _INTENT)

    assert answer == "Cancelado, no hice nada."
    assert confirm["performed"] == []
    assert confirm["cleared"] == 1


@pytest.mark.parametrize(
    "reply",
    ["no sé", "bueno", "sí pero cámbiale la hora", "¿qué tarea?", "sí no", ""],
)
async def test_anything_else_asks_again_and_keeps_the_action(confirm, reply):
    answer = await orchestrator._handle_confirmation(1, reply, _INTENT)

    assert answer == "No entendí. ¿Confirmas #7 'comprar pan'? Responde sí o no."
    assert confirm["performed"] == []
    assert confirm["cleared"] == 0          # la acción sigue pendiente


async def test_yes_to_an_unknown_action_clears_it(confirm):
    answer = await orchestrator._handle_confirmation(1, "sí", {"action": "no_existe", "args": {}})

    assert answer == "No pude ejecutar la acción pendiente (acción desconocida)."
    assert confirm["cleared"] == 1
