from __future__ import annotations

from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import BEIJING_TZ, beijing_day_bounds_utc, beijing_now, utc_now
from app.core.enums import MessageAuthor, MessageStatus
from app.models.entities import Contact, DailyDigest, Message, TodoItem


def generate_daily_digest(db: Session, local_date: str | None = None) -> DailyDigest:
    today = beijing_now().date()
    if local_date:
        from datetime import datetime
        today = datetime.strptime(local_date, "%Y-%m-%d").date()
    from datetime import datetime
    start, end = beijing_day_bounds_utc(datetime.combine(today, datetime.min.time(), tzinfo=BEIJING_TZ))
    rows = list(db.scalars(select(Message).where(Message.event_at >= start, Message.event_at < end).order_by(Message.event_at.asc())))
    contact_names = dict(db.execute(select(Contact.id, Contact.display_name)).all())
    by_contact = Counter(contact_names.get(item.contact_id, "未知联系人") for item in rows if item.contact_id)
    failed = sum(item.status in {MessageStatus.FAILED, MessageStatus.BLOCKED, MessageStatus.CANCELLED} for item in rows)
    ai_count = sum(item.author == MessageAuthor.AI for item in rows)
    human_count = sum(item.author == MessageAuthor.HUMAN for item in rows)
    inbound = sum(item.author == MessageAuthor.CONTACT for item in rows)
    open_todos = db.scalar(select(func.count(TodoItem.id)).where(TodoItem.status == "OPEN")) or 0
    contacts = "、".join(f"{name} {count} 条" for name, count in by_contact.most_common(8)) or "无"
    content = (
        f"{today.isoformat()}（北京时间）聊天摘要\n\n"
        f"- 联系人消息：{inbound} 条\n- AI 回复：{ai_count} 条\n- 本人回复：{human_count} 条\n"
        f"- 失败/拦截/取消：{failed} 条\n- 未完成待办：{open_todos} 项\n- 活跃联系人：{contacts}\n\n"
        "说明：摘要由本机确定性统计生成，不会自动向任何联系人发送。"
    )
    item = db.scalar(select(DailyDigest).where(DailyDigest.local_date == today.isoformat()))
    metrics = {"inbound": inbound, "ai": ai_count, "human": human_count, "failed": failed, "open_todos": open_todos}
    if item is None:
        item = DailyDigest(local_date=today.isoformat(), content=content, metrics=metrics, generated_at=utc_now())
        db.add(item)
    else:
        item.content = content
        item.metrics = metrics
        item.generated_at = utc_now()
        item.status = "READY"
    db.flush()
    return item
