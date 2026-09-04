from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.core.enums import MessageDirection, MessageStatus
from app.models.entities import Contact, Message, MessageAttachment, OutboundDeliveryEvent


SAFE_PRE_SEND_ERRORS = {"CONNECTOR_MISSING", "CHANNEL_DISABLED", "ACCOUNT_NOT_ACTIVE"}


def next_attempt_no(db: Session, message_id: str) -> int:
    return int(db.scalar(select(func.max(OutboundDeliveryEvent.attempt_no)).where(OutboundDeliveryEvent.message_id == message_id)) or 0) + 1


def record_delivery_event(
    db: Session,
    message: Message,
    *,
    stage: str,
    status: str,
    attempt_no: int = 1,
    delivery_started: bool = False,
    acknowledged: bool = False,
    retry_allowed: bool = False,
    retry_reason: str | None = None,
    error_code: str | None = None,
    external_message_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> OutboundDeliveryEvent:
    event = OutboundDeliveryEvent(
        message_id=message.id,
        attempt_no=attempt_no,
        stage=stage,
        status=status,
        delivery_started=delivery_started,
        acknowledged=acknowledged,
        retry_allowed=retry_allowed,
        retry_reason=retry_reason,
        error_code=error_code,
        external_message_id=external_message_id,
        detail=detail or {},
    )
    db.add(event)
    db.flush()
    return event


def safe_retry_decision(message: Message, events: list[OutboundDeliveryEvent]) -> tuple[bool, str]:
    if message.status != MessageStatus.FAILED:
        return False, "只有发送失败的消息才需要重试。"
    if any(item.delivery_started for item in events):
        return False, "通道调用已经开始，无法确认平台是否实际收到；为避免重复消息，禁止自动重试。"
    if message.policy_reason not in SAFE_PRE_SEND_ERRORS:
        return False, "失败原因不能证明消息从未到达通道，需要人工核对。"
    if message.message_type != "TEXT":
        return False, "带附件消息暂不自动重试，避免文件重复上传。"
    return True, "尚未调用发送通道；重试时会重新执行账号、白名单、门禁和频率检查。"


def outbox_snapshot(db: Session, *, limit: int = 100) -> list[dict[str, Any]]:
    messages = list(
        db.scalars(
            select(Message)
            .where(Message.direction == MessageDirection.OUTBOUND)
            .order_by(desc(Message.created_at))
            .limit(limit)
        )
    )
    if not messages:
        return []
    message_ids = [item.id for item in messages]
    event_rows = list(
        db.scalars(
            select(OutboundDeliveryEvent)
            .where(OutboundDeliveryEvent.message_id.in_(message_ids))
            .order_by(OutboundDeliveryEvent.created_at.asc(), OutboundDeliveryEvent.id.asc())
        )
    )
    by_message: dict[str, list[OutboundDeliveryEvent]] = defaultdict(list)
    for event in event_rows:
        by_message[event.message_id].append(event)
    contacts = {
        item.id: item
        for item in db.scalars(select(Contact).where(Contact.id.in_({message.contact_id for message in messages if message.contact_id})))
    }
    attachment_counts = {
        message_id: count
        for message_id, count in db.execute(
            select(MessageAttachment.message_id, func.count(MessageAttachment.id))
            .where(MessageAttachment.message_id.in_(message_ids))
            .group_by(MessageAttachment.message_id)
        )
    }
    output = []
    for message in messages:
        events = by_message[message.id]
        retry_allowed, retry_reason = safe_retry_decision(message, events)
        if attachment_counts.get(message.id, 0):
            retry_allowed = False
            retry_reason = "带附件消息不自动重试，避免文件重复上传。"
        contact = contacts.get(message.contact_id)
        timeline = [
            {
                "id": item.id,
                "attempt_no": item.attempt_no,
                "stage": item.stage,
                "status": item.status,
                "delivery_started": item.delivery_started,
                "acknowledged": item.acknowledged,
                "error_code": item.error_code,
                "detail": item.detail,
                "created_at": item.created_at,
            }
            for item in events
        ]
        if not timeline:
            # Older and specialized send paths predate the delivery-event table.
            # Reconstruct only states proven by the durable Message row. A
            # failure is treated as delivery-started unless its code proves the
            # channel was never called; this intentionally prefers no duplicate.
            def legacy(stage: str, status: str, *, started: bool = False, acknowledged: bool = False) -> dict[str, Any]:
                return {
                    "id": f"synthetic:{message.id}:{stage}",
                    "attempt_no": 1,
                    "stage": stage,
                    "status": status,
                    "delivery_started": started,
                    "acknowledged": acknowledged,
                    "error_code": message.policy_reason,
                    "detail": {"reconstructed_from_durable_message": True},
                    "created_at": message.created_at,
                }

            timeline = [legacy("GENERATED", "GENERATED")]
            if message.status != MessageStatus.GENERATED:
                timeline.append(legacy("QUEUED", "QUEUED"))
            if message.status == MessageStatus.SENT:
                timeline.extend(
                    [
                        legacy("SEND_STARTED", "IN_FLIGHT", started=True),
                        legacy("PLATFORM_CONFIRMED", "SENT", started=True, acknowledged=True),
                    ]
                )
            elif message.status == MessageStatus.FAILED:
                pre_send = message.policy_reason in SAFE_PRE_SEND_ERRORS
                timeline.append(legacy("PRE_SEND" if pre_send else "SEND_RESULT", "FAILED", started=not pre_send))
            elif message.status == MessageStatus.SHADOWED:
                timeline.append(legacy("SHADOW", "NOT_TRANSMITTED"))
            elif message.status in {MessageStatus.BLOCKED, MessageStatus.CANCELLED}:
                timeline.append(legacy("FINAL_POLICY", str(message.status)))
        output.append(
            {
                "id": message.id,
                "contact_id": message.contact_id,
                "contact_name": contact.display_name if contact else "未知联系人",
                "account_id": message.account_id,
                "platform": message.platform,
                "content": message.content,
                "message_type": message.message_type,
                "status": message.status,
                "provider": message.provider,
                "model": message.model,
                "policy_reason": message.policy_reason,
                "external_message_id": message.external_message_id,
                "created_at": message.created_at,
                "retry_allowed": retry_allowed,
                "retry_reason": retry_reason,
                "timeline": timeline,
                "attachment_count": attachment_counts.get(message.id, 0),
            }
        )
    return output
