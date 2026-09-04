from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.api.routes import activate_napcat_profile
from app.channels.base import ChannelConnector, OutboundMessage, SendPermit, SendResult
from app.core.enums import GlobalMode, MessageStatus
from app.models.entities import Account, ApiCredential, Contact, Message, RuntimeState
from app.services.account_selection import get_active_account
from app.services.keepalive import KeepaliveService


class RecordingConnector(ChannelConnector):
    platform = "QQ_NAPCAT"
    real_channel = True

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def send(self, message: OutboundMessage, permit: SendPermit) -> SendResult:
        permit.assert_valid_for(message)
        self.sent.append(message)
        return SendResult(True, external_message_id=f"keepalive-sent-{len(self.sent)}")

    async def status(self) -> dict[str, object]:
        return {"platform": self.platform, "status": "ONLINE"}


def _napcat_account(db, suffix: str, *, enabled: bool = True) -> Account:
    credential = ApiCredential(
        label=f"NapCat {suffix}",
        provider="QQ_NAPCAT",
        encrypted_secret="test-only",
        masked_hint="test",
    )
    db.add(credential)
    db.flush()
    account = Account(
        platform="QQ_NAPCAT",
        display_name=f"账号 {suffix}",
        connector_kind="NAPCAT_ONEBOT11",
        status="READY",
        enabled=enabled,
        experimental=True,
        credential_id=credential.id,
        config={
            "api_base": f"http://127.0.0.1:{3000 + int(suffix)}",
            "managed_qq_id": f"1000000{suffix}",
            "risk_acknowledged": True,
        },
    )
    db.add(account)
    db.flush()
    return account


@pytest.mark.asyncio
async def test_non_whitelisted_contact_can_only_use_explicit_daily_keepalive(db, monkeypatch) -> None:
    db.add(
        RuntimeState(
            id=1,
            global_mode=GlobalMode.AUTO,
            release_gate="LIVE",
            kill_switch=False,
            live_time_window_enabled=False,
        )
    )
    account = _napcat_account(db, "1")
    contact = Contact(
        platform="QQ_NAPCAT",
        platform_user_id="2042360010",
        display_name="续火联系人",
        whitelisted=False,
        ai_enabled=False,
        keepalive_enabled=True,
        keepalive_time="20:00",
        keepalive_account_id=account.id,
    )
    db.add(contact)
    db.commit()
    connector = RecordingConnector()
    monkeypatch.setattr("app.services.keepalive.build_napcat_connector", lambda *_: connector)

    service = KeepaliveService()
    sent = await service.run_due(db, datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc))
    repeated = await service.run_due(db, datetime(2026, 8, 26, 13, 0, tzinfo=timezone.utc))

    assert sent == 1
    assert repeated == 0
    assert len(connector.sent) == 1
    assert connector.sent[0].target_id == contact.platform_user_id
    assert contact.keepalive_last_attempt_on == "2026-08-26"
    assert contact.keepalive_last_status == "SENT"
    stored = db.scalar(select(Message).where(Message.contact_id == contact.id, Message.message_type == "KEEPALIVE"))
    assert stored is not None
    assert stored.status == MessageStatus.SENT
    assert stored.provider == "LOCAL_TEMPLATE"


@pytest.mark.asyncio
async def test_keepalive_respects_kill_switch_and_release_gate(db, monkeypatch) -> None:
    state = RuntimeState(
        id=1,
        global_mode=GlobalMode.AUTO,
        release_gate="LIVE",
        kill_switch=True,
        live_time_window_enabled=False,
    )
    db.add(state)
    account = _napcat_account(db, "2")
    contact = Contact(
        platform="QQ_NAPCAT",
        platform_user_id="2042360011",
        display_name="受控续火",
        whitelisted=False,
        keepalive_enabled=True,
        keepalive_time="20:00",
        keepalive_account_id=account.id,
    )
    db.add(contact)
    db.commit()
    connector = RecordingConnector()
    monkeypatch.setattr("app.services.keepalive.build_napcat_connector", lambda *_: connector)
    service = KeepaliveService()

    assert await service.run_due(db, datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)) == 0
    assert contact.keepalive_last_attempt_on is None
    state.kill_switch = False
    state.release_gate = "SHADOW"
    db.commit()
    assert await service.run_due(db, datetime(2026, 8, 26, 12, 1, tzinfo=timezone.utc)) == 0
    state.release_gate = "LIVE"
    db.commit()
    assert await service.run_due(db, datetime(2026, 8, 26, 12, 2, tzinfo=timezone.utc)) == 1


@pytest.mark.asyncio
async def test_activating_profile_is_atomic_and_disables_previous_account(db) -> None:
    first = _napcat_account(db, "3", enabled=True)
    second = _napcat_account(db, "4", enabled=False)
    db.commit()

    result = await activate_napcat_profile(second.id, db)

    db.refresh(first)
    db.refresh(second)
    assert result["id"] == second.id
    assert second.enabled is True
    assert second.status == "READY"
    assert first.enabled is False
    assert first.status == "DISCONNECTED"
    assert get_active_account(db, "QQ_NAPCAT").id == second.id


def test_active_account_selection_fails_closed_when_invariant_is_broken(db) -> None:
    first = _napcat_account(db, "5", enabled=True)
    second = _napcat_account(db, "6", enabled=True)
    db.commit()

    assert first.id != second.id
    assert get_active_account(db, "QQ_NAPCAT") is None


def test_connector_permit_distinguishes_keepalive_from_normal_whitelist() -> None:
    message = OutboundMessage("QQ_NAPCAT", "123456", "续火", "daily")
    permit = SendPermit("123456", False, True, True, True, True, True, scheduled_allowed=True)
    permit.assert_valid_for(message)
