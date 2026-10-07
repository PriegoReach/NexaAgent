"""Tests del panel de memoria (/memories): ver y olvidar lo que Nexa recuerda.

Los hechos de largo plazo se guardaban sin forma de verlos ni de borrarlos salvo
borrando la conversación entera. La BD es la real de test; los vectores son ceros
(no hace falta Ollama para listar o borrar).
"""
from sqlalchemy import text

from app.db.session import engine

_ZERO_VEC = "[" + ",".join(["0"] * 768) + "]"


async def _conversation_with_memories(title: str, *facts: str) -> tuple[int, list[int]]:
    async with engine.begin() as conn:
        cid = (await conn.execute(
            text("INSERT INTO conversations (title, created_at) VALUES (:t, now()) RETURNING id"),
            {"t": title},
        )).scalar_one()
        ids = []
        for fact in facts:
            ids.append((await conn.execute(
                text(
                    "INSERT INTO long_term_memories (conversation_id, content, embedding, created_at) "
                    "VALUES (:cid, :c, CAST(:e AS vector), now()) RETURNING id"
                ),
                {"cid": cid, "c": fact, "e": _ZERO_VEC},
            )).scalar_one())
    return cid, ids


async def _remaining() -> list[str]:
    async with engine.begin() as conn:
        return (await conn.execute(text("SELECT content FROM long_term_memories ORDER BY id"))).scalars().all()


async def test_lists_memories_newest_first_with_their_conversation(client, auth_headers):
    cid, _ = await _conversation_with_memories("Mi gato", "El usuario tiene un gato llamado Wenceslao.")
    await _conversation_with_memories("Trabajo", "El usuario trabaja en NexaCorp.")

    body = (await client.get("/memories", headers=auth_headers)).json()

    assert body["total"] == 2
    assert [m["content"] for m in body["items"]] == [
        "El usuario trabaja en NexaCorp.", "El usuario tiene un gato llamado Wenceslao."]
    assert (body["items"][1]["conversation_id"], body["items"][1]["conversation_title"]) == (cid, "Mi gato")


async def test_forgets_one_memory(client, auth_headers):
    _, (first, _second) = await _conversation_with_memories("Charla", "Hecho uno.", "Hecho dos.")

    resp = await client.delete(f"/memories/{first}", headers=auth_headers)

    assert resp.json() == {"deleted": 1}
    assert await _remaining() == ["Hecho dos."]


async def test_forgetting_a_missing_memory_is_404(client, auth_headers):
    assert (await client.delete("/memories/999999", headers=auth_headers)).status_code == 404


async def test_forgets_everything_but_keeps_the_conversations(client, auth_headers):
    await _conversation_with_memories("A", "Hecho uno.", "Hecho dos.")
    await _conversation_with_memories("B", "Hecho tres.")

    resp = await client.delete("/memories", headers=auth_headers)

    assert resp.json() == {"deleted": 3}
    assert await _remaining() == []
    async with engine.begin() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM conversations"))).scalar_one() == 2


async def test_memories_require_auth(client):
    assert (await client.get("/memories")).status_code == 401
    assert (await client.delete("/memories")).status_code == 401
