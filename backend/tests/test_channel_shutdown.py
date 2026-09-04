from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.api.routes import patch_contact, update_account, update_release_gate
from app.channels import qq as qq_module
from app.channels.base import ChannelConnector, InboundEvent, OutboundMessage, SendPermit, SendResult
from app.channels.qq import QQOfficialBotConnector
from app.core.config import Settings
from app.core.enums import GlobalMode, Importance, MessageAuthor, MessageDirection, MessageStatus
from app.llm.gateway import LLMGateway
from app.llm.base import LLMProvider, LLMResponse
from app.llm.providers import SimulatorProvider
from app.models.entities import AcceptanceRun, Account, Contact, Conversation, Message, PersonaProfile, RuntimeState
from app.schemas import AccountConfigUpdate, ContactPatch, ReleaseGateUpdate
from app.services.pipeline import MessagePipeline


TZ = timezone(timedelta(hours=8))


class CountingConnector(ChannelConnector):
    platform = "QQ"
    real_channel = True

    def __init__(self) -> None:
        self.calls = 0

    async def send(self, message: OutboundMessage, permit: SendPermit) -> SendResult:
        self.calls += 1
        return SendResult(True, "should-never-send")

    async def status(self):
        return {"status": "DISABLED"}


class BlockingProvider(LLMProvider):
    name = "blocking"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def chat(self, messages):
        self.started.set()
        await self.release.wait()
        return LLMResponse(content="收到，我晚点再和你聊", model="test", provider=self.name, latency_ms=1)

    async def test_connection(self):
        return True

    async def models(self):
        return ["test"]


def _permit(target_id: str) -> SendPermit:
    return SendPermit(target_id, True, True, True, True, True, True)


@pytest.mark.asyncio
async def test_disabling_account_cancels_queued_messages_and_running_acceptance(db):
    account = Account(
        platform="QQ",
        display_name="QQ Official Bot",
        connector_kind="QQ_OFFICIAL_BOT",
        status="ONLINE",
        enabled=True,
        credential_id="credential-present",
        config={"app_id": "11111111"},
    )
    contact = Contact(platform="QQ", platform_user_id="qq-user", display_name="QQ 用户")
    db.add_all([account, contact])
    db.flush()
    conversation = Conversation(platform="QQ", external_id="c2c:qq-user", contact_id=contact.id)
    db.add(conversation)
    db.flush()
    queued = Message(
        platform="QQ",
        external_message_id="queued-before-disable",
        conversation_id=conversation.id,
        contact_id=contact.id,
        direction=MessageDirection.OUTBOUND,
        author=MessageAuthor.AI,
        content="不应发出",
        status=MessageStatus.QUEUED,
    )
    acceptance = AcceptanceRun(
        kind="QQ_200",
        status="RUNNING",
        target_contact_id=contact.id,
        target_platform_user_id=contact.platform_user_id,
    )
    db.add_all([queued, acceptance])
    db.commit()

    result = await update_account("QQ", AccountConfigUpdate(enabled=False), db)
    db.refresh(queued)
    db.refresh(acceptance)
    db.refresh(account)

    assert result["status"] == "DISCONNECTED"
    assert not account.enabled
    assert queued.status == MessageStatus.CANCELLED
    assert acceptance.status == "CANCELLED"
    assert acceptance.completed_at is not None


