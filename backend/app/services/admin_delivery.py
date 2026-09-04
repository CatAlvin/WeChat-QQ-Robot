from __future__ import annotations

import hashlib
import re
import secrets
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.channels.base import OutboundAttachment
from app.core.config import Settings
from app.core.enums import MessageAuthor, MessageDirection, MessageStatus
from app.models.entities import Contact, Conversation, Message, MessageAttachment
from app.services.admin_syntax import split_command_payload
from app.services.media import MediaStoreError, normalize_media_mime_type, resolve_saved_media_path


DELIVERY_PROVIDER = "LOCAL_ADMIN_DELIVERY"
_CONFIRM_TTL = timedelta(minutes=10)


class AdminDeliveryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class DeliveryDraft:
    message: Message
    code: str
    target: Contact
    attachment_count: int


def is_delivery_command(name: str) -> bool:
    return name.casefold() in {
        "发送", "send", "发送文件", "sendfile", "确认发送", "confirm-send", "取消发送", "cancel-send"
    }


def resolve_target(db: Session, value: str, *, account_id: str | None) -> Contact:
    target_text = value.strip()
    if not target_text:
        raise AdminDeliveryError("TARGET_REQUIRED", "请填写目标联系人名称或 QQ 号。")
    rows = list(
        db.scalars(
            select(Contact).where(
                Contact.platform == "QQ_NAPCAT",
                Contact.account_id == account_id,
                or_(
                    Contact.platform_user_id == target_text,
                    Contact.display_name == target_text,
                ),
            )
        )
    )
    if not rows:
        raise AdminDeliveryError("TARGET_NOT_FOUND", "没有找到该 NapCat 联系人；请填写后台显示的完整名称或 QQ 号。")
    if len(rows) > 1:
        raise AdminDeliveryError("TARGET_AMBIGUOUS", "联系人名称不唯一，请改用 QQ 号。")
    target = rows[0]
    if not target.whitelisted:
        raise AdminDeliveryError("TARGET_NOT_WHITELISTED", "目标联系人不在白名单，禁止管理员指令主动发送。")
    return target


def _conversation(db: Session, target: Contact) -> Conversation:
    external_id = f"napcat:private:{target.platform_user_id}"
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.platform == "QQ_NAPCAT",
            Conversation.account_id == target.account_id,
            Conversation.external_id == external_id,
        )
    )
    if conversation is None:
        conversation = Conversation(
            platform="QQ_NAPCAT", account_id=target.account_id, external_id=external_id, contact_id=target.id
        )
        db.add(conversation)
        db.flush()
    return conversation


def _split_target_and_payload(arguments: tuple[str, ...]) -> tuple[str, str]:
    try:
        return split_command_payload(
            arguments,
            required=False,
            usage="/neko 发送 联系人-要发送的内容",
        )
    except ValueError as exc:
        raise AdminDeliveryError("DELIMITER_INVALID", str(exc)) from exc


def _clone_attachment(db: Session, draft: Message, source: MessageAttachment, index: int) -> MessageAttachment:
    if source.status != "SAVED" or not source.local_path:
        raise AdminDeliveryError("FILE_NOT_SAVED", f"附件《{source.file_name}》没有可发送的本机文件。")
    row = MessageAttachment(
        message_id=draft.id,
        kind=source.kind,
        segment_type=source.segment_type,
        segment_index=index,
        file_name=source.file_name,
        mime_type=source.mime_type,
        size_bytes=source.size_bytes,
        source_ref=None,
        file_id=None,
        local_path=source.local_path,
        sha256=source.sha256,
        status="SAVED",
        attachment_metadata={"admin_delivery_source_attachment_id": source.id},
        analysis_status=source.analysis_status,
        analysis_provider=source.analysis_provider,
        analysis_model=source.analysis_model,
        analysis_text=source.analysis_text,
        analysis_error_code=source.analysis_error_code,
        analysis_metadata=source.analysis_metadata or {},
        analyzed_at=source.analyzed_at,
    )
    db.add(row)
    return row


def _stage_sendbox_file(db: Session, settings: Settings, draft: Message, relative_name: str, max_bytes: int) -> MessageAttachment:
    sendbox = (settings.data_dir.resolve() / "sendbox").resolve()
    sendbox.mkdir(parents=True, exist_ok=True)
    source = (sendbox / relative_name).resolve()
    try:
        source.relative_to(sendbox)
    except ValueError as exc:
        raise AdminDeliveryError("FILE_PATH_INVALID", "只能发送 Neko 数据目录 sendbox 内的文件。") from exc
    if not source.is_file():
        raise AdminDeliveryError("FILE_NOT_FOUND", f"sendbox 中找不到《{relative_name}》。")
    size = source.stat().st_size
    if size > max_bytes:
        raise AdminDeliveryError("FILE_TOO_LARGE", "文件超过后台设置的媒体大小上限。")
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", source.name)[:180] or "附件"
    destination_dir = settings.data_dir.resolve() / "media" / "outbound" / draft.id
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / safe_name
    shutil.copy2(source, destination)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    row = MessageAttachment(
        message_id=draft.id,
        kind="FILE",
        segment_type="file",
        segment_index=0,
        file_name=safe_name,
        mime_type=normalize_media_mime_type(None, file_name=safe_name),
        size_bytes=size,
        local_path=destination.relative_to(settings.data_dir.resolve()).as_posix(),
        sha256=digest,
        status="SAVED",
        attachment_metadata={"admin_delivery_source": "SENDBOX"},
    )
    db.add(row)
    return row


