"""Tests de los recordatorios: un resumen diario de las tareas de hoy y las vencidas.

Antes, "recuérdame comprar pan el viernes" creaba la tarea pero nada avisaba el
viernes. Ahora Celery beat lanza send_daily_digest cada hora y, a partir de
REMINDER_HOUR (hora local), manda UN resumen al día: por correo a REMINDER_EMAIL o,
si no hay correo, por el webhook.

El correo y el webhook se sustituyen por dobles; las tareas y la tabla
reminder_digests van a la BD real de test. El reloj se fija a mano.
"""
from datetime import date, datetime

import pytest
from sqlalchemy import text

from app.agent.tools.gmail import SendResult
from app.core import clock
from app.core.config import settings
from app.db.session import engine
from app.workers import reminders


def _at(monkeypatch, iso: str) -> None:
    monkeypatch.setattr(clock, "_utcnow", lambda: datetime.fromisoformat(iso))


@pytest.fixture
def outbox(monkeypatch):
    """Canales falsos. `emails`/`webhooks` anotan lo enviado; `email_results` y
    `webhook_results` fijan cómo termina cada envío (por defecto, bien)."""
    box = {"emails": [], "webhooks": [], "email_results": [], "webhook_results": [], "webhook_url": None}

    async def fake_send_email_now(to, subject, body):
        box["emails"].append({"to": to, "subject": subject, "body": body})
        return box["email_results"].pop(0) if box["email_results"] else SendResult("sent", "ok")

    async def fake_register_and_send(message, url):
        box["webhooks"].append(message)
        return box["webhook_results"].pop(0) if box["webhook_results"] else "sent"

    monkeypatch.setattr(reminders, "send_email_now", fake_send_email_now)
    monkeypatch.setattr(reminders, "_register_and_send", fake_register_and_send)
    monkeypatch.setattr(reminders, "_webhook_url", lambda: box["webhook_url"])
    monkeypatch.setattr(settings, "calendar_timezone", "America/Mexico_City")
    monkeypatch.setattr(settings, "reminder_hour", 8)
    monkeypatch.setattr(settings, "reminder_email", "yo@example.com")
    _at(monkeypatch, "2026-10-06T15:00:00+00:00")   # martes 6 de octubre, 9:00 en CDMX
    return box


async def _task(content: str, due: date | None, status: str = "pending") -> int:
    async with engine.begin() as conn:
        return (await conn.execute(
            text("INSERT INTO tasks (content, due_date, status) VALUES (:c, :d, :s) RETURNING id"),
            {"c": content, "d": due, "s": status},
        )).scalar_one()


async def _digests() -> list[tuple]:
    async with engine.begin() as conn:
        rows = await conn.execute(text("SELECT day, status, channel, n_tasks FROM reminder_digests"))
        return [tuple(r) for r in rows]


async def test_nothing_before_the_configured_hour(outbox, monkeypatch):
    await _task("comprar pan", date(2026, 10, 6))
    _at(monkeypatch, "2026-10-06T13:30:00+00:00")   # 7:30 en CDMX

    assert await reminders.send_daily_digest() == "too_early"
    assert outbox["emails"] == []


async def test_nothing_when_no_task_is_due(outbox):
    await _task("dentista", date(2026, 10, 8))

    assert await reminders.send_daily_digest() == "no_tasks"
    assert await _digests() == []


