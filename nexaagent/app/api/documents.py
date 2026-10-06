import os
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import text

from app.core.log_context import request_id_var
from app.core.security import require_jwt
from app.db.models import Document
from app.db.session import SessionLocal
from app.rag.ingest import SUPPORTED_SUFFIXES
from app.schemas.chat import DocumentItem, DocumentResponse
from app.workers.tasks import ingest_document_task

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

router = APIRouter(
    prefix="/documents", tags=["documents"], dependencies=[Depends(require_jwt)]
)


@router.post("/upload", response_model=DocumentResponse)
async def upload_document(file: UploadFile = File(...)) -> DocumentResponse:
    # Antes de crear nada: un tipo que el parser no sabe leer (p. ej. .docx) se
    # indexaría como texto basura.
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"No puedo indexar archivos {suffix or 'sin extensión'}. "
                "Sube un PDF o un archivo de texto (.txt, .md, .csv, .json…)."
            ),
        )

    async with SessionLocal() as session:
        doc = Document(filename=file.filename or "untitled", status="pending")
        session.add(doc)
        await session.commit()
        await session.refresh(doc)
        doc_id = doc.id

    dest = UPLOAD_DIR / f"{doc_id}_{os.path.basename(file.filename or 'file')}"
    dest.write_bytes(await file.read())

    # Hand off heavy work (parse + embed) to the Celery worker.
    # Pasamos el request_id: el contextvar no cruza procesos, la tarea lo refija.
    ingest_document_task.delay(doc_id, str(dest), request_id=request_id_var.get())

    return DocumentResponse(document_id=doc_id, filename=doc.filename, status="pending")


@router.get("", response_model=list[DocumentItem])
async def list_documents(
    limit: int = Query(default=100, ge=1, le=200),
) -> list[DocumentItem]:
    """Lista los documentos del knowledge base (subidos a mano o traídos de Drive),
    más recientes primero. Expone `status` (pending|ready|empty|failed) para que la UI
    muestre el progreso de la ingesta, que corre en segundo plano en el worker."""
    async with SessionLocal() as session:
        rows = await session.execute(
            text(
                "SELECT id, filename, status, created_at FROM documents "
                "ORDER BY id DESC LIMIT :limit"
            ),
            {"limit": limit},
        )
        return [
            DocumentItem(
                id=r.id, filename=r.filename, status=r.status, created_at=r.created_at
            )
            for r in rows
        ]


class DocumentDeleteResponse(BaseModel):
    deleted: bool
    document_id: int


@router.delete("/{document_id}", response_model=DocumentDeleteResponse)
async def delete_document(document_id: int) -> DocumentDeleteResponse:
    """Quita un documento del knowledge base. El DELETE de la fila arrastra sus
    chunks (FK ON DELETE CASCADE en document_chunks), así que sale también del RAG.
    Borra además, best-effort, el archivo subido (uploads/{id}_*). 404 si no existe."""
    async with SessionLocal() as session:
        exists = await session.execute(
            text("SELECT 1 FROM documents WHERE id = :id"), {"id": document_id}
        )
        if exists.first() is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Document {document_id} not found",
            )
        await session.execute(
            text("DELETE FROM documents WHERE id = :id"), {"id": document_id}
        )
        await session.commit()

    # Best-effort: limpiar el archivo en disco (los de Drive no tienen, no pasa nada).
    for p in UPLOAD_DIR.glob(f"{document_id}_*"):
        try:
            p.unlink()
        except OSError:
            pass

    return DocumentDeleteResponse(deleted=True, document_id=document_id)
