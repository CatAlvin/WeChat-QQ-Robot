"""Add durable connector event inbox.

Revision ID: 20260824_0002
Revises: 20260824_0001
Create Date: 2026-08-24
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260824_0002"
down_revision = "20260824_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connector_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("external_event_id", sa.String(length=200), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("normalized_payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("platform", "external_event_id", name="uq_connector_event_external"),
    )
    op.create_index("ix_connector_events_status_created", "connector_events", ["status", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_connector_events_status_created", table_name="connector_events")
    op.drop_table("connector_events")
