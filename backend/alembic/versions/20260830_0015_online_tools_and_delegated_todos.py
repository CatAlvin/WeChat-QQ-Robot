"""Add online tools and delegated-contact todo state.

Revision ID: 20260830_0015
Revises: 20260830_0014
Create Date: 2026-08-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260830_0015"
down_revision = "20260830_0014"
branch_labels = None
depends_on = None


def _add_if_missing(name: str, column: sa.Column) -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("todo_items")}
    if name not in columns:
        op.add_column("todo_items", column)


def _index_if_missing(name: str, columns: list[str]) -> None:
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("todo_items")}
    if name not in indexes:
        op.create_index(name, "todo_items", columns)


def upgrade() -> None:
    _add_if_missing("kind", sa.Column("kind", sa.String(32), nullable=False, server_default="MANUAL"))
    _add_if_missing("account_id", sa.Column("account_id", sa.String(36)))
    _add_if_missing("admin_contact_id", sa.Column("admin_contact_id", sa.String(36)))
    _add_if_missing("admin_notification_message_id", sa.Column("admin_notification_message_id", sa.String(36)))
    _add_if_missing("response_message_id", sa.Column("response_message_id", sa.String(36)))
    _add_if_missing(
        "delivery_status",
        sa.Column("delivery_status", sa.String(32), nullable=False, server_default="NOT_REQUIRED"),
    )
    # Older MySQL-compatible servers reject defaults on TEXT columns. The ORM
    # supplies an empty string for new rows.
    _add_if_missing("response_text", sa.Column("response_text", sa.Text(), nullable=True))
    _add_if_missing("last_error", sa.Column("last_error", sa.Text()))
    op.execute("UPDATE todo_items SET response_text = '' WHERE response_text IS NULL")
    _index_if_missing("ix_todo_items_account", ["account_id"])
    _index_if_missing("ix_todo_items_admin_contact", ["admin_contact_id"])


def downgrade() -> None:
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("todo_items")}
    for name in ("ix_todo_items_admin_contact", "ix_todo_items_account"):
        if name in indexes:
            op.drop_index(name, table_name="todo_items")
    with op.batch_alter_table("todo_items") as batch:
        for name in (
            "last_error",
            "response_text",
            "delivery_status",
            "response_message_id",
            "admin_notification_message_id",
            "admin_contact_id",
            "account_id",
            "kind",
        ):
            batch.drop_column(name)
