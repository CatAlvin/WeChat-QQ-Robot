from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.api.routes import acceptance_readiness, conversation_messages, dashboard, list_conversations, update_media_policy, usage
from app.channels.base import InboundEvent
from app.channels.simulator import SimulatorConnector
from app.core.config import Settings
from app.core.enums import GlobalMode, Importance, MessageAuthor, MessageDirection, MessageStatus
from app.llm.gateway import LLMGateway
from app.llm.providers import SimulatorProvider
from app.main import health
from app.models.entities import Account, AuditLog, Contact, Conversation, Message, PersonaProfile, ProviderConfig, RuntimeState
from app.schemas import MediaPolicyUpdate
from app.services.pipeline import MessagePipeline


@pytest.mark.asyncio
async def test_dashboard_includes_ai_runtime_status(db):
    db.add(RuntimeState(id=1, release_gate="SIMULATION"))
    db.commit()
    result = await dashboard(db)
    ai = next(item for item in result.channels if item["platform"] == "AI")
    assert ai["status"] == "SIMULATOR"
    assert ai["configured_providers"] == 0


@pytest.mark.asyncio
async def test_media_policy_is_configurable_and_audited(db):
    db.add(RuntimeState(id=1))
    db.commit()
    result = await update_media_policy(
        MediaPolicyUpdate(
            storage_enabled=True,
            save_images=True,
            save_audio=False,
            save_files=True,
            ai_reply_enabled=True,
            max_file_mb=80,
            understanding_enabled=True,
            understand_images=True,
            transcribe_audio=True,
            extract_documents=True,
            vision_model="qwen3-vl:4b",
            whisper_model="small",
            whisper_device="cuda",
            whisper_allow_download=False,
            understanding_max_chars=7000,
            kimi_upload_enabled=True,
            kimi_upload_images=True,
            kimi_upload_videos=False,
            kimi_max_file_mb=60,
        ),
        db,
    )
    assert result.media_save_audio is False
    assert result.media_ai_reply_enabled is True
    assert result.media_max_file_mb == 80
    assert result.media_understanding_enabled is True
    assert result.media_whisper_device == "cuda"
    assert result.media_understanding_max_chars == 7000
    assert result.kimi_media_upload_enabled is True
    assert result.kimi_media_upload_videos is False
    assert result.kimi_media_max_file_mb == 60
    audit = db.scalar(select(AuditLog).where(AuditLog.event == "MEDIA_POLICY_CHANGED"))
    assert audit is not None
    assert audit.detail["max_file_mb"] == 80
    assert audit.detail["vision_model"] == "qwen3-vl:4b"
    assert audit.detail["kimi_upload_enabled"] is True
    assert audit.detail["kimi_max_file_mb"] == 60


def test_health_reports_live_database_state_instead_of_startup_default(db):
    db.add(RuntimeState(id=1, global_mode=GlobalMode.SILENT, release_gate="SHADOW", kill_switch=True))
    db.commit()
    result = health(db)
    assert result["release_gate"] == "SHADOW"
    assert result["global_mode"] == "SILENT"
    assert result["kill_switch"] is True


@pytest.mark.asyncio
async def test_human_message_envelope_and_conversation_context_are_complete(db):
    db.add(RuntimeState(id=1, release_gate="SIMULATION"))
    contact = Contact(
        platform="SIMULATOR",
        platform_user_id="visible-user",
        display_name="可见联系人",
        relationship_label="朋友",
        whitelisted=True,
        importance=Importance.IMPORTANT,
        ai_enabled=True,
        memory_enabled=False,
    )
    db.add_all([contact, PersonaProfile(id=1)])
    db.commit()
    timestamp = datetime(2026, 8, 24, 12, 34, tzinfo=timezone(timedelta(hours=8)))
    pipeline = MessagePipeline(
        gateway=LLMGateway([SimulatorProvider()]),
        connectors={"SIMULATOR": SimulatorConnector()},
        settings=Settings(),
    )
    result = await pipeline.handle(
        InboundEvent(
            platform="SIMULATOR",
            message_id="human-envelope-1",
            conversation_id="sim:visible-user",
            sender_id=contact.platform_user_id,
            sender_name=contact.display_name,
            content="这条是本人发送的消息",
            author=MessageAuthor.HUMAN,
            timestamp=timestamp,
        ),
        db,
    )
    assert result.code == "HUMAN_TAKEOVER"
    stored = db.scalar(select(Message).where(Message.external_message_id == "human-envelope-1"))
    assert stored is not None
    assert stored.direction == MessageDirection.OUTBOUND
    assert stored.sender_id == "SIMULATOR:USER"
    assert stored.receiver_id == contact.platform_user_id

    conversation = db.scalar(select(Conversation).where(Conversation.external_id == "sim:visible-user"))
    messages = conversation_messages(conversation.id, db)
    assert messages[0]["message_type"] == "TEXT"
    assert messages[0]["event_at"] is not None
    rows = list_conversations(db)
    assert rows[0]["whitelisted"] is True
    assert rows[0]["importance"] == "IMPORTANT"
    assert rows[0]["memory_enabled"] is False
    assert rows[0]["model"] == "尚未生成"


