from __future__ import annotations

import asyncio
import ipaddress
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Any
from urllib.parse import urlparse

import httpx

from app.channels.base import ChannelConnector, InboundAttachment, InboundEvent, OutboundMessage, SendPermit, SendResult


class ExperimentalConnectorError(ValueError):
    """An experimental connector event cannot be normalized safely."""


def normalize_loopback_base_url(value: str) -> str:
    """Accept only a plain HTTP(S) origin bound to this computer."""
    candidate = value.strip().rstrip("/")
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ExperimentalConnectorError("实验连接器地址必须是本机 HTTP(S) 地址")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ExperimentalConnectorError("实验连接器地址只能填写协议、主机和端口")
    host = parsed.hostname.lower()
    if host != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ExperimentalConnectorError("实验连接器只允许连接 127.0.0.1、::1 或 localhost")
        except ValueError as exc:
            raise ExperimentalConnectorError("实验连接器只允许连接 127.0.0.1、::1 或 localhost") from exc
    return candidate


class NapCatConnector(ChannelConnector):
    """Minimal OneBot 11 HTTP sender constrained to a loopback NapCat API."""

    platform = "QQ_NAPCAT"
    real_channel = True

    def __init__(self, *, api_base: str | None, access_token: str | None, enabled: bool, observed_status: str) -> None:
        self.api_base = api_base
        self.access_token = access_token
        self.enabled = enabled
        self.observed_status = observed_status

    @property
    def configured(self) -> bool:
        if not self.api_base or not self.access_token:
            return False
        try:
            normalize_loopback_base_url(self.api_base)
        except ExperimentalConnectorError:
            return False
        return True

    async def send(self, message: OutboundMessage, permit: SendPermit) -> SendResult:
        permit.assert_valid_for(message)
        if not self.enabled:
            return SendResult(False, error_code="NAPCAT_DISABLED", error_detail="NapCat 实验连接器已关闭")
        if not self.configured:
            return SendResult(False, error_code="NAPCAT_NOT_CONFIGURED", error_detail="NapCat 本机地址或 Token 未配置")
        if not message.target_id.isdecimal():
            return SendResult(False, error_code="NAPCAT_INVALID_TARGET", error_detail="NapCat 目标 ID 必须是纯数字")
        if len(message.attachments) > 4:
            return SendResult(False, error_code="NAPCAT_TOO_MANY_ATTACHMENTS", error_detail="单次最多发送 4 个附件")
        endpoint = "send_group_msg" if message.is_group else "send_private_msg"
        target_key = "group_id" if message.is_group else "user_id"
        media_segments: list[dict[str, Any]] = []
        file_attachments = []
        for attachment in message.attachments:
            path = Path(attachment.file_path)
            if not path.is_absolute() or not path.is_file():
                return SendResult(False, error_code="NAPCAT_ATTACHMENT_MISSING", error_detail="待发送附件不存在")
            kind = attachment.kind.upper()
            if kind == "FILE":
                file_attachments.append(attachment)
                continue
            segment_type = {"IMAGE": "image", "AUDIO": "record", "VIDEO": "video"}.get(kind)
            if segment_type is None:
                return SendResult(False, error_code="NAPCAT_ATTACHMENT_UNSUPPORTED", error_detail="附件类型不受支持")
            media_segments.append({"type": segment_type, "data": {"file": str(path)}})
        try:
            # This connector is deliberately loopback-only. Ignore proxy
            # environment variables inherited from scheduled tasks so local
            # OneBot calls cannot be diverted to a system or development proxy.
            async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                api_base = normalize_loopback_base_url(self.api_base or "")
                headers = {"Authorization": f"Bearer {self.access_token}"}
                responses: list[tuple[Any, dict[str, Any] | None]] = []

                async def post_action(endpoint_name: str, payload: dict[str, Any]) -> tuple[Any, dict[str, Any] | None]:
                    """Retry once only after an explicit transient HTTP rejection.

                    Timeouts and connection resets are deliberately not retried because
                    NapCat may already have accepted the action.  A local 502/503/504,
                    however, is an explicit unsuccessful response and is safe to retry
                    once without weakening the duplicate-send boundary.
                    """

                    response = await client.post(f"{api_base}/{endpoint_name}", headers=headers, json=payload)
                    if int(getattr(response, "status_code", 200)) in {502, 503, 504}:
                        await asyncio.sleep(0.4)
                        response = await client.post(f"{api_base}/{endpoint_name}", headers=headers, json=payload)
                    try:
                        data = response.json()
                    except ValueError:
                        data = None
                    return response, data if isinstance(data, dict) else None

                if message.content or media_segments:
                    payload_message: str | list[dict[str, Any]]
                    reply_segments = (
                        [{"type": "reply", "data": {"id": str(message.reply_to_message_id)}}]
                        if str(message.reply_to_message_id or "").strip()
                        else []
                    )
                    content_segments = ([{"type": "text", "data": {"text": message.content}}] if message.content else []) + media_segments
                    if media_segments or reply_segments:
                        payload_message = reply_segments + content_segments
                    else:
                        payload_message = message.content
                    response, data = await post_action(
                        endpoint,
                        {target_key: int(message.target_id), "message": payload_message},
                    )
                    quote_rejected = (
                        reply_segments
                        and response.is_success
                        and isinstance(data, dict)
                        and (data.get("status") != "ok" or str(data.get("retcode")) != "0")
                    )
                    if quote_rejected:
                        # A stale/deleted quote target must not make the whole
                        # reply disappear. Retry once without the quote. HTTP
                        # and malformed-response failures are not treated as a
                        # quote error because their delivery state is unclear.
                        retry_message: str | list[dict[str, Any]] = content_segments if media_segments else message.content
                        response, data = await post_action(
                            endpoint,
                            {target_key: int(message.target_id), "message": retry_message},
                        )
                    responses.append((response, data))
                upload_action = "upload_group_file" if message.is_group else "upload_private_file"
                for attachment in file_attachments:
                    response, data = await post_action(
                        upload_action,
                        {target_key: int(message.target_id), "file": attachment.file_path, "name": attachment.file_name},
                    )
                    responses.append((response, data))
        except (httpx.HTTPError, ValueError, ExperimentalConnectorError) as exc:
            return SendResult(False, error_code="NAPCAT_NETWORK_ERROR", error_detail=type(exc).__name__)
        if not responses:
            return SendResult(False, error_code="NAPCAT_EMPTY_MESSAGE", error_detail="待发送内容为空")
        failed = next(
            (
                (response, data)
                for response, data in responses
                if not response.is_success
                or not isinstance(data, dict)
                or data.get("status") != "ok"
                or str(data.get("retcode")) != "0"
            ),
            None,
        )
        if failed is not None:
            response, data = failed
            status_code = int(getattr(response, "status_code", 0) or 0)
            if not response.is_success:
                return SendResult(
                    False,
                    error_code=f"NAPCAT_HTTP_{status_code}" if status_code else "NAPCAT_HTTP_ERROR",
                    error_detail=f"NapCat OneBot API 返回 HTTP {status_code or '错误'}",
                )
            if not isinstance(data, dict):
                return SendResult(False, error_code="NAPCAT_INVALID_RESPONSE", error_detail="NapCat 返回了无法解析的响应")
            retcode = str(data.get("retcode") or "UNKNOWN")
            detail = str(data.get("wording") or data.get("message") or "NapCat OneBot API 拒绝发送")[:200]
            return SendResult(False, error_code="NAPCAT_API_REJECTED", error_detail=f"retcode={retcode}: {detail}")
        response_data = responses[-1][1].get("data") if isinstance(responses[-1][1].get("data"), dict) else {}
        external_id = str(response_data.get("message_id") or response_data.get("file_id") or "napcat-accepted")
        return SendResult(True, external_message_id=external_id)

    async def status(self) -> dict[str, Any]:
        if not self.enabled:
            value = "DISABLED"
        elif not self.configured:
            value = "NOT_CONFIGURED"
        else:
            value = self.observed_status
        return {
            "platform": self.platform,
            "status": value,
            "real_channel": True,
            "experimental": True,
            "strategy": "NAPCAT_ONEBOT11",
            "enabled": self.enabled,
            "credential_present": bool(self.access_token),
        }


