from __future__ import annotations

from datetime import timedelta

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.core.clock import utc_now
from app.core.enums import MessageStatus
from app.models.entities import AuditLog, BackgroundTask, ConnectorEvent, Incident, Message, RecoveryItem, TodoItem
from app.services.daily_digest import generate_daily_digest


TASK_KINDS = {"DAILY_DIGEST", "KNOWLEDGE_REFRESH", "RECOVERY_SYNC"}
BACKGROUND_TASK_RETENTION = 50


def prune_background_tasks(
    db: Session,
    *,
    keep: int = BACKGROUND_TASK_RETENTION,
    preserve_ids: set[str] | None = None,
) -> int:
    """Keep the task board bounded without discarding unfinished work.

    Pending/running tasks and failed tasks with an open recovery item are
    protected.  Old terminal history is removed first; the API still caps the
    visible board at ``keep`` if an unusual burst creates more active work.
    """

    keep = max(1, keep)
    rows = list(db.scalars(select(BackgroundTask).order_by(desc(BackgroundTask.created_at), desc(BackgroundTask.id))))
    if len(rows) <= keep:
        return 0

    protected = set(preserve_ids or ())
    protected.update(item.id for item in rows if item.status in {"PENDING", "RUNNING"})
    protected.update(
        db.scalars(
            select(RecoveryItem.source_id).where(
                RecoveryItem.source_type == "BACKGROUND_TASK",
                RecoveryItem.status.in_({"OPEN", "RETRYING"}),
            )
        )
    )

    retained = set(protected)
    for item in rows:
        if len(retained) >= keep:
            break
        retained.add(item.id)

    removable = [item for item in rows if item.id not in retained and item.status not in {"PENDING", "RUNNING"}]
    if not removable:
        return 0

    removable_ids = {item.id for item in removable}
    for recovery in db.scalars(
        select(RecoveryItem).where(
            RecoveryItem.source_type == "BACKGROUND_TASK",
            RecoveryItem.source_id.in_(removable_ids),
            RecoveryItem.status.in_({"RESOLVED", "DISMISSED"}),
        )
    ):
        db.delete(recovery)
    for item in removable:
        db.delete(item)
    db.flush()
    return len(removable)


def enqueue_task(db: Session, *, kind: str, title: str, payload: dict | None = None, max_attempts: int = 3) -> BackgroundTask:
    if kind not in TASK_KINDS:
        raise ValueError("不支持的任务类型")
    item = BackgroundTask(kind=kind, title=title, payload=payload or {}, max_attempts=max_attempts, available_at=utc_now())
    db.add(item)
    db.flush()
    prune_background_tasks(db, preserve_ids={item.id})
    return item


def sync_recovery_items(db: Session) -> int:
    created = 0
    sources: list[tuple[str, str, str, str | None, str | None, str | None, str | None]] = []
    for event in db.scalars(select(ConnectorEvent).where(ConnectorEvent.status == "FAILED")):
        sources.append(("CONNECTOR_EVENT", event.id, "INBOUND_REPROCESS", None, None, event.last_error, "连接器入站处理失败"))
    for task in db.scalars(select(BackgroundTask).where(BackgroundTask.status == "FAILED")):
        sources.append(("BACKGROUND_TASK", task.id, "TASK_RETRY", None, None, task.error_code, task.error_detail))
    for message in db.scalars(select(Message).where(Message.status == MessageStatus.FAILED)):
        sources.append(("MESSAGE", message.id, "OUTBOUND_MANUAL_REVIEW", message.contact_id, message.conversation_id, message.policy_reason, "真实发送失败；为避免重复发送，仅允许人工检查"))
    for source_type, source_id, kind, contact_id, conversation_id, code, detail in sources:
        exists = db.scalar(select(RecoveryItem.id).where(RecoveryItem.source_type == source_type, RecoveryItem.source_id == source_id, RecoveryItem.kind == kind))
        if exists:
            continue
        db.add(RecoveryItem(source_type=source_type, source_id=source_id, kind=kind, contact_id=contact_id, conversation_id=conversation_id, error_code=code, error_detail=detail))
        created += 1
    if created:
        db.flush()
    for item in db.scalars(select(RecoveryItem).where(RecoveryItem.status == "RETRYING")):
        if item.source_type == "CONNECTOR_EVENT":
            source = db.get(ConnectorEvent, item.source_id)
        elif item.source_type == "BACKGROUND_TASK":
            source = db.get(BackgroundTask, item.source_id)
        else:
            source = None
        if source is None:
            item.status = "OPEN"
        elif source.status == "DONE":
            item.status = "RESOLVED"
        elif source.status == "FAILED":
            item.status = "OPEN"
    return created


