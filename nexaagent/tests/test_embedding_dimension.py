"""Tests de la dimensión de los embeddings.

Antes, las migraciones creaban siempre vectores de 768 dimensiones: con otro modelo
de embeddings (EMBEDDING_DIM=1024), incluso recreando la base, cada inserción
fallaba. Ahora las migraciones usan EMBEDDING_DIM, y app/db/schema_check.py (que el
servicio migrate ejecuta tras migrar) explica el problema si la configuración y la
base no coinciden.
"""
import psycopg2
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from app.core.config import settings
from app.db import schema_check
from tests.conftest import ROOT, TEST_DATABASE_URL


def test_message_when_the_dimension_does_not_match():
    message = schema_check.mismatch_message(
        {"document_chunks": 768, "long_term_memories": 768}, expected=1024
    )

    assert "EMBEDDING_DIM=1024" in message
    assert "document_chunks: 768" in message and "long_term_memories: 768" in message
    assert "docker compose down -v" in message


def test_no_message_when_it_matches():
    assert schema_check.mismatch_message({"document_chunks": 768}, expected=768) is None


def test_check_passes_on_the_test_database(capsys):
    """La base de test se migró con la configuración por defecto (768)."""
    assert schema_check.main() == 0
    assert "768 dimensiones" in capsys.readouterr().out


def test_check_fails_after_changing_the_dimension(monkeypatch, capsys):
    monkeypatch.setattr(settings, "embedding_dim", 1024)

    assert schema_check.main() == 1
    assert "EMBEDDING_DIM=1024" in capsys.readouterr().err


def _admin_connection():
    url = make_url(TEST_DATABASE_URL)
    conn = psycopg2.connect(
        host=url.host, port=url.port, user=url.username, password=url.password, dbname="postgres"
    )
    conn.autocommit = True
    return conn


def test_new_database_is_created_with_the_configured_dimension(monkeypatch):
    """Una base nueva con EMBEDDING_DIM=1024 se crea con vectores de 1024."""
    fresh = make_url(TEST_DATABASE_URL).set(database=make_url(TEST_DATABASE_URL).database + "_dim")
    admin = _admin_connection()
    try:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{fresh.database}" WITH (FORCE)')
            cur.execute(f'CREATE DATABASE "{fresh.database}"')

        monkeypatch.setattr(settings, "database_url", fresh.render_as_string(hide_password=False))
        monkeypatch.setattr(settings, "embedding_dim", 1024)
        cfg = Config(f"{ROOT}/alembic.ini")
        cfg.set_main_option("script_location", f"{ROOT}/alembic")
        command.upgrade(cfg, "head")

        assert schema_check.main() == 0
        conn = psycopg2.connect(
            host=fresh.host, port=fresh.port, user=fresh.username,
            password=fresh.password, dbname=fresh.database,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT c.relname, a.atttypmod FROM pg_attribute a "
                    "JOIN pg_class c ON c.oid = a.attrelid "
                    "WHERE a.attname = 'embedding' ORDER BY c.relname"
                )
                assert cur.fetchall() == [("document_chunks", 1024), ("long_term_memories", 1024)]
        finally:
            conn.close()
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{fresh.database}" WITH (FORCE)')
        admin.close()
