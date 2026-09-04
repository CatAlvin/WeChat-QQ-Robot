from __future__ import annotations

from pathlib import Path
import importlib.util

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.models.entities import Contact


def test_reliability_migration_does_not_default_mysql_text_columns(monkeypatch):
    backend_root = Path(__file__).resolve().parents[1]
    migration_path = backend_root / "alembic" / "versions" / "20260830_0014_reliability_experience.py"
    spec = importlib.util.spec_from_file_location("reliability_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    created_tables: dict[str, tuple[object, ...]] = {}
    monkeypatch.setattr(migration, "_add_if_missing", lambda *args, **kwargs: None)
    monkeypatch.setattr(migration, "_table_exists", lambda name: False)
    monkeypatch.setattr(
        migration.op,
        "create_table",
        lambda name, *items, **kwargs: created_tables.setdefault(name, items),
    )
    monkeypatch.setattr(migration.op, "create_index", lambda *args, **kwargs: None)

    migration.upgrade()

    reminder_columns = {
        item.name: item
        for item in created_tables["relationship_reminders"]
        if hasattr(item, "name")
    }
    assert reminder_columns["detail"].server_default is None


def test_blank_sqlite_upgrades_to_chat_admin_head(tmp_path):
    backend_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "migration.db"
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    config.attributes["database_url"] = f"sqlite:///{database_path.as_posix()}"
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        columns = {item["name"]: item for item in inspect(engine).get_columns("messages")}
        assert {"sender_id", "receiver_id", "event_at", "raw_envelope"} <= columns.keys()
        assert columns["event_at"]["nullable"] is False
        runtime_columns = {item["name"]: item for item in inspect(engine).get_columns("runtime_state")}
        assert {
            "live_time_window_enabled",
            "live_auto_start",
            "live_auto_end",
            "media_storage_enabled",
            "media_save_images",
            "media_save_audio",
            "media_save_files",
            "media_ai_reply_enabled",
            "media_max_file_mb",
            "media_understanding_enabled",
            "media_understand_images",
            "media_transcribe_audio",
            "media_extract_documents",
            "media_vision_model",
            "media_whisper_model",
            "media_whisper_device",
            "media_whisper_allow_download",
            "media_understanding_max_chars",
            "kimi_media_upload_enabled",
            "kimi_media_upload_images",
            "kimi_media_upload_videos",
            "kimi_media_max_file_mb",
            "tts_enabled",
            "tts_reply_to_audio_only",
            "tts_voice",
            "tts_rate",
            "tts_volume",
            "tts_max_chars",
            "napcat_quote_reply_enabled",
        } <= runtime_columns.keys()
        assert "message_attachments" in inspect(engine).get_table_names()
        attachment_columns = {item["name"] for item in inspect(engine).get_columns("message_attachments")}
        assert {
            "analysis_status",
            "analysis_provider",
            "analysis_model",
            "analysis_text",
            "analysis_error_code",
            "analysis_metadata",
            "analyzed_at",
        } <= attachment_columns
        contact_columns = {item["name"] for item in inspect(engine).get_columns("contacts")}
        assert {
            "keepalive_enabled",
            "keepalive_time",
            "keepalive_account_id",
            "keepalive_last_attempt_on",
            "keepalive_last_sent_at",
            "keepalive_last_status",
            "keepalive_last_content",
            "keepalive_last_error",
            "account_id",
            "reply_time_window_enabled",
            "reply_auto_start",
            "reply_auto_end",
            "media_storage_enabled",
            "media_ai_reply_enabled",
            "birthday_mmdd",
            "relationship_reminders_enabled",
            "dormant_reminder_days",
        } <= contact_columns
        assert {"provider_health", "outbound_delivery_events", "relationship_reminders"} <= set(
            inspect(engine).get_table_names()
        )
        for table in ("chat_groups", "conversations", "messages", "connector_events"):
            assert "account_id" in {item["name"] for item in inspect(engine).get_columns(table)}
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260831_0016"
            assert {
                "admin_memory_import_batches",
                "admin_memory_import_items",
            } <= set(inspect(engine).get_table_names())
        assert {
            "background_tasks",
            "recovery_items",
            "knowledge_documents",
            "todo_items",
            "daily_digests",
            "sticker_assets",
        } <= set(inspect(engine).get_table_names())
        todo_columns = {item["name"] for item in inspect(engine).get_columns("todo_items")}
        assert {
            "kind",
            "account_id",
            "admin_contact_id",
            "admin_notification_message_id",
            "response_message_id",
            "delivery_status",
            "response_text",
            "last_error",
        } <= todo_columns
        memory_columns = {item["name"] for item in inspect(engine).get_columns("memories")}
        assert {"review_status", "pinned", "expires_at", "updated_at"} <= memory_columns
        sticker_columns = {item["name"] for item in inspect(engine).get_columns("sticker_assets")}
        assert {"source_kind", "source_ref"} <= sticker_columns
    finally:
        engine.dispose()


def test_existing_primary_napcat_contact_becomes_default_admin(tmp_path):
    backend_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "existing.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    config.attributes["database_url"] = database_url
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "20260826_0008")

    engine = create_engine(database_url)
    try:
        with Session(engine) as db:
            db.add(
                Contact(
                    platform="QQ_NAPCAT",
                    platform_user_id="1032556054",
                    display_name="程澜喵！",
                    relationship_label="朋友",
                    whitelisted=False,
                    ai_enabled=False,
                )
            )
            db.commit()
        command.upgrade(config, "head")
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT relationship_label, whitelisted, ai_enabled FROM contacts "
                    "WHERE platform = 'QQ_NAPCAT' AND platform_user_id = '1032556054'"
                )
            ).one()
            assert tuple(row) == ("管理员", 1, 1)
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260831_0016"
    finally:
        engine.dispose()
