"""initial schema + full-text setup

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-25

Migración inicial de NexaAgent. Captura en un solo sitio versionado:
  - extensiones (vector, unaccent)
  - configuración de texto es_simple (simple + unaccent, sin stemming)
  - las 4 tablas del modelo
  - la columna generada content_tsv + índice GIN (full-text de la Parte 4)

ORDEN IMPORTANTE: las extensiones y la config de texto van ANTES de las tablas,
porque document_chunks.embedding usa el tipo Vector y content_tsv depende de
que es_simple exista.
"""
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from app.core.config import settings


# revision identifiers, used by Alembic.
revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

# La dimensión de los vectores sale de la configuración (EMBEDDING_DIM), no de un
# número fijo: con otro modelo de embeddings, una base nueva se crea ya con la
# dimensión correcta. En una base existente no cambia nada (la migración ya corrió);
# app/db/schema_check.py avisa si la configuración y la base no coinciden.
EMBEDDING_DIM = settings.embedding_dim


def upgrade() -> None:
    # --- 1. Extensiones (antes de cualquier tabla que dependa de ellas) ---
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")

    # --- 2. Configuración de texto es_simple (simple + unaccent) ---
    # Idempotente: solo crear si no existe (unaccent es de esquema público).
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_ts_config WHERE cfgname = 'es_simple'
            ) THEN
                CREATE TEXT SEARCH CONFIGURATION es_simple (COPY = simple);
                ALTER TEXT SEARCH CONFIGURATION es_simple
                    ALTER MAPPING FOR hword, hword_part, word
                    WITH unaccent, simple;
            END IF;
        END
        $$;
        """
    )

    # --- 3. Tablas ---
    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_messages_conversation_id", "messages", ["conversation_id"]
    )

    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_document_chunks_document_id", "document_chunks", ["document_id"]
    )

    # --- 4. Full-text: columna generada + índice GIN (Parte 4) ---
    # No sale de los modelos de SQLAlchemy; se añade a mano.
    op.execute(
        """
        ALTER TABLE document_chunks
            ADD COLUMN content_tsv tsvector
            GENERATED ALWAYS AS (to_tsvector('es_simple', content)) STORED
        """
    )
    op.execute(
        "CREATE INDEX idx_chunks_tsv ON document_chunks USING GIN (content_tsv)"
    )


def downgrade() -> None:
    # Orden inverso.
    op.drop_index("idx_chunks_tsv", table_name="document_chunks")
    # content_tsv cae con la tabla; no hace falta dropearla aparte.
    op.drop_index("ix_document_chunks_document_id", table_name="document_chunks")
    op.drop_table("document_chunks")
    op.drop_table("documents")
    op.drop_index("ix_messages_conversation_id", table_name="messages")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.execute("DROP TEXT SEARCH CONFIGURATION IF EXISTS es_simple")
    # Las extensiones (vector, unaccent) se dejan; otras cosas podrían usarlas.