@pytest.mark.asyncio
async def test_live_pipeline_refuses_disabled_channel_before_llm_or_send(db):
    db.add(RuntimeState(id=1, global_mode=GlobalMode.AUTO, release_gate="LIVE"))
    db.add(Account(platform="QQ", display_name="QQ", connector_kind="QQ_OFFICIAL_BOT", enabled=False))
    contact = Contact(
        platform="QQ",
        platform_user_id="disabled-user",
        display_name="停用测试",
        relationship_label="朋友",
        whitelisted=True,
        ai_enabled=True,
        importance=Importance.NORMAL,
    )
    db.add_all([contact, PersonaProfile(id=1)])
    db.commit()
    connector = CountingConnector()
    pipeline = MessagePipeline(
        gateway=LLMGateway([SimulatorProvider()]),
        connectors={"QQ": connector},
        settings=Settings(),
    )
    result = await pipeline.handle(
        InboundEvent(
            platform="QQ",
            message_id="disabled-live-1",
            conversation_id="c2c:disabled-user",
            sender_id=contact.platform_user_id,
            sender_name=contact.display_name,
            content="你好",
            timestamp=datetime(2026, 8, 24, 12, 0, tzinfo=TZ),
        ),
        db,
    )

    assert result.code == "CHANNEL_DISABLED"
    assert not result.sent
    assert connector.calls == 0
    assert db.scalar(select(Message).where(Message.direction == MessageDirection.OUTBOUND)) is None


@pytest.mark.asyncio
async def test_final_check_refreshes_contact_changed_during_generation(db):
    db.add(RuntimeState(id=1, global_mode=GlobalMode.AUTO, release_gate="LIVE"))
    db.add(
        Account(
            platform="QQ",
            display_name="QQ",
            connector_kind="QQ_OFFICIAL_BOT",
            enabled=True,
            credential_id="credential-present",
            config={"app_id": "11111111"},
        )
    )
    contact = Contact(
        platform="QQ",
        platform_user_id="race-user",
        display_name="竞态测试",
        relationship_label="朋友",
        whitelisted=True,
        ai_enabled=True,
        importance=Importance.NORMAL,
    )
    db.add_all([contact, PersonaProfile(id=1)])
    db.commit()

    provider = BlockingProvider()
    connector = CountingConnector()
    connector.real_channel = False
    make_session = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    pipeline_db = make_session()
    config_db = make_session()
    pipeline = MessagePipeline(
        gateway=LLMGateway([provider]),
        connectors={"QQ": connector},
        settings=Settings(),
    )
    task = asyncio.create_task(
        pipeline.handle(
            InboundEvent(
                platform="QQ",
                message_id="race-contact-1",
                conversation_id="c2c:race-user",
                sender_id=contact.platform_user_id,
                sender_name=contact.display_name,
                content="你好",
                timestamp=datetime(2026, 8, 24, 12, 0, tzinfo=TZ),
            ),
            pipeline_db,
        )
    )
    try:
        await provider.started.wait()
        await patch_contact(contact.id, ContactPatch(whitelisted=False), config_db)
        provider.release.set()
        result = await task
    finally:
        provider.release.set()
        if not task.done():
            await task
        pipeline_db.close()
        config_db.close()

    assert result.code == "NOT_WHITELISTED"
    assert not result.sent
    assert connector.calls == 0


@pytest.mark.asyncio
async def test_final_check_cancels_when_live_gate_is_lowered_during_generation(db):
    db.add(
        RuntimeState(
            id=1,
            global_mode=GlobalMode.AUTO,
            release_gate="LIVE",
            simulation_accepted=True,
            shadow_accepted=True,
        )
    )
    db.add(
        Account(
            platform="QQ",
            display_name="QQ",
            connector_kind="QQ_OFFICIAL_BOT",
            status="ONLINE",
            enabled=True,
            credential_id="credential-present",
            config={"app_id": "11111111"},
        )
    )
    contact = Contact(
        platform="QQ",
        platform_user_id="gate-race-user",
        display_name="门禁竞态测试",
        relationship_label="朋友",
        whitelisted=True,
        ai_enabled=True,
        importance=Importance.NORMAL,
    )
    db.add_all([contact, PersonaProfile(id=1)])
    db.commit()

    provider = BlockingProvider()
    connector = CountingConnector()
    connector.real_channel = False
    make_session = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    pipeline_db = make_session()
    config_db = make_session()
    pipeline = MessagePipeline(
        gateway=LLMGateway([provider]),
        connectors={"QQ": connector},
        settings=Settings(),
    )
    task = asyncio.create_task(
        pipeline.handle(
            InboundEvent(
                platform="QQ",
                message_id="race-gate-1",
                conversation_id="c2c:gate-race-user",
                sender_id=contact.platform_user_id,
                sender_name=contact.display_name,
                content="你好",
                timestamp=datetime(2026, 8, 24, 12, 0, tzinfo=TZ),
            ),
            pipeline_db,
        )
    )
    try:
        await provider.started.wait()
        await update_release_gate(ReleaseGateUpdate(gate="SHADOW"), config_db)
        provider.release.set()
        result = await task
    finally:
        provider.release.set()
        if not task.done():
            await task
        pipeline_db.close()
        config_db.close()

    assert result.code == "RELEASE_GATE_CHANGED"
    assert not result.sent
    assert connector.calls == 0


