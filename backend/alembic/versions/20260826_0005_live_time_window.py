"""Add configurable LIVE time window controls.

Revision ID: 20260826_0005
Revises: 20260824_0004
Create Date: 2026-08-26
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260826_0005"
down_revision = "20260824_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("runtime_state")}
    if "live_time_window_enabled" not in existing:
        op.add_column(
            "runtime_state",
            sa.Column("live_time_window_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        )
    if "live_auto_start" not in existing:
        op.add_column(
            "runtime_state",
            sa.Column("live_auto_start", sa.String(length=5), nullable=False, server_default="10:00"),
        )
    if "live_auto_end" not in existing:
        op.add_column(
            "runtime_state",
            sa.Column("live_auto_end", sa.String(length=5), nullable=False, server_default="23:30"),
        )


def downgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("runtime_state")}
    with op.batch_alter_table("runtime_state") as batch_op:
        if "live_auto_end" in existing:
            batch_op.drop_column("live_auto_end")
        if "live_auto_start" in existing:
            batch_op.drop_column("live_auto_start")
        if "live_time_window_enabled" in existing:
            batch_op.drop_column("live_time_window_enabled")
