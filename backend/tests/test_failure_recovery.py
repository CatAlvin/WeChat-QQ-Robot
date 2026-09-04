from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.channels.base import ChannelConnector, InboundEvent, OutboundMessage, SendPermit, SendResult
from app.channels.simulator import SimulatorConnector
from app.core.config import Settings
from app.core.enums import GlobalMode, Importance, MessageAuthor, MessageDirection, MessageStatus
from app.database import SessionLocal, engine
from app.llm.base import LLMProvider
from app.llm.gateway import LLMGateway
from app.llm.providers import SimulatorProvider
from app.models.entities import Account, AuditLog, Contact, Conversation, Incident, Message, PersonaProfile, RuntimeState
from app.services.pipeline import MessagePipeline
from app.services.runtime import cancel_queued_messages


class SlowProvider(LLMProvider):
    name = "slow"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages):
        self.calls += 1
        await asyncio.sleep(0.1)

    async def test_connection(self):
        return False

    async def models(self):
        return []


class DisconnectedConnector(ChannelConnector):
    platform = "QQ"
    real_channel = False

    def __init__(self) -> None:
        self.calls = 0

    async def send(self, message: OutboundMessage, permit: SendPermit) -> SendResult:
        permit.assert_valid_for(message)
        self.calls += 1
        return SendResult(False, error_code="QQ_NETWORK_ERROR", error_detail="ConnectionError")

    async def status(self):
        return {"status": "DISCONNECTED"}


def _allowed_contact(db, platform: str = "SIMULATOR") -> Contact:
    item = Contact(
        platform=platform,
        platform_user_id="allowed-user",
        display_name="安全测试联系人",
        relationship_label="朋友",
        whitelisted=True,
        importance=Importance.NORMAL,
    )
    db.add(item)
    db.add(PersonaProfile(id=1))
    db.flush()
    return item


@pytest.mark.asyncio
async def test_llm_timeout_retries_once_then_creates_incident_without_send(db):
    db.add(RuntimeState(id=1, release_gate="SIMULATION"))
    contact = _allowed_contact(db)
    db.commit()
    provider = SlowProvider()
    connector = SimulatorConnector()
    pipeline = MessagePipeline(
        gateway=LLMGateway([provider], timeout_seconds=0.01),
        connectors={"SIMULATOR": connector},
        settings=Settings(llm_timeout_seconds=0.01),
    )
    result = await pipeline.handle(
        InboundEvent(
            platform="SIMULATOR",
            message_id="timeout-1",
            conversation_id="timeout-conversation",
            sender_id=contact.platform_user_id,
            sender_name=contact.display_name,
            content="你好",
            timestamp=datetime(2026, 8, 24, 12, 0, tzinfo=timezone(timedelta(hours=8))),
        ),
        db,
    )
    assert result.code == "ALL_PROVIDERS_FAILED"
    assert provider.calls == 2
    assert connector.sent == []
    assert db.scalar(select(Incident).where(Incident.kind == "LLM_PROVIDER_FAILURE")) is not None
    assert db.scalar(select(AuditLog).where(AuditLog.event == "INCIDENT")) is not None


@pytest.mark.asyncio
async def test_three_recent_llm_failures_warn_without_permanently_muting_personal_system(db):
    state = RuntimeState(id=1, global_mode=GlobalMode.AUTO, release_gate="SIMULATION")
    db.add(state)
    contact = _allowed_contact(db)
    db.commit()
    provider = SlowProvider()
    pipeline = MessagePipeline(
        gateway=LLMGateway([provider], timeout_seconds=0.01),
        connectors={"SIMULATOR": SimulatorConnector()},
        settings=Settings(llm_timeout_seconds=0.01, failure_circuit_breaker_count=3),
    )
    for index in range(3):
        result = await pipeline.handle(
            InboundEvent(
                platform="SIMULATOR",
                message_id=f"circuit-{index}",
                conversation_id="circuit-conversation",
                sender_id=contact.platform_user_id,
                sender_name=contact.display_name,
                content="你好",
                timestamp=datetime(2026, 8, 24, 12, index, tzinfo=timezone(timedelta(hours=8))),
            ),
            db,
        )
        assert result.code == "ALL_PROVIDERS_FAILED"

    db.refresh(state)
    assert state.global_mode == GlobalMode.AUTO
    warning = db.scalar(select(AuditLog).where(AuditLog.event == "FAILURE_BURST_WARNING"))
    assert warning is not None
    assert warning.detail["recent_failures"] == 3
    assert warning.detail["automatic_silence"] is False


@pytest.mark.asyncio
async def test_connector_loss_creates_incident_and_replay_never_resends(db):
    db.add(RuntimeState(id=1, release_gate="LIVE", live_auto_start="00:00", live_auto_end="23:59"))
    db.add(
        Account(
            platform="QQ",
            display_name="QQ Official Bot",
            connector_kind="QQ_OFFICIAL_BOT",
            enabled=True,
            credential_id="credential-present",
            config={"app_id": "11111111"},
        )
    )
    contact = _allowed_contact(db, "QQ")
    db.commit()
    connector = DisconnectedConnector()
    pipeline = MessagePipeline(
        gateway=LLMGateway([SimulatorProvider()]),
        connectors={"QQ": connector},
        settings=Settings(),
    )
    event = InboundEvent(
        platform="QQ",
        message_id="connection-loss-1",
        conversation_id="qq-user",
        sender_id=contact.platform_user_id,
        sender_name=contact.display_name,
        content="你好",
        timestamp=datetime(2026, 8, 24, 12, 0, tzinfo=timezone(timedelta(hours=8))),
    )
    first = await pipeline.handle(event, db)
    replay = await pipeline.handle(event, db)
    assert first.code == "QQ_NETWORK_ERROR" and not first.sent
    assert replay.duplicate and not replay.sent
    assert connector.calls == 1
    assert db.scalar(select(Incident).where(Incident.kind == "CONNECTOR_FAILURE")) is not None


def test_restart_cancels_every_queued_draft(db):
    contact = _allowed_contact(db)
    conversation = Conversation(platform="SIMULATOR", external_id="restart-conversation", contact_id=contact.id)
    db.add(conversation)
    db.flush()
    queued = Message(
        platform="SIMULATOR",
        external_message_id="queued-before-crash",
        conversation_id=conversation.id,
        contact_id=contact.id,
        direction=MessageDirection.OUTBOUND,
        author=MessageAuthor.AI,
        content="旧进程草稿",
        status=MessageStatus.QUEUED,
    )
    db.add(queued)
    db.commit()
    assert cancel_queued_messages(db) == 1
    db.commit()
    db.refresh(queued)
    assert queued.status == MessageStatus.CANCELLED


def test_database_pool_reconnects_after_dispose():
    assert engine.pool._pre_ping is True
    with SessionLocal() as db:
        assert db.scalar(select(1)) == 1
    engine.dispose()
    with SessionLocal() as db:
        assert db.scalar(select(1)) == 1