async def test_one_digest_a_day_with_todays_and_overdue_tasks(outbox):
    today = await _task("comprar pan", date(2026, 10, 6))
    overdue = await _task("llamar a Ana", date(2026, 10, 3))
    await _task("dentista", date(2026, 10, 8))                    # futura
    await _task("pagar luz", date(2026, 10, 6), status="done")    # hecha
    await _task("leer un libro", None)                            # sin fecha

    assert await reminders.send_daily_digest() == "sent"
    assert await reminders.send_daily_digest() == "already_done"

    assert len(outbox["emails"]) == 1
    email = outbox["emails"][0]
    assert email["to"] == "yo@example.com"
    assert email["subject"] == "Recordatorio de Nexa: 2 tareas pendientes (martes 6 de octubre)"
    assert f"Para hoy, martes 6 de octubre:\n• #{today} comprar pan" in email["body"]
    assert f"Vencidas:\n• #{overdue} llamar a Ana (era para el sábado 3 de octubre)" in email["body"]
    for not_included in ("dentista", "pagar luz", "leer un libro"):
        assert not_included not in email["body"]
    assert await _digests() == [(date(2026, 10, 6), "sent", "email", 2)]


async def test_a_definitive_failure_is_retried_the_next_hour(outbox, monkeypatch):
    await _task("comprar pan", date(2026, 10, 6))
    outbox["email_results"] = [SendResult("failed", "sin cuenta de Google")]

    assert await reminders.send_daily_digest() == "failed"
    _at(monkeypatch, "2026-10-06T16:00:00+00:00")
    assert await reminders.send_daily_digest() == "sent"

    assert len(outbox["emails"]) == 2
    assert await _digests() == [(date(2026, 10, 6), "sent", "email", 1)]


async def test_an_uncertain_send_is_not_retried(outbox):
    await _task("comprar pan", date(2026, 10, 6))
    outbox["email_results"] = [SendResult("uncertain", "Gmail no respondió bien")]

    assert await reminders.send_daily_digest() == "uncertain"
    assert await reminders.send_daily_digest() == "already_done"
    assert len(outbox["emails"]) == 1


async def test_without_an_email_it_uses_the_webhook(outbox, monkeypatch):
    monkeypatch.setattr(settings, "reminder_email", "")
    outbox["webhook_url"] = "https://hooks.example.com/nexa"
    await _task("comprar pan", date(2026, 10, 6))

    assert await reminders.send_daily_digest() == "sent"

    assert outbox["webhooks"][0].startswith("Recordatorio de Nexa: 1 tarea pendiente (martes 6 de octubre)")
    assert await _digests() == [(date(2026, 10, 6), "sent", "webhook", 1)]


async def test_a_webhook_already_sent_counts_as_sent(outbox, monkeypatch):
    monkeypatch.setattr(settings, "reminder_email", "")
    outbox["webhook_url"] = "https://hooks.example.com/nexa"
    outbox["webhook_results"] = ["DUPLICATE"]
    await _task("comprar pan", date(2026, 10, 6))

    assert await reminders.send_daily_digest() == "sent"


async def test_without_any_channel_it_skips_the_day(outbox, monkeypatch):
    monkeypatch.setattr(settings, "reminder_email", "")
    await _task("comprar pan", date(2026, 10, 6))

    assert await reminders.send_daily_digest() == "skipped"
    assert await reminders.send_daily_digest() == "already_done"   # no lo intenta cada hora
    assert await _digests() == [(date(2026, 10, 6), "skipped", None, 1)]


async def test_today_is_the_users_day(outbox, monkeypatch):
    """A las 3:00 UTC del miércoles 7 en CDMX todavía es martes 6 por la noche."""
    await _task("comprar pan", date(2026, 10, 6))
    await _task("dentista", date(2026, 10, 7))
    _at(monkeypatch, "2026-10-07T03:00:00+00:00")

    assert await reminders.send_daily_digest() == "sent"
    assert "dentista" not in outbox["emails"][0]["body"]
    assert outbox["emails"][0]["subject"].endswith("(martes 6 de octubre)")


def test_beat_runs_the_reminders_every_hour():
    import app.workers.tasks  # noqa: F401  (registra las tareas en celery_app)
    from app.workers.celery_app import celery_app

    entry = celery_app.conf.beat_schedule["daily-reminders"]
    assert entry["task"] == "send_daily_reminders"
    assert "send_daily_reminders" in celery_app.tasks
    assert entry["schedule"].minute == {0} and entry["schedule"].hour == set(range(24))
