from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.enums import GlobalMode, Importance


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class BootstrapRequest(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=10, max_length=200)


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 28800


class RuntimeOut(ORMModel):
    global_mode: str
    kill_switch: bool
    release_gate: str
    simulation_accepted: bool
    shadow_accepted: bool
    live_time_window_enabled: bool
    live_auto_start: str
    live_auto_end: str
    media_storage_enabled: bool
    media_save_images: bool
    media_save_audio: bool
    media_save_files: bool
    media_ai_reply_enabled: bool
    media_max_file_mb: int
    media_understanding_enabled: bool
    media_understand_images: bool
    media_transcribe_audio: bool
    media_extract_documents: bool
    media_vision_model: str
    media_whisper_model: str
    media_whisper_device: str
    media_whisper_allow_download: bool
    media_understanding_max_chars: int
    kimi_media_upload_enabled: bool
    kimi_media_upload_images: bool
    kimi_media_upload_videos: bool
    kimi_media_max_file_mb: int
    tts_enabled: bool
    tts_reply_to_audio_only: bool
    tts_voice: str
    tts_rate: int
    tts_volume: int
    tts_max_chars: int
    napcat_quote_reply_enabled: bool
    updated_at: datetime


class ModeUpdate(BaseModel):
    mode: GlobalMode


class KillSwitchUpdate(BaseModel):
    enabled: bool


class LiveTimeWindowUpdate(BaseModel):
    enabled: bool
    start: str
    end: str
    disable_confirmed: bool = False

    @field_validator("start", "end")
    @classmethod
    def valid_clock_time(cls, value: str) -> str:
        try:
            parsed = datetime.strptime(value, "%H:%M")
        except ValueError as exc:
            raise ValueError("时间必须使用 HH:MM 格式") from exc
        normalized = parsed.strftime("%H:%M")
        if value != normalized:
            raise ValueError("时间必须使用两位 HH:MM 格式")
        return normalized

    @model_validator(mode="after")
    def different_boundaries(self) -> "LiveTimeWindowUpdate":
        if self.start == self.end:
            raise ValueError("开始时间与结束时间不能相同")
        return self


class MediaPolicyUpdate(BaseModel):
    storage_enabled: bool
    save_images: bool
    save_audio: bool
    save_files: bool
    ai_reply_enabled: bool
    max_file_mb: int = Field(ge=1, le=500)
    understanding_enabled: bool = False
    understand_images: bool = True
    transcribe_audio: bool = True
    extract_documents: bool = True
    vision_model: str = Field(default="qwen3-vl:4b", min_length=1, max_length=160)
    whisper_model: Literal["tiny", "base", "small", "medium", "large-v3", "turbo"] = "small"
    whisper_device: Literal["auto", "cuda", "cpu"] = "auto"
    whisper_allow_download: bool = False
    understanding_max_chars: int = Field(default=6000, ge=500, le=20000)
    kimi_upload_enabled: bool = False
    kimi_upload_images: bool = True
    kimi_upload_videos: bool = True
    kimi_max_file_mb: int = Field(default=50, ge=1, le=100)
    tts_enabled: bool = False
    tts_reply_to_audio_only: bool = True
    tts_voice: str = Field(default="", max_length=120)
    tts_rate: int = Field(default=1, ge=-3, le=4)
    tts_volume: int = Field(default=92, ge=1, le=100)
    tts_max_chars: int = Field(default=300, ge=30, le=1000)
    napcat_quote_reply_enabled: bool = False

    @field_validator("whisper_model", mode="before")
    @classmethod
    def normalize_whisper_medium_alias(cls, value: Any) -> Any:
        return "medium" if str(value).strip().casefold() == "median" else value


class ReleaseGateUpdate(BaseModel):
    gate: Literal["SIMULATION", "SHADOW", "LIVE"]


class StageAcceptance(BaseModel):
    confirmed: bool


