from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from fastapi.responses import FileResponse
from sqlalchemy import and_, delete, desc, func, or_, select, text
from sqlalchemy.orm import Session

from app.channels.base import InboundEvent
from app.channels.experimental import ExperimentalConnectorError, normalize_loopback_base_url
from app.core.config import get_settings
from app.core.clock import BEIJING_TZ, as_beijing, as_utc, beijing_start_of_day_utc, utc_now
from app.core.enums import AuditEvent, ConversationMode, GlobalMode, Importance, MessageAuthor, MessageDirection, MessageStatus
from app.core.events import event_hub
from app.core.security import CredentialVault, mask_secret, require_user
from app.database import get_db
from app.models.entities import (
    Account,
    AcceptanceRun,
    ApiCredential,
    AuditLog,
    BackgroundTask,
    ChatGroup,
    Contact,
    ConnectorEvent,
    Conversation,
    ConversationSummary,
    DailyDigest,
    Incident,
    KnowledgeDocument,
    Memory,
    Message,
    MessageAttachment,
    PersonaProfile,
    ProviderConfig,
    RelationshipReminder,
    RecoveryItem,
    RuntimeState,
    StickerAsset,
    TodoItem,
    User,
)
from app.schemas import (
    AccountConfigUpdate,
    ChatExportOut,
    ChatExportRequest,
    ContactCreate,
    ContactOut,
    ContactPatch,
    ConversationManualSend,
    CredentialCreate,
    CredentialOut,
    CredentialUpdate,
    DashboardOut,
    GroupCreate,
    GroupOut,
    GroupPatch,
    KillSwitchUpdate,
    LiveTimeWindowUpdate,
    MediaPolicyUpdate,
    MemoryCreate,
    MemoryOut,
    MemoryReviewUpdate,
    MemoryUpdate,
    ModeUpdate,
    NapCatProfileCreate,
    NapCatProfileUpdate,
    PersonaOut,
    PersonaUpdate,
    ProviderCreate,
    ProviderOut,
    ProviderReorder,
    ProviderUpdate,
    QQAcceptanceStart,
    ReleaseGateUpdate,
    RuntimeOut,
    SimulatorEventRequest,
    ServiceActionRequest,
    OutboxRetryRequest,
    RelationshipReminderAction,
    StageAcceptance,
    TaskCreate,
    TaskOut,
    RecoveryAction,
    RecoveryOut,
    KnowledgeCreate,
    KnowledgePatch,
    KnowledgeOut,
    TodoCreate,
    TodoPatch,
    TodoOut,
    DigestOut,
    StickerCreate,
    StickerPatch,
    StickerOut,
    StickerCacheImportRequest,
    StickerCacheScanOut,
)
from app.services.factory import build_connectors, build_pipeline
from app.services.pipeline import ManualSendError, OutboxRetryError
from app.services.account_selection import get_active_account, lock_platform_accounts
from app.services.admin_commands import ADMIN_RELATIONSHIP
from app.services.chat_export import ChatExportError, ChatExportOptions, ChatExportService
from app.services.memory import safe_memory
from app.services.media import MediaStoreError, normalize_media_mime_type, resolve_saved_media_path
from app.services.media_understanding import MediaUnderstandingService
from app.services.observability import contact_detail, media_library, message_diagnostics, reference_preview
from app.services.provider_ordering import apply_provider_order, capability_order
from app.services.knowledge import content_sha256
from app.services.stickers import import_qq_cache_sticker, qq_cache_candidate_path, resolve_sticker_path, save_sticker, scan_qq_sticker_cache
from app.services.task_center import BACKGROUND_TASK_RETENTION, enqueue_task, retry_recovery_item, sync_recovery_items
from app.services.runtime import runtime_control
from app.services.service_control import dispatch_service_action
from app.services.system_health import inspect_active_napcat, run_self_check
from app.services.configuration_diagnostics import configuration_report
from app.services.outbox import outbox_snapshot
from app.services.online_tools import calendar_snapshot, weather_lookup, web_search
from app.services.provider_health import model_health_snapshot, test_provider as probe_provider
from app.services.relationship_assistant import (
    list_relationship_reminders,
    sync_relationship_reminders,
    update_relationship_reminder,
)


router = APIRouter(dependencies=[Depends(require_user)])


def _runtime(db: Session) -> RuntimeState:
    return runtime_control.get(db)


