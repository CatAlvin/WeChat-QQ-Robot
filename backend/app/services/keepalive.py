from __future__ import annotations

import hashlib
from datetime import datetime, time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.channels.base import OutboundMessage, SendPermit
from app.core.config import Settings, get_settings
from app.core.clock import BEIJING_TZ, as_utc, beijing_start_of_day_utc, utc_now
from app.core.enums import GlobalMode, MessageAuthor, MessageDirection, MessageStatus
from app.core.events import event_hub
from app.models.entities import Account, AuditLog, Contact, Conversation, Incident, Message, RuntimeState
from app.services.factory import build_napcat_connector
from app.services.runtime import runtime_control


KEEPALIVE_MESSAGES = (
    "来续一下今天的小火苗～",
    "今日份轻轻碰一下聊天框，续火成功～",
    "路过你的聊天框，给小火苗添一点点能量～",
    "今天也来打个小小的续火卡～",
    "冒个泡，守住今天的聊天小火苗～",
    "今日续火打卡：愿今天轻轻松松～",
    "来给我们的聊天添一颗小火星～",
    "今天的小火苗也有好好亮着～",
    "敲一下聊天框，完成今日续火～",
    "送达一条轻量续火消息，小火苗继续～",
    "今日份续火已抵达，叮～",
    "给聊天留一个今天的小脚印～",
    "来啦，今天也让小火苗亮一下～",
    "轻轻冒泡，完成今天的续火任务～",
    "今日聊天小火苗维护完成～",
    "点亮一下今天的聊天框～",
    "今天也没有忘记来续个火～",
    "为今天补上一颗聊天小火星～",
    "小火苗巡检完成，今天也在～",
    "今日份无意义但有用的续火消息到达～",
)


