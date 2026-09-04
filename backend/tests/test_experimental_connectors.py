from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json

import httpx
import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import func, select
from starlette.requests import Request

from app.api.connectors import _event_payload, _payload_event, napcat_events
from app.api.routes import update_account, update_release_gate
from app.channels.base import ChannelConnector, InboundEvent, OutboundAttachment, OutboundMessage, SendPermit, SendResult
from app.channels.experimental import (
    AutoWxDraftConnector,
    ExperimentalConnectorError,
    NapCatConnector,
    normalize_autowx_event,
    normalize_loopback_base_url,
    normalize_napcat_event,
)
from app.core.config import Settings
from app.core.enums import ConversationMode, GlobalMode, Importance, MessageAuthor, MessageDirection, MessageStatus
from app.llm.gateway import LLMGateway
from app.llm.providers import SimulatorProvider
from app.models.entities import Account, Contact, ConnectorEvent, Conversation, Message, PersonaProfile, RuntimeState
from app.schemas import AccountConfigUpdate, ReleaseGateUpdate
from app.services.pipeline import DASHBOARD_MANUAL_PROVIDER, ManualSendError, MessagePipeline


TZ = timezone(timedelta(hours=8))


def _napcat_request(payload: dict[str, object]) -> tuple[Request, bytes]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []}, receive)
    return request, body


def _permit(target_id: str) -> SendPermit:
    return SendPermit(target_id, True, True, True, True, True, True)


class RecordingNapCatConnector(ChannelConnector):
    platform = "QQ_NAPCAT"
    real_channel = False

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def send(self, message: OutboundMessage, permit: SendPermit) -> SendResult:
        permit.assert_valid_for(message)
        self.sent.append(message)
        return SendResult(True, external_message_id=f"napcat-sent-{len(self.sent)}")

    async def status(self):
        return {"status": "ONLINE"}


def test_experimental_api_base_is_strictly_loopback() -> None:
    assert normalize_loopback_base_url("http://127.0.0.1:3001/") == "http://127.0.0.1:3001"
    assert normalize_loopback_base_url("http://localhost:3001") == "http://localhost:3001"
    with pytest.raises(ExperimentalConnectorError):
        normalize_loopback_base_url("https://example.com")
    with pytest.raises(ExperimentalConnectorError):
        normalize_loopback_base_url("http://127.0.0.1:3001/onebot")
    with pytest.raises(ExperimentalConnectorError):
        normalize_loopback_base_url("http://user:password@127.0.0.1:3001")


def test_napcat_normalization_handles_private_group_and_self_messages() -> None:
    private = normalize_napcat_event(
        {
            "post_type": "message",
            "message_type": "private",
            "self_id": 9000,
            "user_id": 1001,
            "message_id": 88,
            "message": [{"type": "text", "data": {"text": "你好"}}],
            "sender": {"nickname": "测试联系人"},
            "time": 1_777_000_000,
        }
    )
    assert private is not None
    assert private.platform == "QQ_NAPCAT"
    assert private.sender_id == "1001"
    assert private.content == "你好"
    assert not private.is_group

    group = normalize_napcat_event(
        {
            "post_type": "message",
            "message_type": "group",
            "self_id": 9000,
            "user_id": 1001,
            "group_id": 7001,
            "message_id": 89,
            "message": [
                {"type": "at", "data": {"qq": "9000"}},
                {"type": "text", "data": {"text": " 在吗"}},
            ],
        }
    )
    assert group is not None and group.is_group and group.mentioned_user
    assert group.group_id == "7001"
    assert group.content == "在吗"

    assert normalize_napcat_event(
        {
            "post_type": "message",
            "message_type": "private",
            "self_id": 9000,
            "user_id": 9000,
            "message_id": 90,
            "message": "自己发送",
        }
    ) is None

    own_private = normalize_napcat_event(
        {
            "post_type": "message_sent",
            "message_type": "private",
            "self_id": 9000,
            "user_id": 9000,
            "target_id": 1001,
            "message_id": 90,
            "message": "自己发送",
            "time": 1_777_000_001,
        }
    )
    assert own_private is not None
    assert own_private.author == MessageAuthor.HUMAN
    assert own_private.sender_id == "1001"
    assert own_private.conversation_id == "napcat:private:1001"
    assert own_private.raw["event_type"] == "ONEBOT11_MESSAGE_SENT"
    assert own_private.raw["text_content"] == "自己发送"

    with pytest.raises(ExperimentalConnectorError, match="target_id"):
        normalize_napcat_event(
            {
                "post_type": "message_sent",
                "message_type": "private",
                "self_id": 9000,
                "user_id": 9000,
                "message_id": 92,
                "message": "缺少目标",
            }
        )


