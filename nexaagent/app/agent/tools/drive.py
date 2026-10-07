"""Tools de Google Drive: LISTAR archivos e INGESTARLOS al RAG.

Drive aquí es READ-ONLY y reversible (solo lee/descarga para indexar) -> a
diferencia de Calendar/Gmail, NO necesita confirmación. Es la pieza que CONECTA el
OAuth de Google con el pipeline RAG: traer un archivo de Drive y meterlo por el
mismo ingest_document que ya usa /documents/upload.

PASOS SEPARABLES (no es magia de un paso): list_drive_files busca, ingest_drive_file
trae+indexa, y preguntar es RAG normal (search_knowledge_base) sobre lo ya ingestado.

Dos caminos de ingesta según el mimeType:
  - BINARIO (PDF, Word .docx, imágenes PNG/JPEG, text/*): files.get?alt=media ->
    bytes tal cual; ingest lee el .docx y pasa OCR a imágenes y PDF escaneados.
  - GOOGLE DOC NATIVO (vnd.google-apps.document): files.export?mimeType=text/plain
    -> Google lo convierte a texto (no se puede descargar 'tal cual', es nativo).
  - Sheets/Slides/otros: NO soportados. Mensaje claro, nunca excepción.

El puente al pipeline: ingest_document recibe un PATH y decide por el sufijo
(ver ingest._read_file), así que escribimos lo traído a un archivo
temporal con la extensión correcta y lo borramos al terminar.

httpx directo con timeout, sin google-api-python-client. Cross-loop: todo el I/O
de BD ocurre vía worker_session (engine NullPool atado al loop efímero del
ThreadPoolExecutor de la tool).
"""
import asyncio
import logging
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

import httpx
from langchain_core.tools import tool
from sqlalchemy import text

from app.db.models import Document
from app.db.worker_db import worker_session
from app.integrations.google_oauth import GoogleTokenUnavailable, get_valid_token
from app.rag.ingest import ingest_document

logger = logging.getLogger("nexa.tools")

_DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
_FOLDER_MIME = "application/vnd.google-apps.folder"
_GDOC_MIME = "application/vnd.google-apps.document"
# Descargar puede traer ficheros más grandes que una llamada de metadatos -> margen
# mayor que el _HTTP_TIMEOUT=15 estándar del proyecto.
_HTTP_TIMEOUT = 30

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
# Binarios que se descargan tal cual -> sufijo con el que ingest los reconoce.
_DOWNLOAD_SUFFIX = {
    "application/pdf": ".pdf",
    _DOCX_MIME: ".docx",
    "image/png": ".png",
    "image/jpeg": ".jpg",
}

# mimeType -> etiqueta legible (para que el usuario/agente sepa qué es cada archivo).
_FRIENDLY = {
    "application/pdf": "PDF",
    _DOCX_MIME: "Word (.docx)",
    _GDOC_MIME: "Google Doc",
    "application/vnd.google-apps.spreadsheet": "Google Sheet (no soportado)",
    "application/vnd.google-apps.presentation": "Google Slides (no soportado)",
    "text/plain": "texto",
}


def _run_async(coro):
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _friendly_type(mime: str) -> str:
    if mime in _FRIENDLY:
        return _FRIENDLY[mime]
    if mime.startswith("text/"):
        return "texto"
    if mime.startswith("image/"):
        return "imagen" if mime in _DOWNLOAD_SUFFIX else "imagen (no soportada)"
    return mime