class AutoWxDraftConnector(ChannelConnector):
    """Receive-only personal WeChat strategy; it never owns a send primitive."""

    platform = "WECHAT_AUTOWX"
    real_channel = True

    def __init__(self, *, enabled: bool, credential_present: bool, observed_status: str) -> None:
        self.enabled = enabled
        self.credential_present = credential_present
        self.observed_status = observed_status

    async def send(self, message: OutboundMessage, permit: SendPermit) -> SendResult:
        permit.assert_valid_for(message)
        return SendResult(
            False,
            error_code="AUTOWX_DRAFT_ONLY",
            error_detail="AutoWx 只允许接收入站并生成影子草稿，禁止自动点击发送",
        )

    async def status(self) -> dict[str, Any]:
        if not self.enabled:
            value = "DISABLED"
        elif not self.credential_present:
            value = "NOT_CONFIGURED"
        else:
            value = self.observed_status
        return {
            "platform": self.platform,
            "status": value,
            "real_channel": True,
            "experimental": True,
            "strategy": "AUTOWX_DRAFT_ONLY",
            "send_capability": "BLOCKED",
            "enabled": self.enabled,
            "credential_present": self.credential_present,
        }


def _event_time(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
        except ValueError:
            pass
    return datetime.now(timezone.utc)


_MEDIA_KINDS = {"image": "IMAGE", "record": "AUDIO", "file": "FILE", "video": "VIDEO"}


def _safe_size(value: Any) -> int | None:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return None
    return size if size >= 0 else None


def _attachment_name(kind: str, data: dict[str, Any], index: int) -> str:
    candidate = str(data.get("name") or data.get("file_name") or data.get("file") or "").strip()
    if candidate and not candidate.startswith(("base64://", "data:")):
        parsed = urlparse(candidate)
        leaf = PurePath(parsed.path.replace("\\", "/")).name
        if leaf:
            return leaf[:255]
    defaults = {"IMAGE": "图片", "AUDIO": "语音", "FILE": "文件", "VIDEO": "视频"}
    extensions = {"IMAGE": ".jpg", "AUDIO": ".mp3", "FILE": "", "VIDEO": ".mp4"}
    return f"{defaults[kind]}-{index + 1}{extensions[kind]}"


def _onebot_content(message: Any, self_id: str) -> tuple[str, bool, tuple[InboundAttachment, ...], str, str | None]:
    if isinstance(message, list):
        text_parts: list[str] = []
        attachments: list[InboundAttachment] = []
        mentioned = False
        reply_to_message_id: str | None = None
        for index, segment in enumerate(message):
            if not isinstance(segment, dict):
                continue
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            segment_type = str(segment.get("type") or "")
            if segment_type == "text":
                text_parts.append(str(data.get("text") or ""))
            elif segment_type == "reply":
                candidate = str(data.get("id") or "").strip()
                if candidate and reply_to_message_id is None:
                    reply_to_message_id = candidate
            elif segment_type == "at" and str(data.get("qq") or "") == self_id:
                mentioned = True
            elif segment_type in _MEDIA_KINDS:
                kind = _MEDIA_KINDS[segment_type]
                file_name = _attachment_name(kind, data, index)
                mime_type = mimetypes.guess_type(file_name)[0]
                source_ref = str(data.get("url") or data.get("path") or data.get("file") or "").strip() or None
                file_id = str(data.get("file_id") or "").strip() or None
                napcat_sources = {
                    key: str(data.get(key) or "").strip()
                    for key in ("file", "path", "url")
                    if str(data.get(key) or "").strip()
                }
                attachment_metadata = {
                    key: value for key, value in data.items() if key not in {"url", "path", "file", "file_id"}
                }
                if napcat_sources:
                    # NapCat's media conversion actions expect the opaque
                    # `file` token, while the URL/path remains useful as a
                    # best-effort fallback when conversion is unavailable.
                    attachment_metadata["napcat_sources"] = napcat_sources
                attachments.append(
                    InboundAttachment(
                        kind=kind,
                        segment_type=segment_type,
                        segment_index=index,
                        file_name=file_name,
                        source_ref=source_ref,
                        file_id=file_id,
                        size_bytes=_safe_size(data.get("file_size") or data.get("size")),
                        mime_type=mime_type,
                        metadata=attachment_metadata,
                    )
                )
        text_content = "".join(text_parts).strip()
        if text_content:
            content = text_content
        else:
            labels = []
            for attachment in attachments:
                if attachment.kind == "IMAGE":
                    labels.append("[图片]")
                elif attachment.kind == "AUDIO":
                    labels.append("[语音]")
                elif attachment.kind == "VIDEO":
                    labels.append(f"[视频：{attachment.file_name}]")
                else:
                    labels.append(f"[文件：{attachment.file_name}]")
            content = " ".join(labels)
        return content, mentioned, tuple(attachments), text_content, reply_to_message_id
    raw = str(message or "")
    mentioned = bool(self_id and re.search(rf"\[CQ:at,qq={re.escape(self_id)}(?:,|\])", raw))
    text_content = re.sub(r"\[CQ:[^\]]+\]", "", raw).strip()
    reply_match = re.search(r"\[CQ:reply,id=([^,\]]+)", raw)
    return text_content, mentioned, (), text_content, reply_match.group(1) if reply_match else None


def normalize_napcat_event(payload: dict[str, Any]) -> InboundEvent | None:
    post_type = str(payload.get("post_type") or "")
    if post_type not in {"message", "message_sent"} or payload.get("message_type") not in {"private", "group"}:
        return None
    self_id = str(payload.get("self_id") or "")
    sender_id = str(payload.get("user_id") or "")
    is_self_message = post_type == "message_sent"
    if not sender_id:
        return None
    if is_self_message:
        if not self_id or sender_id != self_id:
            raise ExperimentalConnectorError("NapCat 本人消息的发送者与 self_id 不一致")
        # NapCat adds target_id when reportSelfMessage is enabled. For private
        # chats it is the remote QQ number, which is also the local contact key.
        # Group self-messages are intentionally ignored here: this connector's
        # collection boundary is an explicitly whitelisted private contact.
        if payload.get("message_type") != "private":
            return None
        peer_id = str(payload.get("target_id") or "")
        if not peer_id or peer_id == self_id:
            raise ExperimentalConnectorError("NapCat 本人私聊消息缺少有效 target_id")
        sender_id = peer_id
    elif sender_id == self_id:
        # A normal inbound envelope claiming to be sent by this account is not
        # accepted as a human reply. NapCat reports those as message_sent.
        return None
    message_id = str(payload.get("message_id") or "")
    content, mentioned, attachments, text_content, reply_to_message_id = _onebot_content(
        payload.get("message", payload.get("raw_message")), self_id
    )
    if not message_id or not content:
        raise ExperimentalConnectorError("NapCat 消息缺少 message_id 或可保存的正文/媒体")
    is_group = payload.get("message_type") == "group"
    group_id = str(payload.get("group_id") or "") if is_group else None
    if is_group and not group_id:
        raise ExperimentalConnectorError("NapCat 群消息缺少 group_id")
    sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
    sender_name = str(sender.get("card") or sender.get("nickname") or f"QQ {sender_id}")
    media_types = {attachment.kind for attachment in attachments}
    if text_content and attachments:
        message_type = "MIXED"
    elif len(media_types) == 1:
        message_type = next(iter(media_types))
    elif media_types:
        message_type = "MIXED"
    else:
        message_type = "TEXT"
    event_type = "ONEBOT11_MESSAGE_SENT" if is_self_message else "ONEBOT11_MESSAGE"
    return InboundEvent(
        platform="QQ_NAPCAT",
        message_id=message_id,
        conversation_id=f"napcat:group:{group_id}" if is_group else f"napcat:private:{sender_id}",
        sender_id=sender_id,
        sender_name=sender_name,
        content=content,
        is_group=is_group,
        group_id=group_id,
        group_name=str(payload.get("group_name") or "QQ 群聊") if is_group else None,
        mentioned_user=mentioned,
        author="HUMAN" if is_self_message else "CONTACT",
        message_type=message_type,
        reply_to_message_id=reply_to_message_id,
        attachments=attachments,
        timestamp=_event_time(payload.get("time")),
        raw={"event_type": event_type, "text_content": text_content, "onebot": payload},
    )


def normalize_autowx_event(payload: dict[str, Any]) -> InboundEvent:
    message_id = str(payload.get("message_id") or payload.get("id") or "")
    sender_id = str(payload.get("sender_id") or payload.get("wxid") or payload.get("from_user") or "")
    content = str(payload.get("content") or payload.get("text") or "").strip()
    if not message_id or not sender_id or not content:
        raise ExperimentalConnectorError("AutoWx 事件缺少 message_id、sender_id 或文本正文")
    chat_id = str(payload.get("chat_id") or payload.get("conversation_id") or "")
    group_id = str(payload.get("group_id") or payload.get("chatroom_id") or "") or None
    is_group = bool(payload.get("is_group") or group_id or chat_id.endswith("@chatroom"))
    if is_group and not group_id:
        group_id = chat_id
    return InboundEvent(
        platform="WECHAT_AUTOWX",
        message_id=message_id,
        conversation_id=chat_id or (f"autowx:group:{group_id}" if is_group else f"autowx:private:{sender_id}"),
        sender_id=sender_id,
        sender_name=str(payload.get("sender_name") or payload.get("nickname") or "微信联系人"),
        content=content,
        is_group=is_group,
        group_id=group_id,
        group_name=str(payload.get("group_name") or "微信群聊") if is_group else None,
        mentioned_user=bool(payload.get("mentioned_user") or payload.get("at_me")),
        author="HUMAN" if payload.get("is_self") is True else "CONTACT",
        timestamp=_event_time(payload.get("timestamp") or payload.get("time")),
        raw={"event_type": "AUTOWX_LOCAL_EVENT"},
    )
