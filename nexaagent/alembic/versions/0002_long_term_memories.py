"""long term memories

Revision ID: 0002_long_term_memories
Revises: 0001_initial
Create Date: 2026-05-27

Tabla para la memoria de largo plazo: hechos extraídos de conversaciones,
embebidos para recuperación por similitud (Parte 6). Solo vectorial en v1.
"""
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from app.core.config import settings


revision = "0002_long_term_memories"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

EMBEDDING_DIM = settings.embedding_dim  # de la configuración, como en 0001_initial


def upgrade() -> None:
    op.create_table(
        "long_term_memories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_long_term_memories_conversation_id",
        "long_term_memories",
        ["conversation_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_long_term_memories_conversation_id",
        table_name="long_term_memories",
    )
    op.drop_table("long_term_memories")