@pytest.mark.asyncio
async def test_final_check_refreshes_account_disabled_during_generation(db):
    db.add(RuntimeState(id=1, global_mode=GlobalMode.AUTO, release_gate="LIVE"))
    db.add(
        Account(
            platform="QQ",
            display_name="QQ",
            connector_kind="QQ_OFFICIAL_BOT",
            status="ONLINE",
            enabled=True,
            credential_id="credential-present",
            config={"app_id": "11111111"},
        )
    )
    contact = Contact(
        platform="QQ",
        platform_user_id="account-race-user",
        display_name="账号停用竞态",
        relationship_label="朋友",
        whitelisted=True,
        ai_enabled=True,
        importance=Importance.NORMAL,
    )
    db.add_all([contact, PersonaProfile(id=1)])
    db.commit()

    provider = BlockingProvider()
    connector = CountingConnector()
    connector.real_channel = False
    make_session = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    pipeline_db = make_session()
    config_db = make_session()
    pipeline = MessagePipeline(gateway=LLMGateway([provider]), connectors={"QQ": connector}, settings=Settings())
    task = asyncio.create_task(
        pipeline.handle(
            InboundEvent(
                platform="QQ",
                message_id="race-account-1",
                conversation_id="c2c:account-race-user",
                sender_id=contact.platform_user_id,
                sender_name=contact.display_name,
                content="你好",
                timestamp=datetime(2026, 8, 24, 12, 0, tzinfo=TZ),
            ),
            pipeline_db,
        )
    )
    try:
        await provider.started.wait()
        await update_account("QQ", AccountConfigUpdate(enabled=False), config_db)
        provider.release.set()
        result = await task
    finally:
        provider.release.set()
        if not task.done():
            await task
        pipeline_db.close()
        config_db.close()

    assert result.code == "CHANNEL_DISABLED"
    assert not result.sent
    assert connector.calls == 0


@pytest.mark.asyncio
async def test_disabled_connector_never_opens_network():
    connector = QQOfficialBotConnector(app_id="111", app_secret="secret", enabled=False)
    message = OutboundMessage("QQ", "user-1", "你好", "in-1")
    result = await connector.send(message, _permit("user-1"))
    assert not result.success
    assert result.error_code == "QQ_DISABLED"
    assert (await connector.status())["status"] == "DISABLED"


@pytest.mark.asyncio
async def test_qq_connector_uses_official_c2c_endpoint_and_reply_id(monkeypatch):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if str(request.url) == "https://bots.qq.com/app/getAppAccessToken":
            return httpx.Response(200, json={"access_token": "token-1", "expires_in": 7200})
        return httpx.Response(200, json={"id": "qq-out-1"})

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client_factory(*args, **kwargs):
        return real_client(transport=transport, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(qq_module.httpx, "AsyncClient", client_factory)
    connector = QQOfficialBotConnector(app_id="111", app_secret="secret", enabled=True, observed_status="ONLINE")
    message = OutboundMessage("QQ", "user-1", "你好", "in-1")
    result = await connector.send(message, _permit("user-1"))

    assert result.success and result.external_message_id == "qq-out-1"
    assert str(requests[1].url) == "https://api.sgroup.qq.com/v2/users/user-1/messages"
    assert requests[1].headers["Authorization"] == "QQBot token-1"
    assert json.loads(requests[1].read()) == {"content": "你好", "msg_type": 0, "msg_id": "in-1"}
