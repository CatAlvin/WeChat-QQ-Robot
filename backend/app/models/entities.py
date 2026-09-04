from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import ConversationMode, GlobalMode, Importance, MessageAuthor, MessageDirection, MessageStatus
from app.core.clock import UTCDateTime, utc_now
from app.database import Base


def utcnow() -> datetime:
    return utc_now()


def new_id() -> str:
    return str(uuid4())


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    password_salt: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class RuntimeState(Base):
    __tablename__ = "runtime_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    global_mode: Mapped[str] = mapped_column(String(20), default=GlobalMode.AUTO)
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False)
    release_gate: Mapped[str] = mapped_column(String(20), default="SIMULATION")
    simulation_accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    shadow_accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    live_time_window_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    live_auto_start: Mapped[str] = mapped_column(String(5), default="10:00")
    live_auto_end: Mapped[str] = mapped_column(String(5), default="23:30")
    media_storage_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    media_save_images: Mapped[bool] = mapped_column(Boolean, default=True)
    media_save_audio: Mapped[bool] = mapped_column(Boolean, default=True)
    media_save_files: Mapped[bool] = mapped_column(Boolean, default=True)
    media_ai_reply_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    media_max_file_mb: Mapped[int] = mapped_column(Integer, default=50)
    media_understanding_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    media_understand_images: Mapped[bool] = mapped_column(Boolean, default=True)
    media_transcribe_audio: Mapped[bool] = mapped_column(Boolean, default=True)
    media_extract_documents: Mapped[bool] = mapped_column(Boolean, default=True)
    media_vision_model: Mapped[str] = mapped_column(String(160), default="qwen3-vl:4b")
    media_whisper_model: Mapped[str] = mapped_column(String(80), default="small")
    media_whisper_device: Mapped[str] = mapped_column(String(16), default="auto")
    media_whisper_allow_download: Mapped[bool] = mapped_column(Boolean, default=False)
    media_understanding_max_chars: Mapped[int] = mapped_column(Integer, default=6000)
    kimi_media_upload_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    kimi_media_upload_images: Mapped[bool] = mapped_column(Boolean, default=True)
    kimi_media_upload_videos: Mapped[bool] = mapped_column(Boolean, default=True)
    kimi_media_max_file_mb: Mapped[int] = mapped_column(Integer, default=50)
    tts_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    tts_reply_to_audio_only: Mapped[bool] = mapped_column(Boolean, default=True)
    tts_voice: Mapped[str] = mapped_column(String(120), default="")
    tts_rate: Mapped[int] = mapped_column(Integer, default=1)
    tts_volume: Mapped[int] = mapped_column(Integer, default=92)
    tts_max_chars: Mapped[int] = mapped_column(Integer, default=300)
    napcat_quote_reply_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    connector_kind: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="DISCONNECTED")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    experimental: Mapped[bool] = mapped_column(Boolean, default=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    credential_id: Mapped[str | None] = mapped_column(String(36))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("platform", "account_id", "platform_user_id", name="uq_contact_account_user"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", name="fk_contacts_account", ondelete="SET NULL"), index=True
    )
    platform_user_id: Mapped[str] = mapped_column(String(160), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    relationship_label: Mapped[str] = mapped_column(String(80), default="朋友")
    whitelisted: Mapped[bool] = mapped_column(Boolean, default=False)
    importance: Mapped[str] = mapped_column(String(24), default=Importance.NORMAL)
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    style_profile: Mapped[str] = mapped_column(Text, default="")
    custom_prompt: Mapped[str] = mapped_column(Text, default="")
    memory_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Nullable contact-level values inherit the matching global runtime policy.
    reply_time_window_enabled: Mapped[bool | None] = mapped_column(Boolean)
    reply_auto_start: Mapped[str | None] = mapped_column(String(5))
    reply_auto_end: Mapped[str | None] = mapped_column(String(5))
    media_storage_enabled: Mapped[bool | None] = mapped_column(Boolean)
    media_ai_reply_enabled: Mapped[bool | None] = mapped_column(Boolean)
    birthday_mmdd: Mapped[str | None] = mapped_column(String(5))
    relationship_reminders_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    dormant_reminder_days: Mapped[int] = mapped_column(Integer, default=14)
    # Daily "keep the conversation alive" messages use a separate explicit
    # authorization from the normal inbound/AI whitelist.  This lets a contact
    # receive one deterministic scheduled message without granting the model
    # permission to answer arbitrary inbound content.
    keepalive_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    keepalive_time: Mapped[str] = mapped_column(String(5), default="20:00")
    keepalive_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", name="fk_contacts_keepalive_account", ondelete="SET NULL")
    )
    keepalive_last_attempt_on: Mapped[str | None] = mapped_column(String(10))
    keepalive_last_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    keepalive_last_status: Mapped[str] = mapped_column(String(24), default="NEVER")
    keepalive_last_content: Mapped[str | None] = mapped_column(Text)
    keepalive_last_error: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class ChatGroup(Base):
    __tablename__ = "chat_groups"
    __table_args__ = (UniqueConstraint("platform", "account_id", "platform_group_id", name="uq_group_account_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", name="fk_chat_groups_account", ondelete="SET NULL"), index=True
    )
    platform_group_id: Mapped[str] = mapped_column(String(160), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("platform", "account_id", "external_id", name="uq_conversation_account_external"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", name="fk_conversations_account", ondelete="SET NULL"), index=True
    )
    external_id: Mapped[str] = mapped_column(String(180), nullable=False)
    contact_id: Mapped[str | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    group_id: Mapped[str | None] = mapped_column(ForeignKey("chat_groups.id", ondelete="SET NULL"))
    mode: Mapped[str] = mapped_column(String(32), default=ConversationMode.AUTO_READY)
    human_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cooldown_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    managed_rounds: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_ai_sends: Mapped[int] = mapped_column(Integer, default=0)
    last_active_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    contact: Mapped[Contact | None] = relationship()
    group: Mapped[ChatGroup | None] = relationship()


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("platform", "account_id", "external_message_id", name="uq_message_account_external"),
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", name="fk_messages_account", ondelete="SET NULL"), index=True
    )
    external_message_id: Mapped[str] = mapped_column(String(200), nullable=False)
    sender_id: Mapped[str | None] = mapped_column(String(200))
    receiver_id: Mapped[str | None] = mapped_column(String(200))
    event_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    contact_id: Mapped[str | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    direction: Mapped[str] = mapped_column(String(16), default=MessageDirection.INBOUND)
    author: Mapped[str] = mapped_column(String(16), default=MessageAuthor.CONTACT)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    message_type: Mapped[str] = mapped_column(String(32), default="TEXT")
    status: Mapped[str] = mapped_column(String(24), default=MessageStatus.RECEIVED)
    reply_to_external_id: Mapped[str | None] = mapped_column(String(200))
    provider: Mapped[str | None] = mapped_column(String(60))
    model: Mapped[str | None] = mapped_column(String(120))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    policy_reason: Mapped[str | None] = mapped_column(String(160))
    raw_envelope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class MessageAttachment(Base):
    __tablename__ = "message_attachments"
    __table_args__ = (Index("ix_message_attachments_message", "message_id", "segment_index"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    segment_type: Mapped[str] = mapped_column(String(32), nullable=False)
    segment_index: Mapped[int] = mapped_column(Integer, nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(160))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    source_ref: Mapped[str | None] = mapped_column(Text)
    file_id: Mapped[str | None] = mapped_column(String(500))
    local_path: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="METADATA_ONLY")
    error_code: Mapped[str | None] = mapped_column(String(80))
    attachment_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    analysis_status: Mapped[str] = mapped_column(String(32), nullable=False, default="NOT_REQUESTED")
    analysis_provider: Mapped[str | None] = mapped_column(String(80))
    analysis_model: Mapped[str | None] = mapped_column(String(160))
    analysis_text: Mapped[str | None] = mapped_column(Text)
    analysis_error_code: Mapped[str | None] = mapped_column(String(100))
    analysis_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    analyzed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ConversationSummary(Base):
    __tablename__ = "conversation_summaries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Memory(Base):
    __tablename__ = "memories"
    __table_args__ = (Index("ix_memories_contact_created", "contact_id", "created_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    contact_id: Mapped[str] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))
    review_status: Mapped[str] = mapped_column(String(24), nullable=False, default="APPROVED")
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class AdminMemoryImportBatch(Base):
    __tablename__ = "admin_memory_import_batches"
    __table_args__ = (
        Index("ix_admin_memory_import_admin_status", "admin_contact_id", "status"),
        Index("ix_admin_memory_import_account_created", "account_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    admin_contact_id: Mapped[str] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False)
    target_contact_id: Mapped[str] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ACTIVE")
    started_message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))
    completed_message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))
    screenshot_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    extracted_text_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    memory_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    provider: Mapped[str | None] = mapped_column(String(80))
    model: Mapped[str | None] = mapped_column(String(160))
    last_error: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class AdminMemoryImportItem(Base):
    __tablename__ = "admin_memory_import_items"
    __table_args__ = (
        UniqueConstraint("batch_id", "attachment_id", name="uq_admin_memory_import_attachment"),
        Index("ix_admin_memory_import_items_batch_position", "batch_id", "position"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("admin_memory_import_batches.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), nullable=False)
    attachment_id: Mapped[str] = mapped_column(
        ForeignKey("message_attachments.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class BackgroundTask(Base):
    __tablename__ = "background_tasks"
    __table_args__ = (Index("ix_background_tasks_status_available", "status", "available_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(60), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    available_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class RecoveryItem(Base):
    __tablename__ = "recovery_items"
    __table_args__ = (
        UniqueConstraint("source_type", "source_id", "kind", name="uq_recovery_source_kind"),
        Index("ix_recovery_items_status_created", "status", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    source_id: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="OPEN")
    contact_id: Mapped[str | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_retry_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (Index("ix_knowledge_documents_enabled_updated", "enabled", "updated_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    source_name: Mapped[str] = mapped_column(String(255), nullable=False, default="手动录入")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="READY")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class TodoItem(Base):
    __tablename__ = "todo_items"
    __table_args__ = (Index("ix_todo_items_status_due", "status", "due_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="OPEN")
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="NORMAL")
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    reminder_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    contact_id: Mapped[str | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    source_message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="MANUAL")
    account_id: Mapped[str | None] = mapped_column(String(36), index=True)
    admin_contact_id: Mapped[str | None] = mapped_column(String(36), index=True)
    admin_notification_message_id: Mapped[str | None] = mapped_column(String(36))
    response_message_id: Mapped[str | None] = mapped_column(String(36))
    delivery_status: Mapped[str] = mapped_column(String(32), nullable=False, default="NOT_REQUIRED")
    response_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    last_error: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class DailyDigest(Base):
    __tablename__ = "daily_digests"
    __table_args__ = (UniqueConstraint("local_date", name="uq_daily_digest_local_date"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    local_date: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="READY")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    generated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class StickerAsset(Base):
    __tablename__ = "sticker_assets"
    __table_args__ = (Index("ix_sticker_assets_enabled_created", "enabled", "created_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    local_path: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(24), nullable=False, default="MANUAL")
    source_ref: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_reply_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class ApiCredential(Base):
    __tablename__ = "api_credentials"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    provider: Mapped[str] = mapped_column(String(60), nullable=False)
    encrypted_secret: Mapped[str] = mapped_column(Text, nullable=False)
    masked_hint: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class ProviderConfig(Base):
    __tablename__ = "llm_providers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    provider_type: Mapped[str] = mapped_column(String(60), nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    credential_id: Mapped[str | None] = mapped_column(ForeignKey("api_credentials.id", ondelete="SET NULL"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    timeout_seconds: Mapped[float] = mapped_column(Float, default=25.0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    credential: Mapped[ApiCredential | None] = relationship()


class ProviderHealth(Base):
    __tablename__ = "provider_health"
    __table_args__ = (UniqueConstraint("provider_id", name="uq_provider_health_provider"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider_id: Mapped[str] = mapped_column(ForeignKey("llm_providers.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="UNKNOWN")
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_failure_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_detail: Mapped[str | None] = mapped_column(Text)
    last_latency_ms: Mapped[int | None] = mapped_column(Integer)
    last_model: Mapped[str | None] = mapped_column(String(160))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class OutboundDeliveryEvent(Base):
    __tablename__ = "outbound_delivery_events"
    __table_args__ = (
        Index("ix_outbound_delivery_message_created", "message_id", "created_at"),
        Index("ix_outbound_delivery_status_created", "status", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    delivery_started: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retry_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retry_reason: Mapped[str | None] = mapped_column(String(200))
    error_code: Mapped[str | None] = mapped_column(String(100))
    external_message_id: Mapped[str | None] = mapped_column(String(200))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class RelationshipReminder(Base):
    __tablename__ = "relationship_reminders"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_relationship_reminder_dedupe"),
        Index("ix_relationship_reminder_status_due", "status", "due_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    contact_id: Mapped[str] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="OPEN")
    due_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    source_message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))
    source_memory_id: Mapped[str | None] = mapped_column(ForeignKey("memories.id", ondelete="SET NULL"))
    dedupe_key: Mapped[str] = mapped_column(String(160), nullable=False)
    admin_notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    snoozed_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class PersonaProfile(Base):
    __tablename__ = "persona_profiles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    global_persona: Mapped[str] = mapped_column(Text, default="可爱、活泼、自然，带轻微猫咪感但不过度喵喵叫。")
    user_style: Mapped[str] = mapped_column(Text, default="简洁、友好、自然。")
    safety_policy: Mapped[str] = mapped_column(Text, default="不作金钱承诺，不处理正式事务，不泄露敏感信息。")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_created", "created_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event: Mapped[str] = mapped_column(String(80), nullable=False)
    level: Mapped[str] = mapped_column(String(16), default="INFO")
    conversation_id: Mapped[str | None] = mapped_column(String(36))
    message_id: Mapped[str | None] = mapped_column(String(36))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (Index("ix_incidents_created", "created_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), default="WARNING")
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ConnectorEvent(Base):
    __tablename__ = "connector_events"
    __table_args__ = (
        UniqueConstraint("platform", "account_id", "external_event_id", name="uq_connector_event_account_external"),
        Index("ix_connector_events_status_created", "status", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", name="fk_connector_events_account", ondelete="SET NULL"), index=True
    )
    external_event_id: Mapped[str] = mapped_column(String(200), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    normalized_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="PENDING", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class AcceptanceRun(Base):
    __tablename__ = "acceptance_runs"
    __table_args__ = (Index("ix_acceptance_runs_kind_started", "kind", "started_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(40), nullable=False, default="QQ_200")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="RUNNING")
    target_contact_id: Mapped[str | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    target_platform_user_id: Mapped[str] = mapped_column(String(160), nullable=False)
    expected_messages: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

