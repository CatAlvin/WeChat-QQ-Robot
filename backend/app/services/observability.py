from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.clock import as_beijing
from app.core.enums import MessageAuthor, MessageStatus
from app.models.entities import (
    Account,
    AuditLog,
    Contact,
    Conversation,
    Incident,
    Message,
    MessageAttachment,
    RuntimeState,
)
from app.schemas import ContactOut


def _clock(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(int(hour), int(minute))


def _inside_window(value: time, start: str, end: str) -> bool:
    lower, upper = _clock(start), _clock(end)
    return lower <= value < upper if lower < upper else value >= lower or value < upper


def _step(key: str, label: str, status: str, detail: str) -> dict[str, str]:
    return {"key": key, "label": label, "status": status, "detail": detail}


_REASONS = {
    "NOT_WHITELISTED": "联系人不在白名单",
    "AI_DISABLED": "该联系人的 AI 回复已关闭",
    "OUTSIDE_TIME_WINDOW": "消息到达时间不在允许回复时段",
    "GLOBAL_SILENT": "全局处于静默模式",
    "GLOBAL_READ_ONLY": "全局处于只读模式",
    "GLOBAL_STOPPED": "托管已停止",
    "KILL_SWITCH": "急停已开启",
    "CHANNEL_DISABLED": "发送通道未启用或配置不完整",
    "HUMAN_TAKEOVER": "本人刚刚回复过，AI 正在人工接管冷却期",
    "CONTACT_COOLDOWN": "联系人会话处于冷却期",
    "GROUP_NOT_ALLOWED": "群聊未获得回复许可或消息未 @ 机器人",
    "ALL_PROVIDERS_FAILED": "所有已启用模型均调用失败",
    "MEDIA_AI_REPLY_DISABLED": "媒体 AI 回复已关闭",
    "MEDIA_STORED_NO_AI": "媒体已保存，但媒体 AI 回复已关闭",
    "MEDIA_UNDERSTANDING_UNAVAILABLE": "媒体识别没有产出可靠内容",
}


def message_diagnostics(db: Session, message_id: str, settings: Settings) -> dict[str, Any]:
    inbound = db.get(Message, message_id)
    if inbound is None:
        raise LookupError("消息不存在")
    conversation = db.get(Conversation, inbound.conversation_id)
    contact = db.get(Contact, inbound.contact_id) if inbound.contact_id else None
    account = db.get(Account, inbound.account_id) if inbound.account_id else None
    state = db.get(RuntimeState, 1)
    outbounds = list(
        db.scalars(
            select(Message)
            .where(
                Message.conversation_id == inbound.conversation_id,
                Message.reply_to_external_id == inbound.external_message_id,
                Message.direction == "OUTBOUND",
            )
            .order_by(Message.created_at.asc())
        )
    )
    message_ids = [inbound.id, *[item.id for item in outbounds]]
    audits = list(
        db.scalars(
            select(AuditLog)
            .where(
                AuditLog.conversation_id == inbound.conversation_id,
                AuditLog.created_at >= inbound.created_at - timedelta(seconds=5),
                or_(AuditLog.message_id.in_(message_ids), AuditLog.message_id.is_(None)),
            )
            .order_by(AuditLog.created_at.asc())
            .limit(100)
        )
    )
    events = {str(item.event) for item in audits}
    policy_denies = [
        item
        for item in audits
        if str(item.event)
        in {
            "POLICY_DENY",
            "ERROR",
            "SAFETY_DENY",
            "MEDIA_STORED_NO_AI",
            "MEDIA_UNDERSTANDING_UNAVAILABLE",
        }
    ]
    reason_code = ""
    reason = ""
    if outbounds:
        final = outbounds[-1]
        reason_code = str(final.policy_reason or final.status)
        if final.status == MessageStatus.SENT:
            reason = "消息已经通过全部检查并发送"
        elif final.status == MessageStatus.SHADOWED:
            reason = "SHADOW 模式只生成草稿，不会真实发送"
        elif final.status == MessageStatus.QUEUED:
            reason = "回复仍在待发送队列中"
        else:
            reason = _REASONS.get(reason_code, f"回复停在 {final.status} 状态")
    elif policy_denies:
        detail = policy_denies[-1].detail or {}
        reason_code = str(detail.get("code") or policy_denies[-1].event)
        reason = str(detail.get("reason") or _REASONS.get(reason_code) or reason_code)
    elif inbound.author == MessageAuthor.HUMAN:
        reason_code, reason = "HUMAN_MESSAGE", "这是账号本人发送的消息，只入库并触发人工接管"
    else:
        reason_code, reason = "NO_REPLY_CREATED", "尚未找到对应回复；请查看下方链路和审计时间线"

    effective_window = bool(state and state.live_time_window_enabled)
    start = state.live_auto_start if state else settings.auto_start
    end = state.live_auto_end if state else settings.auto_end
    if contact:
        if contact.reply_time_window_enabled is not None:
            effective_window = contact.reply_time_window_enabled
        start = contact.reply_auto_start or start
        end = contact.reply_auto_end or end
    event_local = as_beijing(inbound.event_at)
    in_window = not effective_window or _inside_window(event_local.time().replace(tzinfo=None), start, end)
    model_requested = "LLM_REQUEST" in events
    model_responded = "LLM_RESPONSE" in events or any(item.model for item in outbounds)
    safety_passed = "SAFETY_PASS" in events
    steps = [
        _step("stored", "消息入库", "PASS", f"已保存，消息 ID {inbound.id}"),
        _step(
            "account",
            "账号归属",
            "PASS" if inbound.account_id or inbound.platform == "SIMULATOR" else "WARN",
            account.display_name if account else ("模拟器" if inbound.platform == "SIMULATOR" else "历史消息未绑定账号"),
        ),
        _step("whitelist", "白名单", "PASS" if contact and contact.whitelisted else "BLOCK", "已允许" if contact and contact.whitelisted else "未允许"),
        _step("ai", "联系人 AI", "PASS" if contact and contact.ai_enabled else "BLOCK", "已开启" if contact and contact.ai_enabled else "已关闭"),
        _step("window", "回复时段", "PASS" if in_window else "BLOCK", f"消息到达 {event_local:%H:%M}；有效时段 {start}–{end}{'（门禁关闭）' if not effective_window else ''}"),
        _step("runtime", "运行模式", "PASS" if state and state.global_mode == "AUTO" and not state.kill_switch else "BLOCK", f"{state.global_mode if state else 'UNKNOWN'} · {state.release_gate if state else 'UNKNOWN'} · 急停{'开' if state and state.kill_switch else '关'}"),
        _step("model", "模型调用", "PASS" if model_responded else ("WARN" if model_requested else "SKIP"), "已获得模型响应" if model_responded else ("已发起但未成功" if model_requested else "未进入模型阶段")),
        _step("safety", "内容审核", "PASS" if safety_passed else ("BLOCK" if "SAFETY_DENY" in events else "SKIP"), "审核通过" if safety_passed else ("审核拦截" if "SAFETY_DENY" in events else "未进入审核阶段")),
        _step("send", "发送结果", "PASS" if outbounds and outbounds[-1].status == MessageStatus.SENT else "BLOCK", f"{outbounds[-1].status} · {outbounds[-1].policy_reason or '无拦截码'}" if outbounds else "未创建待发回复"),
    ]
    return {
        "message": {"id": inbound.id, "content": inbound.content, "author": inbound.author, "status": inbound.status, "event_at": inbound.event_at},
        "contact": {"id": contact.id, "display_name": contact.display_name} if contact else None,
        "account": {"id": account.id, "display_name": account.display_name} if account else None,
        "outcome": {"code": reason_code, "reason": reason, "replied": any(item.status == MessageStatus.SENT for item in outbounds)},
        "steps": steps,
        "outbounds": [
            {"id": item.id, "status": item.status, "provider": item.provider, "model": item.model, "policy_reason": item.policy_reason, "content": item.content}
            for item in outbounds
        ],
        "timeline": [
            {"event": str(item.event), "level": item.level, "detail": item.detail or {}, "created_at": item.created_at}
            for item in audits
        ],
    }


def _attachment(item: MessageAttachment) -> dict[str, Any]:
    return {
        "id": item.id,
        "kind": item.kind,
        "file_name": item.file_name,
        "mime_type": item.mime_type,
        "size_bytes": item.size_bytes,
        "status": item.status,
        "error_code": item.error_code,
        "storage_source": (item.attachment_metadata or {}).get("neko_storage_source"),
        "storage_attempts": (item.attachment_metadata or {}).get("neko_storage_attempts", []),
        "sha256": item.sha256,
        "local_path": item.local_path,
        "analysis_status": item.analysis_status,
        "analysis_provider": item.analysis_provider,
        "analysis_model": item.analysis_model,
        "analysis_text": item.analysis_text,
        "analysis_error_code": item.analysis_error_code,
        "download_url": f"/media/{item.id}" if item.status == "SAVED" else None,
    }


def reference_preview(db: Session, message_id: str) -> dict[str, Any]:
    message = db.get(Message, message_id)
    if message is None:
        raise LookupError("消息不存在")
    audit = db.scalar(
        select(AuditLog)
        .where(AuditLog.message_id == message.id, AuditLog.event == "REFERENCE_CONTEXT_RESOLVED")
        .order_by(desc(AuditLog.created_at))
        .limit(1)
    )
    detail = audit.detail if audit else {}
    referenced = db.get(Message, detail.get("referenced_message_id")) if detail.get("referenced_message_id") else None
    if referenced is None and message.reply_to_external_id:
        referenced = db.scalar(
            select(Message).where(
                Message.account_id == message.account_id,
                Message.conversation_id == message.conversation_id,
                Message.external_message_id == message.reply_to_external_id,
            )
        )
    attachments = []
    if referenced:
        attachments = [
            _attachment(item)
            for item in db.scalars(
                select(MessageAttachment)
                .where(MessageAttachment.message_id == referenced.id)
                .order_by(MessageAttachment.segment_index.asc())
            )
        ]
    return {
        "has_reference": referenced is not None,
        "source": detail.get("source") or ("LOCAL_MESSAGE" if referenced else "NONE"),
        "requested_external_id": message.reply_to_external_id,
        "message": {
            "id": referenced.id,
            "external_message_id": referenced.external_message_id,
            "author": referenced.author,
            "content": referenced.content,
            "message_type": referenced.message_type,
            "created_at": referenced.created_at,
        } if referenced else None,
        "attachments": attachments,
    }


def contact_detail(db: Session, contact_id: str, settings: Settings) -> dict[str, Any]:
    contact = db.get(Contact, contact_id)
    if contact is None:
        raise LookupError("联系人不存在")
    state = db.get(RuntimeState, 1)
    account = db.get(Account, contact.account_id) if contact.account_id else None
    conversations = list(db.scalars(select(Conversation.id).where(Conversation.contact_id == contact.id)))
    message_count = db.scalar(select(func.count(Message.id)).where(Message.contact_id == contact.id)) or 0
    media_count = db.scalar(
        select(func.count(MessageAttachment.id))
        .join(Message, Message.id == MessageAttachment.message_id)
        .where(Message.contact_id == contact.id)
    ) or 0
    recent = list(
        db.scalars(
            select(Message).where(Message.contact_id == contact.id).order_by(desc(Message.created_at)).limit(12)
        )
    )
    anomalies = list(
        db.scalars(
            select(AuditLog)
            .where(
                AuditLog.conversation_id.in_(conversations) if conversations else AuditLog.conversation_id == "__none__",
                or_(AuditLog.level.in_(["WARNING", "ERROR"]), AuditLog.event.in_(["POLICY_DENY", "SAFETY_DENY"])),
            )
            .order_by(desc(AuditLog.created_at))
            .limit(10)
        )
    )
    global_start = state.live_auto_start if state else settings.auto_start
    global_end = state.live_auto_end if state else settings.auto_end
    global_window = bool(state and state.live_time_window_enabled)
    return {
        "contact": ContactOut.model_validate(contact).model_dump(mode="json"),
        "account": {"id": account.id, "display_name": account.display_name, "enabled": account.enabled} if account else None,
        "effective_policy": {
            "reply_time_window_enabled": global_window if contact.reply_time_window_enabled is None else contact.reply_time_window_enabled,
            "reply_auto_start": contact.reply_auto_start or global_start,
            "reply_auto_end": contact.reply_auto_end or global_end,
            "media_storage_enabled": bool(state and state.media_storage_enabled) if contact.media_storage_enabled is None else contact.media_storage_enabled,
            "media_ai_reply_enabled": bool(state and state.media_ai_reply_enabled) if contact.media_ai_reply_enabled is None else contact.media_ai_reply_enabled,
        },
        "counts": {
            "messages": message_count,
            "conversations": len(conversations),
            "media": media_count,
            "has_media": media_count > 0,
        },
        "recent_messages": [
            {"id": item.id, "author": item.author, "content": item.content, "status": item.status, "created_at": item.created_at}
            for item in recent
        ],
        "recent_anomalies": [
            {"event": item.event, "level": item.level, "detail": item.detail or {}, "created_at": item.created_at}
            for item in anomalies
        ],
    }


def media_library(
    db: Session,
    *,
    account_id: str | None = None,
    contact_id: str | None = None,
    kind: str | None = None,
    analysis_status: str | None = None,
    query_text: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    query = (
        select(MessageAttachment, Message, Contact, Account)
        .join(Message, Message.id == MessageAttachment.message_id)
        .outerjoin(Contact, Contact.id == Message.contact_id)
        .outerjoin(Account, Account.id == Message.account_id)
    )
    if account_id:
        query = query.where(Message.account_id == account_id)
    if contact_id:
        query = query.where(Message.contact_id == contact_id)
    if kind:
        query = query.where(MessageAttachment.kind == kind.upper())
    if analysis_status:
        query = query.where(MessageAttachment.analysis_status == analysis_status.upper())
    if query_text:
        pattern = f"%{query_text.strip()}%"
        query = query.where(or_(MessageAttachment.file_name.ilike(pattern), MessageAttachment.analysis_text.ilike(pattern)))
    rows = db.execute(query.order_by(desc(MessageAttachment.created_at)).offset(offset).limit(limit)).all()
    result = []
    for attachment, message, contact, account in rows:
        result.append(
            {
                **_attachment(attachment),
                "message_id": message.id,
                "message_created_at": message.created_at,
                "message_content": message.content,
                "contact": {"id": contact.id, "display_name": contact.display_name, "platform_user_id": contact.platform_user_id} if contact else None,
                "account": {"id": account.id, "display_name": account.display_name} if account else None,
            }
        )
    return result
