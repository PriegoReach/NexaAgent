"""Tests de los tipos de documento que se suman a PDF y texto, y de las citas.

- Word (.docx): se lee el XML del zip, un párrafo por línea (también los de tablas
  y cuadros de texto), sin dependencias nuevas.
- OCR de imágenes y de PDF escaneados con tesseract. Los tests que lo ejecutan de
  verdad se saltan donde no está instalado; en la imagen de Docker sí está.
- Citas: search_knowledge_base antepone "[Fuente: archivo]" a cada fragmento para
  que el agente diga de qué documento sale lo que responde.

Sin Ollama ni reranker (se sustituyen). La BD es la real de test.
"""
import importlib
import shutil
import zipfile
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.api import documents
from app.db.session import engine
from app.rag import ingest, retriever
from app.rag.ingest import _read_file, ingest_document

# El paquete app.agent.tools reexporta las tools: importar el módulo por su ruta.
knowledge_base = importlib.import_module("app.agent.tools.knowledge_base")

needs_ocr = pytest.mark.skipif(
    shutil.which("tesseract") is None or shutil.which("pdftoppm") is None,
    reason="OCR no instalado aquí (viene en la imagen de Docker)",
)

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _write_docx(path, body: str) -> None:
    """Un .docx mínimo: Word guarda el cuerpo en word/document.xml dentro de un zip."""
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W_NS}"><w:body>{body}</w:body></w:document>'
    )
    with zipfile.ZipFile(path, "w") as docx:
        docx.writestr("[Content_Types].xml", "<Types/>")
        docx.writestr("word/document.xml", xml)


def _fake_pdf(monkeypatch, *pages: str | None) -> None:
    """PdfReader falso: cada página devuelve el texto que pypdf extraería de ella
    (None o "" en una página que es solo imagen)."""
    fake_pages = [SimpleNamespace(extract_text=lambda t=t: t) for t in pages]
    monkeypatch.setattr(ingest, "PdfReader", lambda path: SimpleNamespace(pages=fake_pages))


def _image_with_text(content: str):
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1600, 240), "white")
    font = ImageFont.load_default(size=72)
    ImageDraw.Draw(image).text((40, 70), content, fill="black", font=font)
    return image


async def _new_document(filename: str, status: str = "pending") -> int:
    async with engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO documents (filename, status, created_at) "
                    "VALUES (:f, :s, now()) RETURNING id"
                ),
                {"f": filename, "s": status},
            )
        ).scalar_one()


async def _status(document_id: int) -> str:
    async with engine.begin() as conn:
        return (
            await conn.execute(text("SELECT status FROM documents WHERE id = :id"), {"id": document_id})
        ).scalar_one()


# --- Word ------------------------------------------------------------------

def test_docx_is_read_one_paragraph_per_line(tmp_path):
    path = tmp_path / "politica.docx"
    _write_docx(
        path,
        "<w:p><w:r><w:t>Política de vacaciones</w:t></w:r></w:p>"
        '<w:p><w:r><w:t xml:space="preserve">Días: </w:t></w:r>'
        "<w:r><w:t>15</w:t><w:tab/><w:t>hábiles</w:t></w:r></w:p>"
        # Tabla: el texto de cada celda va en sus propios párrafos.
        "<w:tbl><w:tr>"
        "<w:tc><w:p><w:r><w:t>Responsable</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Rubí</w:t></w:r></w:p></w:tc>"
        "</w:tr></w:tbl>"
        # Un cuadro de texto mete un párrafo dentro de otro: sale aparte, sin mezclarse.
        "<w:p><w:r><w:t>Antes</w:t></w:r>"
        "<w:r><w:pict><w:txbxContent><w:p><w:r><w:t>Nota del cuadro</w:t></w:r></w:p>"
        "</w:txbxContent></w:pict></w:r>"
        "<w:r><w:br/><w:t>Después</w:t></w:r></w:p>",
    )

    assert _read_file(str(path)).split("\n") == [
        "Política de vacaciones",
        "Días: 15\thábiles",
        "Responsable",
        "Rubí",
        "Antes",
        "Después",
        "Nota del cuadro",
    ]


def test_an_oversized_docx_is_refused(tmp_path, monkeypatch):
    """Un document.xml enorme huele a zip bomb: no se descomprime."""
    path = tmp_path / "enorme.docx"
    _write_docx(path, "<w:p><w:r><w:t>texto</w:t></w:r></w:p>")
    monkeypatch.setattr(ingest, "_DOCX_MAX_XML", 10)

    with pytest.raises(ValueError):
        _read_file(str(path))


# --- PDF escaneado e imágenes ------------------------------------------------

