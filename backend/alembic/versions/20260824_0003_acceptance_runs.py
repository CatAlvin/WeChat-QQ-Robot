"""Add auditable QQ acceptance runs.

Revision ID: 20260824_0003
Revises: 20260824_0002
Create Date: 2026-08-24
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260824_0003"
down_revision = "20260824_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "acceptance_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("target_contact_id", sa.String(length=36), nullable=True),
        sa.Column("target_platform_user_id", sa.String(length=160), nullable=False),
        sa.Column("expected_messages", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["target_contact_id"], ["contacts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_acceptance_runs_kind_started", "acceptance_runs", ["kind", "started_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_acceptance_runs_kind_started", table_name="acceptance_runs")
    op.drop_table("acceptance_runs")
