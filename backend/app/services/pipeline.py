from __future__ import annotations

import asyncio
import hashlib
import random
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from uuid import uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.channels.base import ChannelConnector, InboundEvent, OutboundAttachment, OutboundMessage, SendPermit
from app.channels.simulator import SimulatorConnector
from app.core.config import Settings, get_settings
from app.core.clock import as_utc, beijing_start_of_day_utc, utc_now
from app.core.enums import AuditEvent, ConversationMode, GlobalMode, Importance, MessageAuthor, MessageDirection, MessageStatus
from app.core.events import event_hub
from app.llm.base import LLMMessage
from app.llm.gateway import AllProvidersFailed, LLMGateway
from app.models.entities import (
    Account,
    AdminMemoryImportBatch,
    AdminMemoryImportItem,
    AuditLog,
    ChatGroup,
    Contact,
    Conversation,
    ConversationSummary,
    Incident,
    Memory,
    Message,
    MessageAttachment,
    OutboundDeliveryEvent,
    PersonaProfile,
    StickerAsset,
    TodoItem,
)
from app.policy.engine import PolicyEngine, PolicyInput, ResponseGuard, contains_secret, requires_human_for_formal_topic
from app.policy.rate_limit import RateLimiter
from app.services.account_selection import get_active_account
from app.services.admin_commands import ADMIN_RELATIONSHIP, AdminCommand, apply_admin_plan, parse_admin_command, plan_admin_command
from app.services.admin_delivery import (
    AdminDeliveryError,
    cancel_delivery_draft,
    create_delivery_draft,
    find_delivery_draft,
    is_delivery_command,
    outbound_attachments,
    resolve_target,
)
from app.services.admin_memory_import import (
    DIRECT_MEMORY_COMMANDS,
    MAX_IMPORT_SCREENSHOTS,
    MEMORY_IMPORT_CANCEL_COMMANDS,
    MEMORY_IMPORT_END_COMMANDS,
    MEMORY_IMPORT_START_COMMANDS,
    AdminMemoryImportError,
    active_import_batch,
    add_direct_memory,
    build_import_summary_messages,
    cancel_import_batch,
    capture_import_screenshots,
    complete_import_batch,
    import_evidence,
    is_admin_memory_command,
    parse_direct_memory_request,
    parse_import_extraction,
    resolve_memory_contact,
    start_import_batch,
    store_approved_memories,
)
from app.services.cloud_media import plan_kimi_attachments, prepare_kimi_attachments
from app.services.delegated_todo import (
    RELAY_PROVIDER,
    DelegatedTodoIntent,
    build_relay_response_messages,
    create_delegated_todo,
    detect_delegated_todo,
    fallback_relay_response,
    find_account_admin,
    referenced_delegated_todo,
)
from app.services.formal_matter import append_record_notice, record_formal_matter
from app.services.prompt import build_messages
from app.services.provider_style import normalize_provider_reply
from app.services.outbox import next_attempt_no, record_delivery_event, safe_retry_decision
from app.services.proactive_topic import (
    build_topic_messages,
    build_topic_repair_messages,
    is_topic_command,
    parse_topic_request,
    topic_opener_discloses_owner,
)
from app.services.runtime import RuntimeControl, runtime_control
from app.services.summary import refresh_conversation_summary
from app.services.memory import store_extracted_memories
from app.services.knowledge import search_knowledge
from app.services.long_form import (
    LongFormIncompleteError,
    add_long_form_instructions,
    generate_long_form,
    plan_long_form_request,
)
from app.services.online_tools import online_context_for_message
from app.services.local_tts import LocalTTSError, synthesize_local_speech
from app.services.media import MediaStore
from app.services.media_understanding import MediaUnderstandingService, build_media_context
from app.services.reference_context import resolve_reference_context
from app.services.stickers import select_auto_sticker
from app.services.text_overflow import prepare_text_delivery


@dataclass(frozen=True, slots=True)
class PipelineResult:
    accepted: bool
    sent: bool
    shadowed: bool
    duplicate: bool
    code: str
    reason: str
    inbound_message_id: str | None = None
    outbound_message_id: str | None = None
    reply: str | None = None
    provider: str | None = None
    model: str | None = None


DASHBOARD_MANUAL_PROVIDER = "LOCAL_DASHBOARD_MANUAL"


