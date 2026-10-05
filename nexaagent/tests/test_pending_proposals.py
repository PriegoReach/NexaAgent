"""Tests de la acción pendiente única (pending.py).

Solo cabe una acción esperando confirmación por conversación. Antes, una segunda
propuesta en el mismo turno PISABA a la primera: el usuario veía la pregunta de una
y su "sí" ejecutaba la otra. Ahora la primera manda, la segunda queda anotada en su
`deferred` y el orquestador avisa de ella en la respuesta.

El servicio `tests` no levanta Redis: se sustituye por un doble en memoria con la
semántica de SET NX/XX que usa pending.py. Las tareas sí van a la BD real de test.
"""
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text

from app.agent import orchestrator, pending
from app.agent.tools import gmail, manage_tasks
from app.core.log_context import conversation_id_var
from app.db.session import engine

_CID = 7


class _FakeRedis:
    """Lo justo de redis.asyncio para pending.py: get, delete y set con NX/XX."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def set(self, key, value, ex=None, nx=False, xx=False, keepttl=False):
        if (nx and key in self.data) or (xx and key not in self.data):
            return None
        self.data[key] = value
        return True

    async def get(self, key):
        return self.data.get(key)

    async def delete(self, key):
        self.data.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch) -> _FakeRedis:
    fake = _FakeRedis()

    @asynccontextmanager
    async def _redis():
        yield fake

    monkeypatch.setattr(pending, "_redis", _redis)
    return fake


def _intent(action: str, arg: int, description: str) -> dict:
    return {
        "action": action,
        "args": {"id": arg},
        "description": description,
        "question": f"¿Confirmas {description}?",
    }


async def test_second_proposal_does_not_replace_the_first(fake_redis):
    first = _intent("delete_task", 1, "el borrado de la tarea #1 'a'")
    second = _intent("send_email", 2, "el envío del correo a juan@example.com")

    assert await pending.set_pending(_CID, first) is None
    assert (await pending.set_pending(_CID, second))["action"] == "delete_task"

    stored = await pending.get_pending(_CID)
    assert stored["action"] == "delete_task"           # lo que ejecutará el "sí"…
    assert stored["question"] == first["question"]     # …es lo que se mostró
    assert stored["deferred"] == [second["description"]]


async def test_repeating_the_same_proposal_is_not_deferred(fake_redis):
    first = _intent("delete_task", 1, "el borrado de la tarea #1 'a'")

    assert await pending.set_pending(_CID, first) is None
    assert await pending.set_pending(_CID, dict(first)) is None
    assert "deferred" not in await pending.get_pending(_CID)


async def test_delete_and_email_in_one_turn_show_and_run_the_same_action(fake_redis):
    """"Borra la tarea X y mándale un correo a Juan": la pregunta que se muestra y
    lo que ejecuta el "sí" son el borrado, y el correo queda avisado, no perdido."""
    async with engine.begin() as conn:
        task_id = (
            await conn.execute(
                text("INSERT INTO tasks (content) VALUES ('comprar pan') RETURNING id")
            )
        ).scalar_one()

    question = await manage_tasks._propose_delete(task_id, _CID)
    token = conversation_id_var.set(str(_CID))
    try:
        reply = gmail.send_email.invoke(
            {"to": "juan@example.com", "subject": "Aviso", "body": "Hola Juan."}
        )
    finally:
        conversation_id_var.reset(token)

    assert reply.startswith("Esta acción NO quedó propuesta")
    stored = await pending.get_pending(_CID)
    assert stored["action"] == "delete_task"

    shown = orchestrator._proposal_text(stored)
    assert shown.startswith(question)
    assert "el envío del correo a juan@example.com (asunto: 'Aviso')" in shown
