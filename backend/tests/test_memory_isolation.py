from __future__ import annotations

import pytest

from app.api.routes import update_memory
from app.core.enums import MessageAuthor, MessageDirection, MessageStatus
from app.models.entities import Contact, Conversation, Memory, Message, PersonaProfile
from app.schemas import MemoryUpdate
from app.services.memory import extract_candidate_memories, safe_memory, store_extracted_memories
from app.services.prompt import build_messages


@pytest.mark.parametrize("iteration", range(100))
def test_contact_memory_never_leaks(iteration: int):
    alice = Contact(id=f"alice-{iteration}", platform="SIMULATOR", platform_user_id=f"a-{iteration}", display_name="Alice")
    bob = Contact(id=f"bob-{iteration}", platform="SIMULATOR", platform_user_id=f"b-{iteration}", display_name="Bob")
    memory = Memory(contact_id=alice.id, kind="PERSONAL_FACT", content=f"Alice 私密行程 {iteration}")
    prompt = build_messages(contact=bob, persona=PersonaProfile(id=1), memories=[memory], recent=[], current_message="Alice 最近去哪？")
    assert f"Alice 私密行程 {iteration}" not in prompt[0]["content"]


@pytest.mark.parametrize("secret", ["sk-abcdefghijklmnop1234", "api_key=abcdefghijk", "银行卡 6222021234567890123", "验证码 123456"])
def test_sensitive_memory_is_rejected(secret: str):
    allowed, _ = safe_memory("PERSONAL_FACT", secret)
    assert not allowed


def test_conservative_memory_extraction_uses_only_declared_kinds_and_rejects_secrets():
    candidates = extract_candidate_memories("我喜欢吃日料。我下个月去日本。我最近在找实习。以后叫我小林。")
    assert {kind for kind, _ in candidates} == {"PREFERENCE", "PERSONAL_FACT", "ONGOING_TOPIC", "CONVERSATION_CONVENTION"}
    assert extract_candidate_memories("我喜欢用 api_key=abcdefghijk123456") == []


def test_automatic_memory_is_whitelist_scoped_deduplicated_and_source_bound(db):
    alice = Contact(platform="SIMULATOR", platform_user_id="auto-alice", display_name="Alice", whitelisted=True)
    stranger = Contact(platform="SIMULATOR", platform_user_id="auto-stranger", display_name="Stranger", whitelisted=False)
    db.add_all([alice, stranger])
    db.flush()
    conversation = Conversation(platform="SIMULATOR", external_id="auto-memory", contact_id=alice.id)
    db.add(conversation)
    db.flush()
    source = Message(
        platform="SIMULATOR",
        external_message_id="auto-memory-1",
        conversation_id=conversation.id,
        contact_id=alice.id,
        direction=MessageDirection.INBOUND,
        author=MessageAuthor.CONTACT,
        content="我喜欢吃日料，我下个月去日本。",
        status=MessageStatus.RECEIVED,
    )
    db.add(source)
    db.flush()
    assert store_extracted_memories(db, alice, source) == 2
    assert store_extracted_memories(db, alice, source) == 0
    assert store_extracted_memories(db, stranger, source) == 0
    items = list(db.query(Memory).filter(Memory.contact_id == alice.id))
    assert len(items) == 2
    assert all(item.review_status == "APPROVED" for item in items)
    assert all(item.source_message_id == source.id for item in items)


def test_memory_can_still_be_edited_after_automatic_approval(db):
    contact = Contact(platform="SIMULATOR", platform_user_id="editable-memory", display_name="可编辑联系人")
    db.add(contact)
    db.flush()
    item = Memory(
        contact_id=contact.id,
        kind="PERSONAL_FACT",
        content="旧内容",
        review_status="APPROVED",
    )
    db.add(item)
    db.commit()

    updated = update_memory(
        item.id,
        MemoryUpdate(kind="PREFERENCE", content="喜欢简洁、自然的回复"),
        db,
    )

    assert updated.kind == "PREFERENCE"
    assert updated.content == "喜欢简洁、自然的回复"
    assert updated.review_status == "APPROVED"
