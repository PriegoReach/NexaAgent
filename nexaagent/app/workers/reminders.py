"""Recordatorios: un resumen diario de las tareas de hoy y las vencidas.

"Recuérdame comprar pan el viernes" crea una tarea con fecha, pero hasta ahora nada
avisaba el viernes. Celery beat lanza send_daily_digest cada hora en punto
(celery_app.beat_schedule): la primera ejecución a partir de settings.reminder_hour,
en la hora local del usuario, en la que haya tareas pendientes para hoy o vencidas
manda UN resumen; el resto del día ya no.

Canal: correo por Gmail a settings.reminder_email si está configurado; si no, el
webhook; si no hay ninguno, no se manda nada (y se avisa en el log). El correo va
al propio usuario y lo redacta el código, no el modelo: por eso no pasa por la
confirmación de send_email.

Una vez al día aunque haya reinicios o varios workers: el día se reclama en
reminder_digests ANTES de enviar (registrar-primero, como sent_emails). Un fallo
definitivo ('failed') deja reintentar en la hora siguiente; uno incierto (el correo
pudo salir) no.
"""
import logging
from datetime import date

from sqlalchemy import text

from app.agent.tools.call_webhook import _register_and_send, _webhook_url
from app.agent.tools.create_task import _DIAS, _MESES
from app.agent.tools.gmail import send_email_now
from app.core import clock
from app.core.config import settings
from app.db.worker_db import worker_session

logger = logging.getLogger("nexa.reminders")


def _fmt_day(d: date) -> str:
    return f"{_DIAS[d.weekday()]} {d.day} de {_MESES[d.month - 1]}"


async def _due_tasks(today: date) -> list[dict]:
    """Tareas pendientes con fecha de hoy o anterior, las vencidas primero."""
    async with worker_session() as session:
        rows = await session.execute(
            text(
                "SELECT id, content, due_date FROM tasks "
                "WHERE status != 'done' AND due_date IS NOT NULL AND due_date <= :today "
                "ORDER BY due_date, id"
            ),
            {"today": today},
        )
        return [dict(r._mapping) for r in rows]


async def _claim(today: date) -> bool:
    """Reclama el resumen de hoy. True si este intento debe enviarlo: el día es
    nuevo, o el intento anterior falló sin salir ('failed')."""
    async with worker_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO reminder_digests (day, status) VALUES (:day, 'pending') "
                "ON CONFLICT (day) DO UPDATE SET status = 'pending', updated_at = now() "
                "WHERE reminder_digests.status = 'failed' "
                "RETURNING day"
            ),
            {"day": today},
        )
        claimed = result.first() is not None
        await session.commit()
    return claimed


async def _finish(today: date, status: str, channel: str | None, n_tasks: int) -> None:
    async with worker_session() as session:
        await session.execute(
            text(
                "UPDATE reminder_digests SET status = :status, channel = :channel, "
                "n_tasks = :n, updated_at = now() WHERE day = :day"
            ),
            {"status": status, "channel": channel, "n": n_tasks, "day": today},
        )
        await session.commit()


def compose(tasks: list[dict], today: date) -> tuple[str, str]:
    """(asunto, cuerpo) del resumen."""
    for_today = [t for t in tasks if t["due_date"] == today]
    overdue = [t for t in tasks if t["due_date"] < today]
    n = len(tasks)
    subject = (f"Recordatorio de Nexa: {n} {'tarea pendiente' if n == 1 else 'tareas pendientes'} "
               f"({_fmt_day(today)})")
    lines = ["Hola, estas son tus tareas pendientes:", ""]
    if for_today:
        lines.append(f"Para hoy, {_fmt_day(today)}:")
        lines += [f"• #{t['id']} {t['content']}" for t in for_today]
        lines.append("")
    if overdue:
        lines.append("Vencidas:")
        lines += [f"• #{t['id']} {t['content']} (era para el {_fmt_day(t['due_date'])})"
                  for t in overdue]
        lines.append("")
    lines += ["Para marcarlas como hechas o cambiarles la fecha, díselo a Nexa.", "", "— Nexa"]
    return subject, "\n".join(lines)


async def _deliver(subject: str, body: str) -> tuple[str | None, str]:
    """(canal, estado). Estado: 'sent', 'failed', 'uncertain' o 'skipped'."""
    if settings.reminder_email:
        result = await send_email_now(settings.reminder_email, subject, body)
        return "email", result.status
    url = _webhook_url()
    if url:
        outcome = await _register_and_send(f"{subject}\n\n{body}", url)
        # DUPLICATE = ese mismo mensaje ya se había mandado: ya avisó.
        return "webhook", "sent" if outcome == "DUPLICATE" else outcome
    return None, "skipped"


async def send_daily_digest() -> str:
    """Manda el resumen de hoy si toca. Devuelve qué pasó (para el log y los tests):
    'too_early', 'no_tasks', 'already_done', 'sent', 'failed', 'uncertain' o
    'skipped'."""
    now = clock.local_now()
    if now.hour < settings.reminder_hour:
        return "too_early"
    today = now.date()
    tasks = await _due_tasks(today)
    if not tasks:
        return "no_tasks"
    if not await _claim(today):
        return "already_done"

    subject, body = compose(tasks, today)
    channel, status = await _deliver(subject, body)
    await _finish(today, status, channel, len(tasks))
    if status == "skipped":
        logger.warning("daily digest not sent: configure REMINDER_EMAIL or the webhook",
                       extra={"event": "reminders_no_channel"})
    logger.info("daily digest", extra={"event": "reminders_digest", "status": status,
                                       "channel": channel, "n_tasks": len(tasks)})
    return status
