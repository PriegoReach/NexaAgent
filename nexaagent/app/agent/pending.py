"""Intención pendiente de confirmación (confirmación asistida).

Una acción IRREVERSIBLE propuesta (delete_task, create_calendar_event, send_email...)
se guarda aquí en Redis, APARTE del historial (clave nexa:pending:<conv_id>, no
nexa:memory:<id>), con TTL corto (efímera: o se confirma pronto, o caduca). El turno
siguiente la lee para saber que es una respuesta a la propuesta y no un mensaje normal.

UNA sola pendiente por conversación, y la primera manda: una propuesta nunca pisa a
otra (SET NX). Si en un mismo turno el modelo propone dos acciones, la segunda no se
guarda: queda anotada en el `deferred` de la primera para avisar al usuario. Así la
pregunta que ve el usuario y la acción que ejecuta su "sí" son siempre la misma.

Si Redis cae, se degrada con gracia (NO revienta). Y para una acción DESTRUCTIVA la
degradación es FAIL-SAFE: sin intención visible, el turno de confirmación no ejecuta
nada -> ante Redis caído, NO se borra. Fallar hacia no-actuar es lo correcto aquí.
"""
import json
import logging
from contextlib import asynccontextmanager

import redis.asyncio as redis
from redis.exceptions import RedisError

from app.core.config import settings

logger = logging.getLogger("nexa.pending")

_TTL = 600  # 10 min: ventana para confirmar; luego caduca sola


@asynccontextmanager
async def _redis():
    """Cliente Redis EFÍMERO por llamada, atado al loop activo.

    pending se invoca desde DOS loops: el principal de FastAPI (get/clear en el
    orquestador) y un loop nuevo en un ThreadPoolExecutor (set_pending desde la
    tool, vía _run_async). Un cliente async Redis NO cruza loops ('Future attached
    to a different loop'). Crear+cerrar por llamada lo evita — el mismo patrón que
    worker_db.py usa con el engine NullPool para SQLAlchemy.
    """
    client = redis.from_url(settings.redis_url, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


def _key(conversation_id: int) -> str:
    return f"nexa:pending:{conversation_id}"


async def set_pending(conversation_id: int, intent: dict) -> dict | None:
    """Guarda `intent` SOLO si no hay otra pendiente. Devuelve None si quedó guardada
    (o si era la misma propuesta repetida). Si ya había OTRA, no la toca: anota la
    descripción de la nueva en su `deferred` y devuelve la existente."""
    key = _key(conversation_id)
    try:
        async with _redis() as client:
            if await client.set(key, json.dumps(intent), ex=_TTL, nx=True):
                return None
            raw = await client.get(key)
            if raw is None:
                # Caducó justo entre el SET y el GET: no queda nada guardado, igual
                # que con Redis caído (fail-safe: un "sí" no hallará nada que ejecutar).
                return None
            existing = json.loads(raw)
            if (existing.get("action"), existing.get("args")) == (
                intent.get("action"), intent.get("args")
            ):
                return None   # la misma propuesta repetida: ya está pendiente
            deferred = existing.setdefault("deferred", [])
            description = intent.get("description", "otra acción")
            if description not in deferred:
                deferred.append(description)
                await client.set(key, json.dumps(existing), keepttl=True, xx=True)
    except RedisError as exc:
        # Fail-safe: sin intención guardada, el siguiente "sí" se trata como
        # mensaje normal -> NO se borra. Para lo destructivo, eso es lo correcto.
        logger.warning(
            "redis unavailable on set_pending, intent NOT stored (fail-safe: no delete)",
            extra={"event": "redis_degraded", "op": "set_pending", "exc_type": type(exc).__name__},
        )
        return None
    logger.info(
        "proposal deferred: another action is already pending",
        extra={"action": intent.get("action"), "pending_action": existing.get("action")},
    )
    return existing


def busy_message(existing: dict) -> str:
    """Lo que la tool le responde al MODELO cuando su propuesta no se guardó porque
    ya hay otra pendiente. Al usuario se lo cuenta el orquestador, con `deferred`."""
    return (
        "Esta acción NO quedó propuesta: ya hay otra esperando la confirmación del "
        f"usuario ({existing.get('description', 'otra acción')}). No la vuelvas a "
        "proponer en este turno; el usuario podrá pedirla cuando confirme o cancele "
        "la que está pendiente."
    )


async def get_pending(conversation_id: int) -> dict | None:
    try:
        async with _redis() as client:
            raw = await client.get(_key(conversation_id))
        return json.loads(raw) if raw else None
    except RedisError as exc:
        # Degrada a "no hay pendiente" -> flujo normal -> no ejecuta acción guardada.
        logger.warning(
            "redis unavailable on get_pending, degrading to no-pending",
            extra={"event": "redis_degraded", "op": "get_pending", "exc_type": type(exc).__name__},
        )
        return None


async def clear_pending(conversation_id: int) -> None:
    try:
        async with _redis() as client:
            await client.delete(_key(conversation_id))
    except RedisError as exc:
        # No relanza: si no se borra la clave, el TTL la limpia; y reintentar
        # perform_delete sobre algo ya borrado es no-op benigno.
        logger.warning(
            "redis unavailable on clear_pending, will expire via TTL",
            extra={"event": "redis_degraded", "op": "clear_pending", "exc_type": type(exc).__name__},
        )