def _clock(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def _inside_window(now_value: time, start_value: str, end_value: str) -> bool:
    start = _clock(start_value)
    end = _clock(end_value)
    if start < end:
        return start <= now_value < end
    return now_value >= start or now_value < end


def unique_keepalive_content(contact: Contact, local_day: str) -> str:
    digest = hashlib.sha256(f"{contact.id}:{local_day}".encode("utf-8")).digest()
    index = int.from_bytes(digest[:4], "big") % len(KEEPALIVE_MESSAGES)
    content = KEEPALIVE_MESSAGES[index]
    if contact.keepalive_last_content and contact.keepalive_last_content.startswith(content):
        content = KEEPALIVE_MESSAGES[(index + 1) % len(KEEPALIVE_MESSAGES)]
    day = datetime.strptime(local_day, "%Y-%m-%d")
    return f"{content}〔{day.month}月{day.day}日〕"


class KeepaliveService:
    """Send at most one explicitly authorized NapCat keepalive per local day.

    A durable attempt marker is committed before calling OneBot.  If the
    process crashes after NapCat accepted a message, the same day's scheduler
    will not retry and accidentally duplicate it.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def run_due(self, db: Session, now: datetime | None = None) -> int:
        now_utc = as_utc(now) if now is not None else utc_now()
        local_now = now_utc.astimezone(BEIJING_TZ)
        local_day = local_now.date().isoformat()
        contacts = list(
            db.scalars(
                select(Contact)
                .where(Contact.platform == "QQ_NAPCAT", Contact.keepalive_enabled.is_(True))
                .order_by(Contact.keepalive_time.asc(), Contact.id.asc())
            )
        )
        sent = 0
        for contact in contacts:
            if contact.keepalive_last_attempt_on == local_day:
                continue
            if local_now.time().replace(tzinfo=None) < _clock(contact.keepalive_time):
                continue
            if await self._attempt(db, contact.id, local_day, now_utc, local_now.time().replace(tzinfo=None)):
                sent += 1
        return sent

    async def _attempt(self, db: Session, contact_id: str, local_day: str, now_utc: datetime, local_time: time) -> bool:
        async with runtime_control.send_lock:
            state = db.get(RuntimeState, 1)
            contact = db.get(Contact, contact_id)
            if state is None or contact is None or not contact.keepalive_enabled:
                return False
            if state.release_gate != "LIVE" or state.global_mode != GlobalMode.AUTO or state.kill_switch:
                return False
            if state.live_time_window_enabled and not _inside_window(local_time, state.live_auto_start, state.live_auto_end):
                return False
            if contact.keepalive_last_attempt_on == local_day:
                return False
            account = db.get(Account, contact.keepalive_account_id) if contact.keepalive_account_id else None
            if account is None or account.platform != "QQ_NAPCAT" or not account.enabled:
                contact.keepalive_last_status = "WAITING_ACCOUNT"
                contact.keepalive_last_error = "BOUND_ACCOUNT_NOT_ACTIVE"
                db.commit()
                return False
            if contact.account_id and contact.account_id != account.id:
                contact.keepalive_last_status = "BLOCKED"
                contact.keepalive_last_error = "ACCOUNT_ISOLATION_MISMATCH"
                db.commit()
                return False
            if not account.credential_id or not (account.config or {}).get("risk_acknowledged"):
                contact.keepalive_last_status = "BLOCKED"
                contact.keepalive_last_error = "ACCOUNT_NOT_CONFIGURED"
                db.commit()
                return False

            start_of_day = beijing_start_of_day_utc(now_utc)
            reserved = db.scalar(
                select(func.count(Message.id)).where(
                    Message.author == MessageAuthor.AI,
                    Message.status.in_([MessageStatus.SENT, MessageStatus.QUEUED]),
                    Message.created_at >= start_of_day,
                )
            ) or 0
            if reserved >= min(self.settings.daily_send_limit, self.settings.daily_send_hard_limit, 1000):
                contact.keepalive_last_status = "BLOCKED"
                contact.keepalive_last_error = "DAILY_LIMIT"
                db.commit()
                return False

            conversation = db.scalar(
                select(Conversation).where(
                    Conversation.platform == "QQ_NAPCAT",
                    Conversation.account_id == account.id,
                    Conversation.external_id == f"napcat:private:{contact.platform_user_id}",
                )
            )
            if conversation is None:
                conversation = Conversation(
                    platform="QQ_NAPCAT",
                    account_id=account.id,
                    external_id=f"napcat:private:{contact.platform_user_id}",
                    contact_id=contact.id,
                    last_active_at=now_utc,
                )
                db.add(conversation)
                db.flush()

            content = unique_keepalive_content(contact, local_day)
            durable_key = f"keepalive:{contact.id}:{local_day}"
            message = Message(
                platform="QQ_NAPCAT",
                account_id=account.id,
                external_message_id=durable_key,
                sender_id=str((account.config or {}).get("managed_qq_id") or "QQ_NAPCAT:KEEPALIVE"),
                receiver_id=contact.platform_user_id,
                event_at=now_utc,
                conversation_id=conversation.id,
                contact_id=contact.id,
                direction=MessageDirection.OUTBOUND,
                author=MessageAuthor.AI,
                content=content,
                message_type="KEEPALIVE",
                status=MessageStatus.QUEUED,
                provider="LOCAL_TEMPLATE",
                model="keepalive-v1",
                raw_envelope={"kind": "DAILY_KEEPALIVE", "local_day": local_day, "account_id": account.id},
            )
            contact.keepalive_last_attempt_on = local_day
            contact.keepalive_last_status = "SENDING"
            contact.keepalive_last_error = None
            db.add(message)
            db.add(
                AuditLog(
                    event="KEEPALIVE_QUEUED",
                    conversation_id=conversation.id,
                    message_id=message.id,
                    detail={"contact_id": contact.id, "account_id": account.id, "local_day": local_day},
                )
            )
            db.commit()

            # Re-read every mutable gate after the durable queue write.
            db.refresh(state)
            db.refresh(contact)
            db.refresh(account)
            if (
                state.release_gate != "LIVE"
                or state.global_mode != GlobalMode.AUTO
                or state.kill_switch
                or not contact.keepalive_enabled
                or contact.keepalive_account_id != account.id
                or not account.enabled
            ):
                message.status = MessageStatus.CANCELLED
                message.policy_reason = "KEEPALIVE_FINAL_GATE_CHANGED"
                contact.keepalive_last_status = "CANCELLED"
                contact.keepalive_last_error = "FINAL_GATE_CHANGED"
                db.commit()
                return False

            connector = build_napcat_connector(db, account)
            outbound = OutboundMessage(
                platform="QQ_NAPCAT",
                target_id=contact.platform_user_id,
                content=content,
                reply_to_message_id="",
                is_group=False,
            )
            permit = SendPermit(
                target_id=contact.platform_user_id,
                whitelisted=contact.whitelisted,
                policy_passed=True,
                guard_passed=True,
                rate_limit_passed=True,
                global_auto=True,
                kill_switch_off=True,
                scheduled_allowed=contact.keepalive_enabled,
            )
            result = await connector.send(outbound, permit)
            if not result.success:
                message.status = MessageStatus.FAILED
                message.policy_reason = result.error_code
                contact.keepalive_last_status = "FAILED"
                contact.keepalive_last_error = result.error_code or "SEND_FAILED"
                db.add(
                    Incident(
                        kind="KEEPALIVE_SEND_FAILURE",
                        title="每日续火发送失败",
                        detail=result.error_code or "NapCat send failed",
                    )
                )
                db.commit()
                await event_hub.broadcast("incident", {"kind": "KEEPALIVE_SEND_FAILURE"})
                return False

            message.status = MessageStatus.SENT
            message.external_message_id = result.external_message_id or message.external_message_id
            contact.keepalive_last_sent_at = now_utc
            contact.keepalive_last_status = "SENT"
            contact.keepalive_last_content = content
            contact.keepalive_last_error = None
            conversation.last_active_at = now_utc
            db.add(
                AuditLog(
                    event="KEEPALIVE_SENT",
                    conversation_id=conversation.id,
                    message_id=message.id,
                    detail={"contact_id": contact.id, "account_id": account.id, "local_day": local_day},
                )
            )
            db.commit()
            await event_hub.broadcast("message_sent", {"message_id": message.id, "conversation_id": conversation.id})
            return True
