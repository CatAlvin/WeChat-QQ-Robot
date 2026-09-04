"""Add controlled local media persistence.

Revision ID: 20260826_0006
Revises: 20260826_0005
Create Date: 2026-08-26
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260826_0006"
down_revision = "20260826_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    message_columns = {column["name"] for column in inspector.get_columns("messages")}
    if "raw_envelope" not in message_columns:
        op.add_column("messages", sa.Column("raw_envelope", sa.JSON(), nullable=True))
        op.execute("UPDATE messages SET raw_envelope = '{}' WHERE raw_envelope IS NULL")
        with op.batch_alter_table("messages") as batch_op:
            batch_op.alter_column("raw_envelope", existing_type=sa.JSON(), nullable=False)

    runtime_columns = {column["name"] for column in inspector.get_columns("runtime_state")}
    additions = (
        ("media_storage_enabled", sa.Boolean(), sa.true()),
        ("media_save_images", sa.Boolean(), sa.true()),
        ("media_save_audio", sa.Boolean(), sa.true()),
        ("media_save_files", sa.Boolean(), sa.true()),
        ("media_ai_reply_enabled", sa.Boolean(), sa.false()),
        ("media_max_file_mb", sa.Integer(), sa.text("50")),
    )
    for name, column_type, default in additions:
        if name not in runtime_columns:
            op.add_column("runtime_state", sa.Column(name, column_type, nullable=False, server_default=default))

    if "message_attachments" not in inspector.get_table_names():
        op.create_table(
            "message_attachments",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("message_id", sa.String(length=36), nullable=False),
            sa.Column("kind", sa.String(length=24), nullable=False),
            sa.Column("segment_type", sa.String(length=32), nullable=False),
            sa.Column("segment_index", sa.Integer(), nullable=False),
            sa.Column("file_name", sa.String(length=255), nullable=False),
            sa.Column("mime_type", sa.String(length=160), nullable=True),
            sa.Column("size_bytes", sa.BigInteger(), nullable=True),
            sa.Column("source_ref", sa.Text(), nullable=True),
            sa.Column("file_id", sa.String(length=500), nullable=True),
            sa.Column("local_path", sa.Text(), nullable=True),
            sa.Column("sha256", sa.String(length=64), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="METADATA_ONLY"),
            sa.Column("error_code", sa.String(length=80), nullable=True),
            sa.Column("metadata", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_message_attachments_message", "message_attachments", ["message_id", "segment_index"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "message_attachments" in inspector.get_table_names():
        op.drop_index("ix_message_attachments_message", table_name="message_attachments")
        op.drop_table("message_attachments")
    runtime_columns = {column["name"] for column in inspector.get_columns("runtime_state")}
    with op.batch_alter_table("runtime_state") as batch_op:
        for name in (
            "media_max_file_mb",
            "media_ai_reply_enabled",
            "media_save_files",
            "media_save_audio",
            "media_save_images",
            "media_storage_enabled",
        ):
            if name in runtime_columns:
                batch_op.drop_column(name)
    message_columns = {column["name"] for column in inspector.get_columns("messages")}
    if "raw_envelope" in message_columns:
        with op.batch_alter_table("messages") as batch_op:
            batch_op.drop_column("raw_envelope")