def _credential_out(item: ApiCredential) -> CredentialOut:
    return CredentialOut(
        id=item.id,
        label=item.label,
        provider=item.provider,
        masked_hint=item.masked_hint,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.get("/dashboard", response_model=DashboardOut, tags=["dashboard"])
async def dashboard(db: Session = Depends(get_db)) -> DashboardOut:
    state = _runtime(db)
    now = utc_now()
    start = beijing_start_of_day_utc(now)
    received = db.scalar(select(func.count(Message.id)).where(Message.direction == MessageDirection.INBOUND, Message.created_at >= start)) or 0
    replies = db.scalar(select(func.count(Message.id)).where(Message.author == MessageAuthor.AI, Message.status == MessageStatus.SENT, Message.created_at >= start)) or 0
    tokens = db.scalar(select(func.sum(Message.input_tokens + Message.output_tokens)).where(Message.created_at >= start)) or 0
    active = db.scalar(select(func.count(Conversation.id)).where(Conversation.last_active_at >= now - timedelta(hours=24))) or 0
    attention = db.scalar(select(func.count(Incident.id)).where(Incident.resolved.is_(False))) or 0
    incidents = list(db.scalars(select(Incident).where(Incident.resolved.is_(False)).order_by(desc(Incident.created_at)).limit(5)))
    channels = [await connector.status() for connector in build_connectors(db).values()]
    enabled_provider_count = db.scalar(select(func.count(ProviderConfig.id)).where(ProviderConfig.enabled.is_(True))) or 0
    channels.append(
        {
            "platform": "AI",
            "status": "RUNNING" if enabled_provider_count else ("SIMULATOR" if state.release_gate == "SIMULATION" else "NOT_CONFIGURED"),
            "real_channel": False,
            "configured_providers": enabled_provider_count,
        }
    )
    return DashboardOut(
        runtime=RuntimeOut.model_validate(state),
        channels=channels,
        metrics={"received": received, "ai_replies": replies, "tokens": tokens, "active_conversations": active, "need_attention": attention},
        recent_incidents=[{"id": item.id, "kind": item.kind, "severity": item.severity, "title": item.title, "detail": item.detail, "created_at": item.created_at} for item in incidents],
    )


@router.get("/runtime", response_model=RuntimeOut, tags=["control"])
def runtime_state(db: Session = Depends(get_db)) -> RuntimeOut:
    return RuntimeOut.model_validate(_runtime(db))


@router.put("/runtime/mode", response_model=RuntimeOut, tags=["control"])
async def update_mode(payload: ModeUpdate, db: Session = Depends(get_db)) -> RuntimeOut:
    state = await runtime_control.set_mode(db, payload.mode)
    db.add(AuditLog(event=AuditEvent.MODE_CHANGED, detail={"mode": payload.mode.value}))
    db.commit()
    await event_hub.broadcast("mode_changed", {"mode": payload.mode.value})
    return RuntimeOut.model_validate(state)


@router.put("/runtime/kill-switch", response_model=RuntimeOut, tags=["control"])
async def kill_switch(payload: KillSwitchUpdate, db: Session = Depends(get_db)) -> RuntimeOut:
    state = await runtime_control.set_kill_switch(db, payload.enabled)
    db.add(AuditLog(event="KILL_SWITCH_CHANGED", level="WARNING" if payload.enabled else "INFO", detail={"enabled": payload.enabled}))
    db.commit()
    await event_hub.broadcast("mode_changed", {"kill_switch": payload.enabled})
    return RuntimeOut.model_validate(state)


@router.put("/runtime/live-time-window", response_model=RuntimeOut, tags=["control"])
async def update_live_time_window(payload: LiveTimeWindowUpdate, db: Session = Depends(get_db)) -> RuntimeOut:
    if not payload.enabled and not payload.disable_confirmed:
        raise HTTPException(status_code=422, detail="关闭 LIVE 时间门禁必须明确确认")
    async with runtime_control.send_lock:
        state = _runtime(db)
        state.live_time_window_enabled = payload.enabled
        state.live_auto_start = payload.start
        state.live_auto_end = payload.end
        cancelled = db.query(Message).filter(Message.status == MessageStatus.QUEUED).update({Message.status: MessageStatus.CANCELLED})
        db.add(
            AuditLog(
                event="LIVE_TIME_WINDOW_CHANGED",
                level="INFO" if payload.enabled else "WARNING",
                detail={
                    "enabled": payload.enabled,
                    "start": payload.start,
                    "end": payload.end,
                    "queued_cancelled": cancelled,
                },
            )
        )
        db.commit()
        db.refresh(state)
    await event_hub.broadcast(
        "mode_changed",
        {"live_time_window_enabled": payload.enabled, "live_auto_start": payload.start, "live_auto_end": payload.end},
    )
    return RuntimeOut.model_validate(state)


@router.put("/runtime/media-policy", response_model=RuntimeOut, tags=["control"])
async def update_media_policy(payload: MediaPolicyUpdate, db: Session = Depends(get_db)) -> RuntimeOut:
    async with runtime_control.send_lock:
        state = _runtime(db)
        state.media_storage_enabled = payload.storage_enabled
        state.media_save_images = payload.save_images
        state.media_save_audio = payload.save_audio
        state.media_save_files = payload.save_files
        state.media_ai_reply_enabled = payload.ai_reply_enabled
        state.media_max_file_mb = payload.max_file_mb
        state.media_understanding_enabled = payload.understanding_enabled
        state.media_understand_images = payload.understand_images
        state.media_transcribe_audio = payload.transcribe_audio
        state.media_extract_documents = payload.extract_documents
        state.media_vision_model = payload.vision_model.strip()
        state.media_whisper_model = payload.whisper_model
        state.media_whisper_device = payload.whisper_device
        state.media_whisper_allow_download = payload.whisper_allow_download
        state.media_understanding_max_chars = payload.understanding_max_chars
        state.kimi_media_upload_enabled = payload.kimi_upload_enabled
        state.kimi_media_upload_images = payload.kimi_upload_images
        state.kimi_media_upload_videos = payload.kimi_upload_videos
        state.kimi_media_max_file_mb = payload.kimi_max_file_mb
        state.tts_enabled = payload.tts_enabled
        state.tts_reply_to_audio_only = payload.tts_reply_to_audio_only
        state.tts_voice = payload.tts_voice.strip()
        state.tts_rate = payload.tts_rate
        state.tts_volume = payload.tts_volume
        state.tts_max_chars = payload.tts_max_chars
        state.napcat_quote_reply_enabled = payload.napcat_quote_reply_enabled
        db.add(
            AuditLog(
                event="MEDIA_POLICY_CHANGED",
                detail={
                    "storage_enabled": payload.storage_enabled,
                    "save_images": payload.save_images,
                    "save_audio": payload.save_audio,
                    "save_files": payload.save_files,
                    "ai_reply_enabled": payload.ai_reply_enabled,
                    "max_file_mb": payload.max_file_mb,
                    "understanding_enabled": payload.understanding_enabled,
                    "understand_images": payload.understand_images,
                    "transcribe_audio": payload.transcribe_audio,
                    "extract_documents": payload.extract_documents,
                    "vision_model": payload.vision_model.strip(),
                    "whisper_model": payload.whisper_model,
                    "whisper_device": payload.whisper_device,
                    "tts_enabled": payload.tts_enabled,
                    "tts_reply_to_audio_only": payload.tts_reply_to_audio_only,
                    "napcat_quote_reply_enabled": payload.napcat_quote_reply_enabled,
                    "whisper_allow_download": payload.whisper_allow_download,
                    "understanding_max_chars": payload.understanding_max_chars,
                    "kimi_upload_enabled": payload.kimi_upload_enabled,
                    "kimi_upload_images": payload.kimi_upload_images,
                    "kimi_upload_videos": payload.kimi_upload_videos,
                    "kimi_max_file_mb": payload.kimi_max_file_mb,
                },
            )
        )
        db.commit()
        db.refresh(state)
    await event_hub.broadcast("media_policy_changed", {"storage_enabled": payload.storage_enabled})
    return RuntimeOut.model_validate(state)


@router.post("/runtime/accept/{stage}", response_model=RuntimeOut, tags=["control"])
def accept_stage(stage: str, payload: StageAcceptance, db: Session = Depends(get_db)) -> RuntimeOut:
    if not payload.confirmed:
        raise HTTPException(status_code=422, detail="必须明确确认已完成该阶段验收")
    state = _runtime(db)
    normalized = stage.upper()
    if normalized == "SIMULATION":
        state.simulation_accepted = True
    elif normalized == "SHADOW":
        if not state.simulation_accepted:
            raise HTTPException(status_code=409, detail="必须先完成 SIMULATION 验收")
        if state.release_gate != "SHADOW":
            raise HTTPException(status_code=409, detail="必须先进入 SHADOW 并观察真实消息")
        live_accounts = list(
            db.scalars(
                select(Account).where(
                    Account.platform.in_(["QQ", "QQ_NAPCAT"]),
                    Account.enabled.is_(True),
                    Account.status == "ONLINE",
                )
            )
        )
        if not live_accounts:
            raise HTTPException(status_code=409, detail="尚未收到有效的 QQ 官方或 NapCat 真实入站消息")
        live_platforms = [item.platform for item in live_accounts]
        shadowed = db.scalar(
            select(func.count(Message.id)).where(
                Message.platform.in_(live_platforms),
                Message.status == MessageStatus.SHADOWED,
            )
        ) or 0
        if shadowed < 1:
            raise HTTPException(status_code=409, detail="尚未生成可供审阅的真实通道影子回复")
        state.shadow_accepted = True
    else:
        raise HTTPException(status_code=404, detail="未知验收阶段")
    db.add(AuditLog(event="RELEASE_STAGE_ACCEPTED", detail={"stage": normalized}))
    db.commit()
    return RuntimeOut.model_validate(state)


@router.put("/runtime/release-gate", response_model=RuntimeOut, tags=["control"])
async def update_release_gate(payload: ReleaseGateUpdate, db: Session = Depends(get_db)) -> RuntimeOut:
    async with runtime_control.send_lock:
        state = _runtime(db)
        if payload.gate == "SHADOW" and not state.simulation_accepted:
            raise HTTPException(status_code=409, detail="SIMULATION 尚未验收，不能进入 SHADOW")
        if payload.gate == "LIVE":
            if not (state.simulation_accepted and state.shadow_accepted):
                raise HTTPException(status_code=409, detail="SIMULATION 与 SHADOW 必须先完成验收")
            candidates: list[Account] = []
            for account in db.scalars(
                select(Account).where(
                    Account.platform.in_(["QQ", "QQ_NAPCAT"]),
                    Account.enabled.is_(True),
                    Account.status == "ONLINE",
                )
            ):
                config = account.config or {}
                configured = bool(
                    account.credential_id
                    and (
                        (account.platform == "QQ" and config.get("app_id"))
                        or (
                            account.platform == "QQ_NAPCAT"
                            and config.get("api_base")
                            and config.get("risk_acknowledged")
                        )
                    )
                )
                if configured:
                    candidates.append(account)
            if len(candidates) != 1:
                raise HTTPException(status_code=409, detail="LIVE 必须恰好启用 1 个已收到入站消息的 QQ 官方或 NapCat 通道")
            selected_account = candidates[0]
            initial_contacts = db.scalar(
                select(func.count(Contact.id)).where(
                    Contact.platform == selected_account.platform,
                    Contact.whitelisted.is_(True),
                    Contact.ai_enabled.is_(True),
                    Contact.importance != "MANUAL_ONLY",
                )
            ) or 0
            if initial_contacts != 1:
                raise HTTPException(status_code=409, detail="首次进入 LIVE 必须为所选通道恰好保留 1 个测试联系人白名单")
        state.release_gate = payload.gate
        cancelled = 0
        if payload.gate != "LIVE":
            cancelled = db.query(Message).filter(Message.status == MessageStatus.QUEUED).update({Message.status: MessageStatus.CANCELLED})
        db.add(AuditLog(event="RELEASE_GATE_CHANGED", level="WARNING", detail={"gate": payload.gate, "queued_cancelled": cancelled}))
        db.commit()
    return RuntimeOut.model_validate(state)


def _qq_acceptance_result(db: Session, run: AcceptanceRun) -> dict[str, Any]:
    end = run.completed_at or utc_now()
    official_events = list(
        db.scalars(
            select(ConnectorEvent).where(
                ConnectorEvent.platform == "QQ",
                ConnectorEvent.created_at >= run.started_at,
                ConnectorEvent.created_at <= end,
            )
        )
    )
    official_event_ids = {item.external_event_id for item in official_events}
    messages = list(
        db.scalars(
            select(Message).where(
                Message.platform == "QQ",
                Message.created_at >= run.started_at,
                Message.created_at <= end,
            )
        )
    )
    target_inbound = [
        item for item in messages
        if item.contact_id == run.target_contact_id
        and item.direction == MessageDirection.INBOUND
        and item.external_message_id in official_event_ids
    ]
    target_sent = [
        item for item in messages
        if item.contact_id == run.target_contact_id and item.direction == MessageDirection.OUTBOUND and item.status == MessageStatus.SENT
    ]
    all_sent = [
        item for item in messages
        if item.direction == MessageDirection.OUTBOUND and item.status == MessageStatus.SENT
    ]
    target_message_ids = {item.id for item in target_inbound}
    target_external_ids = {item.external_message_id for item in target_inbound}
    reply_counts = Counter(item.reply_to_external_id for item in target_sent if item.reply_to_external_id)
    duplicate_sends = sum(max(0, count - 1) for count in reply_counts.values())
    wrong_recipient_sends = sum(item.contact_id != run.target_contact_id for item in all_sent)

    audits = list(
        db.scalars(
            select(AuditLog).where(
                AuditLog.created_at >= run.started_at,
                AuditLog.created_at <= end,
                AuditLog.event.in_([AuditEvent.POLICY_DENY, AuditEvent.DUPLICATE_IGNORED]),
            )
        )
    )
    duplicate_webhooks = sum(
        item.event == AuditEvent.DUPLICATE_IGNORED and item.message_id in target_message_ids for item in audits
    )
    kill_denied_message_ids = {
        item.message_id
        for item in audits
        if item.event == AuditEvent.POLICY_DENY
        and item.message_id in target_message_ids
        and str((item.detail or {}).get("code", "")).endswith("KILL_SWITCH")
    }
    kill_denied_external_ids = {
        item.external_message_id for item in target_inbound if item.id in kill_denied_message_ids
    }
    kill_switch_violations = sum(
        item.reply_to_external_id in kill_denied_external_ids for item in target_sent
    )
    connector_failures = db.scalar(
        select(func.count(Incident.id)).where(
            Incident.created_at >= run.started_at,
            Incident.created_at <= end,
            Incident.kind.in_(["CONNECTOR_FAILURE", "QQ_WEBHOOK_PROCESSING_FAILURE"]),
        )
    ) or 0
    pending_inbox = db.scalar(
        select(func.count(ConnectorEvent.id)).where(
            ConnectorEvent.platform == "QQ",
            ConnectorEvent.created_at >= run.started_at,
            ConnectorEvent.status.in_(["PENDING", "PROCESSING"]),
        )
    ) or 0
    unique_received = len(target_external_ids)
    violations = duplicate_sends + wrong_recipient_sends + kill_switch_violations
    result = {
        "expected_messages": run.expected_messages,
        "unique_received": unique_received,
        "remaining_messages": max(0, run.expected_messages - unique_received),
        "sent_replies": len(target_sent),
        "blocked_or_cancelled": sum(
            item.contact_id == run.target_contact_id
            and item.direction == MessageDirection.OUTBOUND
            and item.status in {MessageStatus.BLOCKED, MessageStatus.CANCELLED}
            for item in messages
        ),
        "duplicate_webhooks_safely_ignored": duplicate_webhooks,
        "duplicate_sends": duplicate_sends,
        "wrong_recipient_sends": wrong_recipient_sends,
        "kill_switch_tested": bool(kill_denied_message_ids),
        "kill_switch_violations": kill_switch_violations,
        "connector_or_platform_failures": connector_failures,
        "pending_inbox": pending_inbox,
        "system_violations": violations,
    }
    previous_status = run.status
    if run.status == "RUNNING":
        if violations:
            run.status = "FAILED"
            run.completed_at = utc_now()
        elif unique_received >= run.expected_messages and result["kill_switch_tested"] and pending_inbox == 0:
            run.status = "PASSED"
            run.completed_at = utc_now()
    run.result = result
    if run.status != previous_status:
        db.add(
            AuditLog(
                event="QQ_ACCEPTANCE_COMPLETED",
                level="INFO" if run.status == "PASSED" else "ERROR",
                detail={"run_id": run.id, "status": run.status, "system_violations": violations},
            )
        )
    db.commit()
    contact = db.get(Contact, run.target_contact_id) if run.target_contact_id else None
    return {
        "id": run.id,
        "kind": run.kind,
        "status": run.status,
        "target_contact_id": run.target_contact_id,
        "target_name": contact.display_name if contact else "已删除联系人",
        "target_platform_user_id": run.target_platform_user_id,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        **result,
    }


@router.get("/acceptance/qq/current", tags=["acceptance"])
def current_qq_acceptance(db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.scalar(
        select(AcceptanceRun)
        .where(AcceptanceRun.kind == "QQ_200")
        .order_by(desc(AcceptanceRun.started_at))
        .limit(1)
    )
    return {"status": "NOT_STARTED"} if run is None else _qq_acceptance_result(db, run)


@router.get("/acceptance/readiness", tags=["acceptance"])
def acceptance_readiness(db: Session = Depends(get_db)) -> dict[str, Any]:
    state = _runtime(db)
    dialect = db.get_bind().dialect.name
    try:
        revision = str(db.scalar(text("SELECT version_num FROM alembic_version")) or "UNKNOWN")
    except Exception:
        revision = "UNAVAILABLE"
    providers = list(db.scalars(select(ProviderConfig).order_by(ProviderConfig.priority.asc())))
    audits = list(
        db.scalars(
            select(AuditLog)
            .where(AuditLog.event.in_(["PROVIDER_TESTED", "PROVIDER_TEST_FAILED"]))
            .order_by(desc(AuditLog.created_at))
            .limit(1000)
        )
    )
    provider_results: dict[str, bool] = {}
    provider_tested_at: dict[str, datetime] = {}
    for audit in audits:
        detail = audit.detail or {}
        provider_id = str(detail.get("provider_id") or "")
        provider_name = str(detail.get("provider") or "")
        for provider in providers:
            if provider.id in provider_results:
                continue
            if provider.id == provider_id or (not provider_id and provider.name == provider_name):
                provider_results[provider.id] = audit.event == "PROVIDER_TESTED" and detail.get("ok") is True
                provider_tested_at[provider.id] = audit.created_at
    required_types = {
        provider_type: any(provider.enabled and provider.provider_type == provider_type and provider_results.get(provider.id) for provider in providers)
        for provider_type in ("DEEPSEEK", "OPENAI", "OLLAMA")
    }
    qq = db.scalar(select(Account).where(Account.platform == "QQ"))
    qq_run = db.scalar(
        select(AcceptanceRun)
        .where(AcceptanceRun.kind == "QQ_200")
        .order_by(desc(AcceptanceRun.started_at))
        .limit(1)
    )
    qq_configured = bool(qq and qq.enabled and qq.credential_id and (qq.config or {}).get("app_id"))
    qq_online = bool(qq_configured and qq.status == "ONLINE")
    checks = [
        {"id": "mysql", "label": "MySQL 专用数据库", "ready": dialect == "mysql", "detail": f"当前数据库：{dialect} · revision {revision}"},
        *(
            {"id": provider_type.lower(), "label": f"{provider_type} 真实连接", "ready": ready, "detail": "最近连接测试成功" if ready else "尚无成功的真实连接测试"}
            for provider_type, ready in required_types.items()
        ),
        {"id": "qq_configured", "label": "QQ Official Bot 配置", "ready": qq_configured, "detail": "AppID / AppSecret 已配置并启用" if qq_configured else "尚未配置并启用官方 Bot"},
        {"id": "qq_online", "label": "QQ 真实入站", "ready": qq_online, "detail": "已收到有效签名回调" if qq_online else "尚未收到有效签名回调"},
        {"id": "qq_200", "label": "QQ 200 条端到端", "ready": bool(qq_run and qq_run.status == "PASSED"), "detail": f"当前状态：{qq_run.status if qq_run else 'NOT_STARTED'}"},
    ]
    return {
        "ready": all(check["ready"] for check in checks),
        "release_gate": state.release_gate,
        "checks": checks,
        "providers": [
            {
                "id": provider.id,
                "name": provider.name,
                "provider_type": provider.provider_type,
                "enabled": provider.enabled,
                "tested_ok": provider_results.get(provider.id, False),
                "tested_at": provider_tested_at.get(provider.id),
            }
            for provider in providers
        ],
    }


@router.post("/acceptance/qq/start", status_code=201, tags=["acceptance"])
def start_qq_acceptance(payload: QQAcceptanceStart, db: Session = Depends(get_db)) -> dict[str, Any]:
    running = db.scalar(
        select(AcceptanceRun).where(AcceptanceRun.kind == "QQ_200", AcceptanceRun.status == "RUNNING")
    )
    if running is not None:
        raise HTTPException(status_code=409, detail="已有 QQ 200 条验收正在进行")
    state = _runtime(db)
    if state.release_gate != "LIVE" or state.global_mode != GlobalMode.AUTO or state.kill_switch:
        raise HTTPException(status_code=409, detail="开始验收前必须处于 LIVE + AUTO，且急停已关闭")
    contact = db.get(Contact, payload.contact_id)
    if (
        contact is None
        or contact.platform != "QQ"
        or not contact.whitelisted
        or not contact.ai_enabled
        or contact.importance == "MANUAL_ONLY"
    ):
        raise HTTPException(status_code=409, detail="请选择唯一启用 AI 的 QQ 白名单测试联系人")
    eligible = db.scalar(
        select(func.count(Contact.id)).where(
            Contact.platform == "QQ",
            Contact.whitelisted.is_(True),
            Contact.ai_enabled.is_(True),
            Contact.importance != "MANUAL_ONLY",
        )
    ) or 0
    if eligible != 1:
        raise HTTPException(status_code=409, detail="验收期间必须恰好只有 1 个 QQ 自动回复白名单联系人")
    run = AcceptanceRun(
        kind="QQ_200",
        status="RUNNING",
        target_contact_id=contact.id,
        target_platform_user_id=contact.platform_user_id,
        expected_messages=200,
        result={},
    )
    db.add(run)
    db.flush()
    db.add(
        AuditLog(
            event="QQ_ACCEPTANCE_STARTED",
            level="WARNING",
            detail={"run_id": run.id, "contact_id": contact.id, "expected_messages": 200},
        )
    )
    db.commit()
    db.refresh(run)
    return _qq_acceptance_result(db, run)


@router.post("/acceptance/qq/{run_id}/cancel", tags=["acceptance"])
def cancel_qq_acceptance(run_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.get(AcceptanceRun, run_id)
    if run is None or run.kind != "QQ_200":
        raise HTTPException(status_code=404, detail="验收记录不存在")
    if run.status == "RUNNING":
        run.status = "CANCELLED"
        run.completed_at = utc_now()
        db.add(AuditLog(event="QQ_ACCEPTANCE_CANCELLED", level="WARNING", detail={"run_id": run.id}))
        db.commit()
    return _qq_acceptance_result(db, run)


@router.get("/contacts", response_model=list[ContactOut], tags=["contacts"])
def list_contacts(db: Session = Depends(get_db), account_id: str | None = None) -> list[Contact]:
    query = select(Contact)
    if account_id:
        query = query.where(Contact.account_id == account_id)
    return list(db.scalars(query.order_by(Contact.display_name.asc())))


@router.post("/contacts", response_model=ContactOut, status_code=201, tags=["contacts"])
def create_contact(payload: ContactCreate, db: Session = Depends(get_db)) -> Contact:
    account_id = payload.account_id
    if payload.platform != "SIMULATOR":
        account = db.get(Account, account_id) if account_id else get_active_account(db, payload.platform)
        if account is None or account.platform != payload.platform:
            raise HTTPException(status_code=422, detail="请选择与联系人平台一致的托管账号")
        account_id = account.id
    exists = db.scalar(
        select(Contact).where(
            Contact.platform == payload.platform,
            Contact.account_id == account_id,
            Contact.platform_user_id == payload.platform_user_id,
        )
    )
    if exists:
        raise HTTPException(status_code=409, detail="该平台联系人已存在")
    if payload.relationship_label.strip() == ADMIN_RELATIONSHIP and (payload.platform != "QQ_NAPCAT" or not payload.whitelisted):
        raise HTTPException(status_code=422, detail="聊天管理员必须是 NapCat 私聊白名单联系人")
    values = payload.model_dump(mode="json")
    values["account_id"] = account_id
    contact = Contact(**values)
    db.add(contact)
    db.commit()
    db.refresh(contact)
    return contact


@router.patch("/contacts/{contact_id}", response_model=ContactOut, tags=["contacts"])
async def patch_contact(contact_id: str, payload: ContactPatch, db: Session = Depends(get_db)) -> Contact:
    async with runtime_control.send_lock:
        contact = db.get(Contact, contact_id)
        if contact is None:
            raise HTTPException(status_code=404, detail="联系人不存在")
        was_keepalive_enabled = contact.keepalive_enabled
        for key, value in payload.model_dump(exclude_unset=True, mode="json").items():
            setattr(contact, key, value)
        if contact.relationship_label.strip() == ADMIN_RELATIONSHIP and (contact.platform != "QQ_NAPCAT" or not contact.whitelisted):
            db.rollback()
            raise HTTPException(status_code=422, detail="聊天管理员必须保持为 NapCat 私聊白名单联系人")
        if contact.keepalive_enabled:
            if contact.platform != "QQ_NAPCAT":
                raise HTTPException(status_code=422, detail="续火目前只支持 NapCat 私聊联系人")
            if not contact.platform_user_id.isdigit():
                raise HTTPException(status_code=422, detail="续火联系人 QQ 号必须是纯数字")
            account = db.get(Account, contact.keepalive_account_id) if contact.keepalive_account_id else None
            if account is None or account.platform != "QQ_NAPCAT":
                raise HTTPException(status_code=422, detail="开启续火前必须选择一个 NapCat 托管账号")
            if not (account.config or {}).get("risk_acknowledged") or not account.credential_id:
                raise HTTPException(status_code=409, detail="所选 NapCat 账号尚未完成风险确认与 Token 配置")
            if not was_keepalive_enabled:
                contact.keepalive_last_status = "WAITING"
                contact.keepalive_last_error = None
            if contact.account_id and account.id != contact.account_id:
                raise HTTPException(status_code=422, detail="续火发送账号必须与联系人所属账号一致")
        if not contact.whitelisted or not contact.ai_enabled or contact.importance == Importance.MANUAL_ONLY or not contact.keepalive_enabled:
            db.query(Message).filter(Message.contact_id == contact.id, Message.status == MessageStatus.QUEUED).update({Message.status: MessageStatus.CANCELLED})
        db.commit()
        db.refresh(contact)
    return contact


@router.get("/exports/defaults", tags=["exports"])
def chat_export_defaults() -> dict[str, Any]:
    now = utc_now()
    return {
        "output_directory": ChatExportService().default_output_directory(),
        "start_at": (now - timedelta(days=30)).isoformat(),
        "end_at": now.isoformat(),
        "format": "MARKDOWN",
        "include_contact_messages": True,
        "include_ai_messages": True,
        "include_human_messages": True,
        "include_attachments": True,
        "max_messages": 5000,
    }


@router.post("/exports/chats", response_model=ChatExportOut, tags=["exports"])
def export_chats(payload: ChatExportRequest, db: Session = Depends(get_db)) -> ChatExportOut:
    service = ChatExportService()
    try:
        result = service.export(
            db,
            ChatExportOptions(
                contact_ids=payload.contact_ids,
                start_at=payload.start_at,
                end_at=payload.end_at,
                include_contact_messages=payload.include_contact_messages,
                include_ai_messages=payload.include_ai_messages,
                include_human_messages=payload.include_human_messages,
                format=payload.format,
                include_attachments=payload.include_attachments,
                output_directory=payload.output_directory,
                max_messages=payload.max_messages,
            ),
        )
    except ChatExportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.add(
        AuditLog(
            event="CHAT_EXPORT_CREATED",
            detail={
                "contact_count": result.contact_count,
                "message_count": result.message_count,
                "attachment_count": result.attachment_count,
                "format": payload.format,
                "truncated": result.truncated,
            },
        )
    )
    db.commit()
    return ChatExportOut(
        output_directory=result.output_directory,
        record_files=list(result.record_files),
        archive_file=result.archive_file,
        contact_count=result.contact_count,
        message_count=result.message_count,
        attachment_count=result.attachment_count,
        missing_attachment_count=result.missing_attachment_count,
        truncated=result.truncated,
    )


@router.get("/groups", response_model=list[GroupOut], tags=["groups"])
def list_groups(db: Session = Depends(get_db), account_id: str | None = None) -> list[ChatGroup]:
    query = select(ChatGroup)
    if account_id:
        query = query.where(ChatGroup.account_id == account_id)
    return list(db.scalars(query.order_by(ChatGroup.display_name.asc())))


@router.post("/groups", response_model=GroupOut, status_code=201, tags=["groups"])
def create_group(payload: GroupCreate, db: Session = Depends(get_db)) -> ChatGroup:
    account_id = payload.account_id
    if payload.platform != "SIMULATOR":
        account = db.get(Account, account_id) if account_id else get_active_account(db, payload.platform)
        if account is None or account.platform != payload.platform:
            raise HTTPException(status_code=422, detail="请选择与群聊平台一致的托管账号")
        account_id = account.id
    exists = db.scalar(
        select(ChatGroup).where(
            ChatGroup.platform == payload.platform,
            ChatGroup.account_id == account_id,
            ChatGroup.platform_group_id == payload.platform_group_id,
        )
    )
    if exists:
        raise HTTPException(status_code=409, detail="该群聊已存在")
    values = payload.model_dump(mode="json")
    values["account_id"] = account_id
    group = ChatGroup(**values)
    db.add(group)
    db.commit()
    db.refresh(group)
    return group


@router.patch("/groups/{group_id}", response_model=GroupOut, tags=["groups"])
async def patch_group(group_id: str, payload: GroupPatch, db: Session = Depends(get_db)) -> ChatGroup:
    async with runtime_control.send_lock:
        group = db.get(ChatGroup, group_id)
        if group is None:
            raise HTTPException(status_code=404, detail="群聊不存在")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(group, key, value)
        if not group.allowed or not group.ai_enabled:
            conversation_ids = select(Conversation.id).where(Conversation.group_id == group.id)
            db.query(Message).filter(Message.conversation_id.in_(conversation_ids), Message.status == MessageStatus.QUEUED).update(
                {Message.status: MessageStatus.CANCELLED}, synchronize_session=False
            )
        db.commit()
        db.refresh(group)
    return group


@router.get("/conversations", tags=["conversations"])
def list_conversations(db: Session = Depends(get_db), account_id: str | None = None) -> list[dict[str, Any]]:
    query = select(Conversation)
    if account_id:
        query = query.where(Conversation.account_id == account_id)
    items = list(db.scalars(query.order_by(desc(Conversation.last_active_at)).limit(100)))
    result = []
    for item in items:
        contact = db.get(Contact, item.contact_id) if item.contact_id else None
        last = db.scalar(select(Message).where(Message.conversation_id == item.id).order_by(desc(Message.created_at)).limit(1))
        last_ai = db.scalar(
            select(Message)
            .where(Message.conversation_id == item.id, Message.author == MessageAuthor.AI, Message.model.is_not(None))
            .order_by(desc(Message.created_at))
            .limit(1)
        )
        result.append({
            "id": item.id,
            "platform": item.platform,
            "account_id": item.account_id,
            "display_name": contact.display_name if contact else "群聊",
            "mode": item.mode,
            "managed_rounds": item.managed_rounds,
            "last_active_at": item.last_active_at,
            "last_message": last.content[:160] if last else "",
            "whitelisted": bool(contact and contact.whitelisted),
            "importance": contact.importance if contact else "MANUAL_ONLY",
            "ai_enabled": bool(contact and contact.ai_enabled),
            "memory_enabled": bool(contact and contact.memory_enabled),
            "model": last_ai.model if last_ai else "尚未生成",
        })
    return result


@router.get("/conversations/{conversation_id}/messages", tags=["conversations"])
def conversation_messages(
    conversation_id: str,
    db: Session = Depends(get_db),
    limit: int = 500,
    before_created_at: datetime | None = None,
    before_id: str | None = None,
) -> list[dict[str, Any]]:
    if db.get(Conversation, conversation_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    limit = max(1, min(limit, 501))
    query = select(Message).where(Message.conversation_id == conversation_id)
    if before_created_at is not None:
        query = query.where(
            or_(
                Message.created_at < before_created_at,
                and_(Message.created_at == before_created_at, Message.id < (before_id or "")),
            )
        )
    newest_first = list(db.scalars(query.order_by(desc(Message.created_at), desc(Message.id)).limit(limit)))
    items = list(reversed(newest_first))
    attachment_rows = list(
        db.scalars(
            select(MessageAttachment)
            .where(MessageAttachment.message_id.in_([item.id for item in items]))
            .order_by(MessageAttachment.message_id, MessageAttachment.segment_index)
        )
    ) if items else []
    attachments: dict[str, list[dict[str, Any]]] = {}
    for item in attachment_rows:
        attachments.setdefault(item.message_id, []).append(
            {
                "id": item.id,
                "kind": item.kind,
                "segment_type": item.segment_type,
                "file_name": item.file_name,
                "mime_type": item.mime_type,
                "size_bytes": item.size_bytes,
                "status": item.status,
                "error_code": item.error_code,
                "storage_source": (item.attachment_metadata or {}).get("neko_storage_source"),
                "storage_attempts": (item.attachment_metadata or {}).get("neko_storage_attempts", []),
                "analysis_status": item.analysis_status,
                "analysis_provider": item.analysis_provider,
                "analysis_model": item.analysis_model,
                "analysis_text": item.analysis_text,
                "analysis_error_code": item.analysis_error_code,
                "analysis_metadata": item.analysis_metadata,
                "analyzed_at": item.analyzed_at,
                "download_url": f"/media/{item.id}" if item.status == "SAVED" else None,
            }
        )
    return [{"id": item.id, "author": item.author, "direction": item.direction, "sender_id": item.sender_id, "receiver_id": item.receiver_id, "event_at": item.event_at, "content": item.content, "message_type": item.message_type, "status": item.status, "provider": item.provider, "model": item.model, "created_at": item.created_at, "attachments": attachments.get(item.id, [])} for item in items]


@router.get("/messages/{message_id}/diagnostics", tags=["observability"])
def diagnose_message(message_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return message_diagnostics(db, message_id, get_settings())
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/messages/{message_id}/reference-preview", tags=["observability"])
def message_reference_preview(message_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return reference_preview(db, message_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/contacts/{contact_id}/detail", tags=["contacts"])
def get_contact_detail(contact_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return contact_detail(db, contact_id, get_settings())
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/media-library", tags=["media"])
def list_media_library(
    account_id: str | None = None,
    contact_id: str | None = None,
    kind: str | None = None,
    analysis_status: str | None = None,
    q: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return media_library(
        db,
        account_id=account_id,
        contact_id=contact_id,
        kind=kind,
        analysis_status=analysis_status,
        query_text=q,
        limit=limit,
        offset=offset,
    )


@router.get("/system/self-check", tags=["control"])
async def system_self_check(db: Session = Depends(get_db)) -> dict[str, Any]:
    return await run_self_check(db, get_settings())


@router.post("/system/control", tags=["control"])
def system_control(
    payload: ServiceActionRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if payload.action in {"stop", "restart"} and not payload.confirmed:
        raise HTTPException(status_code=422, detail="停止或重启整套服务必须明确确认")
    db.add(
        AuditLog(
            event="SYSTEM_CONTROL_REQUESTED",
            level="WARNING" if payload.action in {"stop", "restart"} else "INFO",
            detail={"action": payload.action},
        )
    )
    db.commit()
    background_tasks.add_task(dispatch_service_action, payload.action)
    if payload.action == "start":
        return {"accepted": True, "action": "start", "message": "恢复操作已受理；核心在线时会补启缺失的后台页面。"}
    return {"accepted": True, "action": payload.action, "message": "操作已受理，服务会在 2 秒后执行。"}


@router.get("/reliability/configuration", tags=["reliability"])
async def reliability_configuration(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Read-only configuration wizard; it never sends a platform message."""

    return await configuration_report(db, get_settings())


@router.post("/reliability/configuration/{check_id}/retry", tags=["reliability"])
async def retry_configuration_check(check_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    if check_id.startswith("provider:"):
        provider_id = check_id.partition(":")[2]
        item = db.get(ProviderConfig, provider_id)
        if item is None:
            raise HTTPException(status_code=404, detail="模型配置不存在")
        await probe_provider(db, item, get_settings())
        db.commit()
    db.add(AuditLog(event="CONFIGURATION_CHECK_RETRIED", detail={"check_id": check_id}))
    db.commit()
    return await configuration_report(db, get_settings())


@router.get("/reliability/models", tags=["reliability"])
def reliability_models(db: Session = Depends(get_db)) -> dict[str, Any]:
    return model_health_snapshot(db)


@router.post("/reliability/models/{provider_id}/retry", tags=["reliability"])
async def retry_model_health(provider_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    item = db.get(ProviderConfig, provider_id)
    if item is None:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    await probe_provider(db, item, get_settings())
    db.commit()
    return model_health_snapshot(db)


@router.get("/reliability/outbox", tags=["reliability"])
def reliability_outbox(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return outbox_snapshot(db, limit=limit)


@router.post("/reliability/outbox/{message_id}/retry", tags=["reliability"])
async def retry_outbox_message(
    message_id: str,
    payload: OutboxRetryRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if not payload.confirmed:
        raise HTTPException(status_code=422, detail="重试前必须确认；系统只允许重试可证明尚未调用通道的文本消息。")
    try:
        result = await build_pipeline(db).retry_undelivered(db, message_id=message_id)
    except OutboxRetryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return asdict(result)


@router.get("/reliability/reminders", tags=["reliability"])
def relationship_reminders(
    reminder_status: str = Query(default="OPEN", pattern="^(OPEN|DONE|DISMISSED|ALL)$"),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return list_relationship_reminders(db, status=reminder_status, limit=limit)


@router.post("/reliability/reminders/scan", tags=["reliability"])
def scan_relationship_reminders(db: Session = Depends(get_db)) -> dict[str, Any]:
    created = sync_relationship_reminders(db)
    db.add(AuditLog(event="RELATIONSHIP_REMINDERS_SCANNED", detail={"created": created, "delivery": "DASHBOARD_ONLY"}))
    db.commit()
    return {"created": created, "items": list_relationship_reminders(db)}


@router.post("/reliability/reminders/{reminder_id}/action", tags=["reliability"])
def act_on_relationship_reminder(
    reminder_id: str,
    payload: RelationshipReminderAction,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item = db.get(RelationshipReminder, reminder_id)
    if item is None:
        raise HTTPException(status_code=404, detail="提醒不存在")
    update_relationship_reminder(db, item, payload.action, payload.snooze_days)
    db.add(AuditLog(event="RELATIONSHIP_REMINDER_UPDATED", detail={"reminder_id": item.id, "action": payload.action}))
    db.commit()
    return {"ok": True, "status": item.status, "snoozed_until": item.snoozed_until}


@router.get("/media/{attachment_id}", tags=["conversations"])
def saved_media(attachment_id: str, db: Session = Depends(get_db)) -> FileResponse:
    item = db.get(MessageAttachment, attachment_id)
    if item is None or item.status != "SAVED":
        raise HTTPException(status_code=404, detail="媒体文件不存在或尚未保存")
    try:
        path = resolve_saved_media_path(item)
    except MediaStoreError as exc:
        raise HTTPException(status_code=404, detail="媒体文件已不可用") from exc
    return FileResponse(
        path,
        media_type=normalize_media_mime_type(item.mime_type, file_name=item.file_name)
        or "application/octet-stream",
        filename=item.file_name,
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.get("/media-understanding/status", tags=["control"])
async def media_understanding_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    return await MediaUnderstandingService(get_settings()).status(_runtime(db))


@router.get("/usage", tags=["usage"])
def usage(db: Session = Depends(get_db)) -> dict[str, Any]:
    now = utc_now()
    today = as_beijing(now).date()
    first_day = today - timedelta(days=6)
    since = as_utc(datetime.combine(first_day, datetime.min.time(), tzinfo=BEIJING_TZ))
    items = list(
        db.scalars(
            select(Message)
            .where(Message.author == MessageAuthor.AI, Message.created_at >= since)
            .order_by(Message.created_at.asc())
        )
    )
    by_day = {first_day + timedelta(days=offset): {"messages": 0, "input_tokens": 0, "output_tokens": 0} for offset in range(7)}
    by_model: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        day = as_beijing(item.created_at).date()
        if day in by_day:
            bucket = by_day[day]
            bucket["messages"] += 1
            bucket["input_tokens"] += item.input_tokens
            bucket["output_tokens"] += item.output_tokens
        key = (item.provider or "未调用模型", item.model or "—")
        model_bucket = by_model.setdefault(key, {"provider": key[0], "model": key[1], "messages": 0, "input_tokens": 0, "output_tokens": 0})
        model_bucket["messages"] += 1
        model_bucket["input_tokens"] += item.input_tokens
        model_bucket["output_tokens"] += item.output_tokens
    days = [{"date": day.isoformat(), **values, "total_tokens": values["input_tokens"] + values["output_tokens"]} for day, values in by_day.items()]
    today_values = by_day[today]
    return {
        "today": {**today_values, "total_tokens": today_values["input_tokens"] + today_values["output_tokens"]},
        "last_7_days": days,
        "by_model": sorted(
            ({**values, "total_tokens": values["input_tokens"] + values["output_tokens"]} for values in by_model.values()),
            key=lambda value: value["total_tokens"],
            reverse=True,
        ),
    }


@router.get("/conversations/{conversation_id}/summary", tags=["conversations"])
def conversation_summary(conversation_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    if db.get(Conversation, conversation_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    item = db.scalar(
        select(ConversationSummary)
        .where(ConversationSummary.conversation_id == conversation_id)
        .order_by(desc(ConversationSummary.created_at))
        .limit(1)
    )
    if item is None:
        return {"content": "", "created_at": None}
    return {"content": item.content, "created_at": item.created_at}


@router.post("/conversations/{conversation_id}/takeover", tags=["conversations"])
async def take_over(conversation_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    async with runtime_control.send_lock:
        conversation = db.get(Conversation, conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        conversation.mode = ConversationMode.HUMAN
        conversation.human_until = utc_now() + timedelta(minutes=get_settings().human_takeover_minutes)
        db.query(Message).filter(Message.conversation_id == conversation_id, Message.status == MessageStatus.QUEUED).update({Message.status: MessageStatus.CANCELLED})
        db.add(AuditLog(event=AuditEvent.HUMAN_TAKEOVER, conversation_id=conversation_id, detail={"source": "dashboard"}))
        db.commit()
    await event_hub.broadcast("mode_changed", {"conversation_id": conversation_id, "mode": ConversationMode.HUMAN})
    return {"mode": ConversationMode.HUMAN}


@router.post("/conversations/{conversation_id}/resume", tags=["conversations"])
async def resume_ai(conversation_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    async with runtime_control.send_lock:
        conversation = db.get(Conversation, conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        conversation.mode = ConversationMode.AUTO_READY
        conversation.human_until = None
        conversation.cooldown_until = None
        conversation.managed_rounds = 0
        db.add(AuditLog(event="AI_RESUMED", conversation_id=conversation_id, detail={"source": "dashboard"}))
        db.commit()
    await event_hub.broadcast("mode_changed", {"conversation_id": conversation_id, "mode": ConversationMode.AUTO_READY})
    return {"mode": ConversationMode.AUTO_READY}


@router.post("/conversations/{conversation_id}/manual-send", tags=["conversations"])
async def send_conversation_manual_message(
    conversation_id: str,
    payload: ConversationManualSend,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        result = await build_pipeline(db).send_dashboard_manual(
            db,
            conversation_id=conversation_id,
            content=payload.content,
        )
    except ManualSendError as exc:
        status_code = 404 if exc.code in {"CONVERSATION_NOT_FOUND", "CONTACT_NOT_FOUND"} else 409
        raise HTTPException(status_code=status_code, detail=f"{exc}（{exc.code}）") from exc
    if not result.sent:
        raise HTTPException(status_code=502, detail=f"{result.reason}（{result.code}）")
    return {
        "sent": True,
        "message_id": result.outbound_message_id,
        "code": result.code,
        "message": result.reason,
        "takeover_triggered": False,
    }


@router.get("/credentials", response_model=list[CredentialOut], tags=["ai"])
def list_credentials(db: Session = Depends(get_db)) -> list[CredentialOut]:
    return [_credential_out(item) for item in db.scalars(select(ApiCredential).order_by(ApiCredential.created_at.desc()))]


@router.post("/credentials", response_model=CredentialOut, status_code=201, tags=["ai"])
def create_credential(payload: CredentialCreate, db: Session = Depends(get_db)) -> CredentialOut:
    vault = CredentialVault()
    item = ApiCredential(
        label=payload.label,
        provider=payload.provider.upper(),
        encrypted_secret=vault.encrypt(payload.secret),
        masked_hint=mask_secret(payload.secret),
    )
    db.add(item)
    db.add(AuditLog(event="CREDENTIAL_CREATED", detail={"label": item.label, "provider": item.provider}))
    db.commit()
    db.refresh(item)
    return _credential_out(item)


@router.put("/credentials/{credential_id}", response_model=CredentialOut, tags=["ai"])
def update_credential(credential_id: str, payload: CredentialUpdate, db: Session = Depends(get_db)) -> CredentialOut:
    item = db.get(ApiCredential, credential_id)
    if item is None:
        raise HTTPException(status_code=404, detail="凭据不存在")
    if payload.label is not None:
        item.label = payload.label
    if payload.secret is not None:
        item.encrypted_secret = CredentialVault().encrypt(payload.secret)
        item.masked_hint = mask_secret(payload.secret)
    db.add(
        AuditLog(
            event="CREDENTIAL_UPDATED",
            detail={"credential_id": item.id, "label_changed": payload.label is not None, "secret_rotated": payload.secret is not None},
        )
    )
    db.commit()
    db.refresh(item)
    return _credential_out(item)


@router.delete("/credentials/{credential_id}", status_code=204, response_class=Response, tags=["ai"])
def delete_credential(credential_id: str, db: Session = Depends(get_db)) -> Response:
    item = db.get(ApiCredential, credential_id)
    if item is None:
        raise HTTPException(status_code=404, detail="凭据不存在")
    if db.scalar(select(func.count(ProviderConfig.id)).where(ProviderConfig.credential_id == credential_id)):
        raise HTTPException(status_code=409, detail="凭据仍被模型配置引用")
    if db.scalar(select(func.count(Account.id)).where(Account.credential_id == credential_id)):
        raise HTTPException(status_code=409, detail="凭据仍被账号连接器引用")
    db.add(AuditLog(event="CREDENTIAL_DELETED", level="WARNING", detail={"credential_id": item.id, "provider": item.provider}))
    db.delete(item)
    db.commit()
    return Response(status_code=204)


@router.get("/providers", response_model=list[ProviderOut], tags=["ai"])
def list_providers(db: Session = Depends(get_db)) -> list[ProviderConfig]:
    return list(db.scalars(select(ProviderConfig).order_by(ProviderConfig.priority.asc())))


@router.post("/providers", response_model=ProviderOut, status_code=201, tags=["ai"])
def create_provider(payload: ProviderCreate, db: Session = Depends(get_db)) -> ProviderConfig:
    if db.scalar(select(ProviderConfig).where(ProviderConfig.name == payload.name)):
        raise HTTPException(status_code=409, detail="模型配置名称已存在")
    values = payload.model_dump(mode="json")
    if payload.provider_type == "OLLAMA":
        values["credential_id"] = None
    elif not payload.credential_id or db.get(ApiCredential, payload.credential_id) is None:
        raise HTTPException(status_code=422, detail="云端模型必须选择有效的加密凭据")
    item = ProviderConfig(**values)
    db.add(item)
    db.add(AuditLog(event="PROVIDER_CREATED", detail={"name": item.name, "provider_type": item.provider_type}))
    db.commit()
    db.refresh(item)
    return item


@router.post("/providers/reorder", response_model=list[ProviderOut], tags=["ai"])
def reorder_providers(payload: ProviderReorder, db: Session = Depends(get_db)) -> list[ProviderConfig]:
    items = list(db.scalars(select(ProviderConfig).order_by(ProviderConfig.priority.asc(), ProviderConfig.name.asc())))
    if payload.strategy == "CAPABILITY":
        ordered = capability_order(items)
    else:
        existing_ids = {item.id for item in items}
        if len(payload.ordered_ids) != len(items) or set(payload.ordered_ids) != existing_ids:
            raise HTTPException(status_code=422, detail="手动排序必须包含当前全部模型，且每项只能出现一次")
        by_id = {item.id: item for item in items}
        ordered = [by_id[provider_id] for provider_id in payload.ordered_ids]
    apply_provider_order(ordered)
    db.add(
        AuditLog(
            event="PROVIDERS_REORDERED",
            detail={
                "strategy": payload.strategy,
                "order": [
                    {
                        "provider_id": item.id,
                        "name": item.name,
                        "model": item.model,
                        "priority": item.priority,
                    }
                    for item in ordered
                ],
            },
        )
    )
    db.commit()
    return ordered


@router.put("/providers/{provider_id}", response_model=ProviderOut, tags=["ai"])
def update_provider(provider_id: str, payload: ProviderUpdate, db: Session = Depends(get_db)) -> ProviderConfig:
    item = db.get(ProviderConfig, provider_id)
    if item is None:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    updates = payload.model_dump(exclude_unset=True, mode="json")
    if "name" in updates:
        duplicate = db.scalar(select(ProviderConfig.id).where(ProviderConfig.name == updates["name"], ProviderConfig.id != item.id))
        if duplicate:
            raise HTTPException(status_code=409, detail="模型配置名称已存在")
    next_type = updates.get("provider_type", item.provider_type)
    next_credential = updates.get("credential_id", item.credential_id)
    if next_type == "OLLAMA":
        updates["credential_id"] = None
    elif not next_credential or db.get(ApiCredential, next_credential) is None:
        raise HTTPException(status_code=422, detail="云端模型必须选择有效的加密凭据")
    for key, value in updates.items():
        setattr(item, key, value)
    db.add(
        AuditLog(
            event="PROVIDER_UPDATED",
            detail={"provider_id": item.id, "name": item.name, "changed_fields": sorted(updates)},
        )
    )
    db.commit()
    db.refresh(item)
    return item


@router.post("/providers/{provider_id}/test", tags=["ai"])
async def test_provider(provider_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    item = db.get(ProviderConfig, provider_id)
    if item is None:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    health = await probe_provider(db, item, get_settings())
    db.commit()
    return {
        "ok": health.status == "HEALTHY",
        "detail": "连接成功" if health.status == "HEALTHY" else health.last_error_detail or "连接失败",
        "error_type": health.last_error_code,
        "last_success_at": health.last_success_at,
    }


@router.delete("/providers/{provider_id}", status_code=204, response_class=Response, tags=["ai"])
def delete_provider(provider_id: str, db: Session = Depends(get_db)) -> Response:
    item = db.get(ProviderConfig, provider_id)
    if item is None:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    db.add(AuditLog(event="PROVIDER_DELETED", level="WARNING", detail={"provider_id": item.id, "name": item.name}))
    db.delete(item)
    db.commit()
    return Response(status_code=204)


@router.get("/persona", response_model=PersonaOut, tags=["ai"])
def get_persona(db: Session = Depends(get_db)) -> PersonaProfile:
    item = db.get(PersonaProfile, 1)
    if item is None:
        item = PersonaProfile(id=1)
        db.add(item)
        db.commit()
        db.refresh(item)
    return item


@router.put("/persona", response_model=PersonaOut, tags=["ai"])
def update_persona(payload: PersonaUpdate, db: Session = Depends(get_db)) -> PersonaProfile:
    item = db.get(PersonaProfile, 1) or PersonaProfile(id=1)
    item.global_persona = payload.global_persona
    item.user_style = payload.user_style
    item.safety_policy = payload.safety_policy
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.get("/contacts/{contact_id}/memories", response_model=list[MemoryOut], tags=["memory"])
def list_memories(contact_id: str, db: Session = Depends(get_db)) -> list[Memory]:
    if db.get(Contact, contact_id) is None:
        raise HTTPException(status_code=404, detail="联系人不存在")
    return list(db.scalars(select(Memory).where(Memory.contact_id == contact_id).order_by(desc(Memory.created_at))))


@router.post("/contacts/{contact_id}/memories", response_model=MemoryOut, status_code=201, tags=["memory"])
def create_memory(contact_id: str, payload: MemoryCreate, db: Session = Depends(get_db)) -> Memory:
    contact = db.get(Contact, contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="联系人不存在")
    if not contact.memory_enabled:
        raise HTTPException(status_code=409, detail="该联系人的长期记忆已关闭")
    allowed, result = safe_memory(payload.kind, payload.content)
    if not allowed:
        raise HTTPException(status_code=422, detail=result)
    item = Memory(contact_id=contact_id, kind=payload.kind, content=result, review_status="APPROVED")
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.patch("/memories/{memory_id}/review", response_model=MemoryOut, tags=["memory"])
def review_memory(memory_id: str, payload: MemoryReviewUpdate, db: Session = Depends(get_db)) -> Memory:
    item = db.get(Memory, memory_id)
    if item is None:
        raise HTTPException(status_code=404, detail="记忆不存在")
    item.review_status = payload.status
    if payload.pinned is not None:
        item.pinned = payload.pinned
    item.expires_at = payload.expires_at
    db.commit()
    db.refresh(item)
    return item


@router.patch("/memories/{memory_id}", response_model=MemoryOut, tags=["memory"])
def update_memory(memory_id: str, payload: MemoryUpdate, db: Session = Depends(get_db)) -> Memory:
    item = db.get(Memory, memory_id)
    if item is None:
        raise HTTPException(status_code=404, detail="记忆不存在")
    allowed, result = safe_memory(payload.kind, payload.content)
    if not allowed:
        raise HTTPException(status_code=422, detail=result)
    duplicate = db.scalar(
        select(Memory.id).where(
            Memory.contact_id == item.contact_id,
            Memory.kind == payload.kind,
            Memory.content == result,
            Memory.id != item.id,
        )
    )
    if duplicate:
        raise HTTPException(status_code=409, detail="该联系人已有完全相同的记忆")
    item.kind = payload.kind
    item.content = result
    item.review_status = "APPROVED"
    db.commit()
    db.refresh(item)
    return item


@router.delete("/memories/{memory_id}", status_code=204, response_class=Response, tags=["memory"])
def delete_memory(memory_id: str, db: Session = Depends(get_db)) -> Response:
    item = db.get(Memory, memory_id)
    if item is None:
        raise HTTPException(status_code=404, detail="记忆不存在")
    db.delete(item)
    db.commit()
    return Response(status_code=204)


def _account_payload(item: Account) -> dict[str, Any]:
    config = item.config or {}
    return {
        "id": item.id,
        "platform": item.platform,
        "display_name": item.display_name,
        "status": item.status,
        "enabled": item.enabled,
        "experimental": item.experimental,
        "app_id": config.get("app_id"),
        "api_base": config.get("api_base"),
        "managed_qq_id": config.get("managed_qq_id"),
        "strategy": config.get("strategy"),
        "risk_acknowledged": bool(config.get("risk_acknowledged")),
        "credential_present": bool(item.credential_id),
        "last_error": item.last_error,
    }


@router.get("/napcat/profiles", tags=["accounts"])
def list_napcat_profiles(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return [
        _account_payload(item)
        for item in db.scalars(
            select(Account).where(Account.platform == "QQ_NAPCAT").order_by(Account.created_at.asc())
        )
    ]


@router.post("/napcat/profiles", status_code=201, tags=["accounts"])
def create_napcat_profile(payload: NapCatProfileCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    existing = list(db.scalars(select(Account).where(Account.platform == "QQ_NAPCAT")))
    if any(str((item.config or {}).get("managed_qq_id") or "") == payload.managed_qq_id for item in existing):
        raise HTTPException(status_code=409, detail="该 QQ 账号的 NapCat 配置已存在")
    try:
        api_base = normalize_loopback_base_url(payload.api_base)
    except ExperimentalConnectorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    vault = CredentialVault()
    credential = ApiCredential(
        label=f"NapCat Token · {payload.managed_qq_id}",
        provider="QQ_NAPCAT",
        encrypted_secret=vault.encrypt(payload.access_token),
        masked_hint=mask_secret(payload.access_token),
    )
    db.add(credential)
    db.flush()
    item = Account(
        platform="QQ_NAPCAT",
        display_name=payload.display_name.strip(),
        connector_kind="NAPCAT_ONEBOT11",
        status="DISCONNECTED",
        enabled=False,
        experimental=True,
        credential_id=credential.id,
        config={
            "api_base": api_base,
            "managed_qq_id": payload.managed_qq_id,
            "risk_acknowledged": payload.risk_acknowledged,
            "strategy": "LIMITED_LIVE",
        },
    )
    db.add(item)
    db.add(AuditLog(event="NAPCAT_PROFILE_CREATED", detail={"account_id": item.id, "managed_qq_id": payload.managed_qq_id}))
    db.commit()
    db.refresh(item)
    return _account_payload(item)


@router.put("/napcat/profiles/{account_id}", tags=["accounts"])
async def update_napcat_profile(account_id: str, payload: NapCatProfileUpdate, db: Session = Depends(get_db)) -> dict[str, Any]:
    async with runtime_control.send_lock:
        profiles = lock_platform_accounts(db, "QQ_NAPCAT")
        item = next((profile for profile in profiles if profile.id == account_id), None)
        if item is None or item.platform != "QQ_NAPCAT":
            raise HTTPException(status_code=404, detail="NapCat 账号配置不存在")
        config = dict(item.config or {})
        if payload.managed_qq_id is not None:
            # JSON equality is intentionally checked in Python for SQLite/MySQL portability.
            others = list(db.scalars(select(Account).where(Account.platform == "QQ_NAPCAT", Account.id != item.id)))
            if any(str((other.config or {}).get("managed_qq_id") or "") == payload.managed_qq_id for other in others):
                raise HTTPException(status_code=409, detail="该 QQ 账号的 NapCat 配置已存在")
            config["managed_qq_id"] = payload.managed_qq_id
        if payload.api_base is not None:
            try:
                config["api_base"] = normalize_loopback_base_url(payload.api_base)
            except ExperimentalConnectorError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        if payload.risk_acknowledged is not None:
            config["risk_acknowledged"] = payload.risk_acknowledged
        if payload.display_name is not None:
            item.display_name = payload.display_name.strip()
        if payload.access_token is not None:
            credential = db.get(ApiCredential, item.credential_id) if item.credential_id else None
            vault = CredentialVault()
            if credential is None:
                credential = ApiCredential(
                    label=f"NapCat Token · {config.get('managed_qq_id') or item.display_name}",
                    provider="QQ_NAPCAT",
                    encrypted_secret=vault.encrypt(payload.access_token),
                    masked_hint=mask_secret(payload.access_token),
                )
                db.add(credential)
                db.flush()
                item.credential_id = credential.id
            else:
                credential.encrypted_secret = vault.encrypt(payload.access_token)
                credential.masked_hint = mask_secret(payload.access_token)
        item.config = config
        item.status = "READY" if item.enabled else "DISCONNECTED"
        item.last_error = None
        db.add(AuditLog(event="NAPCAT_PROFILE_UPDATED", detail={"account_id": item.id, "enabled": item.enabled}))
        db.commit()
        db.refresh(item)
    return _account_payload(item)


@router.post("/napcat/profiles/{account_id}/activate", tags=["accounts"])
async def activate_napcat_profile(account_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    async with runtime_control.send_lock:
        profiles = lock_platform_accounts(db, "QQ_NAPCAT")
        item = next((profile for profile in profiles if profile.id == account_id), None)
        if item is None or item.platform != "QQ_NAPCAT":
            raise HTTPException(status_code=404, detail="NapCat 账号配置不存在")
        config = item.config or {}
        if not config.get("managed_qq_id") or not config.get("api_base") or not item.credential_id:
            raise HTTPException(status_code=422, detail="激活前必须填写 QQ 号、本机 API 地址与 Token")
        if not config.get("risk_acknowledged"):
            raise HTTPException(status_code=409, detail="激活前必须确认 NapCat 非官方自动化风险")
        for other in profiles:
            if other.id != item.id and other.enabled:
                other.enabled = False
                other.status = "DISCONNECTED"
        cancelled = db.query(Message).filter(Message.platform == "QQ_NAPCAT", Message.status == MessageStatus.QUEUED).update(
            {Message.status: MessageStatus.CANCELLED}
        )
        item.enabled = True
        item.status = "READY"
        item.last_error = None
        db.add(AuditLog(event="NAPCAT_PROFILE_ACTIVATED", level="WARNING", detail={"account_id": item.id, "queued_cancelled": cancelled}))
        db.commit()
        db.refresh(item)
        health = await inspect_active_napcat(db, get_settings(), update_account=False)
    await event_hub.broadcast("account_state", {"platform": item.platform, "account_id": item.id, "status": item.status, "enabled": True})
    return {**_account_payload(item), "activation_diagnostics": health.check}


@router.post("/napcat/profiles/{account_id}/deactivate", tags=["accounts"])
async def deactivate_napcat_profile(account_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    async with runtime_control.send_lock:
        profiles = lock_platform_accounts(db, "QQ_NAPCAT")
        item = next((profile for profile in profiles if profile.id == account_id), None)
        if item is None or item.platform != "QQ_NAPCAT":
            raise HTTPException(status_code=404, detail="NapCat 账号配置不存在")
        item.enabled = False
        item.status = "DISCONNECTED"
        cancelled = db.query(Message).filter(Message.platform == "QQ_NAPCAT", Message.status == MessageStatus.QUEUED).update(
            {Message.status: MessageStatus.CANCELLED}
        )
        db.add(AuditLog(event="NAPCAT_PROFILE_DEACTIVATED", level="WARNING", detail={"account_id": item.id, "queued_cancelled": cancelled}))
        db.commit()
        db.refresh(item)
    return _account_payload(item)


@router.delete("/napcat/profiles/{account_id}", status_code=204, response_class=Response, tags=["accounts"])
async def delete_napcat_profile(account_id: str, db: Session = Depends(get_db)) -> Response:
    async with runtime_control.send_lock:
        profiles = lock_platform_accounts(db, "QQ_NAPCAT")
        item = next((profile for profile in profiles if profile.id == account_id), None)
        if item is None or item.platform != "QQ_NAPCAT":
            raise HTTPException(status_code=404, detail="NapCat 账号配置不存在")
        if item.enabled:
            raise HTTPException(status_code=409, detail="请先停用该 NapCat 账号，再删除配置")
        bound = db.scalar(select(func.count(Contact.id)).where(Contact.keepalive_account_id == item.id)) or 0
        if bound:
            raise HTTPException(status_code=409, detail=f"仍有 {bound} 位联系人绑定此账号的续火设置")
        credential = db.get(ApiCredential, item.credential_id) if item.credential_id else None
        db.delete(item)
        if credential is not None:
            db.delete(credential)
        db.commit()
    return Response(status_code=204)


@router.get("/accounts", tags=["accounts"])
def list_accounts(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    items = list(db.scalars(select(Account).order_by(Account.platform.asc())))
    return [_account_payload(item) for item in items]


@router.put("/accounts/{platform}", tags=["accounts"])
async def update_account(platform: str, payload: AccountConfigUpdate, db: Session = Depends(get_db)) -> dict[str, Any]:
    normalized = platform.upper()
    async with runtime_control.send_lock:
        locked_accounts: list[Account] = []
        if normalized == "QQ_NAPCAT":
            locked_accounts = lock_platform_accounts(db, normalized)
            item = next((account for account in locked_accounts if account.enabled), None)
            if item is None and locked_accounts:
                item = locked_accounts[0]
        else:
            item = db.scalar(
                select(Account)
                .where(Account.platform == normalized)
                .order_by(Account.enabled.desc(), Account.created_at.asc())
            )
        if item is None:
            raise HTTPException(status_code=404, detail="账号不存在")
        if normalized == "WECHAT" and payload.enabled:
            raise HTTPException(status_code=409, detail="个人微信安全接入门禁未通过，不能启用")
        if normalized == "QQ" and payload.enabled and (not payload.app_id and not (item.config or {}).get("app_id")):
            raise HTTPException(status_code=422, detail="启用 QQ Bot 前必须填写 AppID")
        config = dict(item.config or {})
        if payload.app_id is not None:
            config["app_id"] = payload.app_id.strip()
        if normalized == "QQ_NAPCAT" and payload.api_base is not None:
            try:
                config["api_base"] = normalize_loopback_base_url(payload.api_base)
            except ExperimentalConnectorError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        if normalized in {"QQ_NAPCAT", "WECHAT_AUTOWX"}:
            config["risk_acknowledged"] = payload.risk_acknowledged
            config["strategy"] = "LIMITED_LIVE" if normalized == "QQ_NAPCAT" else "DRAFT_ONLY"
        item.config = config
        submitted_secret = payload.app_secret if normalized == "QQ" else payload.access_token
        if submitted_secret is not None:
            vault = CredentialVault()
            if item.credential_id and (credential := db.get(ApiCredential, item.credential_id)):
                credential.encrypted_secret = vault.encrypt(submitted_secret)
                credential.masked_hint = mask_secret(submitted_secret)
            else:
                labels = {
                    "QQ": ("QQ Official Bot AppSecret", "QQ"),
                    "QQ_NAPCAT": ("NapCat OneBot Token", "QQ_NAPCAT"),
                    "WECHAT_AUTOWX": ("AutoWx Bridge Token", "WECHAT_AUTOWX"),
                }
                label, provider = labels.get(normalized, (f"{normalized} Connector Token", normalized))
                credential = ApiCredential(label=label, provider=provider, encrypted_secret=vault.encrypt(submitted_secret), masked_hint=mask_secret(submitted_secret))
                db.add(credential)
                db.flush()
                item.credential_id = credential.id
        if normalized == "QQ" and payload.enabled and (not config.get("app_id") or not item.credential_id):
            raise HTTPException(status_code=422, detail="启用 QQ Bot 前必须同时填写 AppID 与 AppSecret")
        if normalized == "QQ_NAPCAT" and payload.enabled:
            if not payload.risk_acknowledged:
                raise HTTPException(status_code=409, detail="启用 NapCat 前必须确认它是非官方个人 QQ 自动化并存在账号风险")
            if not config.get("api_base") or not item.credential_id:
                raise HTTPException(status_code=422, detail="启用 NapCat 前必须填写本机 OneBot HTTP 地址与 Token")
        if normalized == "WECHAT_AUTOWX" and payload.enabled:
            if not payload.risk_acknowledged:
                raise HTTPException(status_code=409, detail="启用 AutoWx 前必须确认个人微信 UI 自动化仍存在风控风险")
            if not item.credential_id:
                raise HTTPException(status_code=422, detail="启用 AutoWx 前必须填写本地 Bridge Token")
        item.enabled = payload.enabled
        if normalized == "QQ_NAPCAT" and payload.enabled:
            for other in locked_accounts:
                if other.id == item.id:
                    continue
                other.enabled = False
                other.status = "DISCONNECTED"
        if normalized == "QQ":
            configured = bool(item.credential_id and config.get("app_id"))
        elif normalized == "QQ_NAPCAT":
            configured = bool(item.credential_id and config.get("api_base") and config.get("risk_acknowledged"))
        elif normalized == "WECHAT_AUTOWX":
            configured = bool(item.credential_id and config.get("risk_acknowledged"))
        else:
            configured = False
        item.status = "READY" if payload.enabled and configured else ("BLOCKED" if normalized == "WECHAT" else "DISCONNECTED")
        item.last_error = None
        cancelled = 0
        if not payload.enabled:
            cancelled = db.query(Message).filter(Message.platform == normalized, Message.status == MessageStatus.QUEUED).update({Message.status: MessageStatus.CANCELLED})
            running = db.scalar(select(AcceptanceRun).where(AcceptanceRun.kind == "QQ_200", AcceptanceRun.status == "RUNNING"))
            if normalized == "QQ" and running is not None:
                running.status = "CANCELLED"
                running.completed_at = utc_now()
        db.add(AuditLog(event="ACCOUNT_CONFIG_UPDATED", detail={"platform": normalized, "enabled": payload.enabled, "credential_present": bool(item.credential_id), "queued_cancelled": cancelled}))
        db.commit()
    await event_hub.broadcast(
        "account_state",
        {"platform": item.platform, "status": item.status, "enabled": item.enabled},
    )
    return {
        "platform": item.platform,
        "enabled": item.enabled,
        "status": item.status,
        "credential_present": bool(item.credential_id),
        "risk_acknowledged": bool((item.config or {}).get("risk_acknowledged")),
    }


@router.post("/simulator/events", tags=["simulator"])
async def simulator_event(payload: SimulatorEventRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    contact = db.get(Contact, payload.contact_id)
    if contact is None or contact.platform != "SIMULATOR":
        raise HTTPException(status_code=404, detail="模拟联系人不存在")
    timestamp = as_utc(payload.timestamp) if payload.timestamp else utc_now()
    event = InboundEvent(
        platform="SIMULATOR",
        message_id=payload.message_id or f"sim-in-{uuid4()}",
        conversation_id=payload.conversation_id or f"sim:{contact.platform_user_id}",
        sender_id=contact.platform_user_id,
        sender_name=contact.display_name,
        content=payload.content,
        author=payload.author,
        timestamp=timestamp,
    )
    return asdict(await build_pipeline(db).handle(event, db))


@router.get("/logs", tags=["logs"])
def list_logs(
    event: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    query = select(AuditLog)
    if event:
        query = query.where(AuditLog.event == event)
    items = list(db.scalars(query.order_by(desc(AuditLog.created_at)).limit(limit)))
    return [{"id": item.id, "event": item.event, "level": item.level, "conversation_id": item.conversation_id, "message_id": item.message_id, "detail": item.detail, "created_at": item.created_at} for item in items]


@router.get("/incidents", tags=["logs"])
def list_incidents(resolved: bool | None = None, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    query = select(Incident)
    if resolved is not None:
        query = query.where(Incident.resolved.is_(resolved))
    items = list(db.scalars(query.order_by(desc(Incident.created_at)).limit(500)))
    return [{"id": item.id, "kind": item.kind, "severity": item.severity, "title": item.title, "detail": item.detail, "resolved": item.resolved, "created_at": item.created_at, "resolved_at": item.resolved_at} for item in items]


@router.post("/incidents/{incident_id}/resolve", tags=["logs"])
def resolve_incident(incident_id: str, db: Session = Depends(get_db)) -> dict[str, bool]:
    item = db.get(Incident, incident_id)
    if item is None:
        raise HTTPException(status_code=404, detail="事件不存在")
    item.resolved = True
    item.resolved_at = utc_now()
    db.commit()
    return {"resolved": True}


@router.get("/tasks", response_model=list[TaskOut], tags=["workspace"])
def list_tasks(status_filter: str | None = Query(default=None, alias="status"), limit: int = Query(default=BACKGROUND_TASK_RETENTION, ge=1, le=BACKGROUND_TASK_RETENTION), db: Session = Depends(get_db)) -> list[BackgroundTask]:
    query = select(BackgroundTask)
    if status_filter:
        query = query.where(BackgroundTask.status == status_filter.upper())
    return list(db.scalars(query.order_by(desc(BackgroundTask.created_at)).limit(limit)))


@router.get("/model-routing", tags=["workspace"])
def model_routing_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    providers = list(db.scalars(select(ProviderConfig).where(ProviderConfig.enabled.is_(True)).order_by(ProviderConfig.priority.asc(), ProviderConfig.name.asc())))
    latest = db.scalar(select(AuditLog).where(AuditLog.event == AuditEvent.LLM_REQUEST).order_by(desc(AuditLog.created_at)).limit(1))
    latest_detail = (latest.detail or {}) if latest else {}
    return {
        "mode": "SMART_WITH_CONFIGURED_FALLBACK",
        "providers": [{"name": item.name, "model": item.model, "priority": item.priority} for item in providers],
        "rules": [
            {"task": "MULTIMODAL", "detail": "图片或视频优先原生多模态模型"},
            {"task": "LONG_CONTEXT", "detail": "长文档与总结优先长上下文模型"},
            {"task": "REASONING", "detail": "代码、报错和分析优先 DeepSeek / Qwen / Kimi"},
            {"task": "PRIVATE_LOCAL", "detail": "明确要求本地处理时优先 Ollama"},
            {"task": "GENERAL_CHAT", "detail": "普通聊天保持后台配置的优先顺序"},
        ],
        "latest": ({"task_type": latest_detail.get("route_task_type"), "reason": latest_detail.get("route_reason"), "provider_order": latest_detail.get("provider_order"), "created_at": latest.created_at} if latest else None),
    }


@router.post("/tasks", response_model=TaskOut, status_code=201, tags=["workspace"])
def create_task(payload: TaskCreate, db: Session = Depends(get_db)) -> BackgroundTask:
    item = enqueue_task(db, kind=payload.kind, title=payload.title, payload=payload.payload, max_attempts=payload.max_attempts)
    db.add(AuditLog(event="BACKGROUND_TASK_QUEUED", detail={"task_id": item.id, "kind": item.kind}))
    db.commit()
    db.refresh(item)
    return item


@router.post("/tasks/{task_id}/retry", response_model=TaskOut, tags=["workspace"])
def retry_task(task_id: str, db: Session = Depends(get_db)) -> BackgroundTask:
    item = db.get(BackgroundTask, task_id)
    if item is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if item.status not in {"FAILED", "CANCELLED"}:
        raise HTTPException(status_code=409, detail="只有失败或已取消的任务可以重试")
    item.status = "PENDING"
    item.available_at = utc_now()
    item.completed_at = None
    item.error_code = None
    item.error_detail = None
    db.commit()
    db.refresh(item)
    return item


@router.get("/recovery", response_model=list[RecoveryOut], tags=["workspace"])
def list_recovery(status_filter: str | None = Query(default="OPEN", alias="status"), db: Session = Depends(get_db)) -> list[RecoveryItem]:
    sync_recovery_items(db)
    db.commit()
    query = select(RecoveryItem)
    if status_filter and status_filter.upper() != "ALL":
        query = query.where(RecoveryItem.status == status_filter.upper())
    return list(db.scalars(query.order_by(desc(RecoveryItem.created_at)).limit(500)))


@router.post("/recovery/{item_id}/action", response_model=RecoveryOut, tags=["workspace"])
def recovery_action(item_id: str, payload: RecoveryAction, db: Session = Depends(get_db)) -> RecoveryItem:
    item = db.get(RecoveryItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="补偿项不存在")
    if payload.action == "DISMISS":
        item.status = "DISMISSED"
    else:
        if not payload.confirmed:
            raise HTTPException(status_code=409, detail="重试前必须明确确认")
        try:
            retry_recovery_item(db, item)
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.add(AuditLog(event="RECOVERY_ACTION", detail={"recovery_id": item.id, "action": payload.action, "source_type": item.source_type}))
    db.commit()
    db.refresh(item)
    return item


@router.get("/knowledge", response_model=list[KnowledgeOut], tags=["workspace"])
def list_knowledge(db: Session = Depends(get_db)) -> list[KnowledgeDocument]:
    return list(db.scalars(select(KnowledgeDocument).order_by(desc(KnowledgeDocument.updated_at)).limit(500)))


@router.post("/knowledge", response_model=KnowledgeOut, status_code=201, tags=["workspace"])
def create_knowledge(payload: KnowledgeCreate, db: Session = Depends(get_db)) -> KnowledgeDocument:
    item = KnowledgeDocument(title=payload.title, source_name=payload.source_name, content=payload.content, sha256=content_sha256(payload.content), enabled=payload.enabled)
    db.add(item)
    db.flush()
    enqueue_task(db, kind="KNOWLEDGE_REFRESH", title=f"刷新知识索引：{item.title}", payload={"document_id": item.id})
    db.commit()
    db.refresh(item)
    return item


@router.patch("/knowledge/{document_id}", response_model=KnowledgeOut, tags=["workspace"])
def patch_knowledge(document_id: str, payload: KnowledgePatch, db: Session = Depends(get_db)) -> KnowledgeDocument:
    item = db.get(KnowledgeDocument, document_id)
    if item is None:
        raise HTTPException(status_code=404, detail="知识文档不存在")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, field, value)
    if payload.content is not None:
        item.sha256 = content_sha256(payload.content)
    enqueue_task(db, kind="KNOWLEDGE_REFRESH", title=f"刷新知识索引：{item.title}", payload={"document_id": item.id})
    db.commit()
    db.refresh(item)
    return item


@router.delete("/knowledge/{document_id}", status_code=204, response_class=Response, tags=["workspace"])
def delete_knowledge(document_id: str, db: Session = Depends(get_db)) -> Response:
    item = db.get(KnowledgeDocument, document_id)
    if item is None:
        raise HTTPException(status_code=404, detail="知识文档不存在")
    db.delete(item)
    db.commit()
    return Response(status_code=204)


@router.get("/todos", response_model=list[TodoOut], tags=["workspace"])
def list_todos(status_filter: str | None = Query(default=None, alias="status"), db: Session = Depends(get_db)) -> list[TodoItem]:
    query = select(TodoItem)
    if status_filter:
        query = query.where(TodoItem.status == status_filter.upper())
    return list(db.scalars(query.order_by(TodoItem.status.asc(), TodoItem.due_at.asc(), desc(TodoItem.created_at)).limit(1000)))


@router.get("/tools/calendar", tags=["workspace"])
def workspace_calendar(db: Session = Depends(get_db)) -> dict[str, Any]:
    snapshot = calendar_snapshot()
    now = utc_now()
    upcoming = list(
        db.scalars(
            select(TodoItem)
            .where(TodoItem.status == "OPEN", TodoItem.due_at.is_not(None), TodoItem.due_at >= now)
            .order_by(TodoItem.due_at.asc())
            .limit(30)
        )
    )
    snapshot["upcoming"] = [
        {
            "id": item.id,
            "title": item.title,
            "due_at": item.due_at,
            "priority": item.priority,
            "kind": item.kind,
        }
        for item in upcoming
    ]
    return snapshot


@router.get("/tools/weather", tags=["workspace"])
async def workspace_weather(location: str = Query(min_length=1, max_length=60)) -> dict[str, Any]:
    try:
        return await weather_lookup(location)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"天气服务暂时不可用：{str(exc)[:160]}") from exc


@router.get("/tools/search", tags=["workspace"])
async def workspace_search(q: str = Query(min_length=1, max_length=160), limit: int = Query(default=5, ge=1, le=8)) -> dict[str, Any]:
    try:
        return await web_search(q, limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"联网搜索暂时不可用：{str(exc)[:160]}") from exc


@router.post("/todos", response_model=TodoOut, status_code=201, tags=["workspace"])
def create_todo(payload: TodoCreate, db: Session = Depends(get_db)) -> TodoItem:
    if payload.contact_id and db.get(Contact, payload.contact_id) is None:
        raise HTTPException(status_code=404, detail="联系人不存在")
    item = TodoItem(**payload.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.patch("/todos/{todo_id}", response_model=TodoOut, tags=["workspace"])
def patch_todo(todo_id: str, payload: TodoPatch, db: Session = Depends(get_db)) -> TodoItem:
    item = db.get(TodoItem, todo_id)
    if item is None:
        raise HTTPException(status_code=404, detail="待办不存在")
    values = payload.model_dump(exclude_unset=True)
    for field, value in values.items():
        setattr(item, field, value)
    if payload.status == "DONE" and item.completed_at is None:
        item.completed_at = utc_now()
        if item.kind == "CONTACT_RELAY" and item.delivery_status != "DELIVERED":
            item.delivery_status = "DONE_WITHOUT_REPLY"
            item.last_error = None
    elif payload.status in {"OPEN", "CANCELLED"}:
        item.completed_at = None
        if payload.status == "OPEN" and item.kind == "CONTACT_RELAY" and item.delivery_status == "DONE_WITHOUT_REPLY":
            item.delivery_status = "NOTIFIED" if item.admin_notification_message_id else "PENDING"
    db.commit()
    db.refresh(item)
    return item


@router.delete("/todos/{todo_id}", status_code=204, response_class=Response, tags=["workspace"])
def delete_todo(todo_id: str, db: Session = Depends(get_db)) -> Response:
    item = db.get(TodoItem, todo_id)
    if item is None:
        raise HTTPException(status_code=404, detail="待办不存在")
    db.delete(item)
    db.commit()
    return Response(status_code=204)


@router.get("/digests", response_model=list[DigestOut], tags=["workspace"])
def list_digests(db: Session = Depends(get_db)) -> list[DailyDigest]:
    return list(db.scalars(select(DailyDigest).order_by(desc(DailyDigest.local_date)).limit(365)))


@router.post("/digests/generate", response_model=TaskOut, status_code=202, tags=["workspace"])
def generate_digest(db: Session = Depends(get_db)) -> BackgroundTask:
    item = enqueue_task(db, kind="DAILY_DIGEST", title="生成今日聊天摘要")
    db.commit()
    db.refresh(item)
    return item


@router.get("/stickers", response_model=list[StickerOut], tags=["workspace"])
def list_stickers(db: Session = Depends(get_db)) -> list[StickerAsset]:
    return list(db.scalars(select(StickerAsset).order_by(desc(StickerAsset.created_at)).limit(500)))


@router.post("/stickers", response_model=StickerOut, status_code=201, tags=["workspace"])
def create_sticker(payload: StickerCreate, db: Session = Depends(get_db)) -> StickerAsset:
    try:
        local_path, digest = save_sticker(get_settings(), file_name=payload.file_name, mime_type=payload.mime_type, data_base64=payload.data_base64)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    item = StickerAsset(label=payload.label, tags=payload.tags, local_path=local_path, mime_type=payload.mime_type, sha256=digest, source_kind="MANUAL", enabled=payload.enabled, auto_reply_enabled=payload.auto_reply_enabled)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.post("/stickers/cache/scan", response_model=StickerCacheScanOut, tags=["workspace"])
def scan_sticker_cache() -> dict[str, Any]:
    candidates, roots = scan_qq_sticker_cache(limit=120)
    return {"roots": roots, "candidates": [asdict(item) for item in candidates]}


@router.get("/stickers/cache/{candidate_id}", tags=["workspace"])
def preview_sticker_cache(candidate_id: str) -> FileResponse:
    try:
        path = qq_cache_candidate_path(candidate_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, headers={"Cache-Control": "private, max-age=60"})


@router.post("/stickers/cache/import", response_model=list[StickerOut], tags=["workspace"])
def import_sticker_cache(payload: StickerCacheImportRequest, db: Session = Depends(get_db)) -> list[StickerAsset]:
    output: list[StickerAsset] = []
    settings = get_settings()
    for requested in payload.items:
        try:
            local_path, digest, mime_type, source_ref = import_qq_cache_sticker(settings, requested.candidate_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        existing = db.scalar(select(StickerAsset).where(StickerAsset.sha256 == digest))
        if existing is not None:
            output.append(existing)
            continue
        item = StickerAsset(
            label=requested.label,
            tags=requested.tags,
            local_path=local_path,
            mime_type=mime_type,
            sha256=digest,
            source_kind="QQ_CACHE",
            source_ref=source_ref,
            enabled=True,
            auto_reply_enabled=False,
        )
        db.add(item)
        output.append(item)
    db.commit()
    for item in output:
        db.refresh(item)
    return output


@router.get("/stickers/{sticker_id}/content", tags=["workspace"])
def preview_sticker(sticker_id: str, db: Session = Depends(get_db)) -> FileResponse:
    item = db.get(StickerAsset, sticker_id)
    if item is None:
        raise HTTPException(status_code=404, detail="贴图不存在")
    try:
        path = resolve_sticker_path(get_settings(), item.local_path)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, media_type=item.mime_type, headers={"Cache-Control": "private, max-age=300"})


@router.patch("/stickers/{sticker_id}", response_model=StickerOut, tags=["workspace"])
def patch_sticker(sticker_id: str, payload: StickerPatch, db: Session = Depends(get_db)) -> StickerAsset:
    item = db.get(StickerAsset, sticker_id)
    if item is None:
        raise HTTPException(status_code=404, detail="贴图不存在")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, field, value)
    db.commit()
    db.refresh(item)
    return item


@router.delete("/stickers/{sticker_id}", status_code=204, response_class=Response, tags=["workspace"])
def delete_sticker(sticker_id: str, db: Session = Depends(get_db)) -> Response:
    item = db.get(StickerAsset, sticker_id)
    if item is None:
        raise HTTPException(status_code=404, detail="贴图不存在")
    # Keep the physical file as a recoverable orphan. The database record is
    # removed immediately, so it can no longer be selected or sent.
    db.delete(item)
    db.commit()
    return Response(status_code=204)

