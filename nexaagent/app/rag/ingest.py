import logging
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader
from sqlalchemy import text

from app.db.worker_db import worker_session
from app.rag.embeddings import get_embeddings

logger = logging.getLogger("nexa.ingest")

# Lo que _read_file sabe leer: PDF (con texto, o escaneado vía OCR), Word (.docx),
# imágenes (OCR) y texto plano. /documents/upload rechaza el resto; la UI usa la
# misma lista.
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg"})
SUPPORTED_SUFFIXES = frozenset({
    ".pdf", ".docx", ".txt", ".text", ".md", ".markdown", ".rst",
    ".csv", ".tsv", ".json", ".yaml", ".yml", ".log",
}) | _IMAGE_SUFFIXES

# Un PDF con menos texto que esto se trata como escaneado y se intenta con OCR.
_MIN_PDF_TEXT = 20
_OCR_LANGS = "spa+eng"
_OCR_MAX_PAGES = 30          # cota: el OCR tarda unos segundos por página
_OCR_TIMEOUT = 120           # segundos por imagen
_DOCX_MAX_XML = 50 * 1024 * 1024   # un document.xml mayor huele a zip bomb


def _read_file(path: str) -> str:
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        reader = PdfReader(str(p))
        content = "\n".join(page.extract_text() or "" for page in reader.pages)
        if len(content.strip()) < _MIN_PDF_TEXT:
            content = _ocr_pdf(p) or content   # escaneado: las páginas son imágenes
        return content
    if suffix == ".docx":
        return _read_docx(p)
    if suffix in _IMAGE_SUFFIXES:
        return _ocr_image(p)
    return p.read_text(encoding="utf-8", errors="ignore")


# --- Word (.docx) ----------------------------------------------------------
# Sin dependencias: un .docx es un zip con el cuerpo en word/document.xml.
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    out: list[str] = []

    def walk(node: ElementTree.Element) -> None:
        for child in node:
            if child.tag == f"{_W}p":
                continue   # párrafo anidado (cuadro de texto): sale por su cuenta
            if child.tag == f"{_W}t" and child.text:
                out.append(child.text)
            elif child.tag == f"{_W}tab":
                out.append("\t")
            elif child.tag in (f"{_W}br", f"{_W}cr"):
                out.append("\n")
            walk(child)

    walk(paragraph)
    return "".join(out)


def _read_docx(path: Path) -> str:
    """Un párrafo (w:p) por línea, también los de las celdas de las tablas."""
    with zipfile.ZipFile(path) as docx:
        if docx.getinfo("word/document.xml").file_size > _DOCX_MAX_XML:
            raise ValueError("word/document.xml demasiado grande")
        root = ElementTree.fromstring(docx.read("word/document.xml"))
    return "\n".join(_paragraph_text(p) for p in root.iter(f"{_W}p"))


# --- OCR ---------------------------------------------------------------------
# tesseract y pdftoppm vienen en la imagen de Docker. Sin ellos (p. ej. fuera de
# Docker) el OCR no se intenta y el documento termina como 'empty' ("Sin texto").

def _ocr_image(path: Path) -> str:
    if shutil.which("tesseract") is None:
        logger.warning("ocr not available (tesseract missing)", extra={"file": path.name})
        return ""
    result = subprocess.run(
        ["tesseract", str(path), "-", "-l", _OCR_LANGS],
        capture_output=True, text=True, timeout=_OCR_TIMEOUT,
    )
    if result.returncode != 0:
        logger.warning("ocr failed", extra={"file": path.name, "stderr": result.stderr[-300:]})
        return ""
    return result.stdout


def _ocr_pdf(path: Path) -> str:
    """PDF escaneado: cada página a imagen (pdftoppm) y luego OCR (tesseract)."""
    if shutil.which("pdftoppm") is None or shutil.which("tesseract") is None:
        logger.warning("ocr not available (pdftoppm/tesseract missing)", extra={"file": path.name})
        return ""
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            ["pdftoppm", "-r", "200", "-l", str(_OCR_MAX_PAGES), "-png", str(path), f"{tmp}/page"],
            check=True, capture_output=True, timeout=_OCR_TIMEOUT * 2,
        )
        pages = sorted(Path(tmp).glob("page*.png"))   # pdftoppm numera con ceros a la izquierda
        logger.info("ocr pdf", extra={"file": path.name, "pages": len(pages)})
        return "\n\n".join(_ocr_image(page) for page in pages)


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
