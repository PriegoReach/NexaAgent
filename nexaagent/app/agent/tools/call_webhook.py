import asyncio
import hashlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor

import httpx
from langchain_core.tools import tool
from sqlalchemy import text

from app.core.config import read_secret_file
from app.db.worker_db import worker_session

logger = logging.getLogger("nexa.tools")


def _run_async(coro):
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _webhook_url() -> str | None:
    # La URL del webhook es una CREDENCIAL externa: archivo en /run/secrets/, con
    # fallback a env var para desarrollo. Opcional: vacía (o sin archivo) = no hay
    # webhook, y la tool lo dice.
    return read_secret_file(os.getenv("WEBHOOK_URL", ""), "webhook_url") or None


def _event_key(message: str) -> str:
    # Clave del evento sobre el contenido normalizado.
    return hashlib.sha256(message.strip().lower().encode("utf-8")).hexdigest()


async def _register_and_send(message: str, url: str) -> str:
    """Registra la notificación y la dispara. Devuelve 'sent', 'failed' (seguro que
    no salió: el mismo mensaje se puede reintentar), 'uncertain' (pudo llegar: no se
    reintenta) o 'DUPLICATE' (ya se había enviado). La misma clasificación que el
    correo: antes, un fallo bloqueaba el reintento y se daba por enviado."""
    key = _event_key(message)
    async with worker_session() as session:
        # PASO 1: registrar ANTES de disparar (sesgo a no-duplicar). Si la clave ya
        # existe solo se reclama un intento que falló SIN salir ('failed').
        result = await session.execute(
            text(
                "INSERT INTO webhook_events (idempotency_key, message, status) "
                "VALUES (:key, :msg, 'pending') "
                "ON CONFLICT (idempotency_key) DO UPDATE SET status = 'pending' "
                "WHERE webhook_events.status = 'failed' "
                "RETURNING id"
            ),
            {"key": key, "msg": message},
        )
        row = result.first()
        await session.commit()
        if row is None:
            existing = (await session.execute(
                text("SELECT status FROM webhook_events WHERE idempotency_key = :key"),
                {"key": key},
            )).scalar_one_or_none()
            logger.info("call_webhook not resent", extra={"idempotency_key": key, "status": existing})
            return "DUPLICATE" if existing == "sent" else "uncertain"
        event_id = row[0]

    # PASO 2: disparar el POST (fuera de la sesión; el registro ya está commiteado).
    try:
        resp = httpx.post(url, json={"message": message}, timeout=15)
        resp.raise_for_status()
        final_status = "sent"
    except httpx.HTTPStatusError as exc:
        # 4xx: el receptor la rechazó, no se procesó. 5xx: pudo procesarla.
        final_status = "uncertain" if exc.response.status_code >= 500 else "failed"
        logger.warning("call_webhook POST failed",
                       extra={"event_id": event_id, "status": exc.response.status_code})
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        final_status = "failed"   # la conexión ni se abrió: no salió
        logger.warning("call_webhook unreachable", extra={"event_id": event_id, "exc": str(exc)})
    except httpx.HTTPError as exc:
        final_status = "uncertain"   # pudo salir y perderse la respuesta
        logger.warning("call_webhook outcome unknown",
                       extra={"event_id": event_id, "exc": str(exc)})

    # PASO 3: registrar el resultado real del POST.
    async with worker_session() as session:
        await session.execute(
            text("UPDATE webhook_events SET status = :s WHERE id = :id"),
            {"s": final_status, "id": event_id},
        )
        await session.commit()
    logger.info("call_webhook executed",
               extra={"event_id": event_id, "status": final_status, "idempotency_key": key})
    return final_status


@tool
def call_webhook(message: str) -> str:
    """Send a notification message to the configured external webhook.
    Use this when the user asks to send a notification, alert, or ping to the
    external system (e.g. "notify the team that the report is ready").

    Args:
        message: The notification text to send.
    """
    url = _webhook_url()
    if not url:
        return "No hay webhook configurado (falta la URL en los secretos)."
    result = _run_async(_register_and_send(message, url))
    if result == "DUPLICATE":
        return "Esa notificación ya se había enviado (no se reenvió)."
    if result == "sent":
        return "Notificación enviada."
    if result == "uncertain":
        return ("No sé si la notificación llegó (el webhook no respondió bien); para no "
                "duplicarla, no la reintento.")
    return ("La notificación no se pudo entregar (el webhook falló); se puede volver a "
            "pedir.")
