from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import delete, select, update

from app.api.auth import router as auth_router
from app.api.connectors import router as connector_router
from app.api.routes import router as api_router
from app.core.config import get_settings
from app.core.events import event_hub
from app.core.security import decode_access_token
from app.database import get_db, init_db, session_scope
from app.models.entities import Account, AuditLog, BackgroundTask, ConnectorEvent, DailyDigest, PersonaProfile, ProviderConfig, RuntimeState
from app.core.clock import beijing_now
from app.services.runtime import cancel_queued_messages
from app.services.keepalive import KeepaliveService
from app.services.task_center import enqueue_task, prune_background_tasks, run_next_task, sync_due_todo_incidents, sync_recovery_items
from app.services.relationship_assistant import sync_relationship_reminders
from app.services.system_health import inspect_active_napcat


def _seed() -> None:
    settings = get_settings()
    with session_scope() as db:
        state = db.get(RuntimeState, 1)
        if state is None:
            db.add(RuntimeState(id=1, release_gate=settings.release_gate))
        if db.get(PersonaProfile, 1) is None:
            db.add(PersonaProfile(id=1))
        if db.scalar(select(Account).where(Account.platform == "WECHAT")) is None:
            db.add(Account(platform="WECHAT", display_name="个人微信", connector_kind="DISABLED_GATE", status="BLOCKED", enabled=False, experimental=True))
        if db.scalar(select(Account).where(Account.platform == "QQ")) is None:
            db.add(Account(platform="QQ", display_name="QQ 官方 Bot", connector_kind="QQ_OFFICIAL_BOT", status="DISCONNECTED", enabled=False, experimental=False))
        if db.scalar(select(Account).where(Account.platform == "QQ_NAPCAT")) is None:
            db.add(
                Account(
                    platform="QQ_NAPCAT",
                    display_name="NapCat / OneBot 11",
                    connector_kind="NAPCAT_ONEBOT11",
                    status="DISCONNECTED",
                    enabled=False,
                    experimental=True,
                    config={"api_base": "http://127.0.0.1:3001", "risk_acknowledged": False},
                )
            )
        if db.scalar(select(Account).where(Account.platform == "WECHAT_AUTOWX")) is None:
            db.add(
                Account(
                    platform="WECHAT_AUTOWX",
                    display_name="AutoWx 本地辅助",
                    connector_kind="AUTOWX_LOCAL_BRIDGE",
                    status="DISCONNECTED",
                    enabled=False,
                    experimental=True,
                    config={"strategy": "DRAFT_ONLY", "risk_acknowledged": False},
                )
            )
        qwen = db.scalar(select(ProviderConfig).where(ProviderConfig.provider_type == "QWEN"))
        qwen_name = db.scalar(select(ProviderConfig).where(ProviderConfig.name == "Qwen Cloud"))
        if qwen is None and qwen_name is None:
            # A credential is intentionally not guessed or copied from another
            # provider. The local administrator can attach a QWEN key, test it,
            # and then enable this ready-made middle fallback.
            db.add(
                ProviderConfig(
                    name="Qwen Cloud",
                    provider_type="QWEN",
                    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                    model="qwen-plus",
                    credential_id=None,
                    enabled=False,
                    priority=30,
                    timeout_seconds=30,
                )
            )
            ollama_configs = list(
                db.scalars(select(ProviderConfig).where(ProviderConfig.provider_type == "OLLAMA"))
            )
            for provider in ollama_configs:
                if provider.priority <= 30:
                    provider.priority = 100
            db.add(
                AuditLog(
                    event="QWEN_FALLBACK_TEMPLATE_CREATED",
                    detail={"priority": 30, "ollama_floor": 100, "enabled": False},
                )
            )
        kimi = db.scalar(select(ProviderConfig).where(ProviderConfig.provider_type == "KIMI"))
        kimi_name = db.scalar(select(ProviderConfig).where(ProviderConfig.name == "Kimi Cloud"))
        if kimi is None and kimi_name is None:
            db.add(
                ProviderConfig(
                    name="Kimi Cloud",
                    provider_type="KIMI",
                    base_url="https://api.moonshot.cn/v1",
                    model="kimi-k3",
                    credential_id=None,
                    enabled=False,
                    priority=10,
                    timeout_seconds=120,
                )
            )
            db.add(
                AuditLog(
                    event="KIMI_CLOUD_FALLBACK_TEMPLATE_CREATED",
                    detail={"model": "kimi-k3", "priority": 10, "enabled": False},
                )
            )
        qwen_local = db.scalar(
            select(ProviderConfig).where(
                ProviderConfig.provider_type == "OLLAMA",
                ProviderConfig.model == "qwen3.5:9b",
            )
        )
        qwen_local_name = db.scalar(select(ProviderConfig).where(ProviderConfig.name == "Qwen Local"))
        if qwen_local is None and qwen_local_name is None:
            db.add(
                ProviderConfig(
                    name="Qwen Local",
                    provider_type="OLLAMA",
                    base_url="http://127.0.0.1:11434",
                    model="qwen3.5:9b",
                    credential_id=None,
                    enabled=False,
                    priority=80,
                    timeout_seconds=120,
                )
            )
            db.add(
                AuditLog(
                    event="QWEN_LOCAL_FALLBACK_TEMPLATE_CREATED",
                    detail={"model": "qwen3.5:9b", "priority": 80, "enabled": False},
                )
            )
        cutoff = datetime.now(timezone.utc) - timedelta(days=settings.log_retention_days)
        db.execute(delete(AuditLog).where(AuditLog.created_at < cutoff))
        db.execute(delete(ConnectorEvent).where(ConnectorEvent.status == "DONE", ConnectorEvent.created_at < cutoff))
        prune_background_tasks(db)
        cancel_queued_messages(db)
        db.execute(update(ConnectorEvent).where(ConnectorEvent.status == "PROCESSING").values(status="PENDING"))