# ==================== LISTAR ====================
async def _list_files(query: str) -> str:
    try:
        token = await get_valid_token()
    except GoogleTokenUnavailable as exc:
        return str(exc)

    # Drive exige escapar la comilla simple dentro del valor de q con barra invertida.
    safe = query.replace("\\", "\\\\").replace("'", "\\'")
    q = (f"name contains '{safe}' and mimeType != '{_FOLDER_MIME}' "
         "and trashed = false")
    params = {
        "q": q,
        "fields": "files(id,name,mimeType)",
        "pageSize": 20,
        "spaces": "drive",
    }
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(_DRIVE_FILES_URL, params=params, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.warning("drive list failed", extra={"status": exc.response.status_code})
        if exc.response.status_code in (401, 403):
            return ("Google rechazó el acceso a Drive (token o permisos). "
                    "Reconecta la cuenta.")
        return "No pude buscar en Drive ahora mismo (error de Google)."
    except httpx.HTTPError as exc:
        logger.warning("drive list http error", extra={"exc": str(exc)})
        return "No pude contactar a Google Drive (problema de red)."

    files = resp.json().get("files", [])
    logger.info("list_drive_files", extra={"query": query, "n": len(files)})
    if not files:
        return f"No encontré archivos en tu Drive que coincidan con '{query}'."
    lines = [
        f"• {f['name']} — {_friendly_type(f.get('mimeType', ''))} (id: {f['id']})"
        for f in files
    ]
    return (f"Encontré estos archivos en tu Drive para '{query}':\n"
            + "\n".join(lines)
            + "\n\nDime cuál quieres que indexe (por nombre o id) y lo traigo al RAG.")


@tool
def list_drive_files(query: str) -> str:
    """Search the user's Google Drive for files by name (read-only).
    Use this when the user wants to find or look up documents in their Drive
    (e.g. "busca en mi Drive documentos sobre ventas", "qué archivos tengo de X").
    Returns each file's name, type and id. The id is needed to ingest it later.

    Args:
        query: Text to search for in file names.
    """
    return _run_async(_list_files(query))


# ==================== INGESTAR (puente Drive -> RAG) ====================
async def _fetch_content(client: httpx.AsyncClient, file_id: str, mime: str,
                         headers: dict) -> tuple[bytes, str] | str:
    """Trae el contenido del archivo. Devuelve (bytes, sufijo) o un string de error.
    Dos caminos: export (Google Doc nativo) vs download alt=media (binario/texto)."""
    if mime == _GDOC_MIME:
        # Camino EXPORT: un Google Doc nativo no se descarga 'tal cual'; Google lo
        # convierte. Exportamos a text/plain (ingest lee texto directo, sin reparseo).
        resp = await client.get(
            f"{_DRIVE_FILES_URL}/{file_id}/export",
            params={"mimeType": "text/plain"},
            headers=headers,
        )
        resp.raise_for_status()
        return resp.content, ".txt"
    if mime in _DOWNLOAD_SUFFIX:
        resp = await client.get(
            f"{_DRIVE_FILES_URL}/{file_id}", params={"alt": "media"}, headers=headers
        )
        resp.raise_for_status()
        return resp.content, _DOWNLOAD_SUFFIX[mime]
    if mime.startswith("text/"):
        resp = await client.get(
            f"{_DRIVE_FILES_URL}/{file_id}", params={"alt": "media"}, headers=headers
        )
        resp.raise_for_status()
        return resp.content, ".txt"
    # Sheets, Slides, Excel, PowerPoint, etc.: no soportados.
    return (f"El archivo es de tipo '{_friendly_type(mime)}' y aún no puedo "
            "ingestarlo. Por ahora soporto PDF, Word (.docx), imágenes PNG/JPEG, "
            "texto y Google Docs.")


async def _get_or_create_document(drive_file_id: str, name: str) -> tuple[int, bool]:
    """Reusa el document_id si este drive_file_id ya se ingestó (idempotencia de
    re-ingesta: ingest_document borrará y reemplazará sus chunks). Si no, crea la
    fila vía ORM (aplica el default created_at)."""
    async with worker_session() as session:
        existing = await session.execute(
            text("SELECT id FROM documents WHERE drive_file_id = :fid"),
            {"fid": drive_file_id},
        )
        row = existing.first()
        if row is not None:
            return row[0], False
        doc = Document(filename=name, status="pending", drive_file_id=drive_file_id)
        session.add(doc)
        await session.commit()
        await session.refresh(doc)
        return doc.id, True


async def _mark_failed(document_id: int) -> None:
    try:
        async with worker_session() as session:
            await session.execute(
                text("UPDATE documents SET status = 'failed' WHERE id = :id"),
                {"id": document_id},
            )
            await session.commit()
    except Exception:
        logger.exception("could not mark drive document failed",
                         extra={"document_id": document_id})


async def _ingest_file(file_id: str) -> str:
    try:
        token = await get_valid_token()
    except GoogleTokenUnavailable as exc:
        return str(exc)
    headers = {"Authorization": f"Bearer {token}"}

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            # 1) metadatos -> nombre + mimeType (decide el camino de descarga).
            meta = await client.get(
                f"{_DRIVE_FILES_URL}/{file_id}",
                params={"fields": "id,name,mimeType"},
                headers=headers,
            )
            meta.raise_for_status()
            info = meta.json()
            name = info.get("name", file_id)
            mime = info.get("mimeType", "")

            # 2) contenido según el tipo.
            fetched = await _fetch_content(client, file_id, mime, headers)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        logger.warning("drive ingest fetch failed",
                       extra={"status": code, "file_id": file_id})
        if code in (401, 403):
            return "Google rechazó el acceso a Drive (token o permisos). Reconecta la cuenta."
        if code == 404:
            return f"No encontré en tu Drive el archivo con id '{file_id}'."
        return "No pude traer el archivo de Drive ahora mismo (error de Google)."
    except httpx.HTTPError as exc:
        logger.warning("drive ingest http error", extra={"file_id": file_id, "exc": str(exc)})
        return "No pude contactar a Google Drive (problema de red)."

    if isinstance(fetched, str):
        return fetched  # tipo no soportado: mensaje claro, no se ingesta nada.
    content, suffix = fetched

    # 3) PUENTE al pipeline: escribir a archivo temporal con la extensión correcta
    # (ingest._read_file decide por el sufijo) y borrarlo al terminar.
    tmp_path = os.path.join(tempfile.gettempdir(), f"drive_{file_id}{suffix}")
    document_id: int | None = None
    try:
        with open(tmp_path, "wb") as fh:
            fh.write(content)
        document_id, created = await _get_or_create_document(file_id, name)
        n_chunks = await ingest_document(document_id, tmp_path)
    except Exception as exc:  # el pipeline no debe propagar al agente como 500.
        logger.exception("drive ingest pipeline error", extra={"file_id": file_id})
        if document_id is not None:
            # Igual que el on_failure de la tarea Celery: 'failed', no 'pending' eterno.
            await _mark_failed(document_id)
        return f"No pude indexar '{name}': {type(exc).__name__}."
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    # OJO: 'created' es un atributo RESERVADO de logging.LogRecord -> usarlo en
    # extra revienta con KeyError. La clave del flag va como 'created_doc'.
    logger.info("ingest_drive_file done",
                extra={"file_id": file_id, "document_id": document_id,
                       "n_chunks": n_chunks, "created_doc": created, "mime": mime})
    if n_chunks == 0:
        return (f"Traje '{name}' de Drive pero no extraje texto indexable "
                "(¿documento vacío o sin texto?).")
    reuse = "" if created else " (actualicé el ya existente, sin duplicar)"
    return (f"Indexé el documento '{name}' ({n_chunks} fragmentos){reuse}. "
            "Ya puedes preguntarme sobre él.")


@tool
def ingest_drive_file(file_id: str) -> str:
    """Bring a file from the user's Google Drive into the knowledge base (RAG) so
    it can be searched and asked about. Use this when the user wants to ingest,
    index or import a specific Drive document (use the id from list_drive_files).
    Supports PDF (also scanned, via OCR), Word (.docx), PNG/JPEG images (OCR),
    plain text and native Google Docs. After ingesting, the user can
    ask about its content normally (it goes through search_knowledge_base).

    Args:
        file_id: The Google Drive file id (from list_drive_files).
    """
    return _run_async(_ingest_file(file_id))
