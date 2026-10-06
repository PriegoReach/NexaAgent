"""Tests de las fechas relativas en la zona horaria del usuario (bug de UTC).

El contenedor corre en UTC. Antes, "mañana" se resolvía contra date.today(): desde
las 18:00 en Ciudad de México (UTC-6) eso ya es el día siguiente, y la tarea o el
evento caían un día tarde. Ahora se resuelve contra la fecha local de
settings.calendar_timezone. Se fija el reloj en las 19:30 del lunes 5 de octubre
de 2026 en Ciudad de México, que son las 01:30 UTC del martes 6.
"""
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text

from app.agent.tools import calendar
from app.agent.tools.create_task import create_task
from app.core import clock
from app.core.config import settings
from app.db.session import engine

_EVENING_IN_MEXICO = datetime(2026, 10, 6, 1, 30, tzinfo=timezone.utc)   # lun 5, 19:30


@pytest.fixture
def evening(monkeypatch):
    monkeypatch.setattr(settings, "calendar_timezone", "America/Mexico_City")
    monkeypatch.setattr(clock, "_utcnow", lambda: _EVENING_IN_MEXICO)


def test_local_today_is_the_users_date_not_utc(evening):
    assert clock.local_today() == date(2026, 10, 5)


def test_local_today_follows_the_configured_timezone(evening, monkeypatch):
    monkeypatch.setattr(settings, "calendar_timezone", "Europe/Madrid")   # 03:30 del martes
    assert clock.local_today() == date(2026, 10, 6)


async def test_task_for_tomorrow_lands_on_tomorrow(evening):
    reply = create_task.invoke({"content": "comprar pan", "due_date": "mañana"})

    assert "martes 6 de octubre de 2026 (2026-10-06)" in reply
    async with engine.begin() as conn:
        due = (
            await conn.execute(text("SELECT due_date FROM tasks WHERE content = 'comprar pan'"))
        ).scalar_one()
    assert due == date(2026, 10, 6)


async def test_task_for_today_is_not_rejected_as_past(evening):
    """Con la fecha UTC, "hoy" ya era el martes 6; ahora es el lunes 5."""
    reply = create_task.invoke({"content": "llamar a Ana", "due_date": "2026-10-05"})

    assert reply.startswith("Tarea #")
    assert "(2026-10-05)" in reply


async def test_event_for_tomorrow_lands_on_tomorrow(evening):
    question = await calendar._propose_event("reunión de equipo", "mañana", "3pm", 60, None)

    assert "martes 6 de octubre de 2026 a las 15:00" in question