def create_delivery_draft(
    db: Session,
    *,
    settings: Settings,
    admin: Contact,
    source_message: Message,
    command_name: str,
    arguments: tuple[str, ...],
    source_attachments: list[MessageAttachment],
    max_file_mb: int,
) -> DeliveryDraft:
    target_text, payload = _split_target_and_payload(arguments)
    target = resolve_target(db, target_text, account_id=admin.account_id)
    is_file = command_name.casefold() in {"发送文件", "sendfile"}
    if not is_file and not payload:
        raise AdminDeliveryError("CONTENT_REQUIRED", "文本发送格式：/neko 发送 联系人-要发送的内容")
    conversation = _conversation(db, target)
    code = secrets.token_hex(3).upper()
    expires_at = datetime.now(timezone.utc) + _CONFIRM_TTL
    draft = Message(
        platform="QQ_NAPCAT",
        account_id=target.account_id,
        external_message_id=f"admin-draft:{source_message.id}:{code}",
        sender_id="QQ_NAPCAT:HUMAN_ADMIN",
        receiver_id=target.platform_user_id,
        conversation_id=conversation.id,
        contact_id=target.id,
        direction=MessageDirection.OUTBOUND,
        author=MessageAuthor.SYSTEM,
        content=payload if not is_file else "",
        message_type="FILE" if is_file else "TEXT",
        status=MessageStatus.GENERATED,
        provider=DELIVERY_PROVIDER,
        model="PENDING_CONFIRMATION",
        raw_envelope={
            "admin_delivery": {
                "code": code,
                "expires_at": expires_at.isoformat(),
                "admin_contact_id": admin.id,
                "source_message_id": source_message.id,
                "kind": "FILE" if is_file else "TEXT",
            }
        },
    )
    db.add(draft)
    db.flush()
    attachment_count = 0
    if is_file:
        selected = source_attachments
        if selected:
            for index, item in enumerate(selected[:4]):
                _clone_attachment(db, draft, item, index)
                attachment_count += 1
        elif payload:
            _stage_sendbox_file(db, settings, draft, payload, max_file_mb * 1024 * 1024)
            attachment_count = 1
        else:
            raise AdminDeliveryError(
                "FILE_REQUIRED",
                "请引用一条已保存的附件消息，或使用：/neko 发送文件 联系人-sendbox内文件名",
            )
    db.flush()
    return DeliveryDraft(draft, code, target, attachment_count)


def find_delivery_draft(db: Session, *, admin: Contact, code: str) -> Message:
    normalized = code.strip().upper()
    cutoff = datetime.now(timezone.utc) - _CONFIRM_TTL
    candidates = list(
        db.scalars(
            select(Message)
            .where(
                Message.provider == DELIVERY_PROVIDER,
                Message.status == MessageStatus.GENERATED,
                Message.created_at >= cutoff,
            )
            .order_by(Message.created_at.desc())
            .limit(30)
        )
    )
    for item in candidates:
        detail = (item.raw_envelope or {}).get("admin_delivery")
        if isinstance(detail, dict) and str(detail.get("admin_contact_id")) == admin.id and str(detail.get("code", "")).upper() == normalized:
            return item
    raise AdminDeliveryError("DRAFT_NOT_FOUND", "未找到待确认发送，可能代码错误、已处理或已超过 10 分钟。")


def cancel_delivery_draft(db: Session, *, admin: Contact, code: str) -> Message:
    draft = find_delivery_draft(db, admin=admin, code=code)
    draft.status = MessageStatus.CANCELLED
    draft.policy_reason = "ADMIN_CANCELLED"
    return draft


def outbound_attachments(db: Session, draft: Message, settings: Settings) -> tuple[OutboundAttachment, ...]:
    rows = list(
        db.scalars(
            select(MessageAttachment)
            .where(MessageAttachment.message_id == draft.id)
            .order_by(MessageAttachment.segment_index.asc())
        )
    )
    output: list[OutboundAttachment] = []
    for row in rows:
        try:
            path = resolve_saved_media_path(row, settings)
        except MediaStoreError as exc:
            raise AdminDeliveryError(exc.code, f"待发送附件《{row.file_name}》不可用。") from exc
        output.append(
            OutboundAttachment(
                row.kind,
                str(path),
                row.file_name,
                normalize_media_mime_type(row.mime_type, file_name=row.file_name),
            )
        )
    return tuple(output)
