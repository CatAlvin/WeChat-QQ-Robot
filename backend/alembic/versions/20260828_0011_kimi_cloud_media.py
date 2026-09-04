"""Add explicit Kimi Cloud image and video upload controls.

Revision ID: 20260828_0011
Revises: 20260827_0010
Create Date: 2026-08-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260828_0011"
down_revision = "20260827_0010"
branch_labels = None
depends_on = None


def _column_names() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("runtime_state")}


def upgrade() -> None:
    columns = _column_names()
    additions = (
        ("kimi_media_upload_enabled", sa.Boolean(), sa.false()),
        ("kimi_media_upload_images", sa.Boolean(), sa.true()),
        ("kimi_media_upload_videos", sa.Boolean(), sa.true()),
        ("kimi_media_max_file_mb", sa.Integer(), sa.text("50")),
    )
    for name, column_type, default in additions:
        if name not in columns:
            op.add_column(
                "runtime_state",
                sa.Column(name, column_type, nullable=False, server_default=default),
            )


def downgrade() -> None:
    columns = _column_names()
    with op.batch_alter_table("runtime_state") as batch:
        for name in (
            "kimi_media_max_file_mb",
            "kimi_media_upload_videos",
            "kimi_media_upload_images",
            "kimi_media_upload_enabled",
        ):
            if name in columns:
                batch.drop_column(name)