def test_autowx_normalization_marks_own_messages_as_human_takeover() -> None:
    event = normalize_autowx_event(
        {
            "message_id": "wx-1",
            "sender_id": "wxid-test",
            "sender_name": "我",
            "content": "我来回复",
            "chat_id": "wxid-test",
            "is_self": True,
            "timestamp": "2026-08-25T10:00:00+08:00",
        }
    )
    assert event.platform == "WECHAT_AUTOWX"
    assert event.author == "HUMAN"


def test_napcat_normalization_preserves_image_audio_file_and_video_segments() -> None:
    event = normalize_napcat_event(
        {
            "post_type": "message",
            "message_type": "private",
            "self_id": 9000,
            "user_id": 1001,
            "message_id": 93,
            "message": [
                {"type": "image", "data": {"file": "photo.jpg", "url": "https://example.com/photo.jpg", "file_size": 12}},
                {"type": "record", "data": {"file": "voice.silk", "path": "D:/NapCat/voice.silk", "file_size": 23}},
                {"type": "file", "data": {"file": "notes.pdf", "file_id": "file-1", "file_size": 34}},
                {"type": "video", "data": {"file": "clip.mp4", "url": "https://example.com/clip.mp4", "file_size": 45}},
            ],
        }
    )
    assert event is not None
    assert event.message_type == "MIXED"
    assert event.content == "[图片] [语音] [文件：notes.pdf] [视频：clip.mp4]"
    assert [item.kind for item in event.attachments] == ["IMAGE", "AUDIO", "FILE", "VIDEO"]
    assert event.attachments[0].source_ref == "https://example.com/photo.jpg"
    assert event.attachments[1].metadata["napcat_sources"]["file"] == "voice.silk"
    assert event.attachments[1].metadata["napcat_sources"]["path"] == "D:/NapCat/voice.silk"
    assert event.attachments[2].file_id == "file-1"


def test_napcat_normalization_preserves_reply_target() -> None:
    event = normalize_napcat_event(
        {
            "post_type": "message",
            "message_type": "private",
            "self_id": 9000,
            "user_id": 1001,
            "message_id": 94,
            "message": [
                {"type": "reply", "data": {"id": "88"}},
                {"type": "text", "data": {"text": "请总结这条"}},
            ],
        }
    )
    assert event is not None
    assert event.reply_to_message_id == "88"
    assert event.content == "请总结这条"
    restored = _payload_event(_event_payload(event))
    assert restored.reply_to_message_id == "88"


def test_durable_inbox_recovers_reply_target_from_legacy_raw_payload() -> None:
    event = _payload_event(
        {
            "platform": "QQ_NAPCAT",
            "message_id": "95",
            "conversation_id": "napcat:private:1001",
            "sender_id": "1001",
            "sender_name": "测试联系人",
            "content": "分析引用文件",
            "message_type": "TEXT",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "raw": {
                "event_type": "ONEBOT11_MESSAGE",
                "onebot": {
                    "message": [
                        {"type": "reply", "data": {"id": "legacy-file-id"}},
                        {"type": "text", "data": {"text": "分析引用文件"}},
                    ]
                },
            },
        }
    )
    assert event.reply_to_message_id == "legacy-file-id"


