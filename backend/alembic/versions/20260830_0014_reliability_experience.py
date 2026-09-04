"""Add reliability center, delivery history and relationship reminders.

Revision ID: 20260830_0014
Revises: 20260828_0013
Create Date: 2026-08-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260830_0014"
down_revision = "20260828_0013"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _add_if_missing(table: str, name: str, column: sa.Column) -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}
    if name not in columns:
        op.add_column(table, column)


def upgrade() -> None:
    _add_if_missing("contacts", "birthday_mmdd", sa.Column("birthday_mmdd", sa.String(5)))
    _add_if_missing(
        "contacts",
        "relationship_reminders_enabled",
        sa.Column("relationship_reminders_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    _add_if_missing(
        "contacts",
        "dormant_reminder_days",
        sa.Column("dormant_reminder_days", sa.Integer(), nullable=False, server_default="14"),
    )

    if not _table_exists("provider_health"):
        op.create_table(
            "provider_health",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("provider_id", sa.String(36), sa.ForeignKey("llm_providers.id", ondelete="CASCADE"), nullable=False),
            sa.Column("status", sa.String(24), nullable=False, server_default="UNKNOWN"),
            sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
            sa.Column("last_success_at", sa.DateTime(timezone=True)),
            sa.Column("last_failure_at", sa.DateTime(timezone=True)),
            sa.Column("last_error_code", sa.String(100)),
            sa.Column("last_error_detail", sa.Text()),
            sa.Column("last_latency_ms", sa.Integer()),
            sa.Column("last_model", sa.String(160)),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("provider_id", name="uq_provider_health_provider"),
        )

    if not _table_exists("outbound_delivery_events"):
        op.create_table(
            "outbound_delivery_events",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("message_id", sa.String(36), sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
            sa.Column("attempt_no", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("stage", sa.String(32), nullable=False),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("delivery_started", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("acknowledged", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("retry_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("retry_reason", sa.String(200)),
            sa.Column("error_code", sa.String(100)),
            sa.Column("external_message_id", sa.String(200)),
            sa.Column("detail", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_outbound_delivery_message_created", "outbound_delivery_events", ["message_id", "created_at"])
        op.create_index("ix_outbound_delivery_status_created", "outbound_delivery_events", ["status", "created_at"])

    if not _table_exists("relationship_reminders"):
        op.create_table(
            "relationship_reminders",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("contact_id", sa.String(36), sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
            sa.Column("kind", sa.String(40), nullable=False),
            sa.Column("title", sa.String(240), nullable=False),
            # MySQL 5.7 and older MySQL-compatible servers reject defaults on
            # TEXT columns. The ORM already supplies an empty string for new
            # reminders, so the database-level default is unnecessary.
            sa.Column("detail", sa.Text(), nullable=False),
            sa.Column("status", sa.String(24), nullable=False, server_default="OPEN"),
            sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source_message_id", sa.String(36), sa.ForeignKey("messages.id", ondelete="SET NULL")),
            sa.Column("source_memory_id", sa.String(36), sa.ForeignKey("memories.id", ondelete="SET NULL")),
            sa.Column("dedupe_key", sa.String(160), nullable=False),
            sa.Column("admin_notified_at", sa.DateTime(timezone=True)),
            sa.Column("snoozed_until", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("dedupe_key", name="uq_relationship_reminder_dedupe"),
        )
        op.create_index("ix_relationship_reminder_status_due", "relationship_reminders", ["status", "due_at"])


def downgrade() -> None:
    for table in ("relationship_reminders", "outbound_delivery_events", "provider_health"):
        if _table_exists(table):
            op.drop_table(table)
    with op.batch_alter_table("contacts") as batch:
        batch.drop_column("dormant_reminder_days")
        batch.drop_column("relationship_reminders_enabled")
        batch.drop_column("birthday_mmdd")
