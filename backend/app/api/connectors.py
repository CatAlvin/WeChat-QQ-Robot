from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
from dataclasses import asdict, replace
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.channels.base import InboundAttachment, InboundEvent
from app.channels.experimental import ExperimentalConnectorError, normalize_autowx_event, normalize_napcat_event
from app.channels.qq_webhook import QQWebhookError, decode_payload, normalize_dispatch, sign_validation_response, verify_webhook_signature
from app.core.config import get_settings
from app.core.clock import utc_now
from app.core.events import event_hub
from app.core.security import CredentialVault
from app.database import get_db, session_scope
from app.models.entities import Account, ApiCredential, AuditLog, ChatGroup, ConnectorEvent, Contact, Incident
from app.services.account_selection import get_active_account
from app.services.factory import build_pipeline


router = APIRouter(prefix="/connectors", tags=["connectors"])


def _qq_secret(db: Session) -> str:
    account = db.scalar(select(Account).where(Account.platform == "QQ"))
    if account is None or not account.credential_id:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="QQ AppSecret 尚未配置")
    credential = db.get(ApiCredential, account.credential_id)
    if credential is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="QQ AppSecret 尚未配置")
    try:
        return CredentialVault().decrypt(credential.encrypted_secret)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="QQ AppSecret 无法解密") from exc


def _experimental_token(db: Session, platform: str) -> str:
    account = get_active_account(db, platform)
    if account is None or not account.enabled or not account.credential_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="实验连接器尚未启用或未配置 Token")
    credential = db.get(ApiCredential, account.credential_id)
    if credential is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="实验连接器 Token 不可用")
    try:
        return CredentialVault().decrypt(credential.encrypted_secret)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="实验连接器 Token 无法解密") from exc


def _verify_bearer(authorization: str | None, expected: str) -> None:
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="实验连接器认证失败")


def _verify_napcat_auth(
    authorization: str | None,
    x_signature: str | None,
    expected: str,
    raw_body: bytes,
) -> None:
    """Accept Bearer auth or NapCat HTTP client's native HMAC-SHA1 signature."""
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() == "bearer" and supplied and hmac.compare_digest(supplied, expected):
        return
    expected_signature = "sha1=" + hmac.new(expected.encode("utf-8"), raw_body, hashlib.sha1).hexdigest()
    if not x_signature or not hmac.compare_digest(x_signature.strip().lower(), expected_signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="NapCat 连接器认证失败")


def _experimental_event_allowed(db: Session, event: InboundEvent) -> bool:
    """Fail closed before persisting content from personal-account automation."""
    if event.is_group:
        if not event.group_id or not event.mentioned_user:
            return False
        group = db.scalar(
            select(ChatGroup).where(
                ChatGroup.platform == event.platform,
                ChatGroup.account_id == event.account_id,
                ChatGroup.platform_group_id == event.group_id,
            )
        )
        if group is None and event.account_id:
            group = db.scalar(
                select(ChatGroup).where(
                    ChatGroup.platform == event.platform,
                    ChatGroup.account_id.is_(None),
                    ChatGroup.platform_group_id == event.group_id,
                )
            )
            if group is not None:
                group.account_id = event.account_id
        # An allowed group may still have AI disabled. Persisting the event and
        # denying generation later keeps collection separate from send policy.
        return bool(group and group.allowed)
    contact = db.scalar(
        select(Contact).where(
            Contact.platform == event.platform,
            Contact.account_id == event.account_id,
            Contact.platform_user_id == event.sender_id,
        )
    )
    if contact is None and event.account_id:
        contact = db.scalar(
            select(Contact).where(
                Contact.platform == event.platform,
                Contact.account_id.is_(None),
                Contact.platform_user_id == event.sender_id,
            )
        )
        if contact is not None:
            contact.account_id = event.account_id
    # Whitelisting controls the data collection boundary. ai_enabled controls
    # whether a stored inbound message is allowed to reach the model.
    return bool(contact and contact.whitelisted)


def _ignore_unallowed_experimental_event(db: Session, event: InboundEvent) -> dict[str, Any]:
    db.add(
        AuditLog(
            event="EXPERIMENTAL_EVENT_IGNORED",
            detail={"platform": event.platform, "is_group": event.is_group, "reason": "NOT_ALLOWLISTED"},
        )
    )
    db.commit()
    return {"accepted": True, "ignored": True, "reason": "NOT_ALLOWLISTED"}


_connector_event_lock = asyncio.Lock()


