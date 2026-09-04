from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.channels.base import InboundEvent
from app.channels.simulator import SimulatorConnector
from app.core.config import Settings
from app.core.enums import ConversationMode, GlobalMode, Importance
from app.llm.base import LLMMessage, LLMResponse
from app.llm.gateway import LLMGateway
from app.llm.providers import SimulatorProvider
from app.models.entities import AuditLog, Contact, Conversation, ConversationSummary, Memory, Message, PersonaProfile, RuntimeState, TodoItem
from app.services.pipeline import MessagePipeline


@pytest.mark.asyncio
async def test_read_only_only_records_and_displays_inbound_message(db):
    db.add(RuntimeState(id=1, global_mode=GlobalMode.READ_ONLY, release_gate="SIMULATION"))
    contact = Contact(
        platform="SIMULATOR",
        platform_user_id="read-only-user",
        display_name="只读联系人",
        relationship_label="朋友",
        whitelisted=True,
        ai_enabled=True,
        memory_enabled=True,
        importance=Importance.NORMAL,
    )
    db.add_all([contact, PersonaProfile(id=1)])
    db.commit()
    connector = SimulatorConnector()
    pipeline = MessagePipeline(
        gateway=LLMGateway([SimulatorProvider()]),
        connectors={"SIMULATOR": connector},
        settings=Settings(),
    )
    result = await pipeline.handle(
        InboundEvent(
            platform="SIMULATOR",
            message_id="read-only-1",
            conversation_id="sim:read-only-user",
            sender_id=contact.platform_user_id,
            sender_name=contact.display_name,
            content="我喜欢咖啡，下周准备去杭州",
            timestamp=datetime(2026, 8, 24, 12, 0, tzinfo=timezone(timedelta(hours=8))),
        ),
        db,
    )

    assert result.code == "GLOBAL_MODE"
    assert db.scalar(select(func.count(Message.id))) == 1
    assert db.scalar(select(func.count(Memory.id))) == 0
    assert db.scalar(select(func.count(ConversationSummary.id))) == 0
    assert connector.sent == []


@pytest.mark.asyncio
async def test_legacy_admin_cooldown_self_heals_on_next_message(db):
    db.add(RuntimeState(id=1, global_mode=GlobalMode.AUTO, release_gate="SIMULATION"))
    contact = Contact(
        platform="SIMULATOR",
        platform_user_id="admin-user",
        display_name="管理员",
        relationship_label="管理员",
        whitelisted=True,
        ai_enabled=True,
        importance=Importance.NORMAL,
    )
    db.add_all([contact, PersonaProfile(id=1)])
    db.flush()
    conversation = Conversation(
        platform="SIMULATOR",
        external_id="sim:admin-user",
        contact_id=contact.id,
        mode=ConversationMode.CONTACT_COOLDOWN,
        managed_rounds=40,
    )
    db.add(conversation)
    db.commit()
    connector = SimulatorConnector()
    result = await MessagePipeline(
        gateway=LLMGateway([SimulatorProvider()]),
        connectors={"SIMULATOR": connector},
        settings=Settings(auto_start="00:00", auto_end="23:59"),
    ).handle(
        InboundEvent(
            platform="SIMULATOR",
            message_id="admin-after-cooldown",
            conversation_id="sim:admin-user",
            sender_id=contact.platform_user_id,
            sender_name=contact.display_name,
            content="继续正常聊天",
            timestamp=datetime(2026, 8, 30, 12, 0, tzinfo=timezone(timedelta(hours=8))),
        ),
        db,
    )

    db.refresh(conversation)
    assert result.sent
    assert conversation.mode == ConversationMode.AUTO_READY
    assert conversation.managed_rounds == 1


@pytest.mark.asyncio
async def test_contact_managed_session_resets_after_idle_period(db):
    event_at = datetime(2026, 9, 2, 1, 0, tzinfo=timezone(timedelta(hours=8)))
    db.add(RuntimeState(id=1, global_mode=GlobalMode.AUTO, release_gate="SIMULATION"))
    contact = Contact(
        platform="SIMULATOR",
        platform_user_id="returning-friend",
        display_name="重新来聊天的朋友",
        relationship_label="朋友",
        whitelisted=True,
        ai_enabled=True,
        importance=Importance.NORMAL,
    )
    db.add_all([contact, PersonaProfile(id=1)])
    db.flush()
    conversation = Conversation(
        platform="SIMULATOR",
        external_id="sim:returning-friend",
        contact_id=contact.id,
        mode=ConversationMode.CONTACT_COOLDOWN,
        managed_rounds=40,
        cooldown_until=event_at + timedelta(hours=1),
        last_active_at=event_at - timedelta(minutes=31),
    )
    db.add(conversation)
    db.commit()
    connector = SimulatorConnector()

    result = await MessagePipeline(
        gateway=LLMGateway([SimulatorProvider()]),
        connectors={"SIMULATOR": connector},
        settings=Settings(
            auto_start="00:00",
            auto_end="23:59",
            contact_cooldown_minutes=30,
        ),
    ).handle(
        InboundEvent(
            platform="SIMULATOR",
            message_id="returning-friend-after-idle",
            conversation_id="sim:returning-friend",
            sender_id=contact.platform_user_id,
            sender_name=contact.display_name,
            content="隔了一阵子，继续聊吧",
            timestamp=event_at,
        ),
        db,
    )

    db.refresh(conversation)
    reset_audit = db.scalar(
        select(AuditLog).where(
            AuditLog.conversation_id == conversation.id,
            AuditLog.event == "MANAGED_SESSION_IDLE_RESET",
        )
    )
    assert result.sent
    assert conversation.mode == ConversationMode.AUTO_READY
    assert conversation.cooldown_until is None
    assert conversation.managed_rounds == 1
    assert reset_audit is not None
    assert reset_audit.detail["previous_managed_rounds"] == 40