def test_conversation_messages_supports_keyset_lazy_loading(db):
    contact = Contact(platform="SIMULATOR", platform_user_id="lazy-user", display_name="懒加载联系人")
    db.add(contact)
    db.flush()
    conversation = Conversation(platform="SIMULATOR", external_id="sim:lazy-user", contact_id=contact.id)
    db.add(conversation)
    db.flush()
    base = datetime(2026, 8, 28, tzinfo=timezone.utc)
    for index in range(65):
        db.add(
            Message(
                id=f"{index:036d}",
                platform="SIMULATOR",
                external_message_id=f"lazy-{index}",
                conversation_id=conversation.id,
                contact_id=contact.id,
                content=f"消息 {index}",
                created_at=base + timedelta(seconds=index),
                event_at=base + timedelta(seconds=index),
            )
        )
    db.flush()

    latest = conversation_messages(conversation.id, db, limit=41)
    assert len(latest) == 41
    assert latest[0]["content"] == "消息 24"
    assert latest[-1]["content"] == "消息 64"

    older = conversation_messages(
        conversation.id,
        db,
        limit=41,
        before_created_at=latest[1]["created_at"],
        before_id=latest[1]["id"],
    )
    assert older[0]["content"] == "消息 0"
    assert older[-1]["content"] == "消息 24"


def test_usage_reports_seven_day_and_provider_breakdown(db):
    contact = Contact(platform="SIMULATOR", platform_user_id="usage-user", display_name="用量联系人")
    db.add(contact)
    db.flush()
    conversation = Conversation(platform="SIMULATOR", external_id="sim:usage-user", contact_id=contact.id)
    db.add(conversation)
    db.flush()
    db.add(
        Message(
            platform="SIMULATOR",
            external_message_id="usage-1",
            conversation_id=conversation.id,
            contact_id=contact.id,
            direction=MessageDirection.OUTBOUND,
            author=MessageAuthor.AI,
            content="用量测试",
            status=MessageStatus.SENT,
            provider="DeepSeek",
            model="deepseek-v4-flash",
            input_tokens=120,
            output_tokens=30,
            created_at=datetime.now(timezone.utc),
        )
    )
    db.commit()
    result = usage(db)
    assert len(result["last_7_days"]) == 7
    assert result["today"] == {"messages": 1, "input_tokens": 120, "output_tokens": 30, "total_tokens": 150}
    assert result["by_model"][0]["provider"] == "DeepSeek"
    assert result["by_model"][0]["total_tokens"] == 150


def test_external_readiness_exposes_evidence_without_credentials(db):
    db.add(RuntimeState(id=1, release_gate="SIMULATION"))
    db.add(Account(platform="QQ", display_name="QQ", connector_kind="QQ_OFFICIAL_BOT", enabled=False))
    provider = ProviderConfig(
        name="Local Ollama",
        provider_type="OLLAMA",
        base_url="http://127.0.0.1:11434",
        model="qwen3:8b",
        enabled=True,
    )
    db.add(provider)
    db.flush()
    db.add(AuditLog(event="PROVIDER_TESTED", detail={"provider_id": provider.id, "provider": provider.name, "ok": True}))
    db.commit()

    result = acceptance_readiness(db)
    checks = {item["id"]: item for item in result["checks"]}
    assert result["ready"] is False
    assert checks["ollama"]["ready"] is True
    assert checks["deepseek"]["ready"] is False
    assert checks["openai"]["ready"] is False
    assert checks["mysql"]["ready"] is False
    assert checks["qq_configured"]["ready"] is False
    assert all("secret" not in str(value).lower() for value in result.values())