class ManualSendError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class OutboxRetryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _parse_time(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(int(hour), int(minute))


def _utc(value: datetime) -> datetime:
    return as_utc(value)


class MessagePipeline:
    def __init__(
        self,
        *,
        gateway: LLMGateway,
        connectors: dict[str, ChannelConnector],
        settings: Settings | None = None,
        policy: PolicyEngine | None = None,
        guard: ResponseGuard | None = None,
        limiter: RateLimiter | None = None,
        runtime: RuntimeControl | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.gateway = gateway
        self.connectors = connectors
        self.policy = policy or PolicyEngine()
        self.guard = guard or ResponseGuard()
        self.limiter = limiter or RateLimiter(
            self.settings.contact_per_minute,
            self.settings.daily_send_limit,
            self.settings.daily_send_hard_limit,
            self.settings.max_consecutive_sends,
        )
        self.runtime = runtime or runtime_control

    async def send_dashboard_manual(
        self,
        db: Session,
        *,
        conversation_id: str,
        content: str,
    ) -> PipelineResult:
        """Send one explicit dashboard message without starting human takeover.

        The durable row is authored by HUMAN so transcripts and exports remain
        truthful. A dedicated provider marker lets the NapCat self-message echo
        reconcile to this row instead of being mistaken for an independent QQ
        client message, which would otherwise start the takeover timer.
        """
        normalized = content.strip()
        if not normalized:
            raise ManualSendError("CONTENT_REQUIRED", "请输入要发送的内容。")

        async with self.runtime.send_lock:
            state = self.runtime.get(db)
            conversation = db.get(Conversation, conversation_id)
            if conversation is None:
                raise ManualSendError("CONVERSATION_NOT_FOUND", "会话不存在。")
            if conversation.platform != "QQ_NAPCAT" or conversation.group_id is not None:
                raise ManualSendError("CHANNEL_UNSUPPORTED", "人工直发目前只支持 NapCat 私聊会话。")
            contact = db.get(Contact, conversation.contact_id) if conversation.contact_id else None
            if contact is None:
                raise ManualSendError("CONTACT_NOT_FOUND", "会话联系人不存在。")
            if not contact.whitelisted:
                raise ManualSendError("TARGET_NOT_WHITELISTED", "联系人不在白名单，人工直发已阻止。")
            if state.release_gate != "LIVE":
                raise ManualSendError("LIVE_REQUIRED", "人工直发只允许在 LIVE 门禁下使用。")
            if state.kill_switch:
                raise ManualSendError("KILL_SWITCH", "急停已开启，人工直发已阻止。")
            if state.global_mode in {GlobalMode.READ_ONLY, GlobalMode.STOPPED}:
                raise ManualSendError("RUNTIME_BLOCKED", "当前运行模式禁止向外发送消息。")

            active_account = get_active_account(db, "QQ_NAPCAT")
            active_config = (active_account.config or {}) if active_account else {}
            if not (
                active_account
                and active_account.enabled
                and active_account.credential_id
                and active_config.get("api_base")
                and active_config.get("risk_acknowledged")
            ):
                raise ManualSendError("CHANNEL_DISABLED", "当前没有完整且唯一启用的 NapCat 托管账号。")
            if not conversation.account_id or not contact.account_id:
                raise ManualSendError("ACCOUNT_BINDING_REQUIRED", "该会话尚未绑定托管账号，请先接收一条该账号的新消息。")
            if conversation.account_id != contact.account_id or conversation.account_id != active_account.id:
                raise ManualSendError("ACCOUNT_ISOLATION_MISMATCH", "该会话不属于当前托管账号，已阻止跨账号发送。")

            connector = self.connectors.get("QQ_NAPCAT")
            if connector is None:
                raise ManualSendError("CONNECTOR_MISSING", "NapCat 发送通道不可用。")
            guard = self.guard.inspect(normalized, "", strict=contact.importance == Importance.IMPORTANT)
            if not guard.allowed:
                raise ManualSendError(guard.code, guard.reason)
            rate = self._manual_rate_decision(db, contact, exclude_message_id=None)
            if not rate.allowed:
                raise ManualSendError(rate.code, rate.reason)

            now = utc_now()
            previous_mode = conversation.mode
            previous_human_until = conversation.human_until
            outbound = Message(
                platform="QQ_NAPCAT",
                account_id=conversation.account_id,
                external_message_id=f"dashboard-manual:{uuid4()}",
                sender_id="QQ_NAPCAT:USER",
                receiver_id=contact.platform_user_id,
                event_at=now,
                conversation_id=conversation.id,
                contact_id=contact.id,
                direction=MessageDirection.OUTBOUND,
                author=MessageAuthor.HUMAN,
                content=guard.content,
                message_type="TEXT",
                status=MessageStatus.QUEUED,
                provider=DASHBOARD_MANUAL_PROVIDER,
                model="DASHBOARD_DIRECT",
                raw_envelope={"source": "dashboard_manual", "triggers_human_takeover": False},
            )
            db.add(outbound)
            db.flush()
            self._audit(
                db,
                AuditEvent.MESSAGE_QUEUED,
                conversation.id,
                outbound.id,
                {"source": "DASHBOARD_MANUAL", "mode_unchanged": str(previous_mode)},
            )
            db.commit()

            try:
                result = await connector.send(
                    OutboundMessage("QQ_NAPCAT", contact.platform_user_id, guard.content, "", False),
                    SendPermit(contact.platform_user_id, True, True, True, True, True, True),
                )
            except Exception as exc:
                outbound.status = MessageStatus.FAILED
                outbound.policy_reason = type(exc).__name__
                self._audit(
                    db,
                    AuditEvent.ERROR,
                    conversation.id,
                    outbound.id,
                    {"code": outbound.policy_reason, "source": "DASHBOARD_MANUAL"},
                    level="ERROR",
                )
                db.commit()
                return PipelineResult(
                    True,
                    False,
                    False,
                    False,
                    outbound.policy_reason,
                    "NapCat 人工直发失败。",
                    outbound_message_id=outbound.id,
                )

            if not result.success:
                outbound.status = MessageStatus.FAILED
                outbound.policy_reason = result.error_code or "SEND_FAILED"
                self._audit(
                    db,
                    AuditEvent.ERROR,
                    conversation.id,
                    outbound.id,
                    {"code": outbound.policy_reason, "source": "DASHBOARD_MANUAL"},
                    level="ERROR",
                )
                db.commit()
                return PipelineResult(
                    True,
                    False,
                    False,
                    False,
                    outbound.policy_reason,
                    "NapCat 人工直发失败。",
                    outbound_message_id=outbound.id,
                )

            outbound.status = MessageStatus.SENT
            outbound.external_message_id = result.external_message_id or outbound.external_message_id
            conversation.last_active_at = now
            conversation.consecutive_ai_sends = 0
            # The explicit invariant for this path: never create, extend, clear,
            # or otherwise alter a pre-existing human takeover window.
            conversation.mode = previous_mode
            conversation.human_until = previous_human_until
            self.limiter.record(contact.id, now)
            refresh_conversation_summary(db, conversation)
            self._audit(
                db,
                AuditEvent.MESSAGE_SENT,
                conversation.id,
                outbound.id,
                {
                    "source": "DASHBOARD_MANUAL",
                    "target_contact_id": contact.id,
                    "human_takeover_triggered": False,
                    "mode_unchanged": str(previous_mode),
                },
            )
            db.commit()

        await event_hub.broadcast(
            "message_sent",
            {"message_id": outbound.id, "conversation_id": conversation.id, "author": MessageAuthor.HUMAN},
        )
        return PipelineResult(
            True,
            True,
            False,
            False,
            "DASHBOARD_MANUAL_SENT",
            "人工消息已发送，AI 托管状态未改变。",
            outbound_message_id=outbound.id,
            reply=outbound.content,
            provider=DASHBOARD_MANUAL_PROVIDER,
            model="DASHBOARD_DIRECT",
        )

    async def retry_undelivered(self, db: Session, *, message_id: str) -> PipelineResult:
        """Retry only a provably pre-send NapCat text failure.

        A persisted SEND_STARTED event permanently makes automatic retry
        ineligible because the platform may have accepted the first request.
        """

        outbound = db.get(Message, message_id)
        if outbound is None or outbound.direction != MessageDirection.OUTBOUND:
            raise OutboxRetryError("MESSAGE_NOT_FOUND", "待发消息不存在。")
        events = list(
            db.scalars(
                select(OutboundDeliveryEvent)
                .where(OutboundDeliveryEvent.message_id == outbound.id)
                .order_by(OutboundDeliveryEvent.created_at.asc())
            )
        )
        allowed, reason = safe_retry_decision(outbound, events)
        if not allowed:
            raise OutboxRetryError("RETRY_NOT_SAFE", reason)
        has_attachments = db.scalar(select(func.count(MessageAttachment.id)).where(MessageAttachment.message_id == outbound.id)) or 0
        if has_attachments:
            raise OutboxRetryError("ATTACHMENT_RETRY_BLOCKED", "带附件消息不自动重试，避免重复上传。")
        if outbound.platform != "QQ_NAPCAT":
            raise OutboxRetryError("PLATFORM_UNSUPPORTED", "安全重试目前只支持 NapCat 私聊文本。")
        contact = db.get(Contact, outbound.contact_id) if outbound.contact_id else None
        conversation = db.get(Conversation, outbound.conversation_id)
        if contact is None or conversation is None or conversation.group_id is not None:
            raise OutboxRetryError("TARGET_UNAVAILABLE", "联系人或私聊会话已不可用。")
        state = self.runtime.get(db)
        active_account = get_active_account(db, "QQ_NAPCAT")
        if active_account is None or outbound.account_id != active_account.id or contact.account_id != active_account.id:
            raise OutboxRetryError("ACCOUNT_ISOLATION_MISMATCH", "消息不属于当前唯一活动账号，禁止跨账号重试。")
        connector = self.connectors.get("QQ_NAPCAT")
        if connector is None:
            raise OutboxRetryError("CONNECTOR_MISSING", "NapCat 通道仍不可用。")
        attempt_no = next_attempt_no(db, outbound.id)
        retry_event = InboundEvent(
            platform="QQ_NAPCAT",
            account_id=active_account.id,
            message_id=outbound.reply_to_external_id or f"outbox-retry:{outbound.id}",
            conversation_id=conversation.external_id,
            sender_id=contact.platform_user_id,
            sender_name=contact.display_name,
            content="待发箱安全重试",
            timestamp=utc_now(),
            raw={"event_type": "OUTBOX_SAFE_RETRY"},
        )
        async with self.runtime.send_lock:
            db.refresh(state)
            db.refresh(contact)
            db.refresh(conversation)
            decision = self.policy.evaluate(
                self._policy_input(db, state, contact, None, conversation, retry_event, now=utc_now())
            )
            if not decision.allowed:
                raise OutboxRetryError(decision.code, f"当前策略不允许重试：{decision.reason}")
            guard = self.guard.inspect(outbound.content, "", strict=contact.importance == Importance.IMPORTANT)
            if not guard.allowed:
                raise OutboxRetryError(guard.code, guard.reason)
            rate = self._rate_decision(db, contact, conversation, exclude_message_id=outbound.id)
            if not rate.allowed:
                raise OutboxRetryError(rate.code, rate.reason)
            outbound.status = MessageStatus.QUEUED
            outbound.policy_reason = None
            record_delivery_event(
                db,
                outbound,
                stage="RETRY_QUEUED",
                status="QUEUED",
                attempt_no=attempt_no,
                detail={"operator": "LOCAL_ADMIN", "previous_failure": events[-1].error_code if events else None},
            )
            db.commit()

            target_id = contact.platform_user_id
            permit = SendPermit(target_id, contact.whitelisted, True, True, True, state.global_mode == GlobalMode.AUTO, not state.kill_switch)
            record_delivery_event(
                db,
                outbound,
                stage="SEND_STARTED",
                status="IN_FLIGHT",
                attempt_no=attempt_no,
                delivery_started=True,
                retry_reason="重试发送已经开始，后续任何失败均不得再次自动重试。",
            )
            db.commit()
            try:
                result = await connector.send(
                    OutboundMessage("QQ_NAPCAT", target_id, guard.content, "", False),
                    permit,
                )
            except Exception as exc:
                outbound.status = MessageStatus.FAILED
                outbound.policy_reason = type(exc).__name__
                record_delivery_event(
                    db,
                    outbound,
                    stage="SEND_RESULT",
                    status="FAILED_AMBIGUOUS",
                    attempt_no=attempt_no,
                    delivery_started=True,
                    error_code=type(exc).__name__,
                    retry_reason="平台接收情况不明确，禁止再次自动重试。",
                )
                db.commit()
                return PipelineResult(True, False, False, False, type(exc).__name__, "重试发送异常；为避免重复，不会再次自动重试。", outbound_message_id=outbound.id)
            if not result.success:
                outbound.status = MessageStatus.FAILED
                outbound.policy_reason = result.error_code or "SEND_FAILED"
                record_delivery_event(
                    db,
                    outbound,
                    stage="SEND_RESULT",
                    status="FAILED",
                    attempt_no=attempt_no,
                    delivery_started=True,
                    error_code=outbound.policy_reason,
                    retry_reason="通道已经处理重试请求，禁止再次自动重试。",
                )
                db.commit()
                return PipelineResult(True, False, False, False, outbound.policy_reason, "重试失败；不会再次自动重试。", outbound_message_id=outbound.id)
            outbound.status = MessageStatus.SENT
            outbound.external_message_id = result.external_message_id or outbound.external_message_id
            record_delivery_event(
                db,
                outbound,
                stage="PLATFORM_CONFIRMED",
                status="SENT",
                attempt_no=attempt_no,
                delivery_started=True,
                acknowledged=True,
                external_message_id=result.external_message_id,
            )
            self._audit(db, AuditEvent.MESSAGE_SENT, conversation.id, outbound.id, {"source": "OUTBOX_SAFE_RETRY", "attempt_no": attempt_no})
            self.limiter.record(contact.id, utc_now())
            db.commit()
        await event_hub.broadcast("message_sent", {"message_id": outbound.id, "conversation_id": conversation.id})
        return PipelineResult(True, True, False, False, "OUTBOX_RETRY_SENT", "待发消息已安全重试并收到平台确认。", outbound_message_id=outbound.id)

    async def handle(self, event: InboundEvent, db: Session) -> PipelineResult:
        existing = db.scalar(
            select(Message).where(
                Message.platform == event.platform,
                Message.account_id == event.account_id,
                Message.external_message_id == event.message_id,
            )
        )
        if existing:
            self._audit(db, AuditEvent.DUPLICATE_IGNORED, message_id=existing.id, detail={"external_message_id": event.message_id})
            db.commit()
            return PipelineResult(True, False, False, True, "DUPLICATE", "重复 webhook 已忽略", existing.id)

        contact = self._contact(db, event)
        reconciled_echo = self._reconcile_napcat_ai_echo(db, event, contact)
        if reconciled_echo is not None:
            return reconciled_echo
        group = self._group(db, event)
        conversation = self._conversation(db, event, contact, group)
        previous_last_active_at = conversation.last_active_at
        is_human_message = event.author == MessageAuthor.HUMAN
        inbound = Message(
            platform=event.platform,
            account_id=event.account_id,
            external_message_id=event.message_id,
            sender_id=f"{event.platform}:USER" if is_human_message else event.sender_id,
            receiver_id=(event.group_id or contact.platform_user_id) if is_human_message else (event.group_id or f"{event.platform}:SELF"),
            event_at=event.timestamp,
            conversation_id=conversation.id,
            contact_id=contact.id,
            direction=MessageDirection.OUTBOUND if is_human_message else MessageDirection.INBOUND,
            author=event.author,
            content=event.content,
            message_type=event.message_type,
            status=MessageStatus.SENT if is_human_message else MessageStatus.RECEIVED,
            reply_to_external_id=event.reply_to_message_id,
            raw_envelope=event.raw,
        )
        db.add(inbound)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            existing = db.scalar(
                select(Message).where(
                    Message.platform == event.platform,
                    Message.account_id == event.account_id,
                    Message.external_message_id == event.message_id,
                )
            )
            return PipelineResult(True, False, False, True, "DUPLICATE", "并发重复消息已忽略", existing.id if existing else None)
        # managed_rounds is a guard against rapid bot-to-bot loops, not a lifetime
        # quota for a contact.  A sufficiently long quiet period starts a fresh
        # managed session, even when the old session happened to stop at the cap.
        idle_reset_minutes = max(1, self.settings.contact_cooldown_minutes)
        idle_seconds = (
            (_utc(event.timestamp) - _utc(previous_last_active_at)).total_seconds()
            if previous_last_active_at
            else 0
        )
        if (
            event.author == MessageAuthor.CONTACT
            and idle_seconds >= idle_reset_minutes * 60
            and (conversation.managed_rounds > 0 or conversation.mode == ConversationMode.CONTACT_COOLDOWN)
        ):
            previous_mode = conversation.mode
            previous_managed_rounds = conversation.managed_rounds
            if conversation.mode == ConversationMode.CONTACT_COOLDOWN:
                conversation.mode = ConversationMode.AUTO_READY
            conversation.cooldown_until = None
            conversation.managed_rounds = 0
            self._audit(
                db,
                "MANAGED_SESSION_IDLE_RESET",
                conversation.id,
                inbound.id,
                {
                    "idle_minutes": round(idle_seconds / 60, 2),
                    "idle_reset_minutes": idle_reset_minutes,
                    "previous_mode": str(previous_mode),
                    "previous_managed_rounds": previous_managed_rounds,
                },
            )
        conversation.last_active_at = event.timestamp
        conversation.consecutive_ai_sends = 0
        state = self.runtime.get(db)
        admin_import_batch = (
            active_import_batch(
                db,
                admin_contact_id=contact.id,
                account_id=contact.account_id,
            )
            if event.attachments
            and contact.relationship_label.strip() == ADMIN_RELATIONSHIP
            and contact.whitelisted
            else None
        )
        media_rows = (
            await MediaStore(self.settings).persist(
                db,
                inbound,
                event,
                state,
                storage_enabled=True if admin_import_batch is not None else contact.media_storage_enabled,
            )
            if event.attachments and contact.whitelisted
            else []
        )
        self._audit(
            db,
            AuditEvent.MESSAGE_SENT if is_human_message else AuditEvent.MESSAGE_RECEIVED,
            conversation.id,
            inbound.id,
            {
                "platform": event.platform,
                "message_type": event.message_type,
                "author": str(event.author),
                "source_event_type": str(event.raw.get("event_type") or ""),
                "attachment_count": len(media_rows),
                "attachments_saved": sum(1 for item in media_rows if item.status == "SAVED"),
            },
        )
        if event.author == MessageAuthor.CONTACT and state.global_mode != GlobalMode.READ_ONLY:
            extracted = store_extracted_memories(db, contact, inbound)
            if extracted:
                self._audit(db, "MEMORY_EXTRACTED", conversation.id, inbound.id, {"count": extracted})
        if state.global_mode != GlobalMode.READ_ONLY:
            refresh_conversation_summary(db, conversation)
        db.commit()

        if event.author == MessageAuthor.HUMAN:
            conversation.mode = ConversationMode.HUMAN
            conversation.human_until = event.timestamp + timedelta(minutes=self.settings.human_takeover_minutes)
            conversation.managed_rounds = 0
            self._audit(db, AuditEvent.HUMAN_TAKEOVER, conversation.id, inbound.id, {"minutes": self.settings.human_takeover_minutes})
            db.commit()
            await event_hub.broadcast("mode_changed", {"conversation_id": conversation.id, "mode": ConversationMode.HUMAN})
            return PipelineResult(True, False, False, False, "HUMAN_TAKEOVER", "检测到本人发送，AI 已暂停 10 分钟", inbound.id)

        admin_command = parse_admin_command(
            contact,
            event.content,
            is_group=event.is_group,
            message_type=event.message_type,
        )
        if admin_command is not None:
            return await self._handle_admin_command(db, state, contact, conversation, event, inbound, admin_command, media_rows)

        delegated_reply = referenced_delegated_todo(
            db,
            admin=contact,
            reply_to_external_id=event.reply_to_message_id,
        )
        if delegated_reply is not None and state.global_mode != GlobalMode.READ_ONLY:
            return await self._handle_delegated_todo_reply(
                db, state, contact, conversation, event, inbound, delegated_reply[0]
            )

        delegated_intent = None
        if not contains_secret(event.content):
            delegated_intent = detect_delegated_todo(
                contact,
                event.content,
                is_group=event.is_group,
                message_type=event.message_type,
            )
        if delegated_intent is not None and state.global_mode != GlobalMode.READ_ONLY:
            return await self._handle_delegated_todo_create(
                db, state, contact, conversation, event, inbound, delegated_intent
            )

        media_context = ""
        effective_current_message = event.content
        transcript_promoted = False
        if event.attachments and contact.whitelisted and state.global_mode != GlobalMode.READ_ONLY:
            insights = await MediaUnderstandingService(self.settings).analyze(
                db,
                media_rows,
                state,
                force_images=admin_import_batch is not None,
            )
            text_content = str(event.raw.get("text_content") or "").strip()
            audio_transcripts = [item.text.strip() for item in insights if item.kind == "AUDIO" and item.text.strip()]
            if not text_content and audio_transcripts:
                # A pure voice event uses a transport placeholder such as
                # "[语音]". Promote the local transcript to the actual user
                # turn so the reply model answers what was said instead of
                # treating it as background attachment metadata.
                effective_current_message = "\n".join(audio_transcripts)
                transcript_promoted = True
                remaining_insights = [item for item in insights if item.kind != "AUDIO"]
                media_context = build_media_context(remaining_insights)
                self._audit(
                    db,
                    "AUDIO_TRANSCRIPT_PROMOTED",
                    conversation.id,
                    inbound.id,
                    {"transcript_count": len(audio_transcripts), "characters": len(effective_current_message)},
                )
            else:
                effective_current_message = text_content or event.content
                media_context = build_media_context(insights)
            self._audit(
                db,
                "MEDIA_UNDERSTANDING_COMPLETED",
                conversation.id,
                inbound.id,
                {
                    "attachment_count": len(media_rows),
                    "completed": len(insights),
                    "failed": sum(1 for item in media_rows if item.analysis_status == "FAILED"),
                    "unavailable": sum(1 for item in media_rows if item.analysis_status == "UNAVAILABLE"),
                    "skipped": sum(1 for item in media_rows if item.analysis_status == "SKIPPED_TYPE"),
                },
            )
            db.commit()

        if (
            event.attachments
            and contact.relationship_label.strip() == ADMIN_RELATIONSHIP
            and contact.whitelisted
        ):
            async with self.runtime.send_lock:
                batch = active_import_batch(
                    db,
                    admin_contact_id=contact.id,
                    account_id=contact.account_id,
                )
                if batch is not None:
                    has_image = any(
                        item.kind.upper() == "IMAGE"
                        or str(item.mime_type or "").casefold().startswith("image/")
                        for item in event.attachments
                    )
                    captured = capture_import_screenshots(
                        db,
                        batch=batch,
                        source_message=inbound,
                        attachments=media_rows,
                    )
                    if captured:
                        self._audit(
                            db,
                            "ADMIN_MEMORY_IMPORT_SCREENSHOT_CAPTURED",
                            conversation.id,
                            inbound.id,
                            {
                                "batch_id": batch.id,
                                "target_contact_id": batch.target_contact_id,
                                "captured": captured,
                                "batch_screenshot_count": batch.screenshot_count,
                            },
                        )
                        db.commit()
                        await event_hub.broadcast(
                            "admin_memory_import_progress",
                            {
                                "batch_id": batch.id,
                                "screenshot_count": batch.screenshot_count,
                            },
                        )
                        return PipelineResult(
                            True,
                            False,
                            False,
                            False,
                            "ADMIN_MEMORY_IMPORT_SCREENSHOT_CAPTURED",
                            f"已收集 {batch.screenshot_count} 张截图；继续发送或使用 /neko 截图结束。",
                            inbound.id,
                        )
                    if has_image:
                        limit_reached = batch.screenshot_count >= MAX_IMPORT_SCREENSHOTS
                        code = (
                            "ADMIN_MEMORY_IMPORT_SCREENSHOT_LIMIT"
                            if limit_reached
                            else "ADMIN_MEMORY_IMPORT_SCREENSHOT_REJECTED"
                        )
                        reason = (
                            f"本批次已达到 {MAX_IMPORT_SCREENSHOTS} 张上限；请使用 /neko 截图结束，"
                            "或取消后重新开始。"
                            if limit_reached
                            else "这张图片未能保存为截图记忆素材；批次仍保持开启，请检查媒体保存状态后重试。"
                        )
                        self._audit(
                            db,
                            code,
                            conversation.id,
                            inbound.id,
                            {
                                "batch_id": batch.id,
                                "target_contact_id": batch.target_contact_id,
                                "batch_screenshot_count": batch.screenshot_count,
                            },
                            level="WARNING",
                        )
                        db.commit()
                        return PipelineResult(
                            True,
                            False,
                            False,
                            False,
                            code,
                            reason,
                            inbound.id,
                        )

        reference = await resolve_reference_context(
            db,
            event=event,
            inbound=inbound,
            conversation=conversation,
            state=state,
            settings=self.settings,
        )
        if reference.text:
            self._audit(
                db,
                "REFERENCE_CONTEXT_RESOLVED",
                conversation.id,
                inbound.id,
                {
                    "source": reference.source,
                    "referenced_message_id": reference.referenced_message_id,
                    "referenced_external_message_id": reference.referenced_external_message_id,
                    "attachment_count": reference.attachment_count,
                },
            )
            db.commit()

        kimi_media_rows = list(media_rows)
        if reference.referenced_message_id:
            kimi_media_rows.extend(
                db.scalars(
                    select(MessageAttachment)
                    .where(MessageAttachment.message_id == reference.referenced_message_id)
                    .order_by(MessageAttachment.segment_index.asc())
                )
            )
        kimi_plan = plan_kimi_attachments(kimi_media_rows, state, self.settings)
        kimi_attachments = ()

        # Expired modes transition only when a fresh inbound message arrives. Old drafts are never replayed.
        if conversation.mode == ConversationMode.HUMAN and conversation.human_until and _utc(conversation.human_until) <= _utc(event.timestamp):
            conversation.mode = ConversationMode.AUTO_READY
            conversation.human_until = None
            self._audit(db, "HUMAN_TAKEOVER_EXPIRED", conversation.id, inbound.id, {"resume_on_fresh_message": True})
            db.commit()
        if conversation.mode == ConversationMode.CONTACT_COOLDOWN and conversation.cooldown_until and _utc(conversation.cooldown_until) <= _utc(event.timestamp):
            conversation.mode = ConversationMode.AUTO_READY
            conversation.cooldown_until = None
            conversation.managed_rounds = 0
            self._audit(db, "CONTACT_COOLDOWN_EXPIRED", conversation.id, inbound.id, {"resume_on_fresh_message": True})
            db.commit()
        if conversation.mode == ConversationMode.CONTACT_COOLDOWN and contact.relationship_label == ADMIN_RELATIONSHIP:
            conversation.mode = ConversationMode.AUTO_READY
            conversation.cooldown_until = None
            conversation.managed_rounds = 0
            self._audit(db, "ADMIN_COOLDOWN_BYPASSED", conversation.id, inbound.id, {"reason": "ADMIN_OPERATOR_CHAT"})
            db.commit()

        media_ai_reply_enabled = (
            state.media_ai_reply_enabled
            if contact.media_ai_reply_enabled is None
            else contact.media_ai_reply_enabled
        )
        if event.attachments and contact.whitelisted and not str(event.raw.get("text_content") or "").strip() and not media_ai_reply_enabled:
            self._audit(
                db,
                "MEDIA_STORED_NO_AI",
                conversation.id,
                inbound.id,
                {"attachment_count": len(event.attachments), "reason": "MEDIA_AI_REPLY_DISABLED"},
            )
            db.commit()
            await event_hub.broadcast("new_message", {"message_id": inbound.id, "policy": "MEDIA_STORED_ONLY"})
            return PipelineResult(
                True,
                False,
                False,
                False,
                "MEDIA_STORED_ONLY",
                "媒体消息已保存；媒体 AI 回复开关处于关闭状态",
                inbound.id,
            )

        if (
            event.attachments
            and contact.whitelisted
            and not str(event.raw.get("text_content") or "").strip()
            and media_ai_reply_enabled
            and not media_context
            and not transcript_promoted
            and not kimi_plan.candidates
        ):
            self._audit(
                db,
                "MEDIA_UNDERSTANDING_UNAVAILABLE",
                conversation.id,
                inbound.id,
                {"attachment_count": len(event.attachments)},
                level="WARNING",
            )
            db.commit()
            await event_hub.broadcast("new_message", {"message_id": inbound.id, "policy": "MEDIA_UNDERSTANDING_UNAVAILABLE"})
            return PipelineResult(
                True,
                False,
                False,
                False,
                "MEDIA_UNDERSTANDING_UNAVAILABLE",
                "媒体未能形成可用的本机识别结果，因此没有生成猜测性回复",
                inbound.id,
            )

        context_parts = [item for item in (media_context, reference.text) if item]
        evidence_context = "\n\n".join(context_parts)
        if (context_parts and contains_secret("\n".join(context_parts))) or (
            transcript_promoted and contains_secret(effective_current_message)
        ):
            self._audit(db, AuditEvent.POLICY_DENY, conversation.id, inbound.id, {"code": "INBOUND_SECRET", "reason": "附件或引用内容疑似包含密钥"})
            db.commit()
            return PipelineResult(True, False, False, False, "INBOUND_SECRET", "附件或引用内容疑似包含密钥，未发送到模型", inbound.id)
        # Formal-topic decisions apply to what the contact actually asks, not
        # to words found inside a quoted paper or attachment extraction.
        policy_input = self._policy_input(
            db,
            state,
            contact,
            group,
            conversation,
            event,
            inbound_content=effective_current_message,
        )
        decision = self.policy.evaluate(policy_input)
        if not decision.allowed:
            self._audit(db, AuditEvent.POLICY_DENY, conversation.id, inbound.id, {"code": decision.code, "reason": decision.reason})
            if decision.needs_attention:
                self._incident(db, "POLICY_ATTENTION", "需要人工处理", decision.reason)
            db.commit()
            await event_hub.broadcast("new_message", {"message_id": inbound.id, "policy": decision.code})
            return PipelineResult(True, False, False, False, decision.code, decision.reason, inbound.id)

        self._audit(
            db,
            AuditEvent.POLICY_PASS,
            conversation.id,
            inbound.id,
            {
                "code": "PASS",
                "release_gate": state.release_gate,
                "time_window_enforced": policy_input.enforce_time_window,
            },
        )
        formal_todo: TodoItem | None = None
        if requires_human_for_formal_topic(effective_current_message):
            formal_todo, created = record_formal_matter(
                db,
                contact=contact,
                source_message=inbound,
                account_id=event.account_id,
                detail=effective_current_message,
            )
            self._audit(
                db,
                "FORMAL_MATTER_RECORDED",
                conversation.id,
                inbound.id,
                {"todo_id": formal_todo.id, "created": created, "kind": formal_todo.kind},
            )
            db.commit()
            await event_hub.broadcast(
                "todo_created" if created else "todo_updated",
                {"todo_id": formal_todo.id, "status": formal_todo.status, "kind": formal_todo.kind},
            )
        if kimi_plan.compression_required:
            notice_sent = await self._send_media_compression_notice(
                db,
                state,
                contact,
                group,
                conversation,
                event,
                inbound,
            )
            self._audit(
                db,
                "MEDIA_COMPRESSION_STARTED",
                conversation.id,
                inbound.id,
                {
                    "candidate_count": len(kimi_plan.candidates),
                    "oversized_count": sum(1 for item in kimi_plan.candidates if item.needs_compression),
                    "progress_notice_sent": notice_sent,
                },
            )
            db.commit()

        kimi_preparation = await prepare_kimi_attachments(kimi_plan, self.settings)
        kimi_attachments = kimi_preparation.attachments
        if kimi_preparation.compression_required:
            self._audit(
                db,
                "MEDIA_COMPRESSION_COMPLETED",
                conversation.id,
                inbound.id,
                {
                    "compressed_count": kimi_preparation.compressed_count,
                    "failed_count": kimi_preparation.failed_count,
                    "failure_codes": list(kimi_preparation.failure_codes),
                    "attachment_count": len(kimi_attachments),
                },
                level="WARNING" if kimi_preparation.failed_count else "INFO",
            )
            db.commit()

        if (
            event.attachments
            and contact.whitelisted
            and not str(event.raw.get("text_content") or "").strip()
            and media_ai_reply_enabled
            and not media_context
            and not transcript_promoted
            and not kimi_attachments
        ):
            self._audit(
                db,
                "MEDIA_UNDERSTANDING_UNAVAILABLE",
                conversation.id,
                inbound.id,
                {
                    "attachment_count": len(event.attachments),
                    "compression_attempted": kimi_preparation.compression_required,
                    "compression_failure_codes": list(kimi_preparation.failure_codes),
                },
                level="WARNING",
            )
            db.commit()
            await event_hub.broadcast("new_message", {"message_id": inbound.id, "policy": "MEDIA_UNDERSTANDING_UNAVAILABLE"})
            return PipelineResult(
                True,
                False,
                False,
                False,
                "MEDIA_UNDERSTANDING_UNAVAILABLE",
                "媒体压缩或识别后仍未形成可用内容，因此没有生成猜测性回复",
                inbound.id,
            )

        persona = db.get(PersonaProfile, 1) or PersonaProfile(id=1)
        if persona.id and db.get(PersonaProfile, persona.id) is None:
            db.add(persona)
            db.flush()
        memories = list(
            db.scalars(
                select(Memory)
                .where(
                    Memory.contact_id == contact.id,
                    Memory.review_status == "APPROVED",
                    or_(Memory.expires_at.is_(None), Memory.expires_at > utc_now()),
                )
                .order_by(Memory.pinned.desc(), Memory.created_at.desc())
                .limit(20)
            )
        ) if contact.memory_enabled else []
        recent = list(db.scalars(select(Message).where(Message.conversation_id == conversation.id).order_by(Message.created_at.desc()).limit(12)))
        summary = db.scalar(select(ConversationSummary).where(ConversationSummary.conversation_id == conversation.id).order_by(ConversationSummary.created_at.desc()).limit(1))
        knowledge_context = search_knowledge(db, effective_current_message)
        online_context = await online_context_for_message(
            effective_current_message,
            recent_messages=tuple(item.content for item in recent if item.id != inbound.id),
        )
        if online_context.text:
            self._audit(
                db,
                "ONLINE_TOOLS_USED",
                conversation.id,
                inbound.id,
                {"tools": list(online_context.tools), "errors": list(online_context.errors)},
            )
            db.commit()
        combined_evidence = "\n\n".join(
            item for item in (evidence_context, knowledge_context, online_context.text) if item
        )
        long_form_plan = plan_long_form_request(effective_current_message)
        prompt = build_messages(
            contact=contact,
            persona=persona,
            memories=memories,
            recent=[] if transcript_promoted else list(reversed(recent)),
            current_message=effective_current_message,
            evidence_context=combined_evidence,
            summary=None if transcript_promoted else summary,
            standalone_current_message=transcript_promoted,
        )
        llm_messages = [LLMMessage(**item) for item in prompt]
        llm_messages = add_long_form_instructions(llm_messages, long_form_plan)
        if kimi_attachments:
            for index in range(len(llm_messages) - 1, -1, -1):
                current = llm_messages[index]
                if current.role == "user":
                    llm_messages[index] = LLMMessage(
                        role=current.role,
                        content=current.content,
                        attachments=kimi_attachments,
                    )
                    break
        route_decision = self.gateway.route_decision(llm_messages)
        self._audit(
            db,
            AuditEvent.LLM_REQUEST,
            conversation.id,
            inbound.id,
            {
                "memory_count": len(memories),
                "recent_count": 0 if transcript_promoted else len(recent),
                "current_input_source": "AUDIO_TRANSCRIPT" if transcript_promoted else "MESSAGE_TEXT",
                "current_input_chars": len(effective_current_message),
                "current_input_sha256": hashlib.sha256(effective_current_message.encode("utf-8")).hexdigest(),
                "prompt_message_count": len(prompt),
                "provider_order": [provider.name for provider in route_decision.providers],
                "route_task_type": route_decision.task_type,
                "route_reason": route_decision.reason,
                "knowledge_context_chars": len(knowledge_context),
                "kimi_media_attachment_count": len(kimi_attachments),
                "kimi_media_sha256": [item.sha256 for item in kimi_attachments if item.sha256],
                "document_mode": long_form_plan.requested,
                "document_format": long_form_plan.format if long_form_plan.requested else None,
            },
        )
        db.commit()
        await event_hub.broadcast("ai_started", {"conversation_id": conversation.id})

        try:
            if long_form_plan.requested:
                long_form_generation = await generate_long_form(self.gateway, llm_messages)
                llm = long_form_generation.response
            else:
                long_form_generation = None
                llm = await self.gateway.chat(llm_messages)
        except LongFormIncompleteError as exc:
            self._audit(
                db,
                AuditEvent.ERROR,
                conversation.id,
                inbound.id,
                {"code": "LONG_FORM_INCOMPLETE", "document_mode": True},
                level="ERROR",
            )
            self._incident(db, "LONG_FORM_INCOMPLETE", "长文生成未完整结束", str(exc))
            db.commit()
            await event_hub.broadcast("ai_finished", {"conversation_id": conversation.id, "failed": True})
            return PipelineResult(
                True,
                False,
                False,
                False,
                "LONG_FORM_INCOMPLETE",
                "模型多次续写后仍报告长度截断，为避免发送残缺文档，本次没有发送。",
                inbound.id,
            )
        except AllProvidersFailed as exc:
            self._audit(db, AuditEvent.ERROR, conversation.id, inbound.id, {"code": "ALL_PROVIDERS_FAILED"}, level="ERROR")
            self._incident(db, "LLM_PROVIDER_FAILURE", "所有模型均不可用", str(exc))
            await self._maybe_trip_failure_circuit(db, "LLM_PROVIDER_FAILURE", conversation.id, inbound.id)
            db.commit()
            await event_hub.broadcast("incident", {"kind": "LLM_PROVIDER_FAILURE"})
            await event_hub.broadcast("ai_finished", {"conversation_id": conversation.id, "failed": True})
            return PipelineResult(True, False, False, False, "ALL_PROVIDERS_FAILED", "模型不可用，未发送任何消息", inbound.id)

        self._audit(
            db,
            AuditEvent.LLM_RESPONSE,
            conversation.id,
            inbound.id,
            {
                "provider": llm.provider,
                "model": llm.model,
                "latency_ms": llm.latency_ms,
                "fallback_used": bool(llm.provider_errors),
                "provider_errors": list(llm.provider_errors),
                "finish_reason": llm.finish_reason,
                "document_mode": long_form_plan.requested,
                "continuation_chunks": long_form_generation.chunks if long_form_generation else 1,
                "continued": long_form_generation.continued if long_form_generation else False,
            },
        )
        provider_style = normalize_provider_reply(
            content=llm.content,
            provider=llm.provider,
            inbound_content=effective_current_message,
            recent=recent,
        )
        if provider_style.changed:
            self._audit(
                db,
                "PROVIDER_STYLE_NORMALIZED",
                conversation.id,
                inbound.id,
                {
                    "provider": llm.provider,
                    "model": llm.model,
                    "reason": provider_style.reason,
                    "removed_emoji_count": provider_style.removed_emoji_count,
                },
            )
        guarded_content = append_record_notice(provider_style.content) if formal_todo is not None else provider_style.content
        guard = self.guard.inspect(
            guarded_content,
            effective_current_message,
            strict=contact.importance == Importance.IMPORTANT,
            truncate_limit=None,
            strict_length_limit=None if long_form_plan.requested else 400,
        )
        if formal_todo is not None and not guard.allowed:
            original_guard_code = guard.code
            safe_fallback = append_record_notice(
                "这件事涉及需要你本人确认的正式事项，我不能替你作出承诺，但可以继续帮你整理资料和下一步建议。"
            )
            guard = self.guard.inspect(
                safe_fallback,
                strict=contact.importance == Importance.IMPORTANT,
                truncate_limit=None,
                strict_length_limit=400,
            )
            self._audit(
                db,
                "FORMAL_MATTER_REPLY_REPAIRED",
                conversation.id,
                inbound.id,
                {"todo_id": formal_todo.id, "blocked_model_reply_code": original_guard_code},
                level="WARNING",
            )
        outbound = Message(
            platform=event.platform,
            account_id=event.account_id,
            external_message_id=f"ai:{event.message_id}:{uuid4()}",
            sender_id=f"{event.platform}:AI",
            receiver_id=group.platform_group_id if event.is_group and group else contact.platform_user_id,
            conversation_id=conversation.id,
            contact_id=contact.id,
            direction=MessageDirection.OUTBOUND,
            author=MessageAuthor.AI,
            content=guard.content or provider_style.content[:800],
            status=MessageStatus.GENERATED if guard.allowed else MessageStatus.BLOCKED,
            reply_to_external_id=event.message_id,
            provider=llm.provider,
            model=llm.model,
            input_tokens=llm.input_tokens,
            output_tokens=llm.output_tokens,
            latency_ms=llm.latency_ms,
            policy_reason=None if guard.allowed else guard.code,
            raw_envelope={
                "long_form": {
                    "requested": long_form_plan.requested,
                    "format": long_form_plan.format if long_form_plan.requested else None,
                    "title": long_form_plan.title if long_form_plan.requested else None,
                    "finish_reason": llm.finish_reason,
                    "continuation_chunks": long_form_generation.chunks if long_form_generation else 1,
                },
                "formal_matter": {
                    "recorded": formal_todo is not None,
                    "todo_id": formal_todo.id if formal_todo is not None else None,
                },
            },
        )
        db.add(outbound)
        db.flush()
        record_delivery_event(
            db,
            outbound,
            stage="GENERATED",
            status=str(outbound.status),
            detail={"provider": llm.provider, "model": llm.model},
        )
        text_delivery = prepare_text_delivery(
            db,
            settings=self.settings,
            message=outbound,
            content=guard.content,
            segment_index=1,
            document_mode=long_form_plan.requested,
            document_format=long_form_plan.format,
            document_title=long_form_plan.title,
        ) if guard.allowed else None
        if text_delivery and text_delivery.overflowed:
            self._audit(
                db,
                "AI_TEXT_OVERFLOW_FILE_CREATED",
                conversation.id,
                outbound.id,
                {
                    "format": text_delivery.format,
                    "full_characters": text_delivery.full_characters,
                    "transport_limit": self.settings.napcat_text_safe_limit,
                    "document_mode": text_delivery.document_mode,
                },
            )
        delivery_content = text_delivery.content if text_delivery else (guard.content or outbound.content)
        sticker = (
            select_auto_sticker(db, delivery_content)
            if not long_form_plan.requested and event.platform.upper() in {"QQ_NAPCAT", "SIMULATOR"}
            else None
        )
        sticker_attachment: tuple[OutboundAttachment, ...] = ()
        if sticker is not None:
            sticker_path = (self.settings.data_dir / sticker.local_path).resolve()
            try:
                sticker_path.relative_to((self.settings.data_dir / "stickers").resolve())
            except ValueError:
                sticker = None
            if sticker is not None and sticker_path.is_file():
                db.add(
                    MessageAttachment(
                        message_id=outbound.id,
                        kind="IMAGE",
                        segment_type="image",
                        segment_index=0,
                        file_name=sticker_path.name,
                        mime_type=sticker.mime_type,
                        size_bytes=sticker_path.stat().st_size,
                        local_path=sticker.local_path,
                        sha256=sticker.sha256,
                        status="SAVED",
                        attachment_metadata={"source": "STICKER_LIBRARY", "sticker_id": sticker.id, "label": sticker.label},
                    )
                )
                sticker_attachment = (OutboundAttachment("IMAGE", str(sticker_path), sticker_path.name, sticker.mime_type),)
                self._audit(db, "STICKER_SELECTED", conversation.id, outbound.id, {"sticker_id": sticker.id, "label": sticker.label})
        refresh_conversation_summary(db, conversation)
        if not guard.allowed:
            record_delivery_event(
                db,
                outbound,
                stage="SAFETY_CHECK",
                status="BLOCKED",
                error_code=guard.code,
                retry_reason="内容安全拦截不能通过待发箱重试。",
            )
            self._audit(db, AuditEvent.SAFETY_DENY, conversation.id, outbound.id, {"code": guard.code, "reason": guard.reason})
            self._incident(db, "SAFETY_BLOCK", "AI 回复被安全检查拦截", guard.reason)
            db.commit()
            await event_hub.broadcast("ai_finished", {"conversation_id": conversation.id, "blocked": True})
            return PipelineResult(True, False, False, False, guard.code, guard.reason, inbound.id, outbound.id)
        self._audit(db, AuditEvent.SAFETY_PASS, conversation.id, outbound.id, {"code": "PASS"})

        channel_attachments = list(sticker_attachment)
        if text_delivery:
            channel_attachments.extend(text_delivery.attachments)
        has_inbound_audio = any(
            item.kind.upper() == "AUDIO" or str(item.mime_type or "").lower().startswith("audio/")
            for item in event.attachments
        )
        if (
            state.tts_enabled
            and event.platform.upper() == "QQ_NAPCAT"
            and (not state.tts_reply_to_audio_only or has_inbound_audio)
        ):
            try:
                speech = await asyncio.to_thread(
                    synthesize_local_speech,
                    self.settings,
                    delivery_content,
                    voice=state.tts_voice,
                    rate=state.tts_rate,
                    volume=state.tts_volume,
                    max_chars=state.tts_max_chars,
                )
            except LocalTTSError as exc:
                self._audit(
                    db,
                    "LOCAL_TTS_FAILED",
                    conversation.id,
                    outbound.id,
                    {"code": exc.code},
                    level="WARNING",
                )
            else:
                db.add(
                    MessageAttachment(
                        message_id=outbound.id,
                        kind="AUDIO",
                        segment_type="record",
                        segment_index=len(channel_attachments),
                        file_name=speech.absolute_path.name,
                        mime_type="audio/wav",
                        size_bytes=speech.size_bytes,
                        local_path=speech.relative_path,
                        sha256=speech.sha256,
                        status="SAVED",
                        attachment_metadata={"source": "LOCAL_TTS", "engine": speech.engine, "voice": speech.voice},
                    )
                )
                channel_attachments.append(
                    OutboundAttachment("AUDIO", str(speech.absolute_path), speech.absolute_path.name, "audio/wav")
                )
                self._audit(
                    db,
                    "LOCAL_TTS_READY",
                    conversation.id,
                    outbound.id,
                    {"engine": speech.engine, "voice": speech.voice, "audio_only_trigger": state.tts_reply_to_audio_only},
                )

        rate = self._rate_decision(db, contact, conversation)
        if not rate.allowed:
            outbound.status = MessageStatus.BLOCKED
            outbound.policy_reason = rate.code
            record_delivery_event(
                db,
                outbound,
                stage="RATE_LIMIT",
                status="BLOCKED",
                error_code=rate.code,
                retry_reason="频率限制应等待新消息触发，不重放旧回复。",
            )
            self._audit(db, AuditEvent.POLICY_DENY, conversation.id, outbound.id, {"code": rate.code, "reason": rate.reason})
            self._incident(db, "RATE_LIMIT_INCIDENT", "发送频率保护已触发", rate.reason)
            if rate.force_silent:
                state.global_mode = GlobalMode.SILENT
            db.commit()
            await event_hub.broadcast("ai_finished", {"conversation_id": conversation.id, "blocked": True})
            return PipelineResult(True, False, False, False, rate.code, rate.reason, inbound.id, outbound.id)

        outbound.status = MessageStatus.QUEUED
        queued_gate = state.release_gate
        record_delivery_event(db, outbound, stage="QUEUED", status="QUEUED")
        self._audit(db, AuditEvent.MESSAGE_QUEUED, conversation.id, outbound.id, {"release_gate": queued_gate})
        db.commit()

        if state.release_gate == "SHADOW":
            outbound.status = MessageStatus.SHADOWED
            record_delivery_event(db, outbound, stage="SHADOW", status="NOT_TRANSMITTED")
            self._audit(db, AuditEvent.MESSAGE_SHADOWED, conversation.id, outbound.id, {"target": "not_transmitted"})
            db.commit()
            await event_hub.broadcast("ai_finished", {"conversation_id": conversation.id, "shadowed": True})
            return PipelineResult(
                True,
                False,
                True,
                False,
                "SHADOWED",
                "影子模式：已生成但未真实发送",
                inbound.id,
                outbound.id,
                delivery_content,
                provider=llm.provider,
                model=llm.model,
            )

        connector: ChannelConnector | None
        if state.release_gate == "SIMULATION":
            connector = self.connectors.get("SIMULATOR")
        else:
            connector = self.connectors.get(event.platform.upper())
        if connector is None:
            outbound.status = MessageStatus.FAILED
            outbound.policy_reason = "CONNECTOR_MISSING"
            record_delivery_event(
                db,
                outbound,
                stage="PRE_SEND",
                status="FAILED",
                retry_allowed=True,
                retry_reason="尚未调用任何发送通道；通道恢复后可重新复检并安全重试。",
                error_code="CONNECTOR_MISSING",
            )
            self._incident(db, "CONNECTOR_MISSING", "发送通道不可用", f"{event.platform} connector is unavailable")
            db.commit()
            await event_hub.broadcast("ai_finished", {"conversation_id": conversation.id, "failed": True})
            return PipelineResult(True, False, False, False, "CONNECTOR_MISSING", "发送通道不可用", inbound.id, outbound.id)

        if state.release_gate == "LIVE" and connector.real_channel:
            await asyncio.sleep(random.uniform(0.8, 2.4))
        async with self.runtime.send_lock:
            db.expire(state)
            db.refresh(state)
            db.refresh(contact)
            db.refresh(conversation, attribute_names=["mode", "human_until", "cooldown_until", "managed_rounds"])
            if group is not None:
                db.refresh(group)
            if state.release_gate != queued_gate:
                outbound.status = MessageStatus.CANCELLED
                outbound.policy_reason = "RELEASE_GATE_CHANGED"
                record_delivery_event(db, outbound, stage="FINAL_POLICY", status="CANCELLED", error_code="RELEASE_GATE_CHANGED")
                self._audit(db, AuditEvent.POLICY_DENY, conversation.id, outbound.id, {"code": "FINAL_RELEASE_GATE_CHANGED", "from": queued_gate, "to": state.release_gate})
                db.commit()
                await event_hub.broadcast("ai_finished", {"conversation_id": conversation.id, "cancelled": True})
                return PipelineResult(True, False, False, False, "RELEASE_GATE_CHANGED", "发布门禁已变化，队列消息已取消", inbound.id, outbound.id)
            final_decision = self.policy.evaluate(self._policy_input(db, state, contact, group, conversation, event, now=utc_now()))
            if not final_decision.allowed:
                outbound.status = MessageStatus.CANCELLED
                outbound.policy_reason = final_decision.code
                record_delivery_event(db, outbound, stage="FINAL_POLICY", status="CANCELLED", error_code=final_decision.code)
                self._audit(db, AuditEvent.POLICY_DENY, conversation.id, outbound.id, {"code": f"FINAL_{final_decision.code}"})
                db.commit()
                await event_hub.broadcast("ai_finished", {"conversation_id": conversation.id, "cancelled": True})
                return PipelineResult(True, False, False, False, final_decision.code, "发送前复检未通过，队列消息已取消", inbound.id, outbound.id)
            final_rate = self._rate_decision(db, contact, conversation, exclude_message_id=outbound.id)
            if not final_rate.allowed:
                outbound.status = MessageStatus.BLOCKED
                outbound.policy_reason = final_rate.code
                record_delivery_event(db, outbound, stage="FINAL_RATE_LIMIT", status="BLOCKED", error_code=final_rate.code)
                self._audit(db, AuditEvent.POLICY_DENY, conversation.id, outbound.id, {"code": f"FINAL_{final_rate.code}", "reason": final_rate.reason})
                self._incident(db, "RATE_LIMIT_INCIDENT", "发送前频率复检已阻止消息", final_rate.reason)
                if final_rate.force_silent:
                    state.global_mode = GlobalMode.SILENT
                db.commit()
                await event_hub.broadcast("ai_finished", {"conversation_id": conversation.id, "blocked": True})
                return PipelineResult(True, False, False, False, final_rate.code, "发送前频率复检未通过", inbound.id, outbound.id)
            target_id = group.platform_group_id if event.is_group and group else contact.platform_user_id
            quote_target = event.message_id if state.napcat_quote_reply_enabled and event.platform.upper() == "QQ_NAPCAT" else ""
            message = OutboundMessage(event.platform, target_id, delivery_content, quote_target, event.is_group, tuple(channel_attachments))
            permit = SendPermit(target_id, contact.whitelisted, True, True, True, state.global_mode == GlobalMode.AUTO, not state.kill_switch)
            record_delivery_event(
                db,
                outbound,
                stage="SEND_STARTED",
                status="IN_FLIGHT",
                delivery_started=True,
                retry_reason="发送调用已经开始，除非平台返回唯一确认，否则禁止自动重试。",
            )
            db.commit()
            try:
                result = await connector.send(message, permit)
            except Exception as exc:
                outbound.status = MessageStatus.FAILED
                outbound.policy_reason = type(exc).__name__
                record_delivery_event(
                    db,
                    outbound,
                    stage="SEND_RESULT",
                    status="FAILED_AMBIGUOUS",
                    delivery_started=True,
                    error_code=type(exc).__name__,
                    retry_reason="发送调用异常中断，平台是否已收到无法确认，禁止自动重试。",
                )
                self._audit(db, AuditEvent.ERROR, conversation.id, outbound.id, {"code": type(exc).__name__}, level="ERROR")
                self._incident(db, "CONNECTOR_FAILURE", "消息通道发送异常", type(exc).__name__)
                db.commit()
                await event_hub.broadcast("incident", {"kind": "CONNECTOR_FAILURE"})
                await event_hub.broadcast("ai_finished", {"conversation_id": conversation.id, "failed": True})
                return PipelineResult(True, False, False, False, type(exc).__name__, "通道发送异常，未自动重试", inbound.id, outbound.id)

        if not result.success:
            outbound.status = MessageStatus.FAILED
            outbound.policy_reason = result.error_code
            record_delivery_event(
                db,
                outbound,
                stage="SEND_RESULT",
                status="FAILED",
                delivery_started=True,
                acknowledged=False,
                error_code=result.error_code,
                retry_reason="通道已处理发送请求，为避免重复消息，禁止自动重试。",
            )
            self._audit(db, AuditEvent.ERROR, conversation.id, outbound.id, {"code": result.error_code}, level="ERROR")
            self._incident(db, "CONNECTOR_FAILURE", "消息通道发送失败", result.error_detail or result.error_code or "unknown")
            await self._maybe_trip_failure_circuit(db, "CONNECTOR_FAILURE", conversation.id, outbound.id)
            db.commit()
            await event_hub.broadcast("incident", {"kind": "CONNECTOR_FAILURE"})
            await event_hub.broadcast("ai_finished", {"conversation_id": conversation.id, "failed": True})
            return PipelineResult(True, False, False, False, result.error_code or "SEND_FAILED", "通道发送失败", inbound.id, outbound.id)

        outbound.status = MessageStatus.SENT
        if sticker is not None and sticker_attachment:
            sticker.use_count += 1
        outbound.external_message_id = result.external_message_id or outbound.external_message_id
        record_delivery_event(
            db,
            outbound,
            stage="PLATFORM_CONFIRMED",
            status="SENT",
            delivery_started=True,
            acknowledged=True,
            external_message_id=result.external_message_id,
        )
        conversation.managed_rounds += 1
        conversation.consecutive_ai_sends += 1
        self.limiter.record(contact.id, utc_now())
        self._audit(db, AuditEvent.MESSAGE_SENT, conversation.id, outbound.id, {"platform": event.platform, "simulation": not connector.real_channel})
        if conversation.managed_rounds == self.settings.conversation_warning_rounds:
            self._incident(db, "LONG_CONVERSATION_WARNING", "连续托管已达 20 轮", "请关注是否存在机器人互聊")
        if conversation.managed_rounds >= self.settings.conversation_stop_rounds and contact.relationship_label != ADMIN_RELATIONSHIP:
            conversation.mode = ConversationMode.CONTACT_COOLDOWN
            conversation.cooldown_until = utc_now() + timedelta(minutes=self.settings.contact_cooldown_minutes)
        db.commit()
        await event_hub.broadcast("message_sent", {"message_id": outbound.id, "conversation_id": conversation.id})
        await event_hub.broadcast("ai_finished", {"conversation_id": conversation.id, "sent": True})
        return PipelineResult(
            True,
            True,
            False,
            False,
            "SENT",
            "安全检查通过并已发送",
            inbound.id,
            outbound.id,
            delivery_content,
            provider=llm.provider,
            model=llm.model,
        )

    async def _send_media_compression_notice(
        self,
        db: Session,
        state,
        contact: Contact,
        group: ChatGroup | None,
        conversation: Conversation,
        event: InboundEvent,
        inbound: Message,
    ) -> bool:
        """Send one deterministic wait notice without bypassing normal gates.

        The notice is intentionally LIVE-only. SHADOW and SIMULATION record the
        compression audit but never invoke a real connector. A final policy and
        rate-limit check runs under the same send lock as an AI reply.
        """

        if state.release_gate != "LIVE":
            self._audit(
                db,
                "MEDIA_COMPRESSION_NOTICE_SKIPPED",
                conversation.id,
                inbound.id,
                {"reason": f"RELEASE_GATE_{state.release_gate}"},
            )
            db.commit()
            return False
        connector = self.connectors.get(event.platform.upper())
        if connector is None or not connector.real_channel:
            self._audit(
                db,
                "MEDIA_COMPRESSION_NOTICE_SKIPPED",
                conversation.id,
                inbound.id,
                {"reason": "CONNECTOR_UNAVAILABLE"},
                level="WARNING",
            )
            db.commit()
            return False
        rate = self._rate_decision(db, contact, conversation)
        if not rate.allowed:
            self._audit(
                db,
                "MEDIA_COMPRESSION_NOTICE_SKIPPED",
                conversation.id,
                inbound.id,
                {"reason": rate.code},
                level="WARNING",
            )
            db.commit()
            return False

        content = "收到啦，这个图片或视频比较大，我需要先在本机压缩处理，可能要等一会儿，处理好后再回复你。"
        notice = Message(
            platform=event.platform,
            account_id=event.account_id,
            external_message_id=f"system:media-compress:{event.message_id}:{uuid4()}",
            sender_id=f"{event.platform}:SYSTEM",
            receiver_id=group.platform_group_id if event.is_group and group else contact.platform_user_id,
            event_at=utc_now(),
            conversation_id=conversation.id,
            contact_id=contact.id,
            direction=MessageDirection.OUTBOUND,
            author=MessageAuthor.SYSTEM,
            content=content,
            message_type="TEXT",
            status=MessageStatus.QUEUED,
            reply_to_external_id=event.message_id,
            provider="LOCAL_MEDIA",
            model="DETERMINISTIC_PROGRESS",
        )
        db.add(notice)
        db.flush()
        self._audit(
            db,
            AuditEvent.MESSAGE_QUEUED,
            conversation.id,
            notice.id,
            {"source": "MEDIA_COMPRESSION_PROGRESS", "release_gate": "LIVE"},
        )
        db.commit()

        try:
            async with self.runtime.send_lock:
                db.refresh(state)
                db.refresh(contact)
                db.refresh(conversation, attribute_names=["mode", "human_until", "cooldown_until", "managed_rounds"])
                if group is not None:
                    db.refresh(group)
                if state.release_gate != "LIVE":
                    notice.status = MessageStatus.CANCELLED
                    notice.policy_reason = "RELEASE_GATE_CHANGED"
                    db.commit()
                    return False
                final_decision = self.policy.evaluate(
                    self._policy_input(db, state, contact, group, conversation, event, now=utc_now())
                )
                if not final_decision.allowed:
                    notice.status = MessageStatus.CANCELLED
                    notice.policy_reason = final_decision.code
                    self._audit(
                        db,
                        AuditEvent.POLICY_DENY,
                        conversation.id,
                        notice.id,
                        {"code": f"FINAL_{final_decision.code}", "source": "MEDIA_COMPRESSION_PROGRESS"},
                    )
                    db.commit()
                    return False
                final_rate = self._rate_decision(db, contact, conversation, exclude_message_id=notice.id)
                if not final_rate.allowed:
                    notice.status = MessageStatus.BLOCKED
                    notice.policy_reason = final_rate.code
                    db.commit()
                    return False
                target_id = group.platform_group_id if event.is_group and group else contact.platform_user_id
                quote_target = event.message_id if state.napcat_quote_reply_enabled and event.platform.upper() == "QQ_NAPCAT" else ""
                result = await connector.send(
                    OutboundMessage(event.platform, target_id, content, quote_target, event.is_group),
                    SendPermit(target_id, contact.whitelisted, True, True, True, True, not state.kill_switch),
                )
        except Exception as exc:
            notice.status = MessageStatus.FAILED
            notice.policy_reason = type(exc).__name__
            self._audit(
                db,
                AuditEvent.ERROR,
                conversation.id,
                notice.id,
                {"code": "MEDIA_COMPRESSION_NOTICE_FAILED", "error": type(exc).__name__},
                level="ERROR",
            )
            db.commit()
            return False

        if not result.success:
            notice.status = MessageStatus.FAILED
            notice.policy_reason = result.error_code or "SEND_FAILED"
            self._audit(
                db,
                AuditEvent.ERROR,
                conversation.id,
                notice.id,
                {"code": notice.policy_reason, "source": "MEDIA_COMPRESSION_PROGRESS"},
                level="ERROR",
            )
            db.commit()
            return False
        notice.status = MessageStatus.SENT
        notice.external_message_id = result.external_message_id or notice.external_message_id
        conversation.consecutive_ai_sends += 1
        self.limiter.record(contact.id, utc_now())
        self._audit(
            db,
            AuditEvent.MESSAGE_SENT,
            conversation.id,
            notice.id,
            {"source": "MEDIA_COMPRESSION_PROGRESS", "platform": event.platform},
        )
        db.commit()
        await event_hub.broadcast("message_sent", {"message_id": notice.id, "conversation_id": conversation.id})
        return True

    def _reconcile_napcat_ai_echo(
        self,
        db: Session,
        event: InboundEvent,
        contact: Contact,
    ) -> PipelineResult | None:
        """Bind NapCat's message_sent echo to Neko's durable outbound row.

        Enabling reportSelfMessage is required to capture manual QQ replies, but
        NapCat also reports messages that Neko itself just sent through OneBot.
        The queued AI row is already durable before the connector call, so an
        exact, short-lived match prevents a second HUMAN row and a false human
        takeover even if the echo reaches us before the OneBot call returns.
        Dashboard manual sends are HUMAN-authored for truthful transcripts, so
        only their dedicated provider marker is eligible for reconciliation.
        """
        if event.author != MessageAuthor.HUMAN or event.raw.get("event_type") != "ONEBOT11_MESSAGE_SENT":
            return None
        event_at = _utc(event.timestamp)
        candidate = db.scalar(
            select(Message)
            .where(
                Message.platform == event.platform,
                Message.contact_id == contact.id,
                Message.direction == MessageDirection.OUTBOUND,
                or_(
                    Message.author.in_([MessageAuthor.AI, MessageAuthor.SYSTEM]),
                    and_(
                        Message.author == MessageAuthor.HUMAN,
                        Message.provider == DASHBOARD_MANUAL_PROVIDER,
                    ),
                ),
                Message.status.in_([MessageStatus.QUEUED, MessageStatus.SENT]),
                Message.content == event.content,
                Message.created_at >= event_at - timedelta(seconds=30),
                Message.created_at <= event_at + timedelta(seconds=30),
            )
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        if candidate is None and event.attachments:
            recent_candidates = list(
                db.scalars(
                    select(Message)
                    .join(MessageAttachment, MessageAttachment.message_id == Message.id)
                    .where(
                        Message.platform == event.platform,
                        Message.contact_id == contact.id,
                        Message.direction == MessageDirection.OUTBOUND,
                        Message.author.in_([MessageAuthor.AI, MessageAuthor.SYSTEM]),
                        Message.status.in_([MessageStatus.QUEUED, MessageStatus.SENT]),
                        Message.created_at >= event_at - timedelta(seconds=30),
                        Message.created_at <= event_at + timedelta(seconds=30),
                    )
                    .order_by(Message.created_at.desc())
                    .limit(5)
                )
            )
            event_kinds = {item.kind for item in event.attachments}
            event_names = {item.file_name for item in event.attachments if item.file_name}
            for item in recent_candidates:
                stored_attachments = list(
                    db.scalars(select(MessageAttachment).where(MessageAttachment.message_id == item.id))
                )
                stored_kinds = {attachment.kind for attachment in stored_attachments}
                stored_names = {attachment.file_name for attachment in stored_attachments if attachment.file_name}
                has_supported_source = item.provider == "LOCAL_ADMIN_DELIVERY" or any(
                    (attachment.attachment_metadata or {}).get("source")
                    in {"AI_TEXT_OVERFLOW", "AI_DOCUMENT_RESPONSE"}
                    for attachment in stored_attachments
                )
                names_match = not event_names or not stored_names or bool(event_names & stored_names)
                if has_supported_source and event_kinds.issubset(stored_kinds) and names_match:
                    candidate = item
                    break
        if candidate is None:
            return None
        is_dashboard_manual = candidate.provider == DASHBOARD_MANUAL_PROVIDER
        is_generated_file_echo = bool(
            event.attachments
            and (
                (candidate.raw_envelope or {}).get("text_overflow")
                or (candidate.raw_envelope or {}).get("document_delivery")
            )
        )
        if not is_generated_file_echo or candidate.external_message_id.startswith(("ai:", "admin-topic:")):
            candidate.external_message_id = event.message_id
        self._audit(
            db,
            AuditEvent.DUPLICATE_IGNORED,
            candidate.conversation_id,
            candidate.id,
            {
                "external_message_id": event.message_id,
                "source": (
                    "NAPCAT_DASHBOARD_MANUAL_ECHO"
                    if is_dashboard_manual
                    else "NAPCAT_AI_GENERATED_FILE_ECHO"
                    if is_generated_file_echo
                    else "NAPCAT_AI_SEND_ECHO"
                ),
            },
        )
        db.commit()
        return PipelineResult(
            True,
            False,
            False,
            True,
            "DASHBOARD_MANUAL_SEND_ECHO" if is_dashboard_manual else "AI_SEND_ECHO",
            "NapCat 已发送回显已关联到人工直发消息" if is_dashboard_manual else "NapCat 已发送回显已关联到原 AI 消息",
            outbound_message_id=candidate.id,
        )

    async def _handle_delegated_todo_create(
        self,
        db: Session,
        state,
        source: Contact,
        source_conversation: Conversation,
        event: InboundEvent,
        inbound: Message,
        intent: DelegatedTodoIntent,
    ) -> PipelineResult:
        async with self.runtime.send_lock:
            db.refresh(state)
            db.refresh(source)
            admin = find_account_admin(db, source)
            todo = create_delegated_todo(
                db,
                source=source,
                source_message=inbound,
                intent=intent,
                admin=admin,
            )
            self._audit(
                db,
                "CONTACT_RELAY_TODO_CREATED",
                source_conversation.id,
                inbound.id,
                {"todo_id": todo.id, "source_contact_id": source.id, "admin_contact_id": admin.id if admin else None},
            )
            db.commit()
            if admin is None:
                acknowledgement = await self._send_relay_message(
                    db,
                    state=state,
                    contact=source,
                    conversation=source_conversation,
                    source_message=inbound,
                    content="我已经把这件事记进后台待办；当前没有配置可用的管理员联系人，请管理员稍后从后台处理。",
                    provider=RELAY_PROVIDER,
                    model="DETERMINISTIC_ACK",
                    reply_to_external_id=event.message_id,
                    author=MessageAuthor.SYSTEM,
                    envelope={"contact_relay": {"todo_id": todo.id, "role": "SOURCE_ACK", "admin_missing": True}},
                )
                await event_hub.broadcast("todo_created", {"todo_id": todo.id, "delivery_status": todo.delivery_status})
                return PipelineResult(
                    True,
                    acknowledgement.sent,
                    acknowledgement.shadowed,
                    False,
                    "CONTACT_RELAY_ADMIN_MISSING",
                    "已创建待办，但当前账号没有可用的白名单管理员联系人",
                    inbound.id,
                    acknowledgement.outbound_message_id,
                    acknowledgement.reply,
                    RELAY_PROVIDER,
                    "DETERMINISTIC",
                )

            admin_conversation = self._private_contact_conversation(db, admin, event.account_id)
            notification = await self._send_relay_message(
                db,
                state=state,
                contact=admin,
                conversation=admin_conversation,
                source_message=inbound,
                content=intent.notification,
                provider=RELAY_PROVIDER,
                model="DETERMINISTIC_NOTIFICATION",
                reply_to_external_id="",
                author=MessageAuthor.SYSTEM,
                envelope={"contact_relay": {"todo_id": todo.id, "role": "ADMIN_NOTIFICATION", "source_contact_id": source.id}},
            )
            todo.admin_notification_message_id = notification.outbound_message_id
            if notification.sent:
                todo.delivery_status = "NOTIFIED"
                todo.last_error = None
            elif notification.shadowed:
                todo.delivery_status = "SHADOWED"
                todo.last_error = "当前不是 LIVE，管理员通知仅作为影子消息保存"
            else:
                todo.delivery_status = "NOTIFY_FAILED"
                todo.last_error = notification.reason
            db.commit()

            if notification.sent:
                acknowledgement_content = intent.acknowledgement
            elif notification.shadowed:
                acknowledgement_content = "我已经把这件事记进后台待办；当前是测试模式，没有真的通知主人。"
            else:
                acknowledgement_content = "我已经把这件事记进后台待办；刚才通知主人没有送达，待办会保留给管理员处理。"
            acknowledgement = await self._send_relay_message(
                db,
                state=state,
                contact=source,
                conversation=source_conversation,
                source_message=inbound,
                content=acknowledgement_content,
                provider=RELAY_PROVIDER,
                model="DETERMINISTIC_ACK",
                reply_to_external_id=event.message_id,
                author=MessageAuthor.SYSTEM,
                envelope={"contact_relay": {"todo_id": todo.id, "role": "SOURCE_ACK"}},
            )
            if notification.sent and not acknowledgement.sent and not acknowledgement.shadowed:
                todo.last_error = f"管理员已通知，但联系人确认未送达：{acknowledgement.reason}"
                db.commit()
            await event_hub.broadcast("todo_created", {"todo_id": todo.id, "delivery_status": todo.delivery_status})
            if notification.sent and not acknowledgement.sent and not acknowledgement.shadowed:
                result_code = "CONTACT_RELAY_SOURCE_ACK_FAILED"
            elif notification.sent:
                result_code = "CONTACT_RELAY_CREATED"
            else:
                result_code = f"CONTACT_RELAY_{todo.delivery_status}"
            return PipelineResult(
                True,
                bool(notification.sent or acknowledgement.sent),
                bool(notification.shadowed or acknowledgement.shadowed),
                False,
                result_code,
                (
                    "联系人转交待办已创建并通知管理员"
                    if notification.sent and acknowledgement.sent
                    else todo.last_error or "联系人转交待办已创建"
                ),
                inbound.id,
                acknowledgement.outbound_message_id or notification.outbound_message_id,
                acknowledgement_content,
                RELAY_PROVIDER,
                "DETERMINISTIC",
            )

    async def _handle_delegated_todo_reply(
        self,
        db: Session,
        state,
        admin: Contact,
        admin_conversation: Conversation,
        event: InboundEvent,
        inbound: Message,
        todo: TodoItem,
    ) -> PipelineResult:
        async with self.runtime.send_lock:
            db.refresh(state)
            db.refresh(admin)
            target = db.get(Contact, todo.contact_id) if todo.contact_id else None
            admin_reply = str(event.raw.get("text_content") or event.content or "").strip()
            if target is None:
                todo.delivery_status = "TARGET_MISSING"
                todo.last_error = "原联系人已不存在"
                db.commit()
                receipt = await self._deliver_admin_response(
                    db, state, admin, admin_conversation, event, inbound, "原联系人已不存在，待办仍保留，请在后台处理。", "CONTACT_RELAY_TARGET_MISSING"
                )
                return PipelineResult(True, receipt.sent, receipt.shadowed, False, "CONTACT_RELAY_TARGET_MISSING", todo.last_error, inbound.id, receipt.outbound_message_id)
            if not admin_reply:
                receipt = await self._deliver_admin_response(
                    db, state, admin, admin_conversation, event, inbound, "请在引用待办时同时填写要转达给对方的文字答复。", "CONTACT_RELAY_REPLY_REQUIRED"
                )
                return PipelineResult(True, receipt.sent, receipt.shadowed, False, "CONTACT_RELAY_REPLY_REQUIRED", "管理员答复为空", inbound.id, receipt.outbound_message_id)
            if contains_secret(admin_reply):
                todo.delivery_status = "REPLY_BLOCKED"
                todo.last_error = "管理员答复疑似包含凭据或密钥"
                db.commit()
                receipt = await self._deliver_admin_response(
                    db,
                    state,
                    admin,
                    admin_conversation,
                    event,
                    inbound,
                    "这条答复疑似包含凭据或密钥，未发送给模型或联系人；待办仍保持未完成。",
                    "CONTACT_RELAY_SECRET_BLOCKED",
                )
                return PipelineResult(
                    True,
                    receipt.sent,
                    receipt.shadowed,
                    False,
                    "CONTACT_RELAY_SECRET_BLOCKED",
                    todo.last_error,
                    inbound.id,
                    receipt.outbound_message_id,
                )

            provider = "LOCAL_TEMPLATE"
            model = "DETERMINISTIC_RELAY"
            generated = fallback_relay_response(admin_reply)
            try:
                llm = await self.gateway.chat(
                    build_relay_response_messages(target=target, original=todo.detail, admin_reply=admin_reply)
                )
                generated = llm.content.strip() or generated
                provider = llm.provider
                model = llm.model
            except AllProvidersFailed as exc:
                self._audit(
                    db,
                    "CONTACT_RELAY_MODEL_FALLBACK",
                    admin_conversation.id,
                    inbound.id,
                    {"todo_id": todo.id, "error": str(exc)[:180]},
                    level="WARNING",
                )
                db.commit()
            guard = self.guard.inspect(
                generated,
                f"{todo.detail}\n{admin_reply}",
                strict=target.importance == Importance.IMPORTANT,
            )
            if not guard.allowed:
                todo.delivery_status = "REPLY_BLOCKED"
                todo.last_error = guard.reason
                db.commit()
                receipt = await self._deliver_admin_response(
                    db, state, admin, admin_conversation, event, inbound, f"答复未转发：{guard.reason}。待办仍保持未完成。", "CONTACT_RELAY_GUARD_BLOCKED"
                )
                return PipelineResult(True, receipt.sent, receipt.shadowed, False, "CONTACT_RELAY_GUARD_BLOCKED", guard.reason, inbound.id, receipt.outbound_message_id)

            source_message = db.get(Message, todo.source_message_id) if todo.source_message_id else inbound
            delivery = await self._send_relay_message(
                db,
                state=state,
                contact=target,
                conversation=self._private_contact_conversation(db, target, todo.account_id),
                source_message=source_message or inbound,
                content=guard.content,
                provider=provider,
                model=model,
                reply_to_external_id=source_message.external_message_id if source_message else "",
                author=MessageAuthor.AI,
                envelope={
                    "contact_relay": {
                        "todo_id": todo.id,
                        "role": "SOURCE_RESPONSE",
                        "admin_reply_message_id": inbound.id,
                    }
                },
            )
            todo.response_message_id = delivery.outbound_message_id
            todo.response_text = guard.content
            if delivery.sent:
                todo.status = "DONE"
                todo.delivery_status = "DELIVERED"
                todo.completed_at = utc_now()
                todo.last_error = None
                receipt_text = f"已把你的答复整理后发给 {target.display_name}，待办已完成。\n转达内容：{guard.content}"
            else:
                todo.delivery_status = "SHADOWED" if delivery.shadowed else "REPLY_FAILED"
                todo.last_error = delivery.reason
                receipt_text = f"暂未转发给 {target.display_name}：{delivery.reason}。待办仍保持未完成。"
            self._audit(
                db,
                "CONTACT_RELAY_COMPLETED" if delivery.sent else "CONTACT_RELAY_REPLY_FAILED",
                admin_conversation.id,
                inbound.id,
                {"todo_id": todo.id, "target_contact_id": target.id, "provider": provider, "model": model},
                level="INFO" if delivery.sent else "WARNING",
            )
            db.commit()
            receipt = await self._deliver_admin_response(
                db, state, admin, admin_conversation, event, inbound, receipt_text, "CONTACT_RELAY_RECEIPT"
            )
            await event_hub.broadcast("todo_updated", {"todo_id": todo.id, "status": todo.status, "delivery_status": todo.delivery_status})
            return PipelineResult(
                True,
                delivery.sent,
                delivery.shadowed,
                False,
                "CONTACT_RELAY_DELIVERED" if delivery.sent else "CONTACT_RELAY_REPLY_PENDING",
                receipt_text,
                inbound.id,
                delivery.outbound_message_id or receipt.outbound_message_id,
                guard.content,
                provider,
                model,
            )

    async def _send_relay_message(
        self,
        db: Session,
        *,
        state,
        contact: Contact,
        conversation: Conversation,
        source_message: Message,
        content: str,
        provider: str,
        model: str,
        reply_to_external_id: str,
        author: str,
        envelope: dict,
    ) -> PipelineResult:
        outbound = Message(
            platform="QQ_NAPCAT",
            account_id=contact.account_id,
            external_message_id=f"contact-relay:{source_message.id}:{uuid4()}",
            sender_id="QQ_NAPCAT:AI" if author == MessageAuthor.AI else "QQ_NAPCAT:SYSTEM",
            receiver_id=contact.platform_user_id,
            event_at=utc_now(),
            conversation_id=conversation.id,
            contact_id=contact.id,
            direction=MessageDirection.OUTBOUND,
            author=author,
            content=content,
            message_type="TEXT",
            status=MessageStatus.GENERATED,
            reply_to_external_id=reply_to_external_id or None,
            provider=provider,
            model=model,
            raw_envelope=envelope,
        )
        db.add(outbound)
        db.flush()
        record_delivery_event(
            db,
            outbound,
            stage="GENERATED",
            status=str(outbound.status),
            detail={"source": "CONTACT_RELAY", "provider": provider, "model": model},
        )
        if state.release_gate != "LIVE":
            outbound.status = MessageStatus.SHADOWED
            outbound.policy_reason = "CONTACT_RELAY_NOT_LIVE"
            record_delivery_event(db, outbound, stage="SHADOW", status="NOT_TRANSMITTED")
            self._audit(db, AuditEvent.MESSAGE_SHADOWED, conversation.id, outbound.id, {"source": "CONTACT_RELAY"})
            db.commit()
            return PipelineResult(True, False, True, False, "SHADOWED", "当前不是 LIVE，消息仅作为影子记录", source_message.id, outbound.id, content, provider, model)
        if state.kill_switch or state.global_mode != GlobalMode.AUTO:
            outbound.status = MessageStatus.BLOCKED
            outbound.policy_reason = "KILL_SWITCH" if state.kill_switch else f"GLOBAL_{state.global_mode}"
            record_delivery_event(db, outbound, stage="FINAL_POLICY", status="BLOCKED", error_code=outbound.policy_reason)
            self._audit(db, AuditEvent.POLICY_DENY, conversation.id, outbound.id, {"code": outbound.policy_reason, "source": "CONTACT_RELAY"})
            db.commit()
            return PipelineResult(True, False, False, False, outbound.policy_reason, "当前运行状态禁止发送", source_message.id, outbound.id, content, provider, model)
        if not contact.whitelisted:
            outbound.status = MessageStatus.BLOCKED
            outbound.policy_reason = "NOT_WHITELISTED"
            record_delivery_event(db, outbound, stage="FINAL_POLICY", status="BLOCKED", error_code=outbound.policy_reason)
            self._audit(db, AuditEvent.POLICY_DENY, conversation.id, outbound.id, {"code": outbound.policy_reason, "source": "CONTACT_RELAY"})
            db.commit()
            return PipelineResult(True, False, False, False, "NOT_WHITELISTED", "目标联系人已不在白名单", source_message.id, outbound.id, content, provider, model)
        active = get_active_account(db, "QQ_NAPCAT")
        if active is None or contact.account_id != active.id:
            outbound.status = MessageStatus.BLOCKED
            outbound.policy_reason = "ACCOUNT_ISOLATION_MISMATCH"
            record_delivery_event(db, outbound, stage="FINAL_POLICY", status="BLOCKED", error_code=outbound.policy_reason)
            self._audit(db, AuditEvent.POLICY_DENY, conversation.id, outbound.id, {"code": outbound.policy_reason, "source": "CONTACT_RELAY"})
            db.commit()
            return PipelineResult(True, False, False, False, outbound.policy_reason, "目标联系人不属于当前活动账号", source_message.id, outbound.id, content, provider, model)
        connector = self.connectors.get("QQ_NAPCAT")
        if connector is None:
            outbound.status = MessageStatus.FAILED
            outbound.policy_reason = "CONNECTOR_MISSING"
            record_delivery_event(
                db,
                outbound,
                stage="PRE_SEND",
                status="FAILED",
                retry_allowed=True,
                retry_reason="通道恢复后可由新的联系人消息重新触发；不要盲目重复发送。",
                error_code=outbound.policy_reason,
            )
            db.commit()
            return PipelineResult(True, False, False, False, "CONNECTOR_MISSING", "NapCat 发送通道不可用", source_message.id, outbound.id, content, provider, model)
        rate = self._rate_decision(db, contact, conversation, exclude_message_id=outbound.id)
        if not rate.allowed:
            outbound.status = MessageStatus.BLOCKED
            outbound.policy_reason = rate.code
            record_delivery_event(db, outbound, stage="FINAL_RATE_LIMIT", status="BLOCKED", error_code=rate.code)
            self._audit(db, AuditEvent.POLICY_DENY, conversation.id, outbound.id, {"code": rate.code, "source": "CONTACT_RELAY"})
            db.commit()
            return PipelineResult(True, False, False, False, rate.code, rate.reason, source_message.id, outbound.id, content, provider, model)
        outbound.status = MessageStatus.QUEUED
        record_delivery_event(db, outbound, stage="QUEUED", status="QUEUED")
        self._audit(db, AuditEvent.MESSAGE_QUEUED, conversation.id, outbound.id, {"source": "CONTACT_RELAY"})
        db.commit()
        result = await connector.send(
            OutboundMessage(
                "QQ_NAPCAT",
                contact.platform_user_id,
                content,
                reply_to_external_id if state.napcat_quote_reply_enabled else "",
                False,
            ),
            SendPermit(contact.platform_user_id, True, True, True, True, True, True),
        )
        if not result.success:
            outbound.status = MessageStatus.FAILED
            outbound.policy_reason = result.error_code or "SEND_FAILED"
            record_delivery_event(
                db,
                outbound,
                stage="PLATFORM_SEND",
                status="FAILED",
                delivery_started=True,
                retry_allowed=False,
                retry_reason="平台调用已经发生，需人工核对对方是否收到，避免重复发送。",
                error_code=outbound.policy_reason,
                detail={"error_detail": result.error_detail or ""},
            )
            self._audit(db, AuditEvent.ERROR, conversation.id, outbound.id, {"code": outbound.policy_reason, "source": "CONTACT_RELAY"})
            db.commit()
            return PipelineResult(True, False, False, False, outbound.policy_reason, result.error_detail or "平台发送失败", source_message.id, outbound.id, content, provider, model)
        outbound.status = MessageStatus.SENT
        outbound.external_message_id = result.external_message_id or outbound.external_message_id
        record_delivery_event(
            db,
            outbound,
            stage="PLATFORM_ACK",
            status="SENT",
            delivery_started=True,
            acknowledged=True,
            detail={"external_message_id": result.external_message_id},
        )
        self._audit(db, AuditEvent.MESSAGE_SENT, conversation.id, outbound.id, {"source": "CONTACT_RELAY"})
        conversation.consecutive_ai_sends += 1
        self.limiter.record(contact.id, utc_now())
        db.commit()
        return PipelineResult(True, True, False, False, "SENT", "平台已确认发送", source_message.id, outbound.id, content, provider, model)

    @staticmethod
    def _private_contact_conversation(db: Session, contact: Contact, account_id: str | None) -> Conversation:
        effective_account_id = contact.account_id or account_id
        external_id = f"napcat:private:{contact.platform_user_id}"
        conversation = db.scalar(
            select(Conversation).where(
                Conversation.platform == "QQ_NAPCAT",
                Conversation.account_id == effective_account_id,
                Conversation.external_id == external_id,
            )
        )
        if conversation is None:
            conversation = Conversation(
                platform="QQ_NAPCAT",
                account_id=effective_account_id,
                external_id=external_id,
                contact_id=contact.id,
            )
            db.add(conversation)
            db.flush()
        elif conversation.contact_id != contact.id:
            conversation.contact_id = contact.id
        return conversation

    async def _handle_admin_command(
        self,
        db: Session,
        state,
        contact: Contact,
        conversation: Conversation,
        event: InboundEvent,
        inbound: Message,
        command: AdminCommand,
        media_rows: list[MessageAttachment],
    ) -> PipelineResult:
        async with self.runtime.send_lock:
            db.refresh(state)
            db.refresh(contact)
            db.refresh(conversation)
            legacy_separator = "|" in command.name or any("|" in item for item in command.arguments)
            if not legacy_separator and is_admin_memory_command(command.name):
                return await self._handle_admin_memory_command(
                    db, state, contact, conversation, event, inbound, command
                )
            if not legacy_separator and is_topic_command(command.name):
                return await self._handle_admin_topic_command(
                    db, state, contact, conversation, event, inbound, command
                )
            if not legacy_separator and is_delivery_command(command.name):
                return await self._handle_admin_delivery_command(
                    db, state, contact, conversation, event, inbound, command, media_rows
                )
            plan = plan_admin_command(db, state, command, account_id=contact.account_id)
            self._audit(
                db,
                "ADMIN_COMMAND_RECEIVED",
                conversation.id,
                inbound.id,
                {"command": command.name, "code": plan.code, "argument_count": len(command.arguments)},
            )
            db.commit()
            delivery: PipelineResult | None = None
            if plan.acknowledge_before_apply:
                delivery = await self._deliver_admin_response(db, state, contact, conversation, event, inbound, plan.response, plan.code)
            apply_admin_plan(db, state, plan, contact=contact, message_id=inbound.id)
            if not plan.acknowledge_before_apply:
                delivery = await self._deliver_admin_response(db, state, contact, conversation, event, inbound, plan.response, plan.code)
        await event_hub.broadcast(
            "admin_command",
            {"conversation_id": conversation.id, "code": plan.code, "state_changed": bool(plan.state_updates)},
        )
        assert delivery is not None
        return PipelineResult(
            True,
            delivery.sent,
            delivery.shadowed,
            False,
            f"ADMIN_{plan.code}",
            "管理员指令已处理" if plan.state_updates else "管理员查询已处理",
            inbound.id,
            delivery.outbound_message_id,
            plan.response,
        )

    async def _handle_admin_memory_command(
        self,
        db: Session,
        state,
        admin: Contact,
        admin_conversation: Conversation,
        event: InboundEvent,
        inbound: Message,
        command: AdminCommand,
    ) -> PipelineResult:
        name = command.name.casefold()
        response = ""
        code = "MEMORY_COMMAND"
        provider: str | None = None
        model: str | None = None
        try:
            if name in MEMORY_IMPORT_CANCEL_COMMANDS:
                batch = active_import_batch(
                    db,
                    admin_contact_id=admin.id,
                    account_id=admin.account_id,
                )
                if batch is None:
                    raise AdminMemoryImportError("MEMORY_IMPORT_NOT_ACTIVE", "当前没有正在收集的聊天截图。")
                target = db.get(Contact, batch.target_contact_id)
                cancel_import_batch(batch, source_message=inbound)
                self._audit(
                    db,
                    "ADMIN_MEMORY_IMPORT_CANCELLED",
                    admin_conversation.id,
                    inbound.id,
                    {"batch_id": batch.id, "target_contact_id": batch.target_contact_id},
                )
                db.commit()
                code = "MEMORY_IMPORT_CANCELLED"
                response = f"已取消为 {target.display_name if target else '该联系人'} 收集的截图；没有写入新记忆。"
            else:
                if state.global_mode == GlobalMode.READ_ONLY:
                    raise AdminMemoryImportError(
                        "MEMORY_READ_ONLY",
                        "当前处于 READ_ONLY，只记录消息但不写入记忆。请先发送 /neko 自动，再重新执行。",
                    )
                if name in DIRECT_MEMORY_COMMANDS:
                    request = parse_direct_memory_request(command.arguments)
                    target = resolve_memory_contact(db, request.target, account_id=admin.account_id)
                    created = add_direct_memory(
                        db,
                        target=target,
                        source_message=inbound,
                        content=request.content,
                    )
                    self._audit(
                        db,
                        "ADMIN_MEMORY_ADDED",
                        admin_conversation.id,
                        inbound.id,
                        {
                            "target_contact_id": target.id,
                            "created": created,
                            "content_sha256": hashlib.sha256(request.content.encode("utf-8")).hexdigest(),
                        },
                    )
                    db.commit()
                    code = "MEMORY_ADDED" if created else "MEMORY_DUPLICATE"
                    response = (
                        f"已把这条信息加入 {target.display_name} 的长期记忆；无需审批，可在后台编辑或删除。"
                        if created
                        else f"{target.display_name} 已有完全相同的记忆，没有重复写入。"
                    )
                elif name in MEMORY_IMPORT_START_COMMANDS:
                    target_text = " ".join(command.arguments).strip()
                    batch, target = start_import_batch(
                        db,
                        admin=admin,
                        source_message=inbound,
                        target_text=target_text,
                    )
                    self._audit(
                        db,
                        "ADMIN_MEMORY_IMPORT_STARTED",
                        admin_conversation.id,
                        inbound.id,
                        {"batch_id": batch.id, "target_contact_id": target.id},
                    )
                    db.commit()
                    code = "MEMORY_IMPORT_STARTED"
                    response = (
                        f"开始收集与 {target.display_name} 有关的聊天截图。现在可以连续发送图片；"
                        "全部发送后使用 /neko 截图结束，放弃请使用 /neko 截图取消。"
                    )
                elif name in MEMORY_IMPORT_END_COMMANDS:
                    batch = active_import_batch(
                        db,
                        admin_contact_id=admin.id,
                        account_id=admin.account_id,
                    )
                    if batch is None:
                        raise AdminMemoryImportError("MEMORY_IMPORT_NOT_ACTIVE", "当前没有正在收集的聊天截图。")
                    target = db.get(Contact, batch.target_contact_id)
                    if target is None:
                        raise AdminMemoryImportError("MEMORY_TARGET_NOT_FOUND", "这批截图对应的联系人已不存在。")
                    if batch.screenshot_count == 0:
                        raise AdminMemoryImportError(
                            "MEMORY_IMPORT_EMPTY",
                            "这批记录还没有截图；请先发送图片，或使用 /neko 截图取消。",
                        )
                    evidence, usable_count, evidence_chars = import_evidence(db, batch)
                    if not evidence:
                        raise AdminMemoryImportError(
                            "MEMORY_IMPORT_NO_OCR",
                            "已保存截图，但没有得到可用的图片识别文本。批次仍保持开启；请检查媒体模型后重试结束，或取消。",
                        )
                    try:
                        llm = await self.gateway.chat(
                            build_import_summary_messages(target=target, evidence=evidence),
                            preferred_models=("kimi-k3",),
                        )
                    except AllProvidersFailed as exc:
                        batch.last_error = "ALL_PROVIDERS_FAILED"
                        db.commit()
                        raise AdminMemoryImportError(
                            "MEMORY_IMPORT_MODEL_FAILED",
                            "截图已保留，但当前所有模型均不可用；批次仍保持开启，可稍后再次发送 /neko 截图结束。",
                        ) from exc
                    extraction = parse_import_extraction(llm.content)
                    first_item = db.scalar(
                        select(AdminMemoryImportItem)
                        .where(AdminMemoryImportItem.batch_id == batch.id)
                        .order_by(AdminMemoryImportItem.position.asc())
                        .limit(1)
                    )
                    memory_count = store_approved_memories(
                        db,
                        target=target,
                        source_message_id=first_item.message_id if first_item else inbound.id,
                        memories=extraction.memories,
                    )
                    complete_import_batch(
                        batch,
                        source_message=inbound,
                        extraction=extraction,
                        memory_count=memory_count,
                        evidence_chars=evidence_chars,
                        provider=llm.provider,
                        model=llm.model,
                    )
                    self._audit(
                        db,
                        "ADMIN_MEMORY_IMPORT_COMPLETED",
                        admin_conversation.id,
                        inbound.id,
                        {
                            "batch_id": batch.id,
                            "target_contact_id": target.id,
                            "screenshot_count": batch.screenshot_count,
                            "usable_screenshot_count": usable_count,
                            "memory_count": memory_count,
                            "provider": llm.provider,
                            "model": llm.model,
                        },
                    )
                    db.commit()
                    code = "MEMORY_IMPORT_COMPLETED"
                    provider = llm.provider
                    model = llm.model
                    response = (
                        f"已整理 {batch.screenshot_count} 张截图，其中 {usable_count} 张有可用识别文本；"
                        f"为 {target.display_name} 写入 {memory_count} 条新记忆。无需审批，可在后台编辑或删除。\n"
                        f"实际模型：{llm.provider} / {llm.model}"
                    )
                else:
                    raise AdminMemoryImportError("MEMORY_COMMAND_UNKNOWN", "没有识别这个记忆指令。")

            delivery = await self._deliver_admin_response(
                db,
                state,
                admin,
                admin_conversation,
                event,
                inbound,
                response,
                code,
            )
            await event_hub.broadcast(
                "admin_memory_changed",
                {"conversation_id": admin_conversation.id, "code": code},
            )
            return PipelineResult(
                True,
                delivery.sent,
                delivery.shadowed,
                False,
                f"ADMIN_{code}",
                response,
                inbound.id,
                delivery.outbound_message_id,
                response,
                provider,
                model,
            )
        except AdminMemoryImportError as exc:
            db.rollback()
            self._audit(
                db,
                "ADMIN_MEMORY_COMMAND_REJECTED",
                admin_conversation.id,
                inbound.id,
                {"code": exc.code, "command": command.name},
                level="WARNING",
            )
            db.commit()
            response = f"记忆操作未执行：{exc}"
            delivery = await self._deliver_admin_response(
                db,
                state,
                admin,
                admin_conversation,
                event,
                inbound,
                response,
                exc.code,
            )
            return PipelineResult(
                True,
                delivery.sent,
                delivery.shadowed,
                False,
                f"ADMIN_{exc.code}",
                response,
                inbound.id,
                delivery.outbound_message_id,
                response,
            )

    async def _handle_admin_topic_command(
        self,
        db: Session,
        state,
        admin: Contact,
        admin_conversation: Conversation,
        event: InboundEvent,
        inbound: Message,
        command: AdminCommand,
    ) -> PipelineResult:
        try:
            try:
                request = parse_topic_request(command.arguments)
            except ValueError as exc:
                raise AdminDeliveryError("TOPIC_USAGE", str(exc)) from exc
            target = resolve_target(db, request.target, account_id=admin.account_id)
            if target.id == admin.id:
                raise AdminDeliveryError("TOPIC_SELF_TARGET", "管理员本人无需使用主动找人指令，请直接正常聊天。")
            if not target.ai_enabled:
                raise AdminDeliveryError("TARGET_AI_DISABLED", "目标联系人已关闭 AI，不能发起模型生成的主动对话。")
            if state.release_gate != "LIVE":
                raise AdminDeliveryError("LIVE_REQUIRED", "主动话题对话只允许在 LIVE 门禁下执行。")
            if state.kill_switch or state.global_mode != GlobalMode.AUTO:
                raise AdminDeliveryError("RUNTIME_BLOCKED", "当前急停或运行模式禁止发起主动对话。")
            if contains_secret(request.topic):
                raise AdminDeliveryError("TOPIC_SECRET", "话题中疑似包含密钥或令牌，未发送到模型。")

            active_account = get_active_account(db, "QQ_NAPCAT")
            active_config = (active_account.config or {}) if active_account else {}
            if not (
                active_account
                and active_account.enabled
                and active_account.credential_id
                and active_config.get("api_base")
                and active_config.get("risk_acknowledged")
            ):
                raise AdminDeliveryError("CHANNEL_DISABLED", "当前没有唯一、完整且已确认风险的 NapCat 活动账号。")
            if target.account_id and target.account_id != active_account.id:
                raise AdminDeliveryError("ACCOUNT_ISOLATION_MISMATCH", "目标联系人不属于当前活动 NapCat 账号，已阻止跨账号发送。")
            if target.account_id is None:
                target.account_id = active_account.id
            connector = self.connectors.get("QQ_NAPCAT")
            if connector is None:
                raise AdminDeliveryError("CONNECTOR_MISSING", "NapCat 发送通道不可用。")

            target_conversation = db.scalar(
                select(Conversation).where(
                    Conversation.platform == "QQ_NAPCAT",
                    Conversation.account_id == active_account.id,
                    Conversation.external_id == f"napcat:private:{target.platform_user_id}",
                )
            )
            if target_conversation is None:
                target_conversation = db.scalar(
                    select(Conversation).where(
                        Conversation.platform == "QQ_NAPCAT",
                        Conversation.account_id.is_(None),
                        Conversation.external_id == f"napcat:private:{target.platform_user_id}",
                    )
                )
                if target_conversation is not None:
                    target_conversation.account_id = active_account.id
            if target_conversation is None:
                target_conversation = Conversation(
                    platform="QQ_NAPCAT",
                    account_id=active_account.id,
                    external_id=f"napcat:private:{target.platform_user_id}",
                    contact_id=target.id,
                )
                db.add(target_conversation)
                db.flush()
            elif target_conversation.contact_id != target.id:
                target_conversation.contact_id = target.id

            now = utc_now()
            topic_event = InboundEvent(
                platform="QQ_NAPCAT",
                account_id=active_account.id,
                message_id=f"admin-topic:{inbound.id}",
                conversation_id=target_conversation.external_id,
                sender_id=target.platform_user_id,
                sender_name=target.display_name,
                content=request.topic,
                timestamp=now,
                raw={"event_type": "ADMIN_PROACTIVE_TOPIC"},
            )
            decision = self.policy.evaluate(
                self._policy_input(
                    db,
                    state,
                    target,
                    None,
                    target_conversation,
                    topic_event,
                    now=now,
                    inbound_content=request.topic,
                )
            )
            if not decision.allowed:
                raise AdminDeliveryError(decision.code, decision.reason)
            rate = self._rate_decision(db, target, target_conversation)
            if not rate.allowed:
                raise AdminDeliveryError(rate.code, rate.reason)

            persona = db.get(PersonaProfile, 1) or PersonaProfile(id=1)
            if db.get(PersonaProfile, 1) is None:
                db.add(persona)
                db.flush()
            memories = list(
                db.scalars(
                    select(Memory)
                    .where(
                        Memory.contact_id == target.id,
                        Memory.review_status == "APPROVED",
                        or_(Memory.expires_at.is_(None), Memory.expires_at > now),
                    )
                    .order_by(Memory.pinned.desc(), Memory.created_at.desc())
                    .limit(12)
                )
            ) if target.memory_enabled else []
            recent = list(
                reversed(
                    list(
                        db.scalars(
                            select(Message)
                            .where(Message.conversation_id == target_conversation.id)
                            .order_by(Message.created_at.desc())
                            .limit(8)
                        )
                    )
                )
            )
            llm_messages = build_topic_messages(
                target=target,
                topic=request.topic,
                persona=persona,
                memories=memories,
                recent=recent,
                knowledge_context=search_knowledge(db, request.topic),
            )
            topic_preferred_models = ("kimi-k3",)
            route_decision = self.gateway.route_decision(
                llm_messages,
                preferred_models=topic_preferred_models,
            )
            self._audit(
                db,
                AuditEvent.LLM_REQUEST,
                target_conversation.id,
                inbound.id,
                {
                    "source": "ADMIN_PROACTIVE_TOPIC",
                    "admin_contact_id": admin.id,
                    "target_contact_id": target.id,
                    "topic_sha256": hashlib.sha256(request.topic.encode("utf-8")).hexdigest(),
                    "provider_order": [provider.name for provider in route_decision.providers],
                    "preferred_models": list(topic_preferred_models),
                    "route_reason": route_decision.reason,
                },
            )
            db.commit()
            try:
                llm = await self.gateway.chat(
                    llm_messages,
                    preferred_models=topic_preferred_models,
                )
            except AllProvidersFailed as exc:
                self._incident(db, "LLM_PROVIDER_FAILURE", "主动话题生成失败", str(exc))
                db.commit()
                raise AdminDeliveryError("ALL_PROVIDERS_FAILED", "所有模型均不可用，未向联系人发送消息。") from exc

            full_content = llm.content.strip()
            opener_regenerated = False
            if not topic_opener_discloses_owner(full_content):
                try:
                    llm = await self.gateway.chat(
                        build_topic_repair_messages(topic=request.topic, generated=full_content),
                        preferred_models=topic_preferred_models,
                    )
                except AllProvidersFailed as exc:
                    raise AdminDeliveryError(
                        "TOPIC_OPENER_REPAIR_FAILED",
                        "模型没有自然说明主人委托，改写也失败了，因此没有向联系人发送。",
                    ) from exc
                full_content = llm.content.strip()
                opener_regenerated = True
                if not topic_opener_discloses_owner(full_content):
                    raise AdminDeliveryError(
                        "TOPIC_OPENER_MISSING_OWNER_CONTEXT",
                        "模型连续两次都没有自然说明主人委托，因此没有向联系人发送。",
                    )
            provider_style = normalize_provider_reply(
                content=full_content,
                provider=llm.provider,
                inbound_content=request.topic,
                recent=recent,
            )
            full_content = provider_style.content
            if provider_style.changed:
                self._audit(
                    db,
                    "PROVIDER_STYLE_NORMALIZED",
                    target_conversation.id,
                    inbound.id,
                    {
                        "provider": llm.provider,
                        "model": llm.model,
                        "reason": provider_style.reason,
                        "removed_emoji_count": provider_style.removed_emoji_count,
                        "source": "ADMIN_PROACTIVE_TOPIC",
                    },
                )
            guard = self.guard.inspect(
                full_content,
                request.topic,
                strict=target.importance == Importance.IMPORTANT,
                truncate_limit=None,
            )
            if not guard.allowed:
                raise AdminDeliveryError(guard.code, guard.reason)
            outbound = Message(
                platform="QQ_NAPCAT",
                account_id=active_account.id,
                external_message_id=f"admin-topic:{inbound.id}:{uuid4()}",
                sender_id="QQ_NAPCAT:AI",
                receiver_id=target.platform_user_id,
                event_at=now,
                conversation_id=target_conversation.id,
                contact_id=target.id,
                direction=MessageDirection.OUTBOUND,
                author=MessageAuthor.AI,
                content=guard.content,
                message_type="TEXT",
                status=MessageStatus.GENERATED,
                provider=llm.provider,
                model=llm.model,
                input_tokens=llm.input_tokens,
                output_tokens=llm.output_tokens,
                latency_ms=llm.latency_ms,
                raw_envelope={
                    "source": "ADMIN_PROACTIVE_TOPIC",
                    "admin_message_id": inbound.id,
                    "topic": request.topic,
                    "opener_regenerated": opener_regenerated,
                },
            )
            db.add(outbound)
            db.flush()
            text_delivery = prepare_text_delivery(
                db,
                settings=self.settings,
                message=outbound,
                content=guard.content,
            )
            if text_delivery.overflowed:
                self._audit(
                    db,
                    "AI_TEXT_OVERFLOW_FILE_CREATED",
                    target_conversation.id,
                    outbound.id,
                    {
                        "source": "ADMIN_PROACTIVE_TOPIC",
                        "format": "TXT",
                        "full_characters": text_delivery.full_characters,
                    },
                )
            outbound.status = MessageStatus.QUEUED
            self._audit(
                db,
                AuditEvent.MESSAGE_QUEUED,
                target_conversation.id,
                outbound.id,
                {
                    "source": "ADMIN_PROACTIVE_TOPIC",
                    "admin_contact_id": admin.id,
                    "target_contact_id": target.id,
                },
            )
            db.commit()

            db.refresh(state)
            db.refresh(target)
            db.refresh(target_conversation)
            final_decision = self.policy.evaluate(
                self._policy_input(
                    db,
                    state,
                    target,
                    None,
                    target_conversation,
                    topic_event,
                    now=utc_now(),
                    inbound_content=request.topic,
                )
            )
            if not final_decision.allowed:
                outbound.status = MessageStatus.CANCELLED
                outbound.policy_reason = final_decision.code
                db.commit()
                raise AdminDeliveryError(final_decision.code, "发送前复检未通过，主动话题已取消。")
            final_rate = self._rate_decision(db, target, target_conversation, exclude_message_id=outbound.id)
            if not final_rate.allowed:
                outbound.status = MessageStatus.BLOCKED
                outbound.policy_reason = final_rate.code
                db.commit()
                raise AdminDeliveryError(final_rate.code, final_rate.reason)
            result = await connector.send(
                OutboundMessage(
                    "QQ_NAPCAT",
                    target.platform_user_id,
                    text_delivery.content,
                    "",
                    False,
                    text_delivery.attachments,
                ),
                SendPermit(target.platform_user_id, True, True, True, True, True, True),
            )
            if not result.success:
                outbound.status = MessageStatus.FAILED
                outbound.policy_reason = result.error_code or "SEND_FAILED"
                self._audit(
                    db,
                    AuditEvent.ERROR,
                    target_conversation.id,
                    outbound.id,
                    {"code": outbound.policy_reason, "source": "ADMIN_PROACTIVE_TOPIC"},
                    level="ERROR",
                )
                db.commit()
                response = f"主动找 {target.display_name} 失败：{outbound.policy_reason}。没有自动重试，避免重复发送。"
            else:
                outbound.status = MessageStatus.SENT
                outbound.external_message_id = result.external_message_id or outbound.external_message_id
                target_conversation.last_active_at = now
                target_conversation.managed_rounds += 1
                target_conversation.consecutive_ai_sends += 1
                self.limiter.record(target.id, utc_now())
                refresh_conversation_summary(db, target_conversation)
                self._audit(
                    db,
                    AuditEvent.MESSAGE_SENT,
                    target_conversation.id,
                    outbound.id,
                    {
                        "source": "ADMIN_PROACTIVE_TOPIC",
                        "admin_contact_id": admin.id,
                        "target_contact_id": target.id,
                        "provider": llm.provider,
                        "model": llm.model,
                        "overflow_file": text_delivery.overflowed,
                    },
                )
                db.commit()
                response = (
                    f"已经让橙蓝去找 {target.display_name} 聊「{request.topic}」啦。\n"
                    f"实际模型：{llm.provider} / {llm.model}"
                    f"{'；完整长回复已作为 TXT 附件发送。' if text_delivery.overflowed else ''}"
                )

            acknowledgement = await self._deliver_admin_response(
                db,
                state,
                admin,
                admin_conversation,
                event,
                inbound,
                response,
                "PROACTIVE_TOPIC",
            )
            await event_hub.broadcast(
                "message_sent",
                {"message_id": outbound.id, "conversation_id": target_conversation.id},
            )
            return PipelineResult(
                True,
                result.success,
                False,
                False,
                "ADMIN_PROACTIVE_TOPIC_SENT" if result.success else "ADMIN_PROACTIVE_TOPIC_FAILED",
                response,
                inbound.id,
                outbound.id,
                response,
                provider=llm.provider,
                model=llm.model,
            )
        except AdminDeliveryError as exc:
            db.rollback()
            self._audit(
                db,
                "ADMIN_PROACTIVE_TOPIC_REJECTED",
                admin_conversation.id,
                inbound.id,
                {"code": exc.code},
                level="WARNING",
            )
            db.commit()
            response = f"主动话题未执行：{exc}"
            acknowledgement = await self._deliver_admin_response(
                db,
                state,
                admin,
                admin_conversation,
                event,
                inbound,
                response,
                "PROACTIVE_TOPIC_REJECTED",
            )
            return PipelineResult(
                True,
                False,
                acknowledgement.shadowed,
                False,
                f"ADMIN_{exc.code}",
                response,
                inbound.id,
                acknowledgement.outbound_message_id,
                response,
            )

    async def _handle_admin_delivery_command(
        self,
        db: Session,
        state,
        admin: Contact,
        admin_conversation: Conversation,
        event: InboundEvent,
        inbound: Message,
        command: AdminCommand,
        media_rows: list[MessageAttachment],
    ) -> PipelineResult:
        name = command.name.casefold()
        try:
            if name in {"取消发送", "cancel-send"}:
                if not command.arguments:
                    raise AdminDeliveryError("CODE_REQUIRED", "请填写取消代码。")
                draft = cancel_delivery_draft(db, admin=admin, code=command.arguments[0])
                self._audit(db, "ADMIN_DELIVERY_CANCELLED", draft.conversation_id, draft.id, {"requested_by": admin.id})
                db.commit()
                response = "待发送内容已取消。"
                delivery = await self._deliver_admin_response(db, state, admin, admin_conversation, event, inbound, response, "DELIVERY_CANCELLED")
                return PipelineResult(True, delivery.sent, delivery.shadowed, False, "ADMIN_DELIVERY_CANCELLED", response, inbound.id, delivery.outbound_message_id, response)

            if name in {"确认发送", "confirm-send"}:
                if not command.arguments:
                    raise AdminDeliveryError("CODE_REQUIRED", "请填写确认代码。")
                draft = find_delivery_draft(db, admin=admin, code=command.arguments[0])
                target = db.get(Contact, draft.contact_id)
                target_conversation = db.get(Conversation, draft.conversation_id)
                if target is None or target_conversation is None or target.platform != "QQ_NAPCAT":
                    raise AdminDeliveryError("TARGET_UNAVAILABLE", "目标联系人或会话已不存在。")
                if not target.whitelisted:
                    raise AdminDeliveryError("TARGET_NOT_WHITELISTED", "目标联系人已不在白名单，发送已阻止。")
                if state.release_gate != "LIVE":
                    raise AdminDeliveryError("LIVE_REQUIRED", "主动发送只允许在 LIVE 门禁下执行。")
                if state.kill_switch or state.global_mode != GlobalMode.AUTO:
                    raise AdminDeliveryError("RUNTIME_BLOCKED", "当前急停或运行模式禁止主动发送。")
                active_account = get_active_account(db, "QQ_NAPCAT")
                active_config = (active_account.config or {}) if active_account else {}
                if not (
                    active_account
                    and active_account.enabled
                    and active_account.credential_id
                    and active_config.get("api_base")
                    and active_config.get("risk_acknowledged")
                ):
                    raise AdminDeliveryError("CHANNEL_DISABLED", "当前没有唯一、完整且已确认风险的 NapCat 活动账号。")
                if target.account_id and target.account_id != active_account.id:
                    raise AdminDeliveryError("ACCOUNT_ISOLATION_MISMATCH", "目标联系人不属于当前活动 NapCat 账号，已阻止跨账号发送。")
                connector = self.connectors.get("QQ_NAPCAT")
                if connector is None:
                    raise AdminDeliveryError("CONNECTOR_MISSING", "NapCat 发送通道不可用。")
                guard = None
                if draft.content:
                    guard = self.guard.inspect(draft.content, "", strict=target.importance == Importance.IMPORTANT)
                    if not guard.allowed:
                        raise AdminDeliveryError(guard.code, guard.reason)
                    draft.content = guard.content
                attachments = outbound_attachments(db, draft, self.settings)
                if not draft.content and not attachments:
                    raise AdminDeliveryError("EMPTY_DELIVERY", "待发送内容为空。")
                rate = self._rate_decision(db, target, target_conversation, exclude_message_id=draft.id)
                if not rate.allowed:
                    raise AdminDeliveryError(rate.code, rate.reason)
                draft.status = MessageStatus.QUEUED
                draft.model = "ADMIN_CONFIRMED"
                self._audit(
                    db,
                    AuditEvent.MESSAGE_QUEUED,
                    draft.conversation_id,
                    draft.id,
                    {"source": "ADMIN_DELIVERY", "target_contact_id": target.id, "attachment_count": len(attachments)},
                )
                db.commit()
                result = await connector.send(
                    OutboundMessage(
                        "QQ_NAPCAT",
                        target.platform_user_id,
                        draft.content,
                        "",
                        False,
                        attachments,
                    ),
                    SendPermit(target.platform_user_id, target.whitelisted, True, True, True, True, True),
                )
                if not result.success:
                    draft.status = MessageStatus.FAILED
                    draft.policy_reason = result.error_code or "SEND_FAILED"
                    self._audit(db, AuditEvent.ERROR, draft.conversation_id, draft.id, {"code": draft.policy_reason, "source": "ADMIN_DELIVERY"}, level="ERROR")
                    db.commit()
                    response = f"主动发送失败：{draft.policy_reason}。没有自动重试，避免重复发送。"
                else:
                    draft.status = MessageStatus.SENT
                    draft.external_message_id = result.external_message_id or draft.external_message_id
                    target_conversation.consecutive_ai_sends += 1
                    self.limiter.record(target.id, utc_now())
                    self._audit(
                        db,
                        AuditEvent.MESSAGE_SENT,
                        draft.conversation_id,
                        draft.id,
                        {"source": "ADMIN_DELIVERY", "target_contact_id": target.id, "attachment_count": len(attachments)},
                    )
                    db.commit()
                    response = f"已向白名单联系人 {target.display_name} 发送完成。"
                await self._deliver_admin_response(
                    db, state, admin, admin_conversation, event, inbound, response, "DELIVERY_CONFIRMED"
                )
                return PipelineResult(True, result.success, False, False, "ADMIN_DELIVERY_SENT" if result.success else "ADMIN_DELIVERY_FAILED", response, inbound.id, draft.id, response)

            sources = list(media_rows)
            if not sources and event.reply_to_message_id:
                referenced = db.scalar(
                    select(Message).where(
                        Message.platform == event.platform,
                        Message.external_message_id == event.reply_to_message_id,
                        Message.conversation_id == admin_conversation.id,
                    )
                )
                if referenced is not None:
                    sources = list(
                        db.scalars(
                            select(MessageAttachment)
                            .where(MessageAttachment.message_id == referenced.id)
                            .order_by(MessageAttachment.segment_index.asc())
                        )
                    )
            prepared = create_delivery_draft(
                db,
                settings=self.settings,
                admin=admin,
                source_message=inbound,
                command_name=command.name,
                arguments=command.arguments,
                source_attachments=sources,
                max_file_mb=state.media_max_file_mb,
            )
            self._audit(
                db,
                "ADMIN_DELIVERY_DRAFTED",
                prepared.message.conversation_id,
                prepared.message.id,
                {"target_contact_id": prepared.target.id, "attachment_count": prepared.attachment_count},
            )
            db.commit()
            kind = "文件" if prepared.attachment_count else "文本"
            response = (
                f"待发送预览：{kind} → {prepared.target.display_name}（{prepared.target.platform_user_id}）\n"
                f"10 分钟内发送 /neko 确认发送 {prepared.code}\n"
                f"取消请发送 /neko 取消发送 {prepared.code}"
            )
            delivery = await self._deliver_admin_response(db, state, admin, admin_conversation, event, inbound, response, "DELIVERY_DRAFTED")
            return PipelineResult(True, delivery.sent, delivery.shadowed, False, "ADMIN_DELIVERY_DRAFTED", response, inbound.id, prepared.message.id, response)
        except AdminDeliveryError as exc:
            db.rollback()
            self._audit(db, "ADMIN_DELIVERY_REJECTED", admin_conversation.id, inbound.id, {"code": exc.code}, level="WARNING")
            db.commit()
            response = f"主动发送未执行：{exc}"
            delivery = await self._deliver_admin_response(db, state, admin, admin_conversation, event, inbound, response, "DELIVERY_REJECTED")
            return PipelineResult(True, delivery.sent, delivery.shadowed, False, f"ADMIN_{exc.code}", response, inbound.id, delivery.outbound_message_id, response)

    async def _deliver_admin_response(
        self,
        db: Session,
        state,
        contact: Contact,
        conversation: Conversation,
        event: InboundEvent,
        inbound: Message,
        content: str,
        command_code: str,
    ) -> PipelineResult:
        outbound = Message(
            platform=event.platform,
            account_id=event.account_id,
            external_message_id=f"admin:{event.message_id}:{uuid4()}",
            sender_id=f"{event.platform}:SYSTEM",
            receiver_id=contact.platform_user_id,
            conversation_id=conversation.id,
            contact_id=contact.id,
            direction=MessageDirection.OUTBOUND,
            author=MessageAuthor.SYSTEM,
            content=content,
            message_type="TEXT",
            status=MessageStatus.GENERATED,
            reply_to_external_id=event.message_id,
            provider="LOCAL_COMMAND",
            model="DETERMINISTIC",
        )
        db.add(outbound)
        db.flush()
        if state.release_gate != "LIVE":
            outbound.status = MessageStatus.SHADOWED
            outbound.policy_reason = "ADMIN_RESPONSE_NOT_LIVE"
            self._audit(db, AuditEvent.MESSAGE_SHADOWED, conversation.id, outbound.id, {"source": "ADMIN_COMMAND", "code": command_code})
            db.commit()
            return PipelineResult(True, False, True, False, "SHADOWED", "管理员指令已执行；非 LIVE 不发送确认", inbound.id, outbound.id, content)
        if state.kill_switch or state.global_mode != GlobalMode.AUTO:
            outbound.status = MessageStatus.BLOCKED
            outbound.policy_reason = "KILL_SWITCH" if state.kill_switch else f"GLOBAL_{state.global_mode}"
            self._audit(db, AuditEvent.POLICY_DENY, conversation.id, outbound.id, {"code": outbound.policy_reason, "source": "ADMIN_COMMAND"})
            db.commit()
            return PipelineResult(True, False, False, False, outbound.policy_reason, "管理员指令已执行；当前安全状态禁止发送确认", inbound.id, outbound.id, content)
        connector = self.connectors.get(event.platform.upper())
        if connector is None:
            outbound.status = MessageStatus.FAILED
            outbound.policy_reason = "CONNECTOR_MISSING"
            db.commit()
            return PipelineResult(True, False, False, False, "CONNECTOR_MISSING", "管理员指令已执行；确认消息通道不可用", inbound.id, outbound.id, content)
        rate = self._rate_decision(db, contact, conversation, exclude_message_id=outbound.id)
        if not rate.allowed:
            outbound.status = MessageStatus.BLOCKED
            outbound.policy_reason = rate.code
            self._audit(db, AuditEvent.POLICY_DENY, conversation.id, outbound.id, {"code": rate.code, "source": "ADMIN_COMMAND"})
            db.commit()
            return PipelineResult(True, False, False, False, rate.code, "管理员指令已执行；确认消息被频率保护拦截", inbound.id, outbound.id, content)
        outbound.status = MessageStatus.QUEUED
        self._audit(db, AuditEvent.MESSAGE_QUEUED, conversation.id, outbound.id, {"source": "ADMIN_COMMAND", "code": command_code})
        db.commit()
        result = await connector.send(
            OutboundMessage(
                event.platform,
                contact.platform_user_id,
                content,
                event.message_id if state.napcat_quote_reply_enabled and event.platform.upper() == "QQ_NAPCAT" else "",
                False,
            ),
            SendPermit(contact.platform_user_id, contact.whitelisted, True, True, True, True, True),
        )
        if not result.success:
            outbound.status = MessageStatus.FAILED
            outbound.policy_reason = result.error_code or "SEND_FAILED"
            self._audit(db, AuditEvent.ERROR, conversation.id, outbound.id, {"code": outbound.policy_reason, "source": "ADMIN_COMMAND"}, level="ERROR")
            db.commit()
            return PipelineResult(True, False, False, False, outbound.policy_reason, "管理员指令已执行；确认消息发送失败", inbound.id, outbound.id, content)
        outbound.status = MessageStatus.SENT
        outbound.external_message_id = result.external_message_id or outbound.external_message_id
        conversation.consecutive_ai_sends += 1
        self.limiter.record(contact.id, utc_now())
        self._audit(db, AuditEvent.MESSAGE_SENT, conversation.id, outbound.id, {"source": "ADMIN_COMMAND", "code": command_code})
        db.commit()
        return PipelineResult(True, True, False, False, "SENT", "管理员指令已执行并回复确认", inbound.id, outbound.id, content)

    def _rate_decision(
        self,
        db: Session,
        contact: Contact,
        conversation: Conversation,
        *,
        exclude_message_id: str | None = None,
    ):
        now = utc_now()
        start_of_day = beijing_start_of_day_utc(now)
        statuses = [MessageStatus.SENT, MessageStatus.QUEUED]
        daily_query = select(func.count(Message.id)).where(
            Message.author.in_([MessageAuthor.AI, MessageAuthor.SYSTEM]),
            Message.status.in_(statuses),
            Message.created_at >= start_of_day,
        )
        contact_query = select(func.count(Message.id)).where(
            Message.contact_id == contact.id,
            Message.author.in_([MessageAuthor.AI, MessageAuthor.SYSTEM]),
            Message.status.in_(statuses),
            Message.created_at > now - timedelta(minutes=1),
        )
        if exclude_message_id:
            daily_query = daily_query.where(Message.id != exclude_message_id)
            contact_query = contact_query.where(Message.id != exclude_message_id)
        daily_reserved = db.scalar(daily_query) or 0
        recent_contact_reserved = db.scalar(contact_query) or 0
        return self.limiter.allow(
            contact.id,
            now=now,
            daily_sent=daily_reserved,
            consecutive_sends=conversation.consecutive_ai_sends,
            recent_contact_sent=recent_contact_reserved,
        )

    def _manual_rate_decision(
        self,
        db: Session,
        contact: Contact,
        *,
        exclude_message_id: str | None = None,
    ):
        """Apply total send limits without treating AI consecutive rounds as human sends."""
        now = utc_now()
        start_of_day = beijing_start_of_day_utc(now)
        statuses = [MessageStatus.SENT, MessageStatus.QUEUED]
        daily_query = select(func.count(Message.id)).where(
            Message.direction == MessageDirection.OUTBOUND,
            Message.status.in_(statuses),
            Message.created_at >= start_of_day,
        )
        contact_query = select(func.count(Message.id)).where(
            Message.contact_id == contact.id,
            Message.direction == MessageDirection.OUTBOUND,
            Message.status.in_(statuses),
            Message.created_at > now - timedelta(minutes=1),
        )
        if exclude_message_id:
            daily_query = daily_query.where(Message.id != exclude_message_id)
            contact_query = contact_query.where(Message.id != exclude_message_id)
        return self.limiter.allow(
            contact.id,
            now=now,
            daily_sent=db.scalar(daily_query) or 0,
            consecutive_sends=0,
            recent_contact_sent=db.scalar(contact_query) or 0,
        )

    def _policy_input(
        self,
        db: Session,
        state,
        contact: Contact,
        group: ChatGroup | None,
        conversation: Conversation,
        event: InboundEvent,
        now: datetime | None = None,
        inbound_content: str | None = None,
    ) -> PolicyInput:
        channel_enabled = True
        if state.release_gate == "LIVE" and event.platform.upper() != "SIMULATOR":
            platform = event.platform.upper()
            account = db.get(Account, event.account_id) if event.account_id else get_active_account(db, platform)
            config = (account.config or {}) if account else {}
            if platform == "WECHAT_AUTOWX":
                # This strategy intentionally has no send primitive, even when enabled.
                channel_enabled = False
            elif platform == "QQ":
                channel_enabled = bool(account and account.enabled and account.credential_id and config.get("app_id"))
            elif platform == "QQ_NAPCAT":
                channel_enabled = bool(
                    account
                    and account.enabled
                    and account.credential_id
                    and config.get("api_base")
                    and config.get("risk_acknowledged")
                )
            else:
                channel_enabled = bool(account and account.enabled)
        return PolicyInput(
            global_mode=state.global_mode,
            kill_switch=state.kill_switch,
            whitelisted=contact.whitelisted,
            ai_enabled=contact.ai_enabled,
            importance=contact.importance,
            channel_enabled=channel_enabled,
            relationship_label=contact.relationship_label,
            is_group=event.is_group,
            group_allowed=bool(group and group.allowed and group.ai_enabled),
            mentioned_user=event.mentioned_user,
            conversation_mode=conversation.mode,
            human_until=conversation.human_until,
            cooldown_until=conversation.cooldown_until,
            managed_rounds=conversation.managed_rounds,
            now=now or event.timestamp,
            auto_start=_parse_time(contact.reply_auto_start or state.live_auto_start or self.settings.auto_start),
            auto_end=_parse_time(contact.reply_auto_end or state.live_auto_end or self.settings.auto_end),
            # SHADOW has no send primitive, so it may exercise the full model chain at any time.
            # LIVE applies the user's explicit switch and rechecks it immediately before every real send.
            enforce_time_window=state.release_gate == "LIVE" and (
                state.live_time_window_enabled
                if contact.reply_time_window_enabled is None
                else contact.reply_time_window_enabled
            ),
            max_managed_rounds=self.settings.conversation_stop_rounds,
            inbound_content=inbound_content if inbound_content is not None else event.content,
        )

    @staticmethod
    def _contact(db: Session, event: InboundEvent) -> Contact:
        contact = db.scalar(
            select(Contact).where(
                Contact.platform == event.platform,
                Contact.account_id == event.account_id,
                Contact.platform_user_id == event.sender_id,
            )
        )
        if contact is None and event.account_id:
            contact = db.scalar(
                select(Contact).where(
                    Contact.platform == event.platform,
                    Contact.account_id.is_(None),
                    Contact.platform_user_id == event.sender_id,
                )
            )
            if contact is not None:
                contact.account_id = event.account_id
        if contact is None:
            contact = Contact(
                platform=event.platform,
                account_id=event.account_id,
                platform_user_id=event.sender_id,
                display_name=event.sender_name or "陌生人",
                relationship_label="陌生人",
                whitelisted=False,
                importance=Importance.MANUAL_ONLY,
            )
            db.add(contact)
            db.flush()
        return contact

    @staticmethod
    def _group(db: Session, event: InboundEvent) -> ChatGroup | None:
        if not event.is_group or not event.group_id:
            return None
        group = db.scalar(
            select(ChatGroup).where(
                ChatGroup.platform == event.platform,
                ChatGroup.account_id == event.account_id,
                ChatGroup.platform_group_id == event.group_id,
            )
        )
        if group is None and event.account_id:
            group = db.scalar(
                select(ChatGroup).where(
                    ChatGroup.platform == event.platform,
                    ChatGroup.account_id.is_(None),
                    ChatGroup.platform_group_id == event.group_id,
                )
            )
            if group is not None:
                group.account_id = event.account_id
        if group is None:
            group = ChatGroup(
                platform=event.platform,
                account_id=event.account_id,
                platform_group_id=event.group_id,
                display_name=event.group_name or "未命名群聊",
            )
            db.add(group)
            db.flush()
        return group

    @staticmethod
    def _conversation(db: Session, event: InboundEvent, contact: Contact, group: ChatGroup | None) -> Conversation:
        conversation = db.scalar(
            select(Conversation).where(
                Conversation.platform == event.platform,
                Conversation.account_id == event.account_id,
                Conversation.external_id == event.conversation_id,
            )
        )
        if conversation is None and event.account_id:
            conversation = db.scalar(
                select(Conversation).where(
                    Conversation.platform == event.platform,
                    Conversation.account_id.is_(None),
                    Conversation.external_id == event.conversation_id,
                )
            )
            if conversation is not None:
                conversation.account_id = event.account_id
        if conversation is None:
            conversation = Conversation(
                platform=event.platform,
                account_id=event.account_id,
                external_id=event.conversation_id,
                contact_id=contact.id,
                group_id=group.id if group else None,
            )
            db.add(conversation)
            db.flush()
        return conversation

    @staticmethod
    def _audit(db: Session, event: str, conversation_id: str | None = None, message_id: str | None = None, detail: dict | None = None, level: str = "INFO") -> None:
        db.add(AuditLog(event=str(event), level=level, conversation_id=conversation_id, message_id=message_id, detail=detail or {}))

    async def _maybe_trip_failure_circuit(self, db: Session, kind: str, conversation_id: str, message_id: str) -> bool:
        db.flush()
        cutoff = utc_now() - timedelta(minutes=self.settings.failure_circuit_breaker_window_minutes)
        recent = db.scalar(select(func.count(Incident.id)).where(Incident.kind == kind, Incident.created_at >= cutoff)) or 0
        state = self.runtime.get(db)
        if recent != self.settings.failure_circuit_breaker_count or state.global_mode != GlobalMode.AUTO:
            return False
        # A personal workstation should recover automatically after a short
        # cloud/network outage. Individual failed replies are already blocked,
        # so a burst warning is sufficient; do not permanently mute all models.
        self._audit(
            db,
            "FAILURE_BURST_WARNING",
            conversation_id,
            message_id,
            {
                "kind": kind,
                "recent_failures": recent,
                "window_minutes": self.settings.failure_circuit_breaker_window_minutes,
                "mode": state.global_mode,
                "automatic_silence": False,
            },
            level="WARNING",
        )
        db.commit()
        await event_hub.broadcast("incident", {"kind": "FAILURE_BURST_WARNING"})
        return True

    @staticmethod
    def _incident(db: Session, kind: str, title: str, detail: str) -> None:
        db.add(Incident(kind=kind, title=title, detail=detail))
        db.add(AuditLog(event=AuditEvent.INCIDENT, level="WARNING", detail={"kind": kind, "title": title}))
