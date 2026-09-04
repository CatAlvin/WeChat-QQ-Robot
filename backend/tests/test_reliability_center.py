from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.core.clock import beijing_now, utc_now
from app.core.enums import MessageAuthor, MessageDirection, MessageStatus
from app.llm.base import LLMResponse
from app.models.entities import (
    AuditLog,
    Contact,
    Conversation,
    Message,
    OutboundDeliveryEvent,
    ProviderConfig,
    RelationshipReminder,
)
from app.services.outbox import outbox_snapshot, record_delivery_event, safe_retry_decision
from app.services.provider_health import model_health_snapshot, record_provider_failure, record_provider_success
from app.services.relationship_assistant import sync_relationship_reminders


def _conversation(db, contact: Contact, suffix: str = "reliability") -> Conversation:
    conversation = Conversation(
        platform=contact.platform,
        external_id=f"conversation-{suffix}",
        contact_id=contact.id,
    )
    db.add(conversation)
    db.flush()
    return conversation


def test_outbox_only_retries_a_provably_pre_send_failure(db):
    contact = Contact(platform="QQ_NAPCAT", platform_user_id="10001", display_name="测试联系人")
    db.add(contact)
    db.flush()
    conversation = _conversation(db, contact)
    message = Message(
        platform="QQ_NAPCAT",
        external_message_id="outbound-safe-retry",
        conversation_id=conversation.id,
        contact_id=contact.id,
        direction=MessageDirection.OUTBOUND,
        author=MessageAuthor.AI,
        content="这条消息尚未触达通道",
        status=MessageStatus.FAILED,
        policy_reason="CONNECTOR_MISSING",
    )
    db.add(message)
    db.flush()

    allowed, reason = safe_retry_decision(message, [])
    assert allowed is True
    assert "尚未调用" in reason
    assert outbox_snapshot(db)[0]["retry_allowed"] is True

    record_delivery_event(
        db,
        message,
        stage="SEND_STARTED",
        status="IN_FLIGHT",
        delivery_started=True,
    )
    events = list(db.scalars(select(OutboundDeliveryEvent).where(OutboundDeliveryEvent.message_id == message.id)))
    allowed, reason = safe_retry_decision(message, events)
    assert allowed is False
    assert "避免重复" in reason
    assert outbox_snapshot(db)[0]["retry_allowed"] is False


def test_relationship_assistant_is_dashboard_only_and_deduplicated(db):
    contact = Contact(
        platform="QQ_NAPCAT",
        platform_user_id="10002",
        display_name="关系提醒联系人",
        birthday_mmdd=beijing_now().strftime("%m-%d"),
        relationship_reminders_enabled=True,
        dormant_reminder_days=3,
    )
    db.add(contact)
    db.flush()
    conversation = _conversation(db, contact, "reminders")
    db.add_all(
        [
            Message(
                platform="QQ_NAPCAT",
                external_message_id="old-inbound",
                conversation_id=conversation.id,
                contact_id=contact.id,
                direction=MessageDirection.INBOUND,
                author=MessageAuthor.CONTACT,
                content="等你有空回复",
                status=MessageStatus.RECEIVED,
                event_at=utc_now() - timedelta(days=5),
                created_at=utc_now() - timedelta(days=5),
            ),
            Message(
                platform="QQ_NAPCAT",
                external_message_id="human-commitment",
                conversation_id=conversation.id,
                contact_id=contact.id,
                direction=MessageDirection.OUTBOUND,
                author=MessageAuthor.HUMAN,
                content="我会把资料整理好",
                status=MessageStatus.SENT,
                event_at=utc_now() - timedelta(days=2),
                created_at=utc_now() - timedelta(days=2),
            ),
        ]
    )
    db.flush()

    first_created = sync_relationship_reminders(db)
    db.flush()
    first_count = len(list(db.scalars(select(RelationshipReminder))))
    second_created = sync_relationship_reminders(db)
    db.flush()

    kinds = set(db.scalars(select(RelationshipReminder.kind)))
    assert first_created >= 2
    assert {"BIRTHDAY", "COMMITMENT"} <= kinds
    assert second_created == 0
    assert len(list(db.scalars(select(RelationshipReminder)))) == first_count
    assert db.scalar(select(Message).where(Message.external_message_id.like("relationship-reminder:%"))) is None


def test_model_health_records_actual_fallback_and_sanitizes_failures(db):
    primary = ProviderConfig(
        name="Primary Cloud",
        provider_type="OPENAI_COMPATIBLE",
        base_url="https://example.invalid/v1",
        model="primary-model",
        enabled=True,
        priority=10,
    )
    fallback = ProviderConfig(
        name="Qwen Local",
        provider_type="OLLAMA",
        base_url="http://127.0.0.1:11434",
        model="qwen-local",
        enabled=True,
        priority=80,
    )
    db.add_all([primary, fallback])
    db.flush()
    record_provider_failure(db, primary.name, RuntimeError("Authorization: Bearer pretend-secret"))
    record_provider_success(
        db,
        LLMResponse(content="你好", provider=fallback.name, model=fallback.model, latency_ms=42),
    )
    db.add(
        AuditLog(
            event="LLM_RESPONSE",
            detail={"provider": fallback.name, "model": fallback.model},
        )
    )
    db.flush()

    snapshot = model_health_snapshot(db)
    primary_state = next(item for item in snapshot["providers"] if item["id"] == primary.id)
    assert primary_state["status"] == "DEGRADED"
    assert "pretend-secret" not in primary_state["last_error_detail"]
    assert snapshot["primary_provider"] == primary.name
    assert snapshot["actual_provider"] == fallback.name
    assert snapshot["fallback_active"] is True
    assert snapshot["pure_local"] is False
