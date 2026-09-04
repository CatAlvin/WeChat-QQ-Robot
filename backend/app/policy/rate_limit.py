from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    code: str
    reason: str
    force_silent: bool = False


class RateLimiter:
    def __init__(self, per_contact_minute: int = 5, daily_limit: int = 500, hard_limit: int = 1000, max_consecutive: int = 3) -> None:
        self.per_contact_minute = min(max(per_contact_minute, 1), 5)
        self.daily_limit = min(max(daily_limit, 1), hard_limit, 1000)
        self.hard_limit = min(max(hard_limit, 1), 1000)
        self.max_consecutive = min(max(max_consecutive, 1), 3)
        self._contact_events: dict[str, deque[datetime]] = defaultdict(deque)

    def allow(
        self,
        contact_id: str,
        *,
        now: datetime | None = None,
        daily_sent: int = 0,
        consecutive_sends: int = 0,
        recent_contact_sent: int | None = None,
    ) -> RateLimitDecision:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if daily_sent >= self.hard_limit:
            return RateLimitDecision(False, "DAILY_HARD_LIMIT", "达到系统每日 1000 条硬上限", True)
        if daily_sent >= self.daily_limit:
            return RateLimitDecision(False, "DAILY_LIMIT", "达到配置的每日自动发送上限", True)
        if consecutive_sends >= self.max_consecutive:
            return RateLimitDecision(False, "CONSECUTIVE_LIMIT", "未收到新消息前最多连续发送 3 条")
        if recent_contact_sent is None:
            events = self._contact_events[contact_id]
            cutoff = now - timedelta(minutes=1)
            while events and events[0] <= cutoff:
                events.popleft()
            recent_contact_sent = len(events)
        if recent_contact_sent >= self.per_contact_minute:
            return RateLimitDecision(False, "CONTACT_MINUTE_LIMIT", "单联系人每分钟最多 5 条")
        return RateLimitDecision(True, "PASS", "发送频率允许")

    def record(self, contact_id: str, at: datetime | None = None) -> None:
        at = at or datetime.now(timezone.utc)
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        self._contact_events[contact_id].append(at)
