from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.api.routes import accept_stage, update_release_gate
from app.channels.base import ChannelConnector, InboundEvent, OutboundMessage, SendPermit, SendResult
from app.core.config import Settings
from app.core.enums import GlobalMode, Importance, MessageAuthor, MessageDirection, MessageStatus
from app.llm.gateway import LLMGateway
from app.llm.providers import SimulatorProvider
from app.models.entities import Account, Contact, Conversation, Message, PersonaProfile, RuntimeState
from app.schemas import ReleaseGateUpdate, StageAcceptance
from app.services.pipeline import MessagePipeline


class NeverSendConnector(ChannelConnector):
    platform = "QQ"
    real_channel = True

    def __init__(self) -> None:
        self.calls = 0

    async def send(self, message: OutboundMessage, permit: SendPermit) -> SendResult:
        self.calls += 1
        raise AssertionError("SHADOW must never invoke the real connector")

    async def status(self):
        return {"status": "READY"}


def _qq_account(db, status: str = "ONLINE") -> Account:
    account = Account(
        platform="QQ",
        display_name="QQ Official Bot",
        connector_kind="QQ_OFFICIAL_BOT",
        status=status,
        enabled=True,
        credential_id="credential-present",
        config={"app_id": "11111111"},
    )
    db.add(account)
    return account


def _qq_contact(db, suffix: str = "one") -> Contact:
    contact = Contact(
        platform="QQ",
        platform_user_id=f"qq-{suffix}",
        display_name=f"测试联系人 {suffix}",
        relationship_label="朋友",
        whitelisted=True,
        ai_enabled=True,
        importance=Importance.NORMAL,
    )
    db.add(contact)
    return contact


@pytest.mark.asyncio
async def test_entering_shadow_does_not_turn_off_ai_generation(db):
    db.add(RuntimeState(id=1, global_mode=GlobalMode.AUTO, release_gate="SIMULATION", simulation_accepted=True))
    db.commit()
    state = await update_release_gate(ReleaseGateUpdate(gate="SHADOW"), db)
    assert state.release_gate == "SHADOW"
    assert state.global_mode == GlobalMode.AUTO


def test_shadow_acceptance_requires_real_online_event_and_shadowed_reply(db):
    state = RuntimeState(id=1, global_mode=GlobalMode.AUTO, release_gate="SHADOW", simulation_accepted=True)
    db.add(state)
    db.commit()
    with pytest.raises(HTTPException, match="QQ"):
        accept_stage("SHADOW", StageAcceptance(confirmed=True), db)

    _qq_account(db)
    contact = _qq_contact(db)
    db.flush()
    conversation = Conversation(platform="QQ", external_id="qq-one", contact_id=contact.id)
    db.add(conversation)
    db.flush()
    db.add(
        Message(
            platform="QQ",
            external_message_id="shadow-out-1",
            conversation_id=conversation.id,
            contact_id=contact.id,
            direction=MessageDirection.OUTBOUND,
            author=MessageAuthor.AI,
            content="影子回复",
            status=MessageStatus.SHADOWED,
        )
    )
    db.commit()
    accepted = accept_stage("SHADOW", StageAcceptance(confirmed=True), db)
    assert accepted.shadow_accepted


@pytest.mark.asyncio
async def test_live_requires_exactly_one_initial_qq_whitelist_contact(db):
    db.add(RuntimeState(id=1, global_mode=GlobalMode.AUTO, release_gate="SHADOW", simulation_accepted=True, shadow_accepted=True))
    _qq_account(db)
    db.commit()
    with pytest.raises(HTTPException, match="恰好保留 1 个"):
        await update_release_gate(ReleaseGateUpdate(gate="LIVE"), db)

    _qq_contact(db)
    db.commit()
    state = await update_release_gate(ReleaseGateUpdate(gate="LIVE"), db)
    assert state.release_gate == "LIVE"


@pytest.mark.asyncio
async def test_shadow_generates_and_records_but_never_calls_real_connector(db):
    db.add(RuntimeState(id=1, global_mode=GlobalMode.AUTO, release_gate="SHADOW", simulation_accepted=True))
    db.add(PersonaProfile(id=1))
    contact = _qq_contact(db, "shadow")
    db.commit()
    connector = NeverSendConnector()
    pipeline = MessagePipeline(
        gateway=LLMGateway([SimulatorProvider()]),
        connectors={"QQ": connector},
        settings=Settings(),
    )
    result = await pipeline.handle(
        InboundEvent(
            platform="QQ",
            message_id="shadow-in-1",
            conversation_id="qq-shadow",
            sender_id=contact.platform_user_id,
            sender_name=contact.display_name,
            content="你好",
            # SHADOW may exercise model generation outside the LIVE send window.
            timestamp=datetime(2026, 8, 24, 0, 50, tzinfo=timezone(timedelta(hours=8))),
        ),
        db,
    )
    assert result.shadowed and not result.sent
    assert result.code == "SHADOWED"
    assert result.provider == "simulator"
    assert result.model == "neko-simulator-v1"
    assert connector.calls == 0


@pytest.mark.asyncio
async def test_live_still_blocks_outside_time_window_before_connector_call(db):
    db.add(RuntimeState(id=1, global_mode=GlobalMode.AUTO, release_gate="LIVE", simulation_accepted=True, shadow_accepted=True))
    db.add(PersonaProfile(id=1))
    _qq_account(db)
    contact = _qq_contact(db, "live-night")
    db.commit()
    connector = NeverSendConnector()
    pipeline = MessagePipeline(
        gateway=LLMGateway([SimulatorProvider()]),
        connectors={"QQ": connector},
        settings=Settings(),
    )
    result = await pipeline.handle(
        InboundEvent(
            platform="QQ",
            message_id="live-night-in-1",
            conversation_id="qq-live-night",
            sender_id=contact.platform_user_id,
            sender_name=contact.display_name,
            content="你好",
            timestamp=datetime(2026, 8, 24, 0, 50, tzinfo=timezone(timedelta(hours=8))),
        ),
        db,
    )
    assert result.code == "OUTSIDE_TIME_WINDOW"
    assert not result.sent and not result.shadowed
    assert connector.calls == 0
