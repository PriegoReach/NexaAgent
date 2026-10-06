from pathlib import Path

from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader
from sqlalchemy import text

from app.db.worker_db import worker_session
from app.rag.embeddings import get_embeddings


# Lo que _read_file sabe leer: PDF (con PdfReader) y texto plano (todo lo demás se lee
# como texto, así que un binario como .docx daría basura). /documents/upload rechaza
# el resto; la UI usa la misma lista.
SUPPORTED_SUFFIXES = frozenset({
    ".pdf", ".txt", ".text", ".md", ".markdown", ".rst",
    ".csv", ".tsv", ".json", ".yaml", ".yml", ".log",
})


def _read_file(path: str) -> str:
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        reader = PdfReader(str(p))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    return p.read_text(encoding="utf-8", errors="ignore")


# --- Chunking estructural -------------------------------------

def _is_header(lines: list[str], i: int) -> bool:
    line = lines[i].strip()
    if not line or len(line) > 40:
        return False
    if line.endswith((".", ",", ":", ";")):
        return False
    if len(line.split()) > 5:
        return False
    for j in range(i + 1, min(i + 3, len(lines))):
        if lines[j].strip():
            return len(lines[j].strip()) > 80
    return False


def _detect_doc_title(lines: list[str]) -> str:
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if len(s) <= 80 and not s.endswith((".", "?", "!")):
            return s
        return ""
    return ""


def _split_into_sections(raw: str) -> tuple[str, list[tuple[str | None, str]]]:
    lines = raw.split("\n")
    doc_title = _detect_doc_title(lines)
    sections: list[tuple[str | None, str]] = []
    current_title: str | None = None
    current_body: list[str] = []
    for i, line in enumerate(lines):
        if _is_header(lines, i):
            if current_body:
                sections.append((current_title, "\n".join(current_body)))
            current_title = line.strip()
            current_body = []
        else:
            current_body.append(line)
    if current_body:
        sections.append((current_title, "\n".join(current_body)))
    return doc_title, sections


def _enrich_chunks(raw: str, filename: str) -> list[str]:
    doc_title, sections = _split_into_sections(raw)
    if doc_title and "—" in doc_title:
        title_part = doc_title.split("—")[-1].strip()
    else:
        title_part = doc_title or Path(filename).stem

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=800,
        chunk_overlap=150,
        separators=["\n\n", "\n", ". ", " ", ""],
    )

    out: list[str] = []
    for section_title, body in sections:
        if not body.strip():
            continue
        if section_title and section_title != doc_title:
            prefix = f"[{title_part} — {section_title}]"
        else:
            prefix = f"[{title_part}]"
        for piece in splitter.split_text(body):
            if piece.strip():
                out.append(f"{prefix}\n{piece}")
    return out


# -------------------------------------------------------------------------


async def ingest_document(document_id: int, file_path: str) -> int:
    """Parse -> chunk (estructural + contexto) -> embed -> store.

    Usa worker_session(): engine NullPool propio, atado al loop actual de la tarea Celery.

    Sin texto extraíble (un PDF escaneado, un archivo vacío) el documento termina en
    'empty' en vez de quedarse 'pending' para siempre, y pierde los chunks que tuviera
    de una ingesta anterior: ya no corresponden a su contenido.
    """
    raw = _read_file(file_path)
    chunks = _enrich_chunks(raw, file_path)
    if not chunks:
        async with worker_session() as session:
            await session.execute(
                text("DELETE FROM document_chunks WHERE document_id = :doc_id"),
                {"doc_id": document_id},
            )
            await session.execute(
                text("UPDATE documents SET status = 'empty' WHERE id = :id"),
                {"id": document_id},
            )
            await session.commit()
        return 0

    vectors = get_embeddings().embed_documents(chunks)

    async with worker_session() as session:
        # Idempotencia: limpiar cualquier chunk previo de ESTE documento antes de
        # insertar. Correr la tarea N veces == correrla 1 vez. Si la transacción
        # aborta, ni se borró ni se insertó: el doc queda como estaba.
        await session.execute(
            text("DELETE FROM document_chunks WHERE document_id = :doc_id"),
            {"doc_id": document_id},
        )
        for chunk, vec in zip(chunks, vectors, strict=True):
            await session.execute(
                text(
                    "INSERT INTO document_chunks (document_id, content, embedding) "
                    "VALUES (:doc_id, :content, :embedding)"
                ),
                {"doc_id": document_id, "content": chunk, "embedding": str(vec)},
            )
        await session.execute(
            text("UPDATE documents SET status = 'ready' WHERE id = :id"),
            {"id": document_id},
        )
        await session.commit()  # borrado + inserción + estado: atómico
    return len(chunks)
