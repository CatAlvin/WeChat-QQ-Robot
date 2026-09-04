from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from app.core.clock import as_beijing, beijing_start_of_day_utc
from app.core.enums import ConversationMode, GlobalMode, Importance, MessageAuthor, MessageDirection, MessageStatus
from app.models.entities import Contact, Conversation, Message
from app.policy.engine import PolicyEngine, PolicyInput
from app.services.chat_export import ChatExportService


def test_beijing_clock_crosses_the_utc_day_boundary() -> None:
    value = datetime(2026, 8, 27, 16, 30, tzinfo=timezone.utc)

    assert as_beijing(value).isoformat() == "2026-08-28T00:30:00+08:00"
    assert beijing_start_of_day_utc(value).isoformat() == "2026-08-27T16:00:00+00:00"


def test_policy_window_is_fixed_to_beijing_not_host_or_legacy_offset() -> None:
    decision = PolicyEngine().evaluate(
        PolicyInput(
            global_mode=GlobalMode.AUTO,
            kill_switch=False,
            whitelisted=True,
            ai_enabled=True,
            importance=Importance.NORMAL,
            conversation_mode=ConversationMode.AUTO_READY,
            now=datetime(2026, 8, 27, 16, 30, tzinfo=timezone.utc),
            auto_start=time(0, 0),
            auto_end=time(1, 0),
            utc_offset_hours=-7,
            inbound_content="北京时间窗口测试",
        )
    )

    assert decision.allowed


def test_database_naive_datetime_is_restored_as_aware_utc(db) -> None:
    contact = Contact(
        platform="SIMULATOR",
        platform_user_id="beijing-clock-contact",
        display_name="时区测试",
    )
    db.add(contact)
    db.flush()
    conversation = Conversation(
        platform="SIMULATOR",
        external_id="beijing-clock-conversation",
        contact_id=contact.id,
    )
    db.add(conversation)
    db.flush()
    event_at = datetime(2026, 8, 27, 16, 30, tzinfo=timezone.utc)
    message = Message(
        platform="SIMULATOR",
        external_message_id="beijing-clock-message",
        conversation_id=conversation.id,
        contact_id=contact.id,
        direction=MessageDirection.INBOUND,
        author=MessageAuthor.CONTACT,
        content="时区测试",
        status=MessageStatus.RECEIVED,
        event_at=event_at,
    )
    db.add(message)
    db.commit()
    message_id = message.id
    db.expire_all()

    restored = db.get(Message, message_id)
    assert restored is not None
    assert restored.event_at.utcoffset() == timedelta(0)
    assert as_beijing(restored.event_at).isoformat() == "2026-08-28T00:30:00+08:00"


def test_export_treats_naive_form_dates_as_beijing_wall_clock() -> None:
    service = ChatExportService()
    local_midnight = datetime(2026, 8, 28, 0, 0)

    as_stored_utc = service._utc(local_midnight)
    assert as_stored_utc.isoformat() == "2026-08-27T16:00:00+00:00"
    assert service._local(as_stored_utc).isoformat() == "2026-08-28T00:00:00+08:00"
