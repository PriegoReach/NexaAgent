import asyncio
import logging

import httpx
from celery import Task
from celery.signals import setup_logging as celery_setup_logging
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from app.agent.memory_extraction import extract_and_store_memories
from app.core.config import settings
from app.core.log_context import conversation_id_var, request_id_var
from app.core.logging_config import setup_logging
from app.rag.ingest import ingest_document
from app.workers.celery_app import celery_app
from app.workers.reminders import send_daily_digest


@celery_setup_logging.connect
def _config_celery_logging(**kwargs):
    # Celery cede su logging → mismo formato JSON que la API.
    setup_logging()


logger = logging.getLogger("nexa.worker")


def _mark_failed(document_id: int) -> None:
    """Marca el documento como 'failed' desde un contexto SÍNCRONO.

    on_failure de Celery no es async, así que no podemos usar worker_session()
    (async). Reusamos el patrón de Alembic (Parte 5): URL síncrona con psycopg2
    y NullPool, engine efímero solo para este UPDATE.
    """
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE documents SET status = 'failed' WHERE id = :doc_id"),
                {"doc_id": document_id},
            )
    finally:
        engine.dispose()


class IngestTask(Task):
    """Task base con gancho on_failure: cuando se agotan los reintentos, el
    documento se marca 'failed' en vez de quedar 'pending' eternamente."""

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        document_id = args[0] if args else kwargs.get("document_id")
        logger.error(
            "ingest failed permanently",
            extra={
                "event": "ingest_failed",
                "document_id": document_id,
                "exc_type": type(exc).__name__,
            },
        )
        if document_id is not None:
            _mark_failed(document_id)


@celery_app.task(
    name="ingest_document",
    base=IngestTask,
    bind=True,
    autoretry_for=(httpx.ConnectError, httpx.TimeoutException, ConnectionError),
    retry_backoff=True,
    retry_backoff_max=60,
    max_retries=3,
    
)
def ingest_document_task(self, document_id: int, file_path: str, request_id: str = "-") -> int:
    """Celery entrypoint: runs the async ingest pipeline in a fresh loop.

    Idempotente (Parte 14-1): la pipeline borra los chunks previos antes de
    insertar, así que reintentar es inofensivo. autoretry solo para fallos
    transitorios (Ollama caído a mitad de embeddings), no para PDFs corruptos.
    """
    request_id_var.set(request_id)   # correlación (Fase 1) también en reintentos
    logger.info(
        "ingest start",
        extra={
            "event": "ingest_start",
            "document_id": document_id,
            "attempt": self.request.retries,
        },
    )
    return asyncio.run(ingest_document(document_id, file_path))


@celery_app.task(name="extract_memories")
def extract_memories_task(conversation_id: int, request_id: str = "-") -> int:
    """Celery entrypoint: extrae y guarda hechos de largo plazo.

    El contextvar no viaja entre procesos: recibimos el request_id como
    argumento (lo encola el orchestrator) y lo volvemos a fijar aquí para
    correlacionar los logs del worker con la petición original.
    """
    request_id_var.set(request_id)
    conversation_id_var.set(str(conversation_id))
    logger.info("memory task received", extra={"event": "celery_start"})
    return asyncio.run(extract_and_store_memories(conversation_id))


@celery_app.task(name="send_daily_reminders")
def send_daily_reminders_task() -> str:
    """Celery beat la lanza cada hora (celery_app.beat_schedule); send_daily_digest
    decide si toca mandar hoy el resumen de tareas."""
    return asyncio.run(send_daily_digest())
