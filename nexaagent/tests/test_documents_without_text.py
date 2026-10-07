"""Tests de los documentos que no se pueden indexar.

Antes, un documento sin texto extraíble (un PDF escaneado, un archivo vacío) se
quedaba en 'pending' para siempre: la UI lo mostraba «Procesando…» sin fin. Lo
mismo pasaba con un fallo al ingerir desde Drive. Y /documents/upload aceptaba
cualquier tipo, así que un .xlsx se indexaba como texto basura. Ahora:
  - sin texto -> 'empty', y se borran los chunks de una ingesta anterior;
  - un fallo al ingerir desde Drive -> 'failed';
  - un tipo que el parser no sabe leer -> 415, sin crear nada.

Sin Ollama: los documentos sin texto no llegan a calcular embeddings. La BD es la
real de test.
"""
import httpx
from sqlalchemy import text

from app.agent.tools import drive
from app.db.session import engine
from app.rag.ingest import ingest_document

_ZERO_VEC = "[" + ",".join(["0"] * 768) + "]"


async def _new_document(filename: str, status: str = "pending", drive_file_id: str | None = None) -> int:
    async with engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO documents (filename, status, created_at, drive_file_id) "
                    "VALUES (:f, :s, now(), :d) RETURNING id"
                ),
                {"f": filename, "s": status, "d": drive_file_id},
            )
        ).scalar_one()


async def _status_and_chunks(document_id: int) -> tuple[str, int]:
    async with engine.begin() as conn:
        status = (
            await conn.execute(text("SELECT status FROM documents WHERE id = :id"), {"id": document_id})
        ).scalar_one()
        chunks = (
            await conn.execute(
                text("SELECT count(*) FROM document_chunks WHERE document_id = :id"),
                {"id": document_id},
            )
        ).scalar_one()
    return status, chunks


async def test_document_without_text_ends_empty(tmp_path):
    path = tmp_path / "vacio.txt"
    path.write_text("   \n\n  \n", encoding="utf-8")
    document_id = await _new_document("vacio.txt")

    assert await ingest_document(document_id, str(path)) == 0
    assert await _status_and_chunks(document_id) == ("empty", 0)


async def test_reingesting_without_text_drops_stale_chunks(tmp_path):
    """Un documento que tenía texto y ahora no: sus chunks viejos ya no lo representan."""
    document_id = await _new_document("notas.txt", status="ready")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO document_chunks (document_id, content, embedding) "
                "VALUES (:id, 'texto viejo', CAST(:v AS vector))"
            ),
            {"id": document_id, "v": _ZERO_VEC},
        )
    path = tmp_path / "notas.txt"
    path.write_text("", encoding="utf-8")

    await ingest_document(document_id, str(path))

    assert await _status_and_chunks(document_id) == ("empty", 0)


async def test_upload_rejects_types_the_parser_cannot_read(client, auth_headers):
    resp = await client.post(
        "/documents/upload",
        files={"file": ("ventas.xlsx", b"PK\x03\x04 binario", "application/octet-stream")},
        headers=auth_headers,
    )

    assert resp.status_code == 415
    assert ".xlsx" in resp.json()["detail"]
    async with engine.begin() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM documents"))).scalar_one() == 0


async def test_upload_rejects_files_without_extension(client, auth_headers):
    resp = await client.post(
        "/documents/upload",
        files={"file": ("LEEME", b"hola", "text/plain")},
        headers=auth_headers,
    )

    assert resp.status_code == 415
    assert "sin extensión" in resp.json()["detail"]


def _fake_drive(monkeypatch, content: bytes) -> None:
    """Drive falso: metadatos de un .txt y su contenido; token de Google falso."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("alt") == "media":
            return httpx.Response(200, content=content)
        return httpx.Response(200, json={"id": "f1", "name": "notas.txt", "mimeType": "text/plain"})

    real_async_client = httpx.AsyncClient

    def fake_async_client(**kwargs):
        return real_async_client(transport=httpx.MockTransport(handler), **kwargs)

    async def fake_token() -> str:
        return "fake-token"

    monkeypatch.setattr(drive.httpx, "AsyncClient", fake_async_client)
    monkeypatch.setattr(drive, "get_valid_token", fake_token)


async def _drive_document_status() -> str:
    async with engine.begin() as conn:
        return (
            await conn.execute(text("SELECT status FROM documents WHERE drive_file_id = 'f1'"))
        ).scalar_one()


async def test_drive_file_without_text_ends_empty(monkeypatch):
    _fake_drive(monkeypatch, b"")

    reply = await drive._ingest_file("f1")

    assert "no extraje texto indexable" in reply
    assert await _drive_document_status() == "empty"


async def test_drive_ingest_failure_marks_document_failed(monkeypatch):
    _fake_drive(monkeypatch, b"hola")

    async def broken_ingest(document_id: int, path: str) -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(drive, "ingest_document", broken_ingest)

    reply = await drive._ingest_file("f1")

    assert reply == "No pude indexar 'notas.txt': RuntimeError."
    assert await _drive_document_status() == "failed"
