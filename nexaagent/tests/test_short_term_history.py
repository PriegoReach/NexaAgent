"""Tests del historial de corto plazo (memory.load_history).

Antes, el modelo solo veía lo que hubiera en Redis, que caduca a las 24 h: una
conversación retomada otro día llegaba sin contexto aunque la UI la mostrara entera.
Ahora, si Redis no la tiene, se reconstruye desde Postgres (tabla messages) y se
vuelve a guardar en Redis; con Redis caído, también se lee de Postgres.

Redis se sustituye por un doble en memoria; los mensajes van a la BD real de test.
"""
import json

import pytest
import pytest_asyncio
from redis.exceptions import RedisError
from sqlalchemy import text

from app.agent import memory
from app.db.session import engine


class _FakeRedis:
    """Lo justo de redis.asyncio para memory.py: listas con TTL."""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.ttl: dict[str, int] = {}

    async def lrange(self, key, start, end):
        return list(self.lists.get(key, []))

    async def rpush(self, key, *values):
        self.lists.setdefault(key, []).extend(values)

    async def ltrim(self, key, start, end):
        self.lists[key] = self.lists.get(key, [])[start:] if end == -1 else self.lists[key][start:end + 1]

    async def expire(self, key, seconds):
        self.ttl[key] = seconds


class _DownRedis(_FakeRedis):
    async def lrange(self, key, start, end):
        raise RedisError("connection refused")

    async def rpush(self, key, *values):
        raise RedisError("connection refused")


@pytest.fixture
def fake_redis(monkeypatch) -> _FakeRedis:
    fake = _FakeRedis()
    monkeypatch.setattr(memory, "_redis", lambda: fake)
    return fake


@pytest_asyncio.fixture
async def conversation_id() -> int:
    async with engine.begin() as conn:
        return (
            await conn.execute(
                text("INSERT INTO conversations (title, created_at) VALUES ('t', now()) RETURNING id")
            )
        ).scalar_one()


async def _add_messages(cid: int, *messages: tuple[str, str]) -> None:
    async with engine.begin() as conn:
        for role, content in messages:
            await conn.execute(
                text(
                    "INSERT INTO messages (conversation_id, role, content, created_at) "
                    "VALUES (:cid, :role, :content, now())"
                ),
                {"cid": cid, "role": role, "content": content},
            )


async def test_expired_conversation_is_rebuilt_from_postgres(fake_redis, conversation_id):
    await _add_messages(
        conversation_id,
        ("user", "Mi gato se llama Wenceslao"),
        ("assistant", "¡Qué buen nombre!"),
        ("tool", "salida interna de una herramienta"),   # no es parte de la charla
        ("user", "¿Cómo se llama mi gato?"),
    )

    history = await memory.load_history(conversation_id)

    assert history == [
        {"role": "user", "content": "Mi gato se llama Wenceslao"},
        {"role": "assistant", "content": "¡Qué buen nombre!"},
        {"role": "user", "content": "¿Cómo se llama mi gato?"},
    ]
    key = memory._key(conversation_id)
    assert [json.loads(m) for m in fake_redis.lists[key]] == history   # vuelve a Redis
    assert fake_redis.ttl[key] == 60 * 60 * 24


async def test_next_turn_keeps_the_rebuilt_context(fake_redis, conversation_id):
    """Sin volver a guardar en Redis, el append del turno crearía la lista solo con
    él y el turno siguiente volvería a perder la conversación."""
    await _add_messages(conversation_id, ("user", "hola"), ("assistant", "¡Hola!"))

    await memory.load_history(conversation_id)
    await memory.append(conversation_id, "user", "¿qué te dije?")
    await memory.append(conversation_id, "assistant", "Que hola.")

    history = await memory.load_history(conversation_id)
    assert [m["content"] for m in history] == ["hola", "¡Hola!", "¿qué te dije?", "Que hola."]


async def test_only_the_last_messages_are_loaded(fake_redis, conversation_id):
    await _add_messages(conversation_id, *[("user", f"mensaje {i}") for i in range(25)])

    history = await memory.load_history(conversation_id)

    assert [m["content"] for m in history] == [f"mensaje {i}" for i in range(5, 25)]


async def test_redis_copy_wins_when_present(fake_redis, conversation_id):
    await _add_messages(conversation_id, ("user", "en postgres"))
    fake_redis.lists[memory._key(conversation_id)] = [
        json.dumps({"role": "user", "content": "en redis"})
    ]

    assert await memory.load_history(conversation_id) == [{"role": "user", "content": "en redis"}]


async def test_new_conversation_is_empty_and_not_cached(fake_redis, conversation_id):
    assert await memory.load_history(conversation_id) == []
    assert memory._key(conversation_id) not in fake_redis.lists


async def test_redis_down_reads_postgres(monkeypatch, conversation_id):
    monkeypatch.setattr(memory, "_redis", lambda: _DownRedis())
    await _add_messages(conversation_id, ("user", "hola"), ("assistant", "¡Hola!"))

    history = await memory.load_history(conversation_id)

    assert [m["content"] for m in history] == ["hola", "¡Hola!"]
