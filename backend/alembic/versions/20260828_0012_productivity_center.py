"""Add productivity, recovery, knowledge, digest and sticker modules.

Revision ID: 20260828_0012
Revises: 20260828_0011
Create Date: 2026-08-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260828_0012"
down_revision = "20260828_0011"
branch_labels = None
depends_on = None


def _create_table_if_missing(name: str, *columns, **kwargs) -> None:
    if not sa.inspect(op.get_bind()).has_table(name):
        op.create_table(name, *columns, **kwargs)


def _create_index_if_missing(name: str, table: str, columns: list[str]) -> None:
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table)}
    if name not in indexes:
        op.create_index(name, table, columns)


def upgrade() -> None:
    memory_columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("memories")}
    for name, column_type, default, nullable in (
        ("review_status", sa.String(24), "'APPROVED'", False),
        ("pinned", sa.Boolean(), sa.false(), False),
        ("expires_at", sa.DateTime(), None, True),
        ("updated_at", sa.DateTime(), sa.func.now(), False),
    ):
        if name not in memory_columns:
            op.add_column("memories", sa.Column(name, column_type, nullable=nullable, server_default=default))

    _create_table_if_missing(
        "background_tasks",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("kind", sa.String(60), nullable=False),
        sa.Column("title", sa.String(200), nullable=False), sa.Column("status", sa.String(24), nullable=False, server_default="PENDING"),
        sa.Column("payload", sa.JSON(), nullable=False), sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result", sa.JSON(), nullable=False), sa.Column("error_code", sa.String(100)), sa.Column("error_detail", sa.Text()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"), sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("available_at", sa.DateTime(), nullable=False), sa.Column("started_at", sa.DateTime()), sa.Column("completed_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    _create_index_if_missing("ix_background_tasks_status_available", "background_tasks", ["status", "available_at"])
    _create_table_if_missing(
        "recovery_items",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("source_type", sa.String(40), nullable=False),
        sa.Column("source_id", sa.String(200), nullable=False), sa.Column("kind", sa.String(60), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="OPEN"),
        sa.Column("contact_id", sa.String(36), sa.ForeignKey("contacts.id", ondelete="SET NULL")),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="SET NULL")),
        sa.Column("error_code", sa.String(100)), sa.Column("error_detail", sa.Text()),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"), sa.Column("last_retry_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("source_type", "source_id", "kind", name="uq_recovery_source_kind"),
    )
    _create_index_if_missing("ix_recovery_items_status_created", "recovery_items", ["status", "created_at"])
    _create_table_if_missing(
        "knowledge_documents",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("title", sa.String(200), nullable=False),
        sa.Column("source_name", sa.String(255), nullable=False), sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False), sa.Column("status", sa.String(24), nullable=False, server_default="READY"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    _create_index_if_missing("ix_knowledge_documents_enabled_updated", "knowledge_documents", ["enabled", "updated_at"])
    _create_table_if_missing(
        "todo_items",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("title", sa.String(240), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False), sa.Column("status", sa.String(24), nullable=False, server_default="OPEN"),
        sa.Column("priority", sa.String(16), nullable=False, server_default="NORMAL"), sa.Column("due_at", sa.DateTime()),
        sa.Column("reminder_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("contact_id", sa.String(36), sa.ForeignKey("contacts.id", ondelete="SET NULL")),
        sa.Column("source_message_id", sa.String(36), sa.ForeignKey("messages.id", ondelete="SET NULL")),
        sa.Column("completed_at", sa.DateTime()), sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    _create_index_if_missing("ix_todo_items_status_due", "todo_items", ["status", "due_at"])
    _create_table_if_missing(
        "daily_digests",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("local_date", sa.String(10), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="READY"), sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False), sa.Column("generated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("local_date", name="uq_daily_digest_local_date"),
    )
    _create_table_if_missing(
        "sticker_assets",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("label", sa.String(120), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False), sa.Column("local_path", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.String(120), nullable=False), sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("auto_reply_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    _create_index_if_missing("ix_sticker_assets_enabled_created", "sticker_assets", ["enabled", "created_at"])


def downgrade() -> None:
    for table in ("sticker_assets", "daily_digests", "todo_items", "knowledge_documents", "recovery_items", "background_tasks"):
        op.drop_table(table)
    with op.batch_alter_table("memories") as batch:
        for name in ("updated_at", "expires_at", "pinned", "review_status"):
            batch.drop_column(name)