def test_a_pdf_with_text_skips_ocr(tmp_path, monkeypatch):
    _fake_pdf(monkeypatch, "Informe trimestral de ventas", "Resultados por región")
    monkeypatch.setattr(ingest, "_ocr_pdf", lambda path: pytest.fail("OCR innecesario"))

    assert _read_file(str(tmp_path / "informe.pdf")) == (
        "Informe trimestral de ventas\nResultados por región"
    )


def test_a_pdf_without_text_is_read_with_ocr(tmp_path, monkeypatch):
    _fake_pdf(monkeypatch, "", None)
    monkeypatch.setattr(ingest, "_ocr_pdf", lambda path: "Texto leído con OCR")

    assert _read_file(str(tmp_path / "escaneado.pdf")) == "Texto leído con OCR"


def test_without_tesseract_an_image_has_no_text(tmp_path, monkeypatch):
    path = tmp_path / "foto.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(ingest.shutil, "which", lambda name: None)

    assert _read_file(str(path)) == ""


async def test_without_ocr_a_scanned_pdf_ends_empty(tmp_path, monkeypatch):
    """Fuera de Docker (sin tesseract) un escaneado no rompe la ingesta: queda 'empty'."""
    _fake_pdf(monkeypatch, "")
    monkeypatch.setattr(ingest.shutil, "which", lambda name: None)
    document_id = await _new_document("escaneado.pdf")

    assert await ingest_document(document_id, str(tmp_path / "escaneado.pdf")) == 0
    assert await _status(document_id) == "empty"


@needs_ocr
def test_ocr_reads_an_image(tmp_path):
    path = tmp_path / "recibo.png"
    _image_with_text("Presupuesto anual 2026").save(path)

    assert "Presupuesto anual 2026" in " ".join(_read_file(str(path)).split())


@needs_ocr
def test_ocr_reads_a_scanned_pdf(tmp_path):
    path = tmp_path / "escaneado.pdf"
    _image_with_text("Contrato de servicios").save(path, "PDF", resolution=150)

    assert "Contrato de servicios" in " ".join(_read_file(str(path)).split())


async def test_upload_accepts_word_and_images(client, auth_headers, tmp_path, monkeypatch):
    queued: list[str] = []
    monkeypatch.setattr(documents, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(
        documents,
        "ingest_document_task",
        SimpleNamespace(delay=lambda doc_id, path, request_id=None: queued.append(path)),
    )

    for name in ("informe.docx", "recibo.png", "foto.JPG"):
        resp = await client.post(
            "/documents/upload",
            files={"file": (name, b"contenido", "application/octet-stream")},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "pending"

    assert [p.rsplit("_", 1)[-1] for p in queued] == ["informe.docx", "recibo.png", "foto.JPG"]


# --- Citas -------------------------------------------------------------------

async def test_search_with_sources_names_the_document_of_each_fragment(monkeypatch):
    politica = await _new_document("politica.docx", status="ready")
    horarios = await _new_document("horarios.pdf", status="ready")

    async def fake_retrieve_ids(query, k, dual=False):
        return [(politica, "15 días hábiles"), (horarios, "de 9 a 18 h"), (999, "huérfano")]

    monkeypatch.setattr(retriever, "_retrieve_ids", fake_retrieve_ids)
    # El reranker decide el orden: devuelve posiciones dentro de los candidatos.
    monkeypatch.setattr(retriever, "rerank_order", lambda query, chunks, top_k: [1, 2, 0][:top_k])

    assert await retriever.search_with_sources("vacaciones", k=3) == [
        ("de 9 a 18 h", "horarios.pdf"),
        ("huérfano", "documento"),   # su documento se borró entre medias
        ("15 días hábiles", "politica.docx"),
    ]


async def test_search_with_sources_without_candidates(monkeypatch):
    async def nothing(query, k, dual=False):
        return []

    monkeypatch.setattr(retriever, "_retrieve_ids", nothing)

    assert await retriever.search_with_sources("nada") == []


def test_the_knowledge_base_cites_the_source_of_each_fragment(monkeypatch):
    async def fake_search(query, k=8, dual=False):
        return [("15 días hábiles", "politica.docx"), ("de 9 a 18 h", "horarios.pdf")]

    monkeypatch.setattr(knowledge_base, "search_with_sources", fake_search)

    assert knowledge_base.search_knowledge_base.invoke({"query": "vacaciones"}) == (
        "[Fuente: politica.docx]\n15 días hábiles\n\n---\n\n[Fuente: horarios.pdf]\nde 9 a 18 h"
    )


def test_the_knowledge_base_says_when_nothing_matches(monkeypatch):
    async def nothing(query, k=8, dual=False):
        return []

    monkeypatch.setattr(knowledge_base, "search_with_sources", nothing)

    assert knowledge_base.search_knowledge_base.invoke({"query": "x"}) == "No relevant documents found."