class ServiceActionRequest(BaseModel):
    action: Literal["start", "stop", "restart"]
    confirmed: bool = False


class OutboxRetryRequest(BaseModel):
    confirmed: bool = False


class RelationshipReminderAction(BaseModel):
    action: Literal["DONE", "DISMISS", "SNOOZE"]
    snooze_days: int = Field(default=3, ge=1, le=30)


class ConversationManualSend(BaseModel):
    content: str = Field(min_length=1, max_length=4000)

    @field_validator("content")
    @classmethod
    def non_blank_content(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("消息内容不能为空")
        return normalized


class ContactCreate(BaseModel):
    platform: Literal["SIMULATOR", "QQ", "QQ_NAPCAT", "WECHAT_AUTOWX", "WECHAT"] = "SIMULATOR"
    platform_user_id: str = Field(min_length=1, max_length=160)
    display_name: str = Field(min_length=1, max_length=120)
    relationship_label: str = Field(default="朋友", max_length=80)
    whitelisted: bool = False
    importance: Importance = Importance.NORMAL
    ai_enabled: bool = True
    style_profile: str = Field(default="", max_length=2000)
    custom_prompt: str = Field(default="", max_length=4000)
    memory_enabled: bool = True
    account_id: str | None = Field(default=None, max_length=36)
    birthday_mmdd: str | None = None
    relationship_reminders_enabled: bool = True
    dormant_reminder_days: int = Field(default=14, ge=3, le=365)

    @field_validator("birthday_mmdd")
    @classmethod
    def valid_birthday(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        try:
            datetime.strptime(f"2000-{value}", "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("生日必须使用 MM-DD 格式") from exc
        return value


class ContactPatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    relationship_label: str | None = Field(default=None, max_length=80)
    whitelisted: bool | None = None
    importance: Importance | None = None
    ai_enabled: bool | None = None
    style_profile: str | None = Field(default=None, max_length=2000)
    custom_prompt: str | None = Field(default=None, max_length=4000)
    memory_enabled: bool | None = None
    reply_time_window_enabled: bool | None = None
    reply_auto_start: str | None = None
    reply_auto_end: str | None = None
    media_storage_enabled: bool | None = None
    media_ai_reply_enabled: bool | None = None
    keepalive_enabled: bool | None = None
    keepalive_time: str | None = None
    keepalive_account_id: str | None = Field(default=None, max_length=36)
    birthday_mmdd: str | None = None
    relationship_reminders_enabled: bool | None = None
    dormant_reminder_days: int | None = Field(default=None, ge=3, le=365)

    @field_validator("keepalive_time", "reply_auto_start", "reply_auto_end")
    @classmethod
    def valid_keepalive_time(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = datetime.strptime(value, "%H:%M")
        except ValueError as exc:
            raise ValueError("时间必须使用 HH:MM 格式") from exc
        normalized = parsed.strftime("%H:%M")
        if value != normalized:
            raise ValueError("时间必须使用两位 HH:MM 格式")
        return normalized

    @field_validator("keepalive_account_id", mode="before")
    @classmethod
    def empty_keepalive_account_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("birthday_mmdd")
    @classmethod
    def valid_patch_birthday(cls, value: str | None) -> str | None:
        return ContactCreate.valid_birthday(value)


class ContactOut(ORMModel):
    id: str
    platform: str
    account_id: str | None
    platform_user_id: str
    display_name: str
    relationship_label: str
    whitelisted: bool
    importance: str
    ai_enabled: bool
    style_profile: str
    custom_prompt: str
    memory_enabled: bool
    reply_time_window_enabled: bool | None
    reply_auto_start: str | None
    reply_auto_end: str | None
    media_storage_enabled: bool | None
    media_ai_reply_enabled: bool | None
    keepalive_enabled: bool
    keepalive_time: str
    keepalive_account_id: str | None
    keepalive_last_sent_at: datetime | None
    keepalive_last_status: str
    keepalive_last_content: str | None
    keepalive_last_error: str | None
    birthday_mmdd: str | None
    relationship_reminders_enabled: bool
    dormant_reminder_days: int
    created_at: datetime
    updated_at: datetime


class ChatExportRequest(BaseModel):
    contact_ids: list[str] = Field(min_length=1, max_length=100)
    start_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc) - timedelta(days=30))
    end_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    include_contact_messages: bool = True
    include_ai_messages: bool = True
    include_human_messages: bool = True
    format: Literal["PDF", "TXT", "MARKDOWN"] = "MARKDOWN"
    include_attachments: bool = True
    output_directory: str | None = Field(default=None, max_length=1000)
    max_messages: int = Field(default=5000, ge=1, le=50000)

    @field_validator("contact_ids")
    @classmethod
    def unique_contacts(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("联系人 ID 不能为空")
        if len(value) != len(set(value)):
            raise ValueError("联系人不能重复选择")
        return value

    @model_validator(mode="after")
    def valid_export_window(self) -> "ChatExportRequest":
        if self.end_at < self.start_at:
            raise ValueError("结束时间不能早于开始时间")
        if not any((self.include_contact_messages, self.include_ai_messages, self.include_human_messages)):
            raise ValueError("至少选择一种消息来源")
        return self


class ChatExportOut(BaseModel):
    output_directory: str
    record_files: list[str]
    archive_file: str | None
    contact_count: int
    message_count: int
    attachment_count: int
    missing_attachment_count: int
    truncated: bool


class GroupCreate(BaseModel):
    platform: Literal["SIMULATOR", "QQ", "QQ_NAPCAT", "WECHAT_AUTOWX", "WECHAT"] = "SIMULATOR"
    platform_group_id: str = Field(min_length=1, max_length=160)
    display_name: str = Field(min_length=1, max_length=120)
    allowed: bool = False
    ai_enabled: bool = False
    account_id: str | None = Field(default=None, max_length=36)


class GroupPatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    allowed: bool | None = None
    ai_enabled: bool | None = None


class GroupOut(ORMModel):
    id: str
    platform: str
    account_id: str | None
    platform_group_id: str
    display_name: str
    allowed: bool
    ai_enabled: bool
    created_at: datetime


class CredentialCreate(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    provider: str = Field(min_length=1, max_length=60)
    secret: str = Field(min_length=6, max_length=4096)


class CredentialUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=120)
    secret: str | None = Field(default=None, min_length=6, max_length=4096)


class CredentialOut(BaseModel):
    id: str
    label: str
    provider: str
    masked_hint: str
    created_at: datetime
    updated_at: datetime


class ProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    provider_type: Literal["DEEPSEEK", "KIMI", "QWEN", "OPENAI", "OLLAMA", "OPENAI_COMPATIBLE"]
    base_url: str = Field(min_length=8, max_length=500)
    model: str = Field(min_length=1, max_length=160)
    credential_id: str | None = None
    enabled: bool = True
    priority: int = Field(default=100, ge=1, le=1000)
    timeout_seconds: float = Field(default=25, ge=3, le=120)

    @field_validator("base_url")
    @classmethod
    def safe_url(cls, value: str) -> str:
        if not value.startswith(("http://127.0.0.1", "http://localhost", "https://")):
            raise ValueError("仅允许 HTTPS，或本机 Ollama 的 HTTP 地址")
        return value.rstrip("/")


class ProviderUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    provider_type: Literal["DEEPSEEK", "KIMI", "QWEN", "OPENAI", "OLLAMA", "OPENAI_COMPATIBLE"] | None = None
    base_url: str | None = Field(default=None, min_length=8, max_length=500)
    model: str | None = Field(default=None, min_length=1, max_length=160)
    credential_id: str | None = None
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=1, le=1000)
    timeout_seconds: float | None = Field(default=None, ge=3, le=120)

    @field_validator("base_url")
    @classmethod
    def safe_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith(("http://127.0.0.1", "http://localhost", "https://")):
            raise ValueError("仅允许 HTTPS，或本机 Ollama 的 HTTP 地址")
        return value.rstrip("/")


class ProviderReorder(BaseModel):
    strategy: Literal["MANUAL", "CAPABILITY"] = "MANUAL"
    ordered_ids: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("ordered_ids")
    @classmethod
    def unique_provider_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("模型顺序不能包含重复项")
        return value


class ProviderOut(ORMModel):
    id: str
    name: str
    provider_type: str
    base_url: str
    model: str
    credential_id: str | None
    enabled: bool
    priority: int
    timeout_seconds: float
    created_at: datetime


class PersonaUpdate(BaseModel):
    global_persona: str = Field(max_length=4000)
    user_style: str = Field(max_length=4000)
    safety_policy: str = Field(max_length=4000)


class PersonaOut(ORMModel):
    global_persona: str
    user_style: str
    safety_policy: str
    updated_at: datetime


class MemoryCreate(BaseModel):
    kind: Literal["PREFERENCE", "PERSONAL_FACT", "RELATIONSHIP_CONTEXT", "ONGOING_TOPIC", "CONVERSATION_CONVENTION"]
    content: str = Field(min_length=1, max_length=500)


class MemoryOut(ORMModel):
    id: str
    contact_id: str
    kind: str
    content: str
    review_status: str
    pinned: bool
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MemoryReviewUpdate(BaseModel):
    status: Literal["PENDING", "APPROVED", "REJECTED"]
    pinned: bool | None = None
    expires_at: datetime | None = None


class MemoryUpdate(BaseModel):
    kind: Literal["PREFERENCE", "PERSONAL_FACT", "RELATIONSHIP_CONTEXT", "ONGOING_TOPIC", "CONVERSATION_CONVENTION"]
    content: str = Field(min_length=1, max_length=500)


class TaskCreate(BaseModel):
    kind: Literal["DAILY_DIGEST", "KNOWLEDGE_REFRESH", "RECOVERY_SYNC"]
    title: str = Field(default="后台任务", min_length=1, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)
    max_attempts: int = Field(default=3, ge=1, le=10)


class TaskOut(ORMModel):
    id: str
    kind: str
    title: str
    status: str
    payload: dict[str, Any]
    progress: int
    result: dict[str, Any]
    error_code: str | None
    error_detail: str | None
    attempts: int
    max_attempts: int
    available_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RecoveryOut(ORMModel):
    id: str
    source_type: str
    source_id: str
    kind: str
    status: str
    contact_id: str | None
    conversation_id: str | None
    error_code: str | None
    error_detail: str | None
    retry_count: int
    last_retry_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RecoveryAction(BaseModel):
    action: Literal["RETRY", "DISMISS"]
    confirmed: bool = False


class KnowledgeCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    source_name: str = Field(default="手动录入", min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=200000)
    enabled: bool = True


class KnowledgePatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = Field(default=None, min_length=1, max_length=200000)
    enabled: bool | None = None


class KnowledgeOut(ORMModel):
    id: str
    title: str
    source_name: str
    content: str
    sha256: str
    status: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class TodoCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    detail: str = Field(default="", max_length=8000)
    priority: Literal["LOW", "NORMAL", "HIGH"] = "NORMAL"
    due_at: datetime | None = None
    reminder_enabled: bool = False
    contact_id: str | None = None


class TodoPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=240)
    detail: str | None = Field(default=None, max_length=8000)
    status: Literal["OPEN", "DONE", "CANCELLED"] | None = None
    priority: Literal["LOW", "NORMAL", "HIGH"] | None = None
    due_at: datetime | None = None
    reminder_enabled: bool | None = None


class TodoOut(ORMModel):
    id: str
    title: str
    detail: str
    status: str
    priority: str
    due_at: datetime | None
    reminder_enabled: bool
    contact_id: str | None
    source_message_id: str | None
    kind: str
    account_id: str | None
    admin_contact_id: str | None
    admin_notification_message_id: str | None
    response_message_id: str | None
    delivery_status: str
    response_text: str
    last_error: str | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DigestOut(ORMModel):
    id: str
    local_date: str
    status: str
    content: str
    metrics: dict[str, Any]
    generated_at: datetime


class StickerCreate(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    tags: list[str] = Field(default_factory=list, max_length=20)
    file_name: str = Field(min_length=1, max_length=255)
    mime_type: Literal["image/png", "image/jpeg", "image/gif", "image/webp"]
    data_base64: str = Field(min_length=4, max_length=10_000_000)
    enabled: bool = True
    auto_reply_enabled: bool = False

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: list[str]) -> list[str]:
        output: list[str] = []
        for item in value:
            normalized = item.strip()[:40]
            if normalized and normalized not in output:
                output.append(normalized)
        return output


class StickerPatch(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=120)
    tags: list[str] | None = Field(default=None, max_length=20)
    enabled: bool | None = None
    auto_reply_enabled: bool | None = None

    @field_validator("tags")
    @classmethod
    def normalize_patch_tags(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else StickerCreate.normalize_tags(value)


class StickerOut(ORMModel):
    id: str
    label: str
    tags: list[str]
    mime_type: str
    sha256: str
    source_kind: str
    source_ref: str | None
    enabled: bool
    auto_reply_enabled: bool
    use_count: int
    created_at: datetime
    updated_at: datetime


class StickerCacheCandidateOut(BaseModel):
    id: str
    file_name: str
    mime_type: str
    size_bytes: int
    modified_at: datetime
    source_category: str


class StickerCacheScanOut(BaseModel):
    roots: list[str]
    candidates: list[StickerCacheCandidateOut]


class StickerCacheImportItem(BaseModel):
    candidate_id: str = Field(min_length=8, max_length=64)
    label: str = Field(min_length=1, max_length=120)
    tags: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("tags")
    @classmethod
    def normalize_import_tags(cls, value: list[str]) -> list[str]:
        return StickerCreate.normalize_tags(value)


class StickerCacheImportRequest(BaseModel):
    items: list[StickerCacheImportItem] = Field(min_length=1, max_length=50)


class SimulatorEventRequest(BaseModel):
    contact_id: str
    content: str = Field(min_length=1, max_length=4000)
    message_id: str | None = Field(default=None, max_length=200)
    conversation_id: str | None = Field(default=None, max_length=180)
    author: Literal["CONTACT", "HUMAN"] = "CONTACT"
    timestamp: datetime | None = None


class AccountConfigUpdate(BaseModel):
    enabled: bool
    app_id: str | None = Field(default=None, max_length=160)
    app_secret: str | None = Field(default=None, min_length=6, max_length=4096)
    api_base: str | None = Field(default=None, max_length=500)
    access_token: str | None = Field(default=None, min_length=12, max_length=4096)
    risk_acknowledged: bool = False


class NapCatProfileCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    managed_qq_id: str = Field(min_length=5, max_length=20, pattern=r"^\d+$")
    api_base: str = Field(default="http://127.0.0.1:3001", min_length=8, max_length=500)
    access_token: str = Field(min_length=12, max_length=4096)
    risk_acknowledged: bool = False


class NapCatProfileUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    managed_qq_id: str | None = Field(default=None, min_length=5, max_length=20, pattern=r"^\d+$")
    api_base: str | None = Field(default=None, min_length=8, max_length=500)
    access_token: str | None = Field(default=None, min_length=12, max_length=4096)
    risk_acknowledged: bool | None = None


class QQAcceptanceStart(BaseModel):
    contact_id: str = Field(min_length=1, max_length=36)


class DashboardOut(BaseModel):
    runtime: RuntimeOut
    channels: list[dict[str, Any]]
    metrics: dict[str, int]
    recent_incidents: list[dict[str, Any]]