def _connector_exception_detail(exc: Exception) -> str:
    """Return a bounded diagnostic without persisting arbitrary payload data."""
    error_type = type(exc).__name__
    if isinstance(exc, NameError):
        missing_name = str(getattr(exc, "name", "") or "").strip()
        if missing_name:
            return f"{error_type}:{missing_name}"[:120]
    return error_type[:120]


def _event_payload(event: InboundEvent) -> dict[str, Any]:
    return {
        "platform": event.platform,
        "account_id": event.account_id,
        "message_id": event.message_id,
        "conversation_id": event.conversation_id,
        "sender_id": event.sender_id,
        "sender_name": event.sender_name,
        "content": event.content,
        "is_group": event.is_group,
        "group_id": event.group_id,
        "group_name": event.group_name,
        "mentioned_user": event.mentioned_user,
        "author": event.author,
        "message_type": event.message_type,
        "reply_to_message_id": event.reply_to_message_id,
        "attachments": [asdict(item) for item in event.attachments],
        "timestamp": event.timestamp.isoformat(),
        "source_event_type": str(event.raw.get("event_type") or ""),
        "raw": event.raw,
    }


def _reply_id_from_payload(payload: dict[str, Any]) -> str | None:
    direct = str(payload.get("reply_to_message_id") or "").strip()
    if direct:
        return direct
    raw = payload.get("raw")
    onebot = raw.get("onebot") if isinstance(raw, dict) and isinstance(raw.get("onebot"), dict) else {}
    message = onebot.get("message") if isinstance(onebot, dict) else None
    if isinstance(message, list):
        for segment in message:
            if not isinstance(segment, dict) or str(segment.get("type") or "") != "reply":
                continue
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            candidate = str(data.get("id") or "").strip()
            if candidate:
                return candidate
    raw_message = str(onebot.get("raw_message") or "") if isinstance(onebot, dict) else ""
    match = re.search(r"\[CQ:reply,id=([^,\]]+)", raw_message)
    return match.group(1) if match else None


def _payload_event(payload: dict[str, Any]) -> InboundEvent:
    return InboundEvent(
        platform=str(payload["platform"]),
        account_id=str(payload["account_id"]) if payload.get("account_id") else None,
        message_id=str(payload["message_id"]),
        conversation_id=str(payload["conversation_id"]),
        sender_id=str(payload["sender_id"]),
        sender_name=str(payload["sender_name"]),
        content=str(payload["content"]),
        is_group=bool(payload.get("is_group")),
        group_id=str(payload["group_id"]) if payload.get("group_id") else None,
        group_name=str(payload["group_name"]) if payload.get("group_name") else None,
        mentioned_user=bool(payload.get("mentioned_user")),
        author=str(payload.get("author") or "CONTACT"),
        message_type=str(payload.get("message_type") or "TEXT"),
        reply_to_message_id=_reply_id_from_payload(payload),
        attachments=tuple(
            InboundAttachment(
                kind=str(item.get("kind") or "FILE"),
                segment_type=str(item.get("segment_type") or "file"),
                segment_index=int(item.get("segment_index") or 0),
                file_name=str(item.get("file_name") or "附件"),
                source_ref=str(item["source_ref"]) if item.get("source_ref") else None,
                file_id=str(item["file_id"]) if item.get("file_id") else None,
                size_bytes=int(item["size_bytes"]) if item.get("size_bytes") is not None else None,
                mime_type=str(item["mime_type"]) if item.get("mime_type") else None,
                metadata=dict(item.get("metadata") or {}),
            )
            for item in payload.get("attachments", [])
            if isinstance(item, dict)
        ),
        timestamp=datetime.fromisoformat(str(payload["timestamp"])),
        raw=dict(payload.get("raw") or {"event_type": str(payload.get("source_event_type") or "")}),
    )


def _enqueue_connector_event(db: Session, event: InboundEvent, event_type: str) -> ConnectorEvent:
    item = db.scalar(
        select(ConnectorEvent).where(
            ConnectorEvent.platform == event.platform,
            ConnectorEvent.account_id == event.account_id,
            ConnectorEvent.external_event_id == event.message_id,
        )
    )
    if item is None:
        item = ConnectorEvent(
            platform=event.platform,
            account_id=event.account_id,
            external_event_id=event.message_id,
            event_type=event_type,
            normalized_payload=_event_payload(event),
            status="PENDING",
        )
        db.add(item)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            item = db.scalar(
                select(ConnectorEvent).where(
                    ConnectorEvent.platform == event.platform,
                    ConnectorEvent.account_id == event.account_id,
                    ConnectorEvent.external_event_id == event.message_id,
                )
            )
            if item is None:
                raise
    account = db.get(Account, event.account_id) if event.account_id else get_active_account(db, event.platform)
    if account is not None and event.platform != "QQ_NAPCAT":
        account.status = "ONLINE"
        account.last_error = None
    db.commit()
    return item