async def _recover_connector_events() -> None:
    from app.api.connectors import process_pending_connector_event

    while True:
        with session_scope() as db:
            event_ids = list(
                db.scalars(
                    select(ConnectorEvent.id)
                    .where(ConnectorEvent.status == "PENDING")
                    .order_by(ConnectorEvent.created_at.asc())
                    .limit(20)
                )
            )
        for event_id in event_ids:
            await process_pending_connector_event(event_id)
        await asyncio.sleep(2)


async def _run_keepalive_scheduler() -> None:
    service = KeepaliveService()
    while True:
        try:
            with session_scope() as db:
                await service.run_due(db)
        except Exception as exc:
            with session_scope() as db:
                db.add(
                    AuditLog(
                        event="KEEPALIVE_SCHEDULER_ERROR",
                        level="ERROR",
                        detail={"error": type(exc).__name__},
                    )
                )
        await asyncio.sleep(30)


async def _run_task_center() -> None:
    """Run local maintenance tasks without sharing long-lived DB sessions."""

    cycle = 0
    while True:
        try:
            with session_scope() as db:
                if cycle % 15 == 0:
                    sync_recovery_items(db)
                    sync_due_todo_incidents(db)
                    sync_relationship_reminders(db)
                local = beijing_now()
                if local.hour == 23 and local.minute >= 55:
                    date_key = local.date().isoformat()
                    digest_exists = db.scalar(select(DailyDigest.id).where(DailyDigest.local_date == date_key))
                    queued = db.scalar(
                        select(BackgroundTask.id).where(
                            BackgroundTask.kind == "DAILY_DIGEST",
                            BackgroundTask.status.in_(("PENDING", "RUNNING")),
                        )
                    )
                    if not digest_exists and not queued:
                        enqueue_task(db, kind="DAILY_DIGEST", title=f"生成 {date_key} 每日聊天摘要", payload={"local_date": date_key})
                run_next_task(db)
        except Exception as exc:
            with session_scope() as db:
                db.add(AuditLog(event="TASK_CENTER_ERROR", level="ERROR", detail={"error": type(exc).__name__}))
        cycle += 1
        await asyncio.sleep(2)


async def _run_connector_health_monitor() -> None:
    """Keep observed connector state fresh without sending any messages."""

    settings = get_settings()
    while True:
        changed = False
        observed_status: str | None = None
        try:
            with session_scope() as db:
                result = await inspect_active_napcat(db, settings, update_account=True)
                changed = result.changed
                observed_status = result.observed_status
        except Exception as exc:
            with session_scope() as db:
                db.add(AuditLog(event="CONNECTOR_HEALTH_MONITOR_ERROR", level="ERROR", detail={"error": type(exc).__name__}))
        if changed and observed_status:
            await event_hub.broadcast("account_state", {"platform": "QQ_NAPCAT", "status": observed_status})
        await asyncio.sleep(15)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    _seed()
    recovery_task = asyncio.create_task(_recover_connector_events())
    keepalive_task = asyncio.create_task(_run_keepalive_scheduler())
    task_center_task = asyncio.create_task(_run_task_center())
    connector_health_task = asyncio.create_task(_run_connector_health_monitor())
    try:
        yield
    finally:
        recovery_task.cancel()
        keepalive_task.cancel()
        task_center_task.cancel()
        connector_health_task.cancel()
        await asyncio.gather(recovery_task, keepalive_task, task_center_task, connector_health_task, return_exceptions=True)


settings = get_settings()
app = FastAPI(
    title="Neko AI Core",
    version="1.0.0",
    description="确定性发送策略优先的本地微信/QQ AI 托管核心。",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-Neko-Connector-Token"],
)
app.include_router(auth_router, prefix=settings.api_prefix)
app.include_router(connector_router, prefix=settings.api_prefix)
app.include_router(api_router, prefix=settings.api_prefix)


@app.get("/health")
def health(db=Depends(get_db)) -> dict[str, str | bool]:
    state = db.get(RuntimeState, 1)
    return {
        "status": "ok",
        "release_gate": state.release_gate if state is not None else settings.release_gate,
        "global_mode": state.global_mode if state is not None else "AUTO",
        "kill_switch": bool(state and state.kill_switch),
        "version": "1.0.0",
    }


@app.websocket(f"{settings.api_prefix}/ws")
async def websocket_events(websocket: WebSocket, token: str = Query(...)) -> None:
    try:
        decode_access_token(token)
    except HTTPException:
        # Accept only long enough to return a deterministic close code. No event
        # subscription is registered and no application data is exposed.
        await websocket.accept()
        await websocket.close(code=4401, reason="登录已失效")
        return
    await event_hub.connect(websocket)
    try:
        while True:
            message = await websocket.receive_text()
            if message == "ping":
                await websocket.send_json(
                    {
                        "event": "pong",
                        "data": {},
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )
    except WebSocketDisconnect:
        await event_hub.disconnect(websocket)
    except Exception:
        await event_hub.disconnect(websocket)
