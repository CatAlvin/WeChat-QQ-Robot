from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.routes import current_qq_acceptance, start_qq_acceptance
from app.core.enums import AuditEvent, GlobalMode, Importance, MessageAuthor, MessageDirection, MessageStatus
from app.models.entities import AuditLog, ConnectorEvent, Contact, Conversation, Message, RuntimeState
from app.schemas import QQAcceptanceStart


def _ready(db) -> tuple[Contact, Conversation]:
    db.add(
        RuntimeState(
            id=1,
            global_mode=GlobalMode.AUTO,
            kill_switch=False,
            release_gate="LIVE",
            simulation_accepted=True,
            shadow_accepted=True,
        )
    )
    contact = Contact(
        platform="QQ",
        platform_user_id="qq-acceptance-user",
        display_name="QQ 验收联系人",
        relationship_label="朋友",
        whitelisted=True,
        ai_enabled=True,
        importance=Importance.NORMAL,
    )
    db.add(contact)
    db.flush()
    conversation = Conversation(platform="QQ", external_id="c2c:qq-acceptance-user", contact_id=contact.id)
    db.add(conversation)
    db.commit()
    return contact, conversation


def _inbound(db, contact, conversation, index: int, *, official: bool = True) -> Message:
    external_id = f"qq-acceptance-in-{index}"
    if official:
        db.add(
            ConnectorEvent(
                platform="QQ",
                external_event_id=external_id,
                event_type="C2C_MESSAGE_CREATE",
                normalized_payload={"message_id": external_id, "sender_id": contact.platform_user_id},
                status="DONE",
            )
        )
    item = Message(
        platform="QQ",
        external_message_id=external_id,
        conversation_id=conversation.id,
        contact_id=contact.id,
        direction=MessageDirection.INBOUND,
        author=MessageAuthor.CONTACT,
        content=f"真实验收消息 {index}",
        status=MessageStatus.RECEIVED,
    )
    db.add(item)
    return item


def test_qq_acceptance_passes_only_after_200_unique_and_kill_switch_evidence(db):
    contact, conversation = _ready(db)
    started = start_qq_acceptance(QQAcceptanceStart(contact_id=contact.id), db)
    assert started["status"] == "RUNNING"
    items = [_inbound(db, contact, conversation, index) for index in range(200)]
    db.flush()
    db.add(
        AuditLog(
            event=AuditEvent.POLICY_DENY,
            conversation_id=conversation.id,
            message_id=items[-1].id,
            detail={"code": "KILL_SWITCH", "reason": "全局急停已开启"},
        )
    )
    db.commit()
    result = current_qq_acceptance(db)
    assert result["status"] == "PASSED"
    assert result["unique_received"] == 200
    assert result["remaining_messages"] == 0
    assert result["kill_switch_tested"]
    assert result["kill_switch_violations"] == 0
    assert result["system_violations"] == 0


def test_qq_acceptance_fails_on_duplicate_reply_to_same_inbound(db):
    contact, conversation = _ready(db)
    start_qq_acceptance(QQAcceptanceStart(contact_id=contact.id), db)
    inbound = _inbound(db, contact, conversation, 1)
    db.flush()
    for index in range(2):
        db.add(
            Message(
                platform="QQ",
                external_message_id=f"qq-acceptance-out-{index}",
                conversation_id=conversation.id,
                contact_id=contact.id,
                direction=MessageDirection.OUTBOUND,
                author=MessageAuthor.AI,
                content="重复回复",
                status=MessageStatus.SENT,
                reply_to_external_id=inbound.external_message_id,
            )
        )
    db.commit()
    result = current_qq_acceptance(db)
    assert result["status"] == "FAILED"
    assert result["duplicate_sends"] == 1
    assert result["system_violations"] == 1


def test_qq_acceptance_fails_on_any_non_target_send(db):
    contact, _ = _ready(db)
    start_qq_acceptance(QQAcceptanceStart(contact_id=contact.id), db)
    other = Contact(platform="QQ", platform_user_id="other-user", display_name="其他人", whitelisted=False)
    db.add(other)
    db.flush()
    other_conversation = Conversation(platform="QQ", external_id="c2c:other-user", contact_id=other.id)
    db.add(other_conversation)
    db.flush()
    db.add(
        Message(
            platform="QQ",
            external_message_id="wrong-target-out",
            conversation_id=other_conversation.id,
            contact_id=other.id,
            direction=MessageDirection.OUTBOUND,
            author=MessageAuthor.AI,
            content="不应发送",
            status=MessageStatus.SENT,
        )
    )
    db.commit()
    result = current_qq_acceptance(db)
    assert result["status"] == "FAILED"
    assert result["wrong_recipient_sends"] == 1


def test_qq_acceptance_cannot_start_outside_live_auto(db):
    contact, _ = _ready(db)
    state = db.get(RuntimeState, 1)
    state.release_gate = "SHADOW"
    db.commit()
    with pytest.raises(HTTPException, match="LIVE"):
        start_qq_acceptance(QQAcceptanceStart(contact_id=contact.id), db)


def test_qq_acceptance_does_not_count_bridge_or_handwritten_messages(db):
    contact, conversation = _ready(db)
    start_qq_acceptance(QQAcceptanceStart(contact_id=contact.id), db)
    _inbound(db, contact, conversation, 1, official=False)
    db.commit()

    result = current_qq_acceptance(db)

    assert result["unique_received"] == 0
    assert result["remaining_messages"] == 200
    assert result["status"] == "RUNNING"