@pytest.mark.asyncio
async def test_contact_cooldown_still_blocks_during_same_active_session(db):
    event_at = datetime(2026, 9, 2, 1, 0, tzinfo=timezone(timedelta(hours=8)))
    db.add(RuntimeState(id=1, global_mode=GlobalMode.AUTO, release_gate="SIMULATION"))
    contact = Contact(
        platform="SIMULATOR",
        platform_user_id="busy-loop-contact",
        display_name="连续聊天联系人",
        relationship_label="朋友",
        whitelisted=True,
        ai_enabled=True,
        importance=Importance.NORMAL,
    )
    db.add_all([contact, PersonaProfile(id=1)])
    db.flush()
    conversation = Conversation(
        platform="SIMULATOR",
        external_id="sim:busy-loop-contact",
        contact_id=contact.id,
        mode=ConversationMode.CONTACT_COOLDOWN,
        managed_rounds=40,
        cooldown_until=event_at + timedelta(minutes=25),
        last_active_at=event_at - timedelta(minutes=5),
    )
    db.add(conversation)
    db.commit()
    connector = SimulatorConnector()

    result = await MessagePipeline(
        gateway=LLMGateway([SimulatorProvider()]),
        connectors={"SIMULATOR": connector},
        settings=Settings(
            auto_start="00:00",
            auto_end="23:59",
            contact_cooldown_minutes=30,
        ),
    ).handle(
        InboundEvent(
            platform="SIMULATOR",
            message_id="busy-loop-during-cooldown",
            conversation_id="sim:busy-loop-contact",
            sender_id=contact.platform_user_id,
            sender_name=contact.display_name,
            content="还在连续对话",
            timestamp=event_at,
        ),
        db,
    )

    db.refresh(conversation)
    assert not result.sent
    assert result.code == "CONTACT_COOLDOWN"
    assert conversation.mode == ConversationMode.CONTACT_COOLDOWN
    assert conversation.managed_rounds == 40
    assert connector.sent == []


@pytest.mark.asyncio
async def test_formal_matter_replies_and_creates_one_backend_todo(db):
    class UnsafeFormalProvider:
        name = "unsafe-formal-test"

        async def chat(self, messages: list[LLMMessage]) -> LLMResponse:
            return LLMResponse("我代表你确认签署合同。", "unsafe-model", self.name, 1)

        async def test_connection(self) -> bool:
            return True

        async def models(self) -> list[str]:
            return ["unsafe-model"]

    db.add(RuntimeState(id=1, global_mode=GlobalMode.AUTO, release_gate="SIMULATION"))
    contact = Contact(
        platform="SIMULATOR",
        platform_user_id="teacher-user",
        display_name="老师联系人",
        relationship_label="老师",
        whitelisted=True,
        ai_enabled=True,
        importance=Importance.NORMAL,
    )
    db.add_all([contact, PersonaProfile(id=1)])
    db.commit()
    connector = SimulatorConnector()
    pipeline = MessagePipeline(
        gateway=LLMGateway([UnsafeFormalProvider()]),
        connectors={"SIMULATOR": connector},
        settings=Settings(auto_start="00:00", auto_end="23:59"),
    )
    result = await pipeline.handle(
        InboundEvent(
            platform="SIMULATOR",
            message_id="formal-matter-1",
            conversation_id="sim:teacher-user",
            sender_id=contact.platform_user_id,
            sender_name=contact.display_name,
            content="老师让我确认明天的面试安排",
            timestamp=datetime(2026, 8, 31, 12, 0, tzinfo=timezone(timedelta(hours=8))),
        ),
        db,
    )

    todo = db.scalar(select(TodoItem).where(TodoItem.kind == "FORMAL_MATTER"))
    outbound = db.scalar(select(Message).where(Message.author == "AI"))
    assert result.sent
    assert todo is not None and todo.status == "OPEN" and todo.delivery_status == "RECORDED"
    assert todo.source_message_id == result.inbound_message_id
    assert "已在后台记录" in outbound.content
    assert "不能替你作出承诺" in outbound.content
    assert "我代表你确认签署合同" not in outbound.content
    assert outbound.raw_envelope["formal_matter"]["todo_id"] == todo.id

    duplicate = await pipeline.handle(
        InboundEvent(
            platform="SIMULATOR",
            message_id="formal-matter-1",
            conversation_id="sim:teacher-user",
            sender_id=contact.platform_user_id,
            sender_name=contact.display_name,
            content="老师让我确认明天的面试安排",
            timestamp=datetime(2026, 8, 31, 12, 0, tzinfo=timezone(timedelta(hours=8))),
        ),
        db,
    )
    assert duplicate.duplicate
    assert len(list(db.scalars(select(TodoItem).where(TodoItem.kind == "FORMAL_MATTER")))) == 1
