FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# System deps (build tools for some wheels)
RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .

# OCR para PDFs escaneados e imágenes (app/rag/ingest.py): tesseract con español e
# inglés, y pdftoppm (poppler) para pasar las páginas de un PDF a imagen. Va
# DESPUÉS de las capas de pip: añadirlo no obliga a reinstalar torch.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-spa poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Dev/test deps (pytest, pytest-asyncio, ruff) solo bajo demanda, para no
# inflar la imagen de producción. El servicio `tests` del compose construye con
# INSTALL_DEV=true; api/worker la dejan en false (no instalan nada extra).
ARG INSTALL_DEV=false
RUN if [ "$INSTALL_DEV" = "true" ]; then pip install --no-cache-dir ".[dev]"; fi

COPY . .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
