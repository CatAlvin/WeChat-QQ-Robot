from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Contact, Message, TodoItem


FORMAL_MATTER_KIND = "FORMAL_MATTER"
FORMAL_MATTER_NOTICE = "这项正式事务已在后台记录，后续可以在待办中查看。"


def record_formal_matter(
    db: Session,
    *,
    contact: Contact,
    source_message: Message,
    account_id: str | None,
    detail: str,
) -> tuple[TodoItem, bool]:
    """Create one traceable todo per inbound message."""

    existing = db.scalar(
        select(TodoItem).where(
            TodoItem.kind == FORMAL_MATTER_KIND,
            TodoItem.source_message_id == source_message.id,
        )
    )
    if existing is not None:
        return existing, False
    item = TodoItem(
        title=f"正式事务 · {contact.display_name}"[:240],
        detail=detail,
        status="OPEN",
        priority="HIGH",
        contact_id=contact.id,
        source_message_id=source_message.id,
        kind=FORMAL_MATTER_KIND,
        account_id=account_id,
        delivery_status="RECORDED",
    )
    db.add(item)
    db.flush()
    return item, True


def append_record_notice(content: str) -> str:
    normalized = (content or "").strip()
    if FORMAL_MATTER_NOTICE in normalized:
        return normalized
    return f"{normalized}\n\n{FORMAL_MATTER_NOTICE}" if normalized else FORMAL_MATTER_NOTICE