def _enqueue_official_event(db: Session, event: InboundEvent, event_type: str) -> ConnectorEvent:
    return _enqueue_connector_event(db, event, event_type)


async def process_pending_connector_event(event_id: str) -> None:
    """Process durable inbox events in arrival order without holding request sessions."""

    async with _connector_event_lock:
        await _process_pending_connector_event(event_id)


async def _process_pending_connector_event(event_id: str) -> None:
    """Claim and finish one event while the caller owns the ordering lock."""

    with session_scope() as db:
        item = db.get(ConnectorEvent, event_id)
        if item is None or item.status in {"DONE", "FAILED", "PROCESSING"}:
            return
        if item.attempts >= 3:
            item.status = "FAILED"
            item.last_error = "RETRY_LIMIT"
            db.add(
                Incident(
                    kind="CONNECTOR_EVENT_PROCESSING_FAILURE",
                    title="连接器事件处理重试已停止",
                    detail=f"{item.platform}:RETRY_LIMIT",
                )
            )
            return
        item.status = "PROCESSING"
        item.attempts += 1
        payload = dict(item.normalized_payload)

    event = _payload_event(payload)
    try:
        with session_scope() as db:
            await build_pipeline(db).handle(event, db)
            item = db.get(ConnectorEvent, event_id)
            if item is not None:
                item.status = "DONE"
                item.last_error = None
    except Exception as exc:
        error_detail = _connector_exception_detail(exc)
        final_failure = False
        with session_scope() as db:
            item = db.get(ConnectorEvent, event_id)
            if item is not None:
                final_failure = item.attempts >= 3
                item.status = "FAILED" if final_failure else "PENDING"
                item.last_error = error_detail
            if final_failure:
                db.add(
                    Incident(
                        kind="CONNECTOR_EVENT_PROCESSING_FAILURE",
                        title="连接器事件处理失败",
                        detail=f"{event.platform}:{error_detail}",
                    )
                )
        await event_hub.broadcast("account_state", {"platform": event.platform, "status": "ERROR"})
        if final_failure:
            await event_hub.broadcast("incident", {"kind": "CONNECTOR_EVENT_PROCESSING_FAILURE"})


@router.post("/qq/webhook")
async def qq_official_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Direct QQ Open Platform webhook transport (op 13 validation + op 12 ACK)."""
    raw_body = await request.body()
    try:
        payload = decode_payload(raw_body)
    except QQWebhookError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    secret = _qq_secret(db)
    if payload["op"] == 13:
        data = payload.get("d")
        if not isinstance(data, dict) or not data.get("plain_token") or not data.get("event_ts"):
            raise HTTPException(status_code=400, detail="QQ 回调验证缺少 plain_token 或 event_ts")
        return JSONResponse(sign_validation_response(secret, str(data["event_ts"]), str(data["plain_token"])))

    timestamp = request.headers.get("x-signature-timestamp", "")
    signature = request.headers.get("x-signature-ed25519", "")
    if not timestamp or not signature:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="QQ 回调缺少签名")
    if not verify_webhook_signature(secret, timestamp, raw_body, signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="QQ 回调签名无效")

    if payload["op"] == 0:
        account = db.scalar(select(Account).where(Account.platform == "QQ"))
        if account is None or not account.enabled:
            db.add(AuditLog(event="QQ_WEBHOOK_IGNORED_DISABLED", level="WARNING", detail={"event_type": str(payload.get("t") or "")}))
            db.commit()
            return JSONResponse({"op": 12, "d": 0})
        event_type = str(payload.get("t") or "")
        try:
            event = normalize_dispatch(event_type, payload.get("d"))
        except QQWebhookError as exc:
            db.add(Incident(kind="QQ_WEBHOOK_UNSUPPORTED", title="QQ 消息未进入自动回复", detail=str(exc)))
            db.add(AuditLog(event="QQ_WEBHOOK_REJECTED", level="WARNING", detail={"event_type": event_type, "reason": str(exc)}))
            db.commit()
            background_tasks.add_task(event_hub.broadcast, "incident", {"kind": "QQ_WEBHOOK_UNSUPPORTED"})
        else:
            if event is not None:
                event = replace(event, account_id=account.id)
                inbox = _enqueue_official_event(db, event, event_type)
                background_tasks.add_task(event_hub.broadcast, "account_state", {"platform": "QQ", "status": "ONLINE"})
                background_tasks.add_task(process_pending_connector_event, inbox.id)
            else:
                db.add(AuditLog(event="QQ_WEBHOOK_IGNORED", detail={"event_type": event_type}))
                db.commit()

    return JSONResponse({"op": 12, "d": 0})


@router.post("/napcat/events")
async def napcat_events(
    request: Request,
    payload: dict[str, Any],
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
    x_signature: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Authenticated OneBot 11 HTTP event ingress for a local NapCat instance."""
    _verify_napcat_auth(
        authorization,
        x_signature,
        _experimental_token(db, "QQ_NAPCAT"),
        await request.body(),
    )
    try:
        event = normalize_napcat_event(payload)
    except ExperimentalConnectorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if event is None:
        return {"accepted": True, "ignored": True}
    account = get_active_account(db, "QQ_NAPCAT")
    if account is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="NapCat 托管账号未启用")
    event = replace(event, account_id=account.id)
    if not _experimental_event_allowed(db, event):
        return _ignore_unallowed_experimental_event(db, event)
    inbox = _enqueue_connector_event(db, event, str(payload.get("message_type") or "ONEBOT11_EVENT"))
    background_tasks.add_task(event_hub.broadcast, "account_state", {"platform": event.platform, "status": "ONLINE"})
    background_tasks.add_task(process_pending_connector_event, inbox.id)
    return {"accepted": True, "event_id": inbox.id}


