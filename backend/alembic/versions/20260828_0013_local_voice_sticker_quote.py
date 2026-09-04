"""Add local voice, quote reply and QQ sticker provenance.

Revision ID: 20260828_0013
Revises: 20260828_0012
Create Date: 2026-08-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260828_0013"
down_revision = "20260828_0012"
branch_labels = None
depends_on = None


def _add_if_missing(table: str, name: str, column: sa.Column) -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}
    if name not in columns:
        op.add_column(table, column)


def upgrade() -> None:
    for name, column in (
        ("tts_enabled", sa.Column("tts_enabled", sa.Boolean(), nullable=False, server_default=sa.false())),
        ("tts_reply_to_audio_only", sa.Column("tts_reply_to_audio_only", sa.Boolean(), nullable=False, server_default=sa.true())),
        ("tts_voice", sa.Column("tts_voice", sa.String(120), nullable=False, server_default="")),
        ("tts_rate", sa.Column("tts_rate", sa.Integer(), nullable=False, server_default="1")),
        ("tts_volume", sa.Column("tts_volume", sa.Integer(), nullable=False, server_default="92")),
        ("tts_max_chars", sa.Column("tts_max_chars", sa.Integer(), nullable=False, server_default="300")),
        ("napcat_quote_reply_enabled", sa.Column("napcat_quote_reply_enabled", sa.Boolean(), nullable=False, server_default=sa.false())),
    ):
        _add_if_missing("runtime_state", name, column)
    _add_if_missing("sticker_assets", "source_kind", sa.Column("source_kind", sa.String(24), nullable=False, server_default="MANUAL"))
    _add_if_missing("sticker_assets", "source_ref", sa.Column("source_ref", sa.Text()))


def downgrade() -> None:
    with op.batch_alter_table("sticker_assets") as batch:
        batch.drop_column("source_ref")
        batch.drop_column("source_kind")
    with op.batch_alter_table("runtime_state") as batch:
        for name in (
            "napcat_quote_reply_enabled",
            "tts_max_chars",
            "tts_volume",
            "tts_rate",
            "tts_voice",
            "tts_reply_to_audio_only",
            "tts_enabled",
        ):
            batch.drop_column(name)