@pytest.mark.asyncio
async def test_napcat_sender_uses_exact_onebot_target_and_token(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        is_success = True

        @staticmethod
        def json():
            return {"status": "ok", "retcode": 0, "data": {"message_id": 12345}}

    class FakeClient:
        def __init__(self, *, timeout: float, **_kwargs) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url: str, *, headers: dict[str, str], json: dict[str, object]):
            captured.update(url=url, headers=headers, json=json)
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    connector = NapCatConnector(
        api_base="http://127.0.0.1:3001",
        access_token="safe-local-token",
        enabled=True,
        observed_status="ONLINE",
    )
    result = await connector.send(
        OutboundMessage("QQ_NAPCAT", "1001", "收到", "in-1", False),
        _permit("1001"),
    )
    assert result.success and result.external_message_id == "12345"
    assert captured["url"] == "http://127.0.0.1:3001/send_private_msg"
    assert captured["headers"] == {"Authorization": "Bearer safe-local-token"}
    assert captured["json"] == {
        "user_id": 1001,
        "message": [
            {"type": "reply", "data": {"id": "in-1"}},
            {"type": "text", "data": {"text": "收到"}},
        ],
    }


@pytest.mark.asyncio
async def test_napcat_sender_uses_native_file_upload_and_media_segments(monkeypatch, tmp_path) -> None:
    calls: list[dict[str, object]] = []
    image = tmp_path / "photo.png"
    document = tmp_path / "report.pdf"
    image.write_bytes(b"png")
    document.write_bytes(b"pdf")

    class FakeResponse:
        is_success = True

        def __init__(self, data: dict[str, object]) -> None:
            self._data = data

        def json(self):
            return {"status": "ok", "retcode": 0, "data": self._data}

    class FakeClient:
        def __init__(self, *, timeout: float, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url: str, *, headers: dict[str, str], json: dict[str, object]):
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse({"file_id": "file-1"} if url.endswith("upload_private_file") else {"message_id": 55})

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    connector = NapCatConnector(api_base="http://127.0.0.1:3001", access_token="token", enabled=True, observed_status="ONLINE")
    result = await connector.send(
        OutboundMessage(
            "QQ_NAPCAT",
            "1001",
            "请查收",
            "in-1",
            False,
            (
                OutboundAttachment("IMAGE", str(image.resolve()), image.name, "image/png"),
                OutboundAttachment("FILE", str(document.resolve()), document.name, "application/pdf"),
            ),
        ),
        _permit("1001"),
    )
    assert result.success and result.external_message_id == "file-1"
    assert calls[0]["url"].endswith("/send_private_msg")
    assert calls[0]["json"]["message"][0] == {"type": "reply", "data": {"id": "in-1"}}
    assert calls[0]["json"]["message"][2] == {"type": "image", "data": {"file": str(image.resolve())}}
    assert calls[1]["url"].endswith("/upload_private_file")
    assert calls[1]["json"] == {"user_id": 1001, "file": str(document.resolve()), "name": "report.pdf"}


