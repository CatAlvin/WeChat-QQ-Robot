from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager

import httpx
import pytest
from fastapi import FastAPI

from app.api import connectors
from app.channels.qq_webhook import (
    QQWebhookError,
    _private_key,
    normalize_dispatch,
    sign_validation_response,
    verify_webhook_signature,
)
from app.channels.base import InboundEvent
from app.core.security import CredentialVault, mask_secret
from app.database import get_db
from app.models.entities import Account, ApiCredential, AuditLog, ConnectorEvent, Incident


TEST_SECRET = "DG5g3B4j9X2KOErG"


@pytest.mark.asyncio
async def test_durable_inbox_processing_is_serialized(monkeypatch):
    active = 0
    maximum = 0
    order: list[str] = []

    async def fake_process(event_id: str) -> None:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        order.append(f"start:{event_id}")
        await asyncio.sleep(0.01)
        order.append(f"end:{event_id}")
        active -= 1

    monkeypatch.setattr(connectors, "_process_pending_connector_event", fake_process)

    await asyncio.gather(
        connectors.process_pending_connector_event("first"),
        connectors.process_pending_connector_event("second"),
    )

    assert maximum == 1
    assert order == ["start:first", "end:first", "start:second", "end:second"]


def _signed_headers(raw: bytes, timestamp: str = "1725442341") -> dict[str, str]:
    signature = _private_key(TEST_SECRET).sign(timestamp.encode() + raw).hex()
    return {"X-Signature-Timestamp": timestamp, "X-Signature-Ed25519": signature}


def _configured_app(db) -> FastAPI:
    credential = ApiCredential(
        label="QQ test secret",
        provider="QQ",
        encrypted_secret=CredentialVault().encrypt(TEST_SECRET),
        masked_hint=mask_secret(TEST_SECRET),
    )
    db.add(credential)
    db.flush()
    db.add(
        Account(
            platform="QQ",
            display_name="QQ Official Bot",
            connector_kind="QQ_OFFICIAL_BOT",
            status="READY",
            enabled=True,
            credential_id=credential.id,
            config={"app_id": "11111111"},
        )
    )
    db.commit()

    app = FastAPI()
    app.include_router(connectors.router, prefix="/api")

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    return app


def test_official_ed25519_roundtrip_and_tamper_rejection():
    raw = b'{"op":0,"t":"C2C_MESSAGE_CREATE","d":{}}'
    headers = _signed_headers(raw)
    assert verify_webhook_signature(TEST_SECRET, headers["X-Signature-Timestamp"], raw, headers["X-Signature-Ed25519"])
    assert not verify_webhook_signature(TEST_SECRET, headers["X-Signature-Timestamp"], raw + b" ", headers["X-Signature-Ed25519"])
    assert not verify_webhook_signature("wrong-secret", headers["X-Signature-Timestamp"], raw, headers["X-Signature-Ed25519"])


def test_validation_response_signs_event_ts_plus_plain_token():
    result = sign_validation_response(TEST_SECRET, "1725442341", "plain-token")
    assert result["plain_token"] == "plain-token"
    assert len(result["signature"]) == 128
    _private_key(TEST_SECRET).public_key().verify(bytes.fromhex(result["signature"]), b"1725442341plain-token")


def test_normalizes_c2c_and_keeps_generic_group_mention_closed():
    c2c = normalize_dispatch(
        "C2C_MESSAGE_CREATE",
        {"id": "m1", "content": "  hello  ", "timestamp": "2026-08-24T12:00:00+08:00", "author": {"user_openid": "u1"}},
    )
    assert c2c is not None
    assert c2c.sender_id == "u1" and c2c.content == "hello" and not c2c.is_group

    group = normalize_dispatch(
        "GROUP_MESSAGE_CREATE",
        {
            "id": "m2",
            "content": "hello",
            "timestamp": "2026-08-24T12:00:00+08:00",
            "group_openid": "g1",
            "mentions": [{"id": "someone-else"}],
            "author": {"member_openid": "u2"},
        },
    )
    assert group is not None
    assert group.is_group and not group.mentioned_user

    at_group = normalize_dispatch(
        "GROUP_AT_MESSAGE_CREATE",
        {"id": "m3", "content": "hello", "timestamp": "2026-08-24T12:00:00+08:00", "group_openid": "g1", "author": {"member_openid": "u2"}},
    )
    assert at_group is not None and at_group.mentioned_user


def test_media_only_and_malformed_events_never_enter_pipeline():
    with pytest.raises(QQWebhookError, match="纯媒体"):
        normalize_dispatch(
            "C2C_MESSAGE_CREATE",
            {"id": "m1", "content": "", "timestamp": "2026-08-24T12:00:00+08:00", "attachments": [{}], "author": {"user_openid": "u1"}},
        )
    with pytest.raises(QQWebhookError, match="发送者"):
        normalize_dispatch("C2C_MESSAGE_CREATE", {"id": "m1", "content": "hi", "timestamp": "2026-08-24T12:00:00+08:00", "author": {}})