def sync_due_todo_incidents(db: Session) -> int:
    created = 0
    due_items = list(
        db.scalars(
            select(TodoItem).where(
                TodoItem.status == "OPEN",
                TodoItem.reminder_enabled.is_(True),
                TodoItem.due_at.is_not(None),
                TodoItem.due_at <= utc_now(),
            )
        )
    )
    for item in due_items:
        notified = db.scalar(select(AuditLog.id).where(AuditLog.event == "TODO_DUE", AuditLog.message_id == item.id))
        if notified:
            continue
        db.add(Incident(kind="TODO_DUE", severity="INFO", title=f"待办到期：{item.title}", detail=item.detail or "请在智能工作台中处理。"))
        db.add(AuditLog(event="TODO_DUE", message_id=item.id, detail={"todo_id": item.id, "due_at": item.due_at.isoformat() if item.due_at else None}))
        created += 1
    if created:
        db.flush()
    return created


def run_next_task(db: Session) -> BackgroundTask | None:
    item = db.scalar(select(BackgroundTask).where(BackgroundTask.status == "PENDING", BackgroundTask.available_at <= utc_now()).order_by(BackgroundTask.created_at.asc()).limit(1))
    if item is None:
        return None
    item.status = "RUNNING"
    item.started_at = utc_now()
    item.attempts += 1
    item.progress = 10
    db.flush()
    try:
        if item.kind == "DAILY_DIGEST":
            digest = generate_daily_digest(db, item.payload.get("local_date"))
            item.result = {"digest_id": digest.id, "local_date": digest.local_date}
        elif item.kind == "RECOVERY_SYNC":
            item.result = {"created": sync_recovery_items(db)}
        elif item.kind == "KNOWLEDGE_REFRESH":
            item.result = {"status": "ready", "mode": "local_keyword_index"}
        item.progress = 100
        item.status = "DONE"
        item.completed_at = utc_now()
        item.error_code = None
        item.error_detail = None
    except Exception as exc:
        item.error_code = type(exc).__name__
        item.error_detail = str(exc)[:2000]
        if item.attempts < item.max_attempts:
            item.status = "PENDING"
            item.available_at = utc_now() + timedelta(seconds=min(300, 5 * (2 ** item.attempts)))
        else:
            item.status = "FAILED"
            item.completed_at = utc_now()
        item.progress = 0
    db.flush()
    prune_background_tasks(db, preserve_ids={item.id})
    return item


def retry_recovery_item(db: Session, item: RecoveryItem) -> None:
    if item.source_type == "CONNECTOR_EVENT":
        source = db.get(ConnectorEvent, item.source_id)
        if source is None:
            raise ValueError("原始连接器事件不存在")
        source.status = "PENDING"
        source.last_error = None
    elif item.source_type == "BACKGROUND_TASK":
        source = db.get(BackgroundTask, item.source_id)
        if source is None:
            raise ValueError("原始后台任务不存在")
        source.status = "PENDING"
        source.available_at = utc_now()
        source.error_code = None
        source.error_detail = None
        source.completed_at = None
    else:
        raise PermissionError("真实消息失败不会自动重发，以免造成重复消息；请人工核对后处理")
    item.status = "RETRYING"
    item.retry_count += 1
    item.last_retry_at = utc_now()
    db.flush()
