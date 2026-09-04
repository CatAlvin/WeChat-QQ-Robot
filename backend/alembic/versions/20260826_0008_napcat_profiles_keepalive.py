"""Add NapCat profile switching and contact keepalive controls.

Revision ID: 20260826_0008
Revises: 20260826_0007
Create Date: 2026-08-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260826_0008"
down_revision = "20260826_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("contacts")}
    additions = (
        ("keepalive_enabled", sa.Boolean(), False, sa.false()),
        ("keepalive_time", sa.String(length=5), False, "20:00"),
        ("keepalive_account_id", sa.String(length=36), True, None),
        ("keepalive_last_attempt_on", sa.String(length=10), True, None),
        ("keepalive_last_sent_at", sa.DateTime(timezone=True), True, None),
        ("keepalive_last_status", sa.String(length=24), False, "NEVER"),
        ("keepalive_last_content", sa.Text(), True, None),
        ("keepalive_last_error", sa.String(length=120), True, None),
    )
    for name, column_type, nullable, default in additions:
        if name not in columns:
            op.add_column(
                "contacts",
                sa.Column(name, column_type, nullable=nullable, server_default=default),
            )
    inspector = sa.inspect(op.get_bind())
    foreign_key_rows = inspector.get_foreign_keys("contacts")
    has_keepalive_fk = any(item.get("constrained_columns") == ["keepalive_account_id"] for item in foreign_key_rows)
    if not has_keepalive_fk:
        with op.batch_alter_table("contacts") as batch:
            batch.create_foreign_key(
                "fk_contacts_keepalive_account",
                "accounts",
                ["keepalive_account_id"],
                ["id"],
                ondelete="SET NULL",
            )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("contacts")}
    foreign_keys = {item.get("name") for item in inspector.get_foreign_keys("contacts") if item.get("name")}
    with op.batch_alter_table("contacts") as batch:
        if "fk_contacts_keepalive_account" in foreign_keys:
            batch.drop_constraint("fk_contacts_keepalive_account", type_="foreignkey")
        for name in (
            "keepalive_last_error",
            "keepalive_last_content",
            "keepalive_last_status",
            "keepalive_last_sent_at",
            "keepalive_last_attempt_on",
            "keepalive_account_id",
            "keepalive_time",
            "keepalive_enabled",
        ):
            if name in columns:
                batch.drop_column(name)
