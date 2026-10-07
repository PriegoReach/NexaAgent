"""reminder_digests table

Revision ID: reminder_digests
Revises: documents_drive_file_id
Create Date: 2026-10-07

Un resumen de recordatorios por día (app/workers/reminders.py). El día se reclama
AQUÍ antes de enviar (PRIMARY KEY sobre day), el mismo registrar-primero que
sent_emails y webhook_events: aunque Celery beat lo lance cada hora, se reinicie
el worker o haya varios, sale un solo resumen por día. status: 'pending' al
reclamar, luego 'sent', 'failed' (se reintenta en la hora siguiente), 'uncertain'
(pudo salir; no se reintenta) o 'skipped' (no hay canal configurado).

Escrita a mano, como las demás (autogenerate no es de fiar en este proyecto, P6).
"""
from alembic import op
import sqlalchemy as sa


revision = "reminder_digests"
down_revision = "documents_drive_file_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reminder_digests",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("channel", sa.String(length=20), nullable=True),   # 'email' | 'webhook'
        sa.Column("n_tasks", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("reminder_digests")
