"""Add persistent administrator screenshot-memory import batches.

Revision ID: 20260831_0016
Revises: 20260830_0015
Create Date: 2026-08-31
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260831_0016"
down_revision = "20260830_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "admin_memory_import_batches" not in inspector.get_table_names():
        op.create_table(
            "admin_memory_import_batches",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id", ondelete="CASCADE")),
            sa.Column(
                "admin_contact_id",
                sa.String(36),
                sa.ForeignKey("contacts.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "target_contact_id",
                sa.String(36),
                sa.ForeignKey("contacts.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("status", sa.String(24), nullable=False, server_default="ACTIVE"),
            sa.Column("started_message_id", sa.String(36), sa.ForeignKey("messages.id", ondelete="SET NULL")),
            sa.Column("completed_message_id", sa.String(36), sa.ForeignKey("messages.id", ondelete="SET NULL")),
            sa.Column("screenshot_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("extracted_text_chars", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("memory_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("provider", sa.String(80)),
            sa.Column("model", sa.String(160)),
            sa.Column("last_error", sa.Text()),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_admin_memory_import_admin_status",
            "admin_memory_import_batches",
            ["admin_contact_id", "status"],
        )
        op.create_index(
            "ix_admin_memory_import_account_created",
            "admin_memory_import_batches",
            ["account_id", "created_at"],
        )
        op.create_index(
            "ix_admin_memory_import_batches_account_id",
            "admin_memory_import_batches",
            ["account_id"],
        )

    inspector = sa.inspect(op.get_bind())
    if "admin_memory_import_items" not in inspector.get_table_names():
        op.create_table(
            "admin_memory_import_items",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "batch_id",
                sa.String(36),
                sa.ForeignKey("admin_memory_import_batches.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("message_id", sa.String(36), sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
            sa.Column(
                "attachment_id",
                sa.String(36),
                sa.ForeignKey("message_attachments.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("batch_id", "attachment_id", name="uq_admin_memory_import_attachment"),
        )
        op.create_index(
            "ix_admin_memory_import_items_batch_position",
            "admin_memory_import_items",
            ["batch_id", "position"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "admin_memory_import_items" in tables:
        op.drop_table("admin_memory_import_items")
    if "admin_memory_import_batches" in tables:
        op.drop_table("admin_memory_import_batches")
