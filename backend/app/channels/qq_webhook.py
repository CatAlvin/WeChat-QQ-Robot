from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.channels.base import InboundEvent


class QQWebhookError(ValueError):
    """A public QQ webhook payload cannot be normalized safely."""


SUPPORTED_MESSAGE_EVENTS = {
    "C2C_MESSAGE_CREATE",
    "GROUP_AT_MESSAGE_CREATE",
    "GROUP_MESSAGE_CREATE",
}


def _derive_seed(app_secret: str) -> bytes:
    """Mirror QQ's documented repeat-and-truncate Ed25519 seed derivation."""
    if not app_secret:
        raise QQWebhookError("QQ AppSecret 未配置")
    seed = app_secret
    while len(seed) < 32:
        seed += seed
    value = seed[:32].encode("utf-8")
    if len(value) != 32:
        raise QQWebhookError("QQ AppSecret 必须使用平台签发的 ASCII 格式")
    return value


def _private_key(app_secret: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(_derive_seed(app_secret))


def sign_validation_response(app_secret: str, event_ts: str, plain_token: str) -> dict[str, str]:
    message = f"{event_ts}{plain_token}".encode("utf-8")
    signature = _private_key(app_secret).sign(message).hex()
    return {"plain_token": plain_token, "signature": signature}


def verify_webhook_signature(app_secret: str, timestamp: str, raw_body: bytes, signature: str) -> bool:
    if not timestamp or not signature or len(signature) != 128:
        return False
    try:
        signature_bytes = bytes.fromhex(signature)
        _private_key(app_secret).public_key().verify(signature_bytes, timestamp.encode("utf-8") + raw_body)
        return True
    except (InvalidSignature, ValueError, QQWebhookError):
        return False


def decode_payload(raw_body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QQWebhookError("QQ 回调不是有效 JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("op"), int):
        raise QQWebhookError("QQ 回调缺少有效 op")
    return payload


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise QQWebhookError("消息事件缺少 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QQWebhookError("消息 timestamp 格式无效") from exc
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def normalize_dispatch(event_type: str, data: Any) -> InboundEvent | None:
    """Normalize current QQ C2C/group text events; ignore bot self-echoes."""
    if event_type not in SUPPORTED_MESSAGE_EVENTS:
        return None
    if not isinstance(data, dict):
        raise QQWebhookError("QQ 消息事件 d 必须是对象")

    author = data.get("author")
    if not isinstance(author, dict):
        raise QQWebhookError("QQ 消息事件缺少 author")
    if author.get("bot") is True:
        return None

    is_group = event_type in {"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"}
    sender_id = author.get("member_openid") if is_group else author.get("user_openid")
    group_id = data.get("group_openid") if is_group else None
    message_id = data.get("id")
    content = data.get("content")

    if not isinstance(message_id, str) or not message_id:
        raise QQWebhookError("QQ 消息事件缺少 id")
    if not isinstance(sender_id, str) or not sender_id:
        raise QQWebhookError("QQ 消息事件缺少发送者 OpenID")
    if is_group and (not isinstance(group_id, str) or not group_id):
        raise QQWebhookError("QQ 群消息事件缺少 group_openid")
    if not isinstance(content, str) or not content.strip():
        # Voice/image replies remain closed until their deterministic gates exist.
        raise QQWebhookError("空正文或纯媒体消息不会进入自动回复管线")

    sender_name = author.get("username") or author.get("nickname") or "QQ 用户"
    return InboundEvent(
        platform="QQ",
        message_id=message_id,
        conversation_id=f"group:{group_id}" if is_group else f"c2c:{sender_id}",
        sender_id=sender_id,
        sender_name=str(sender_name),
        content=content.strip(),
        is_group=is_group,
        group_id=str(group_id) if group_id else None,
        group_name="QQ 群聊" if is_group else None,
        # Generic group events must never gain send permission merely because
        # they contain a mention of somebody else.
        mentioned_user=event_type == "GROUP_AT_MESSAGE_CREATE",
        timestamp=_timestamp(data.get("timestamp")),
        raw={"event_type": event_type},
    )
