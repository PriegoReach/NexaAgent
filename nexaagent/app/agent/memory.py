"""Memoria de corto plazo: los últimos mensajes de la conversación que ve el modelo.

Redis es la copia rápida (últimos _MAX_TURNS, TTL de 24 h) y Postgres (tabla
messages) la fuente durable. Si Redis no tiene la conversación (caducó, se reinició
o está caído), el historial se reconstruye desde Postgres; sin eso, una conversación
retomada otro día llegaba al modelo sin contexto aunque la UI la mostrara entera.
"""
import json
import logging

import redis.asyncio as redis
from redis.exceptions import RedisError
from sqlalchemy import text

from app.core.config import settings
from app.db.session import SessionLocal

logger = logging.getLogger("nexa.memory")

_client: redis.Redis | None = None
_MAX_TURNS = 20  # how many recent messages to keep in short-term memory
_TTL_SECONDS = 60 * 60 * 24


def _redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(settings.redis_url, decode_responses=True)
    return _client


def _key(conversation_id: int) -> str:
    return f"nexa:memory:{conversation_id}"


async def ping() -> None:
    """Readiness: lanza si Redis no responde. Reusa el cliente/pool ya abierto
    por memory.py (un solo pool por proceso), para que la sonda de /health/ready
    no abra una conexión nueva en cada chequeo."""
    await _redis().ping()


async def _load_from_db(conversation_id: int) -> list[dict]:
    """Los últimos _MAX_TURNS mensajes de la conversación, en orden cronológico."""
    async with SessionLocal() as session:
        rows = await session.execute(
            text(
                "SELECT role, content FROM messages "
                "WHERE conversation_id = :cid AND role IN ('user', 'assistant') "
                "ORDER BY id DESC LIMIT :n"
            ),
            {"cid": conversation_id, "n": _MAX_TURNS},
        )
        return [{"role": r.role, "content": r.content} for r in reversed(rows.fetchall())]


async def load_history(conversation_id: int) -> list[dict]:
    key = _key(conversation_id)
    try:
        raw = await _redis().lrange(key, 0, -1)
    except RedisError as exc:
        # Degradación: sin Redis, el historial sale de Postgres (más lento, pero completo).
        logger.warning(
            "redis unavailable on load, reading history from postgres",
            extra={
                "event": "redis_degraded",
                "op": "load_history",
                "exc_type": type(exc).__name__,
            },
        )
        return await _load_from_db(conversation_id)
    if raw:
        return [json.loads(item) for item in raw]

    # Sin copia en Redis: conversación nueva, o una que caducó (TTL de 24 h). Se
    # reconstruye desde Postgres y se vuelve a guardar en Redis; si no, el append de
    # este turno crearía la lista solo con él y el siguiente perdería el resto.
    history = await _load_from_db(conversation_id)
    if history:
        try:
            client = _redis()
            await client.rpush(key, *(json.dumps(m) for m in history))
            await client.ltrim(key, -_MAX_TURNS, -1)
            await client.expire(key, _TTL_SECONDS)
        except RedisError as exc:
            logger.warning(
                "redis unavailable on warm-up, history served from postgres only",
                extra={"event": "redis_degraded", "op": "warm_history",
                       "exc_type": type(exc).__name__},
            )
        logger.info("short-term history rebuilt from postgres",
                    extra={"event": "history_rebuilt", "n_messages": len(history)})
    return history


async def append(conversation_id: int, role: str, content: str) -> None:
    key = _key(conversation_id)
    try:
        client = _redis()
        await client.rpush(key, json.dumps({"role": role, "content": content}))
        await client.ltrim(key, -_MAX_TURNS, -1)
        await client.expire(key, _TTL_SECONDS)
    except RedisError as exc:
        # No relanza: Postgres ya tiene el mensaje durable. Solo se pierde la copia
        # rápida en Redis de este turno.
        logger.warning(
            "redis unavailable on append, skipping short-term cache",
            extra={
                "event": "redis_degraded",
                "op": "append",
                "exc_type": type(exc).__name__,
            },
        )