@router.post("/autowx/events")
async def autowx_events(
    payload: dict[str, Any],
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Receive-only ingress for an explicitly configured local AutoWx bridge."""
    _verify_bearer(authorization, _experimental_token(db, "WECHAT_AUTOWX"))
    try:
        event = normalize_autowx_event(payload)
    except ExperimentalConnectorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    account = get_active_account(db, "WECHAT_AUTOWX")
    if account is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="AutoWx 托管账号未启用")
    event = replace(event, account_id=account.id)
    if not _experimental_event_allowed(db, event):
        return _ignore_unallowed_experimental_event(db, event)
    inbox = _enqueue_connector_event(db, event, "AUTOWX_LOCAL_EVENT")
    background_tasks.add_task(event_hub.broadcast, "account_state", {"platform": event.platform, "status": "ONLINE"})
    background_tasks.add_task(process_pending_connector_event, inbox.id)
    return {"accepted": True, "draft_only": True, "event_id": inbox.id}


@router.post("/qq/events")
async def qq_event(
    payload: dict[str, Any],
    x_neko_connector_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    expected = get_settings().connector_shared_token
    if not expected:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="QQ 入站连接器尚未启用")
    if not x_neko_connector_token or not hmac.compare_digest(x_neko_connector_token, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="连接器认证失败")
    account = db.scalar(select(Account).where(Account.platform == "QQ"))
    if account is None or not account.enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="QQ 连接器已关闭")
    message_id = str(payload.get("id") or payload.get("message_id") or "")
    author = payload.get("author") or {}
    sender_id = str(author.get("member_openid") or author.get("user_openid") or author.get("id") or payload.get("openid") or "")
    group_id = payload.get("group_openid") or payload.get("group_id")
    content = str(payload.get("content") or "").strip()
    if not (message_id and sender_id and content):
        raise HTTPException(status_code=422, detail="消息事件缺少 id、发送者或正文")
    mentioned_user = payload.get("mentioned_user") is True
    timestamp = datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00")) if payload.get("timestamp") else utc_now()
    event = InboundEvent(
        platform="QQ",
        account_id=account.id,
        message_id=message_id,
        conversation_id=f"group:{group_id}" if group_id else f"c2c:{sender_id}",
        sender_id=sender_id,
        sender_name=str(author.get("username") or author.get("nickname") or "QQ 用户"),
        content=content,
        is_group=bool(group_id),
        group_id=str(group_id) if group_id else None,
        group_name=str(payload.get("group_name") or "QQ 群聊") if group_id else None,
        mentioned_user=mentioned_user,
        timestamp=timestamp,
    )
    account.status = "ONLINE"
    account.last_error = None
    db.commit()
    result = await build_pipeline(db).handle(event, db)
    return {"accepted": result.accepted, "code": result.code, "sent": result.sent, "shadowed": result.shadowed}
