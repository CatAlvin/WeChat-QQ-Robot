from __future__ import annotations

import hashlib
import re
from calendar import monthrange
from datetime import datetime, time, timedelta

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.core.clock import BEIJING_TZ, as_beijing, utc_now
from app.core.enums import MessageAuthor, MessageDirection, MessageStatus
from app.models.entities import Contact, Memory, Message, RelationshipReminder


_COMMITMENT_RE = re.compile(r"(?:我会|我答应|我保证|我记得|回头我|稍后我|明天我|之后我会|有空我)")


def _dedupe(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _create_once(db: Session, **values) -> bool:
    if db.scalar(select(RelationshipReminder.id).where(RelationshipReminder.dedupe_key == values["dedupe_key"])):
        return False
    db.add(RelationshipReminder(**values))
    return True


def _next_birthday(mmdd: str, now_local: datetime) -> datetime | None:
    try:
        month, day = (int(item) for item in mmdd.split("-", 1))
    except (TypeError, ValueError):
        return None
    year = now_local.year
    for candidate_year in (year, year + 1):
        try:
            safe_day = min(day, monthrange(candidate_year, month)[1])
        except (ValueError, IndexError):
            return None
        candidate = datetime.combine(datetime(candidate_year, month, safe_day).date(), time(9, 0), tzinfo=BEIJING_TZ)
        # A birthday remains relevant for the whole Beijing calendar day. Using
        # a 09:00 timestamp comparison made today's reminder disappear after
        # 09:00 and caused time-of-day dependent behaviour.
        if candidate.date() >= now_local.date():
            return candidate
    return None


def sync_relationship_reminders(db: Session) -> int:
    """Create private dashboard reminders; this function never sends messages."""

    now = utc_now()
    now_local = as_beijing(now)
    created = 0
    contacts = list(
        db.scalars(
            select(Contact).where(Contact.relationship_reminders_enabled.is_(True)).order_by(Contact.created_at.asc())
        )
    )
    for contact in contacts:
        if contact.birthday_mmdd:
            birthday = _next_birthday(contact.birthday_mmdd, now_local)
            if birthday and birthday <= now_local + timedelta(days=7):
                due_at = birthday.astimezone(now.tzinfo)
                created += int(
                    _create_once(
                        db,
                        contact_id=contact.id,
                        kind="BIRTHDAY",
                        title=f"{contact.display_name} 的生日快到了",
                        detail=f"生日为 {contact.birthday_mmdd}。这只是给管理员的提醒，不会自动联系对方。",
                        due_at=due_at,
                        dedupe_key=_dedupe("birthday", contact.id, str(birthday.year)),
                    )
                )

        latest = db.scalar(
            select(Message)
            .where(
                Message.contact_id == contact.id,
                Message.status.in_((MessageStatus.RECEIVED, MessageStatus.SENT, MessageStatus.SHADOWED)),
            )
            .order_by(desc(Message.event_at), desc(Message.created_at))
            .limit(1)
        )
        threshold = max(3, min(contact.dormant_reminder_days, 365))
        if latest and latest.author == MessageAuthor.CONTACT and latest.event_at <= now - timedelta(days=threshold):
            period = as_beijing(now).strftime("%Y-%W")
            created += int(
                _create_once(
                    db,
                    contact_id=contact.id,
                    kind="UNANSWERED",
                    title=f"{contact.display_name} 有一条消息长期未回复",
                    detail=f"最后一条联系人消息已等待至少 {threshold} 天。请先查看上下文，再决定是否回复。",
                    due_at=now,
                    source_message_id=latest.id,
                    dedupe_key=_dedupe("unanswered", contact.id, latest.id, period),
                )
            )

        commitments = list(
            db.scalars(
                select(Message)
                .where(
                    Message.contact_id == contact.id,
                    Message.direction == MessageDirection.OUTBOUND,
                    Message.author == MessageAuthor.HUMAN,
                    Message.created_at >= now - timedelta(days=30),
                    Message.created_at <= now - timedelta(hours=12),
                )
                .order_by(desc(Message.created_at))
                .limit(30)
            )
        )
        for message in commitments:
            if not _COMMITMENT_RE.search(message.content or ""):
                continue
            created += int(
                _create_once(
                    db,
                    contact_id=contact.id,
                    kind="COMMITMENT",
                    title=f"检查对 {contact.display_name} 的待处理承诺",
                    detail=(message.content or "")[:500],
                    due_at=message.created_at + timedelta(days=1),
                    source_message_id=message.id,
                    dedupe_key=_dedupe("commitment", message.id),
                )
            )

        topics = list(
            db.scalars(
                select(Memory)
                .where(
                    Memory.contact_id == contact.id,
                    Memory.kind == "ONGOING_TOPIC",
                    Memory.review_status == "APPROVED",
                    Memory.updated_at <= now - timedelta(days=threshold),
                )
                .order_by(desc(Memory.updated_at))
                .limit(5)
            )
        )
        for memory in topics:
            period = now_local.strftime("%Y-%m")
            created += int(
                _create_once(
                    db,
                    contact_id=contact.id,
                    kind="TOPIC_FOLLOW_UP",
                    title=f"可以回顾与 {contact.display_name} 的重要话题",
                    detail=memory.content[:500],
                    due_at=now,
                    source_memory_id=memory.id,
                    dedupe_key=_dedupe("topic", memory.id, period),
                )
            )
    if created:
        db.flush()
    return created


def list_relationship_reminders(db: Session, *, status: str = "OPEN", limit: int = 100) -> list[dict]:
    now = utc_now()
    query = select(RelationshipReminder)
    if status != "ALL":
        query = query.where(RelationshipReminder.status == status)
    items = list(db.scalars(query.order_by(RelationshipReminder.due_at.asc(), desc(RelationshipReminder.created_at)).limit(limit)))
    contacts = {
        item.id: item for item in db.scalars(select(Contact).where(Contact.id.in_({row.contact_id for row in items})))
    } if items else {}
    return [
        {
            "id": item.id,
            "contact_id": item.contact_id,
            "contact_name": contacts[item.contact_id].display_name if item.contact_id in contacts else "未知联系人",
            "kind": item.kind,
            "title": item.title,
            "detail": item.detail,
            "status": item.status,
            "due_at": item.due_at,
            "source_message_id": item.source_message_id,
            "source_memory_id": item.source_memory_id,
            "snoozed_until": item.snoozed_until,
            "is_due": item.due_at <= now and (item.snoozed_until is None or item.snoozed_until <= now),
            "created_at": item.created_at,
        }
        for item in items
    ]


def update_relationship_reminder(db: Session, item: RelationshipReminder, action: str, snooze_days: int = 3) -> None:
    if action == "DONE":
        item.status = "DONE"
        item.snoozed_until = None
    elif action == "DISMISS":
        item.status = "DISMISSED"
        item.snoozed_until = None
    elif action == "SNOOZE":
        item.status = "OPEN"
        item.snoozed_until = utc_now() + timedelta(days=max(1, min(snooze_days, 30)))
    else:
        raise ValueError("不支持的提醒操作")
    db.flush()
