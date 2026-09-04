"""Add opt-in local media understanding.

Revision ID: 20260826_0007
Revises: 20260826_0006
Create Date: 2026-08-26
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260826_0007"
down_revision = "20260826_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    runtime_columns = {column["name"] for column in inspector.get_columns("runtime_state")}
    runtime_additions = (
        ("media_understanding_enabled", sa.Boolean(), sa.false()),
        ("media_understand_images", sa.Boolean(), sa.true()),
        ("media_transcribe_audio", sa.Boolean(), sa.true()),
        ("media_extract_documents", sa.Boolean(), sa.true()),
        ("media_vision_model", sa.String(length=160), "qwen3-vl:4b"),
        ("media_whisper_model", sa.String(length=80), "small"),
        ("media_whisper_device", sa.String(length=16), "auto"),
        ("media_whisper_allow_download", sa.Boolean(), sa.false()),
        ("media_understanding_max_chars", sa.Integer(), sa.text("6000")),
    )
    for name, column_type, default in runtime_additions:
        if name not in runtime_columns:
            op.add_column("runtime_state", sa.Column(name, column_type, nullable=False, server_default=default))

    attachment_columns = {column["name"] for column in inspector.get_columns("message_attachments")}
    attachment_additions = (
        ("analysis_status", sa.String(length=32), False, "NOT_REQUESTED"),
        ("analysis_provider", sa.String(length=80), True, None),
        ("analysis_model", sa.String(length=160), True, None),
        ("analysis_text", sa.Text(), True, None),
        ("analysis_error_code", sa.String(length=100), True, None),
        ("analyzed_at", sa.DateTime(timezone=True), True, None),
    )
    for name, column_type, nullable, default in attachment_additions:
        if name not in attachment_columns:
            op.add_column(
                "message_attachments",
                sa.Column(name, column_type, nullable=nullable, server_default=default),
            )
    if "analysis_metadata" not in attachment_columns:
        # MySQL rejects defaults on JSON/BLOB columns. Add it nullable, backfill,
        # then enforce NOT NULL; the ORM supplies {} for all new rows.
        op.add_column("message_attachments", sa.Column("analysis_metadata", sa.JSON(), nullable=True))
        op.execute("UPDATE message_attachments SET analysis_metadata = '{}' WHERE analysis_metadata IS NULL")
        with op.batch_alter_table("message_attachments") as batch_op:
            batch_op.alter_column("analysis_metadata", existing_type=sa.JSON(), nullable=False)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    attachment_columns = {column["name"] for column in inspector.get_columns("message_attachments")}
    with op.batch_alter_table("message_attachments") as batch_op:
        for name in (
            "analyzed_at",
            "analysis_metadata",
            "analysis_error_code",
            "analysis_text",
            "analysis_model",
            "analysis_provider",
            "analysis_status",
        ):
            if name in attachment_columns:
                batch_op.drop_column(name)
    runtime_columns = {column["name"] for column in inspector.get_columns("runtime_state")}
    with op.batch_alter_table("runtime_state") as batch_op:
        for name in (
            "media_understanding_max_chars",
            "media_whisper_allow_download",
            "media_whisper_device",
            "media_whisper_model",
            "media_vision_model",
            "media_extract_documents",
            "media_transcribe_audio",
            "media_understand_images",
            "media_understanding_enabled",
        ):
            if name in runtime_columns:
                batch_op.drop_column(name)
