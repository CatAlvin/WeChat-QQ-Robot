"""Create the Neko AI V1 schema.

Revision ID: 20260824_0001
Revises: None
Create Date: 2026-08-24
"""
from __future__ import annotations

from alembic import op

from app.database import Base
from app.models import entities  # noqa: F401


revision = "20260824_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # create_all is intentional for the baseline: it is idempotent for the
    # existing SQLite development database and creates the same schema on MySQL.
    later_tables = {"connector_events", "acceptance_runs"}
    baseline_tables = [table for name, table in Base.metadata.tables.items() if name not in later_tables]
    Base.metadata.create_all(bind=op.get_bind(), tables=baseline_tables)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
