"""Store the complete raw message envelope.

Revision ID: 20260824_0004
Revises: 20260824_0003
Create Date: 2026-08-24
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260824_0004"
down_revision = "20260824_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("messages")}
    if "sender_id" not in existing:
        op.add_column("messages", sa.Column("sender_id", sa.String(length=200), nullable=True))
    if "receiver_id" not in existing:
        op.add_column("messages", sa.Column("receiver_id", sa.String(length=200), nullable=True))
    if "event_at" not in existing:
        op.add_column("messages", sa.Column("event_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE messages SET event_at = created_at WHERE event_at IS NULL")
    with op.batch_alter_table("messages") as batch_op:
        batch_op.alter_column("event_at", existing_type=sa.DateTime(timezone=True), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("messages") as batch_op:
        batch_op.drop_column("event_at")
        batch_op.drop_column("receiver_id")
        batch_op.drop_column("sender_id")
