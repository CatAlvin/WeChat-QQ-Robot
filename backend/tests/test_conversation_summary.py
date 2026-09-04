from __future__ import annotations

from sqlalchemy import select

from app.core.enums import MessageAuthor, MessageDirection, MessageStatus
from app.models.entities import Contact, Conversation, ConversationSummary, Message
from app.services.prompt import build_messages
from app.services.summary import refresh_conversation_summary
from app.models.entities import PersonaProfile


def _conversation(db, suffix: str) -> tuple[Contact, Conversation]:
    contact = Contact(platform="SIMULATOR", platform_user_id=f"user-{suffix}", display_name=suffix)
    db.add(contact)
    db.flush()
    conversation = Conversation(platform="SIMULATOR", external_id=f"conversation-{suffix}", contact_id=contact.id)
    db.add(conversation)
    db.flush()
    return contact, conversation


def test_summary_is_updated_in_place_and_redacts_common_secrets(db):
    _, conversation = _conversation(db, "Alice")
    db.add(
        Message(
            platform="SIMULATOR",
            external_message_id="alice-1",
            conversation_id=conversation.id,
            direction=MessageDirection.INBOUND,
            author=MessageAuthor.CONTACT,
            content="之后提醒我，api_key=abcdefghijk123456",
            status=MessageStatus.RECEIVED,
        )
    )
    db.flush()
    first = refresh_conversation_summary(db, conversation)
    assert "abcdefghijk123456" not in first.content
    assert "敏感信息已隐藏" in first.content

    db.add(
        Message(
            platform="SIMULATOR",
            external_message_id="alice-2",
            conversation_id=conversation.id,
            direction=MessageDirection.OUTBOUND,
            author=MessageAuthor.AI,
            content="好的",
            status=MessageStatus.SENT,
        )
    )
    db.flush()
    second = refresh_conversation_summary(db, conversation)
    assert second.id == first.id
    assert len(list(db.scalars(select(ConversationSummary)))) == 1


def test_summary_never_crosses_conversation_prompt_boundary(db):
    alice, alice_conversation = _conversation(db, "Alice")
    bob, bob_conversation = _conversation(db, "Bob")
    alice_summary = ConversationSummary(conversation_id=alice_conversation.id, content="Alice 私密行程：去日本")
    bob_summary = ConversationSummary(conversation_id=bob_conversation.id, content="Bob 喜欢咖啡")

    prompt = build_messages(
        contact=bob,
        persona=PersonaProfile(id=1),
        memories=[],
        recent=[],
        current_message="最近聊了什么？",
        summary=bob_summary,
    )
    assert "Bob 喜欢咖啡" in prompt[0]["content"]
    assert alice.display_name == "Alice"
    assert "Alice 私密行程" not in prompt[0]["content"]
    assert alice_summary.conversation_id != bob_summary.conversation_id


def test_summary_reports_only_explicit_contact_attitude(db):
    _, conversation = _conversation(db, "Attitude")
    db.add_all(
        [
            Message(
                platform="SIMULATOR",
                external_message_id="attitude-1",
                conversation_id=conversation.id,
                direction=MessageDirection.INBOUND,
                author=MessageAuthor.CONTACT,
                content="我很开心，这个方案我支持",
                status=MessageStatus.RECEIVED,
            ),
            Message(
                platform="SIMULATOR",
                external_message_id="attitude-2",
                conversation_id=conversation.id,
                direction=MessageDirection.OUTBOUND,
                author=MessageAuthor.AI,
                content="我很担心你会迟到",
                status=MessageStatus.SENT,
            ),
        ]
    )
    db.flush()
    summary = refresh_conversation_summary(db, conversation)
    attitude_section = summary.content.split("联系人情绪/态度：", 1)[1].split("可能需要用户关注：", 1)[0]
    assert "联系人明确表达：我很开心" in attitude_section
    assert "我很担心你会迟到" not in attitude_section