@pytest.mark.asyncio
async def test_napcat_sender_retries_one_explicit_transient_http_rejection(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class FakeResponse:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code
            self.is_success = status_code == 200

        def json(self):
            if not self.is_success:
                raise ValueError("temporary non-json gateway response")
            return {"status": "ok", "retcode": 0, "data": {"message_id": 7788}}

    class FakeClient:
        def __init__(self, *, timeout: float, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url: str, *, headers: dict[str, str], json: dict[str, object]):
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse(502 if len(calls) == 1 else 200)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    connector = NapCatConnector(api_base="http://127.0.0.1:3001", access_token="token", enabled=True, observed_status="ONLINE")
    result = await connector.send(
        OutboundMessage("QQ_NAPCAT", "1001", "收到", "", False),
        _permit("1001"),
    )

    assert result.success and result.external_message_id == "7788"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_napcat_sender_does_not_treat_http_failure_as_quote_rejection(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 502
        is_success = False

        @staticmethod
        def json():
            raise ValueError("non-json")

    class FakeClient:
        def __init__(self, *, timeout: float, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url: str, *, headers: dict[str, str], json: dict[str, object]):
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    connector = NapCatConnector(api_base="http://127.0.0.1:3001", access_token="token", enabled=True, observed_status="ONLINE")
    result = await connector.send(
        OutboundMessage("QQ_NAPCAT", "1001", "收到", "quoted-message", False),
        _permit("1001"),
    )

    assert not result.success and result.error_code == "NAPCAT_HTTP_502"
    # One explicit transient retry is allowed, but there is no third quote-less
    # retry because an HTTP failure does not prove that the quote was invalid.
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_autowx_has_no_send_primitive() -> None:
    connector = AutoWxDraftConnector(enabled=True, credential_present=True, observed_status="ONLINE")
    result = await connector.send(
        OutboundMessage("WECHAT_AUTOWX", "wxid-test", "草稿", "in-1", False),
        _permit("wxid-test"),
    )
    assert not result.success
    assert result.error_code == "AUTOWX_DRAFT_ONLY"


@pytest.mark.asyncio
async def test_enabling_napcat_requires_risk_ack_loopback_and_token(db) -> None:
    db.add(
        Account(
            platform="QQ_NAPCAT",
            display_name="NapCat",
            connector_kind="NAPCAT_ONEBOT11",
            experimental=True,
        )
    )
    db.commit()
    with pytest.raises(HTTPException, match="确认"):
        await update_account(
            "QQ_NAPCAT",
            AccountConfigUpdate(
                enabled=True,
                api_base="http://127.0.0.1:3001",
                access_token="safe-local-token",
            ),
            db,
        )
    db.rollback()
    with pytest.raises(HTTPException, match="127.0.0.1"):
        await update_account(
            "QQ_NAPCAT",
            AccountConfigUpdate(
                enabled=False,
                api_base="https://remote.example.com",
                access_token="safe-local-token",
                risk_acknowledged=True,
            ),
            db,
        )
    db.rollback()
    result = await update_account(
        "QQ_NAPCAT",
        AccountConfigUpdate(
            enabled=True,
            api_base="http://127.0.0.1:3001",
            access_token="safe-local-token",
            risk_acknowledged=True,
        ),
        db,
    )
    assert result["enabled"] and result["status"] == "READY"


@pytest.mark.asyncio
async def test_napcat_ingress_accepts_native_signature_or_bearer_and_is_durable(db) -> None:
    db.add(
        Account(
            platform="QQ_NAPCAT",
            display_name="NapCat",
            connector_kind="NAPCAT_ONEBOT11",
            experimental=True,
        )
    )
    db.commit()
    await update_account(
        "QQ_NAPCAT",
        AccountConfigUpdate(
            enabled=True,
            api_base="http://127.0.0.1:3001",
            access_token="safe-local-token",
            risk_acknowledged=True,
        ),
        db,
    )
    payload = {
        "post_type": "message",
        "message_type": "private",
        "self_id": 9000,
        "user_id": 1001,
        "message_id": 91,
        "message": "你好",
    }
    request, body = _napcat_request(payload)
    with pytest.raises(HTTPException) as error:
        await napcat_events(request, payload, BackgroundTasks(), "Bearer wrong-token", None, db)
    assert error.value.status_code == 401

    signature = "sha1=" + hmac.new(b"safe-local-token", body, hashlib.sha1).hexdigest()
    request, _ = _napcat_request(payload)
    ignored = await napcat_events(request, payload, BackgroundTasks(), None, signature, db)
    assert ignored == {"accepted": True, "ignored": True, "reason": "NOT_ALLOWLISTED"}
    assert db.query(ConnectorEvent).count() == 0

    db.add(
        Contact(
            platform="QQ_NAPCAT",
            platform_user_id="1001",
            display_name="NapCat 测试联系人",
            relationship_label="朋友",
            whitelisted=True,
            # Collection is controlled by the whitelist, independently of AI.
            ai_enabled=False,
            importance=Importance.NORMAL,
        )
    )
    db.commit()
    request, _ = _napcat_request(payload)
    result = await napcat_events(request, payload, BackgroundTasks(), "Bearer safe-local-token", None, db)
    assert result["accepted"]
    event = db.get(ConnectorEvent, result["event_id"])
    assert event is not None
    assert event.platform == "QQ_NAPCAT"
    assert event.external_event_id == "91"
    assert event.status == "PENDING"
    assert event.normalized_payload["source_event_type"] == "ONEBOT11_MESSAGE"


@pytest.mark.asyncio
@pytest.mark.parametrize("release_gate", ["SHADOW", "LIVE"])
async def test_napcat_manual_private_reply_is_recorded_in_shadow_and_live(db, release_gate: str) -> None:
    db.add(RuntimeState(id=1, global_mode=GlobalMode.AUTO, release_gate=release_gate))
    contact = Contact(
        platform="QQ_NAPCAT",
        platform_user_id="1001",
        display_name="NapCat 白名单联系人",
        relationship_label="朋友",
        whitelisted=True,
        ai_enabled=True,
        importance=Importance.NORMAL,
    )
    db.add_all([contact, PersonaProfile(id=1)])
    db.commit()
    event = normalize_napcat_event(
        {
            "post_type": "message_sent",
            "message_type": "private",
            "self_id": 9000,
            "user_id": 9000,
            "target_id": 1001,
            "message_id": 301,
            "message": "这是我在 QQ 客户端手动发送的回复",
            "time": 1_777_000_002,
        }
    )
    assert event is not None
    pipeline = MessagePipeline(
        gateway=LLMGateway([SimulatorProvider()]),
        connectors={"QQ_NAPCAT": RecordingNapCatConnector()},
        settings=Settings(),
    )
    result = await pipeline.handle(event, db)
    assert result.code == "HUMAN_TAKEOVER"
    stored = db.scalar(select(Message).where(Message.external_message_id == "301"))
    assert stored is not None
    assert stored.author == MessageAuthor.HUMAN
    assert stored.direction == MessageDirection.OUTBOUND
    assert stored.status == MessageStatus.SENT
    assert stored.receiver_id == contact.platform_user_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("release_gate", "expected_status", "expected_sends"),
    [("SHADOW", MessageStatus.SHADOWED, 0), ("LIVE", MessageStatus.SENT, 1)],
)
async def test_napcat_contact_and_ai_messages_are_recorded_in_shadow_and_live(
    db,
    release_gate: str,
    expected_status: str,
    expected_sends: int,
) -> None:
    db.add(
        RuntimeState(
            id=1,
            global_mode=GlobalMode.AUTO,
            release_gate=release_gate,
            live_time_window_enabled=False,
        )
    )
    if release_gate == "LIVE":
        # Regression: a previously active profile remains stored before the
        # newly selected profile. Policy must never pick this disabled row.
        db.add(
            Account(
                platform="QQ_NAPCAT",
                display_name="Old NapCat",
                connector_kind="NAPCAT_ONEBOT11",
                status="DISCONNECTED",
                enabled=False,
                credential_id="old-credential",
                config={"api_base": "http://127.0.0.1:3002", "risk_acknowledged": True},
            )
        )
        db.add(
            Account(
                platform="QQ_NAPCAT",
                display_name="NapCat",
                connector_kind="NAPCAT_ONEBOT11",
                status="ONLINE",
                enabled=True,
                credential_id="credential-present",
                config={"api_base": "http://127.0.0.1:3001", "risk_acknowledged": True},
            )
        )
    contact = Contact(
        platform="QQ_NAPCAT",
        platform_user_id="1001",
        display_name="NapCat 白名单联系人",
        relationship_label="朋友",
        whitelisted=True,
        ai_enabled=True,
        importance=Importance.NORMAL,
    )
    db.add_all([contact, PersonaProfile(id=1)])
    db.commit()
    connector = RecordingNapCatConnector()
    pipeline = MessagePipeline(
        gateway=LLMGateway([SimulatorProvider()]),
        connectors={"QQ_NAPCAT": connector},
        settings=Settings(),
    )
    result = await pipeline.handle(
        InboundEvent(
            platform="QQ_NAPCAT",
            message_id=f"contact-{release_gate.lower()}",
            conversation_id="napcat:private:1001",
            sender_id=contact.platform_user_id,
            sender_name=contact.display_name,
            content="请记录这条白名单消息",
            timestamp=datetime.now(timezone.utc),
            raw={"event_type": "ONEBOT11_MESSAGE"},
        ),
        db,
    )
    assert result.shadowed is (release_gate == "SHADOW")
    assert result.sent is (release_gate == "LIVE")
    assert len(connector.sent) == expected_sends
    rows = list(db.scalars(select(Message).where(Message.contact_id == contact.id)))
    assert len(rows) == 2
    inbound = next(item for item in rows if item.author == MessageAuthor.CONTACT)
    outbound = next(item for item in rows if item.author == MessageAuthor.AI)
    assert inbound.direction == MessageDirection.INBOUND
    assert inbound.status == MessageStatus.RECEIVED
    assert outbound.direction == MessageDirection.OUTBOUND
    assert outbound.status == expected_status


@pytest.mark.asyncio
async def test_napcat_ai_send_echo_reuses_queued_ai_row_without_human_takeover(db) -> None:
    now = datetime.now(timezone.utc)
    contact = Contact(
        platform="QQ_NAPCAT",
        platform_user_id="1001",
        display_name="NapCat 白名单联系人",
        relationship_label="朋友",
        whitelisted=True,
        ai_enabled=True,
        importance=Importance.NORMAL,
    )
    db.add(contact)
    db.flush()
    conversation = Conversation(platform="QQ_NAPCAT", external_id="napcat:private:1001", contact_id=contact.id)
    db.add(conversation)
    db.flush()
    queued = Message(
        platform="QQ_NAPCAT",
        external_message_id="ai:pending:one",
        conversation_id=conversation.id,
        contact_id=contact.id,
        direction=MessageDirection.OUTBOUND,
        author=MessageAuthor.AI,
        content="AI 即将发送的正文",
        status=MessageStatus.QUEUED,
        created_at=now,
    )
    db.add(queued)
    db.commit()
    event = normalize_napcat_event(
        {
            "post_type": "message_sent",
            "message_type": "private",
            "self_id": 9000,
            "user_id": 9000,
            "target_id": 1001,
            "message_id": 401,
            "message": queued.content,
            "time": int(now.timestamp()),
        }
    )
    assert event is not None
    result = await MessagePipeline(
        gateway=LLMGateway([SimulatorProvider()]),
        connectors={"QQ_NAPCAT": RecordingNapCatConnector()},
        settings=Settings(),
    ).handle(event, db)
    assert result.duplicate
    assert result.code == "AI_SEND_ECHO"
    db.refresh(queued)
    assert queued.external_message_id == "401"
    assert db.scalar(select(func.count(Message.id))) == 1
    assert db.scalar(select(func.count(Message.id)).where(Message.author == MessageAuthor.HUMAN)) == 0


@pytest.mark.asyncio
async def test_dashboard_manual_send_is_stored_as_human_without_starting_takeover(db) -> None:
    class RecordingWithoutExternalId(RecordingNapCatConnector):
        async def send(self, message: OutboundMessage, permit: SendPermit) -> SendResult:
            permit.assert_valid_for(message)
            self.sent.append(message)
            return SendResult(True)

    db.add(RuntimeState(id=1, global_mode=GlobalMode.AUTO, release_gate="LIVE", kill_switch=False))
    account = Account(
        platform="QQ_NAPCAT",
        display_name="当前 NapCat",
        connector_kind="NAPCAT_ONEBOT11",
        status="ONLINE",
        enabled=True,
        credential_id="credential-present",
        config={"api_base": "http://127.0.0.1:3001", "risk_acknowledged": True},
    )
    db.add(account)
    db.flush()
    contact = Contact(
        platform="QQ_NAPCAT",
        account_id=account.id,
        platform_user_id="1001",
        display_name="NapCat 白名单联系人",
        relationship_label="朋友",
        whitelisted=True,
        ai_enabled=True,
        importance=Importance.NORMAL,
    )
    db.add(contact)
    db.flush()
    conversation = Conversation(
        platform="QQ_NAPCAT",
        account_id=account.id,
        external_id="napcat:private:1001",
        contact_id=contact.id,
        mode=ConversationMode.AUTO_READY,
        consecutive_ai_sends=3,
    )
    db.add(conversation)
    db.commit()
    connector = RecordingWithoutExternalId()
    pipeline = MessagePipeline(
        gateway=LLMGateway([SimulatorProvider()]),
        connectors={"QQ_NAPCAT": connector},
        settings=Settings(),
    )

    result = await pipeline.send_dashboard_manual(
        db,
        conversation_id=conversation.id,
        content="  这是后台人工直发  ",
    )

    assert result.sent
    assert result.code == "DASHBOARD_MANUAL_SENT"
    assert len(connector.sent) == 1
    stored = db.get(Message, result.outbound_message_id)
    assert stored is not None
    assert stored.author == MessageAuthor.HUMAN
    assert stored.direction == MessageDirection.OUTBOUND
    assert stored.provider == DASHBOARD_MANUAL_PROVIDER
    assert stored.content == "这是后台人工直发"
    assert stored.status == MessageStatus.SENT
    db.refresh(conversation)
    assert conversation.mode == ConversationMode.AUTO_READY
    assert conversation.human_until is None
    assert conversation.consecutive_ai_sends == 0

    echo = normalize_napcat_event(
        {
            "post_type": "message_sent",
            "message_type": "private",
            "self_id": 9000,
            "user_id": 9000,
            "target_id": 1001,
            "message_id": 402,
            "message": stored.content,
            "time": int(datetime.now(timezone.utc).timestamp()),
        }
    )
    assert echo is not None
    echo = InboundEvent(
        platform=echo.platform,
        account_id=account.id,
        message_id=echo.message_id,
        conversation_id=echo.conversation_id,
        sender_id=echo.sender_id,
        sender_name=echo.sender_name,
        content=echo.content,
        author=echo.author,
        timestamp=echo.timestamp,
        raw=echo.raw,
    )
    echo_result = await pipeline.handle(echo, db)
    assert echo_result.duplicate
    assert echo_result.code == "DASHBOARD_MANUAL_SEND_ECHO"
    assert db.scalar(select(func.count(Message.id)).where(Message.conversation_id == conversation.id)) == 1
    db.refresh(conversation)
    assert conversation.mode == ConversationMode.AUTO_READY
    assert conversation.human_until is None


@pytest.mark.asyncio
async def test_dashboard_manual_send_blocks_a_conversation_from_an_inactive_account(db) -> None:
    db.add(RuntimeState(id=1, global_mode=GlobalMode.AUTO, release_gate="LIVE", kill_switch=False))
    old_account = Account(
        platform="QQ_NAPCAT",
        display_name="旧账号",
        connector_kind="NAPCAT_ONEBOT11",
        status="DISCONNECTED",
        enabled=False,
        credential_id="old-credential",
        config={"api_base": "http://127.0.0.1:3002", "risk_acknowledged": True},
    )
    active_account = Account(
        platform="QQ_NAPCAT",
        display_name="当前账号",
        connector_kind="NAPCAT_ONEBOT11",
        status="ONLINE",
        enabled=True,
        credential_id="active-credential",
        config={"api_base": "http://127.0.0.1:3001", "risk_acknowledged": True},
    )
    db.add_all([old_account, active_account])
    db.flush()
    contact = Contact(
        platform="QQ_NAPCAT",
        account_id=old_account.id,
        platform_user_id="1001",
        display_name="旧账号联系人",
        relationship_label="朋友",
        whitelisted=True,
        ai_enabled=True,
        importance=Importance.NORMAL,
    )
    db.add(contact)
    db.flush()
    conversation = Conversation(
        platform="QQ_NAPCAT",
        account_id=old_account.id,
        external_id="napcat:private:1001",
        contact_id=contact.id,
    )
    db.add(conversation)
    db.commit()
    connector = RecordingNapCatConnector()
    pipeline = MessagePipeline(
        gateway=LLMGateway([SimulatorProvider()]),
        connectors={"QQ_NAPCAT": connector},
        settings=Settings(),
    )

    with pytest.raises(ManualSendError, match="不属于当前托管账号") as error:
        await pipeline.send_dashboard_manual(db, conversation_id=conversation.id, content="不应发送")

    assert error.value.code == "ACCOUNT_ISOLATION_MISMATCH"
    assert connector.sent == []
    assert db.scalar(select(func.count(Message.id))) == 0


@pytest.mark.asyncio
async def test_autowx_is_policy_blocked_in_live_before_any_send(db) -> None:
    db.add(RuntimeState(id=1, global_mode=GlobalMode.AUTO, release_gate="LIVE"))
    db.add(
        Account(
            platform="WECHAT_AUTOWX",
            display_name="AutoWx",
            connector_kind="AUTOWX_LOCAL_BRIDGE",
            status="ONLINE",
            enabled=True,
            experimental=True,
            credential_id="credential-present",
            config={"risk_acknowledged": True, "strategy": "DRAFT_ONLY"},
        )
    )
    db.add(PersonaProfile(id=1))
    contact = Contact(
        platform="WECHAT_AUTOWX",
        platform_user_id="wxid-test",
        display_name="微信测试联系人",
        relationship_label="朋友",
        whitelisted=True,
        ai_enabled=True,
        importance=Importance.NORMAL,
    )
    db.add(contact)
    db.commit()
    connector = AutoWxDraftConnector(enabled=True, credential_present=True, observed_status="ONLINE")
    pipeline = MessagePipeline(
        gateway=LLMGateway([SimulatorProvider()]),
        connectors={"WECHAT_AUTOWX": connector},
        settings=Settings(),
    )
    result = await pipeline.handle(
        InboundEvent(
            platform="WECHAT_AUTOWX",
            message_id="wx-live-1",
            conversation_id="wxid-test",
            sender_id="wxid-test",
            sender_name="微信测试联系人",
            content="你好",
            timestamp=datetime(2026, 8, 25, 12, 0, tzinfo=TZ),
        ),
        db,
    )
    assert not result.sent
    assert result.code == "CHANNEL_DISABLED"


@pytest.mark.asyncio
async def test_live_gate_can_select_one_verified_napcat_channel(db) -> None:
    db.add(
        RuntimeState(
            id=1,
            global_mode=GlobalMode.AUTO,
            release_gate="SHADOW",
            simulation_accepted=True,
            shadow_accepted=True,
        )
    )
    db.add(
        Account(
            platform="QQ_NAPCAT",
            display_name="NapCat",
            connector_kind="NAPCAT_ONEBOT11",
            status="ONLINE",
            enabled=True,
            experimental=True,
            credential_id="credential-present",
            config={
                "api_base": "http://127.0.0.1:3001",
                "risk_acknowledged": True,
                "strategy": "LIMITED_LIVE",
            },
        )
    )
    db.add(
        Contact(
            platform="QQ_NAPCAT",
            platform_user_id="1001",
            display_name="NapCat 测试联系人",
            relationship_label="朋友",
            whitelisted=True,
            ai_enabled=True,
            importance=Importance.NORMAL,
        )
    )
    db.commit()
    state = await update_release_gate(ReleaseGateUpdate(gate="LIVE"), db)
    assert state.release_gate == "LIVE"