@pytest.mark.asyncio
async def test_webhook_validation_and_signed_ack(db, monkeypatch):
    app = _configured_app(db)
    received = []

    async def capture(event_id):
        received.append(event_id)

    monkeypatch.setattr(connectors, "process_pending_connector_event", capture)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        validation_raw = json.dumps(
            {"op": 13, "d": {"plain_token": "abc", "event_ts": "123"}},
            separators=(",", ":"),
        ).encode()
        validation = await client.post(
            "/api/connectors/qq/webhook",
            content=validation_raw,
        )
        assert validation.status_code == 200
        assert validation.headers["content-type"] == "application/json"
        assert validation.json()["plain_token"] == "abc"

        payload = {
            "op": 0,
            "t": "C2C_MESSAGE_CREATE",
            "d": {"id": "msg-1", "content": "hello", "timestamp": "2026-08-24T12:00:00+08:00", "author": {"user_openid": "user-1"}},
        }
        raw = json.dumps(payload, separators=(",", ":")).encode()
        response = await client.post("/api/connectors/qq/webhook", content=raw, headers=_signed_headers(raw))

    assert response.status_code == 200
    assert response.json() == {"op": 12, "d": 0}
    assert len(received) == 1
    inbox = db.get(ConnectorEvent, received[0])
    assert inbox is not None
    assert inbox.status == "PENDING"
    assert inbox.external_event_id == "msg-1"
    assert inbox.normalized_payload["message_id"] == "msg-1"


@pytest.mark.asyncio
async def test_webhook_rejects_missing_or_invalid_signature(db):
    app = _configured_app(db)
    raw = b'{"op":0,"t":"C2C_MESSAGE_CREATE","d":{}}'
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.post("/api/connectors/qq/webhook", content=raw)
        invalid = await client.post(
            "/api/connectors/qq/webhook",
            content=raw,
            headers={"X-Signature-Timestamp": "123", "X-Signature-Ed25519": "0" * 128},
        )
    assert missing.status_code == 401
    assert invalid.status_code == 401


@pytest.mark.asyncio
async def test_webhook_validation_does_not_require_request_signature(db):
    app = _configured_app(db)
    raw = b'{"op":13,"d":{"plain_token":"abc","event_ts":"123"}}'
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/connectors/qq/webhook", content=raw)
    assert response.status_code == 200
    assert response.json()["plain_token"] == "abc"


@pytest.mark.asyncio
async def test_disabled_account_acknowledges_signed_webhook_without_enqueueing(db, monkeypatch):
    app = _configured_app(db)
    account = db.query(Account).filter(Account.platform == "QQ").one()
    account.enabled = False
    db.commit()
    received = []

    async def capture(event_id):
        received.append(event_id)

    monkeypatch.setattr(connectors, "process_pending_connector_event", capture)
    payload = {
        "op": 0,
        "t": "C2C_MESSAGE_CREATE",
        "d": {"id": "disabled-1", "content": "hello", "timestamp": "2026-08-24T12:00:00+08:00", "author": {"user_openid": "user-1"}},
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/connectors/qq/webhook", content=raw, headers=_signed_headers(raw))

    assert response.status_code == 200
    assert response.json() == {"op": 12, "d": 0}
    assert received == []
    assert db.scalar(db.query(ConnectorEvent).statement) is None
    assert db.query(AuditLog).filter(AuditLog.event == "QQ_WEBHOOK_IGNORED_DISABLED").one_or_none() is not None


@pytest.mark.asyncio
async def test_durable_inbox_is_claimed_and_marked_done(db, monkeypatch):
    _configured_app(db)
    event = InboundEvent(
        platform="QQ",
        message_id="durable-1",
        conversation_id="c2c:user-1",
        sender_id="user-1",
        sender_name="QQ 用户",
        content="你好",
    )
    inbox = connectors._enqueue_official_event(db, event, "C2C_MESSAGE_CREATE")
    handled = []

    class FakePipeline:
        async def handle(self, value, request_db):
            handled.append(value)

    @contextmanager
    def fake_scope():
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise

    monkeypatch.setattr(connectors, "session_scope", fake_scope)
    monkeypatch.setattr(connectors, "build_pipeline", lambda request_db: FakePipeline())
    await connectors.process_pending_connector_event(inbox.id)
    db.refresh(inbox)
    assert inbox.status == "DONE"
    assert inbox.attempts == 1
    assert len(handled) == 1 and handled[0].message_id == "durable-1"


@pytest.mark.asyncio
async def test_durable_inbox_records_one_bounded_incident_after_final_retry(db, monkeypatch):
    _configured_app(db)
    event = InboundEvent(
        platform="QQ",
        message_id="durable-failure-1",
        conversation_id="c2c:user-1",
        sender_id="user-1",
        sender_name="QQ 用户",
        content="触发失败",
    )
    inbox = connectors._enqueue_official_event(db, event, "C2C_MESSAGE_CREATE")

    class FailingPipeline:
        async def handle(self, value, request_db):
            raise NameError("name 'Account' is not defined", name="Account")

    @contextmanager
    def fake_scope():
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise

    monkeypatch.setattr(connectors, "session_scope", fake_scope)
    monkeypatch.setattr(connectors, "build_pipeline", lambda request_db: FailingPipeline())

    for _ in range(3):
        await connectors.process_pending_connector_event(inbox.id)

    db.refresh(inbox)
    assert inbox.status == "FAILED"
    assert inbox.attempts == 3
    assert inbox.last_error == "NameError:Account"
    incidents = list(db.scalars(db.query(Incident).filter(Incident.kind == "CONNECTOR_EVENT_PROCESSING_FAILURE").statement))
    assert len(incidents) == 1
    assert incidents[0].detail == "QQ:NameError:Account"
