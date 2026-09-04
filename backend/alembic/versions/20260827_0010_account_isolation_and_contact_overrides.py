"""Isolate channel data per managed account and add contact policy overrides.

Revision ID: 20260827_0010
Revises: 20260827_0009
Create Date: 2026-08-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260827_0010"
down_revision = "20260827_0009"
branch_labels = None
depends_on = None


ACCOUNT_TABLES = ("contacts", "chat_groups", "conversations", "messages", "connector_events")


def _column_names(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _constraint_names(table: str) -> set[str]:
    return {
        item.get("name")
        for item in sa.inspect(op.get_bind()).get_unique_constraints(table)
        if item.get("name")
    }


def upgrade() -> None:
    for table in ACCOUNT_TABLES:
        if "account_id" not in _column_names(table):
            op.add_column(table, sa.Column("account_id", sa.String(length=36), nullable=True))

    contact_columns = _column_names("contacts")
    additions = (
        ("reply_time_window_enabled", sa.Boolean()),
        ("reply_auto_start", sa.String(length=5)),
        ("reply_auto_end", sa.String(length=5)),
        ("media_storage_enabled", sa.Boolean()),
        ("media_ai_reply_enabled", sa.Boolean()),
    )
    for name, column_type in additions:
        if name not in contact_columns:
            op.add_column("contacts", sa.Column(name, column_type, nullable=True))

    # Existing records belong to the previously active (or oldest) profile for
    # their platform. This preserves all historical data while making every
    # newly received event unambiguously account-scoped.
    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id, platform, enabled FROM accounts ORDER BY enabled DESC, created_at ASC")
    ).mappings()
    defaults: dict[str, str] = {}
    for row in rows:
        defaults.setdefault(str(row["platform"]), str(row["id"]))
    for platform, account_id in defaults.items():
        for table in ACCOUNT_TABLES:
            connection.execute(
                sa.text(f"UPDATE {table} SET account_id = :account_id WHERE platform = :platform AND account_id IS NULL"),
                {"account_id": account_id, "platform": platform},
            )

    old_constraints = {
        "contacts": "uq_contact_platform_user",
        "chat_groups": "uq_group_platform_id",
        "conversations": "uq_conversation_external",
        "messages": "uq_message_external",
        "connector_events": "uq_connector_event_external",
    }
    new_constraints = {
        "contacts": ("uq_contact_account_user", ["platform", "account_id", "platform_user_id"]),
        "chat_groups": ("uq_group_account_id", ["platform", "account_id", "platform_group_id"]),
        "conversations": ("uq_conversation_account_external", ["platform", "account_id", "external_id"]),
        "messages": ("uq_message_account_external", ["platform", "account_id", "external_message_id"]),
        "connector_events": ("uq_connector_event_account_external", ["platform", "account_id", "external_event_id"]),
    }
    foreign_keys = {
        "contacts": "fk_contacts_account",
        "chat_groups": "fk_chat_groups_account",
        "conversations": "fk_conversations_account",
        "messages": "fk_messages_account",
        "connector_events": "fk_connector_events_account",
    }
    for table in ACCOUNT_TABLES:
        constraints = _constraint_names(table)
        fk_names = {item.get("name") for item in sa.inspect(op.get_bind()).get_foreign_keys(table)}
        index_names = {item.get("name") for item in sa.inspect(op.get_bind()).get_indexes(table)}
        with op.batch_alter_table(table) as batch:
            old_name = old_constraints[table]
            if old_name in constraints:
                batch.drop_constraint(old_name, type_="unique")
            new_name, columns = new_constraints[table]
            if new_name not in constraints:
                batch.create_unique_constraint(new_name, columns)
            if foreign_keys[table] not in fk_names:
                batch.create_foreign_key(
                    foreign_keys[table], "accounts", ["account_id"], ["id"], ondelete="SET NULL"
                )
        if f"ix_{table}_account_id" not in index_names:
            op.create_index(f"ix_{table}_account_id", table, ["account_id"], unique=False)


def downgrade() -> None:
    old_constraints = {
        "contacts": ("uq_contact_platform_user", ["platform", "platform_user_id"]),
        "chat_groups": ("uq_group_platform_id", ["platform", "platform_group_id"]),
        "conversations": ("uq_conversation_external", ["platform", "external_id"]),
        "messages": ("uq_message_external", ["platform", "external_message_id"]),
        "connector_events": ("uq_connector_event_external", ["platform", "external_event_id"]),
    }
    new_names = {
        "contacts": "uq_contact_account_user",
        "chat_groups": "uq_group_account_id",
        "conversations": "uq_conversation_account_external",
        "messages": "uq_message_account_external",
        "connector_events": "uq_connector_event_account_external",
    }
    foreign_keys = {
        "contacts": "fk_contacts_account",
        "chat_groups": "fk_chat_groups_account",
        "conversations": "fk_conversations_account",
        "messages": "fk_messages_account",
        "connector_events": "fk_connector_events_account",
    }
    for table in reversed(ACCOUNT_TABLES):
        try:
            op.drop_index(f"ix_{table}_account_id", table_name=table)
        except Exception:
            pass
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(new_names[table], type_="unique")
            batch.drop_constraint(foreign_keys[table], type_="foreignkey")
            old_name, columns = old_constraints[table]
            batch.create_unique_constraint(old_name, columns)
            batch.drop_column("account_id")
    with op.batch_alter_table("contacts") as batch:
        for name in (
            "media_ai_reply_enabled",
            "media_storage_enabled",
            "reply_auto_end",
            "reply_auto_start",
            "reply_time_window_enabled",
        ):
            batch.drop_column(name)
