from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time

from app.core.clock import BEIJING_TZ, as_utc, utc_now
from app.core.language import to_simplified_chinese
from app.core.enums import ConversationMode, GlobalMode, Importance


FORMAL_HIGH_RISK_TOPICS = {
    "学校申请", "申请学校", "报考", "报名", "招聘", "面试", "入职", "工作合同",
    "合同", "签证", "诉讼", "律师", "法律", "辞职", "薪资", "offer",
}
FORMAL_INTERACTION_PATTERNS = [
    re.compile(r"^(?:老师|导师|hr|领导|客户)$", re.I),
    re.compile(r"(?:老师|导师|hr|领导|客户).{0,16}(?:找我|联系|回复|回话|要求|安排|通知|意见|问我|让我|叫我|约我)", re.I),
    re.compile(r"(?:回复|联系|答复|告诉).{0,16}(?:老师|导师|hr|领导|客户)", re.I),
]
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:sk|ds)-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:api[_ -]?key|access[_ -]?token|session[_ -]?token|password)\s*[:=]\s*\S+", re.I),
]
MONEY_COMMITMENTS = [
    re.compile(pattern, re.I)
    for pattern in (
        r"我(?:现在|马上|稍后)?(?:给你)?转(?:账|钱)",
        r"我(?:答应|保证).{0,10}(?:借|付|还|转)",
        r"我(?:可以|会)(?:替你)?(?:付款|借你|转给你)",
        r"银行卡号|支付密码|验证码",
    )
]
FORMAL_COMMITMENTS = [
    re.compile(pattern, re.I)
    for pattern in (r"我(?:代表|确认|同意).{0,16}(?:签署|签约|入职|离职|合同)", r"我保证.{0,30}(?:按时完成|承担责任)")
]
IMPORTANT_COMMITMENTS = [
    re.compile(pattern, re.I)
    for pattern in (
        r"我(?:一定|肯定|保证).{0,30}(?:做到|完成|解决|处理|负责|办好)",
        r"(?:这件事|这个).{0,12}(?:包在我身上|交给我)",
        r"我(?:替你|帮你).{0,10}(?:答应|确认|承诺)",
    )
]


def _as_utc(value: datetime | None) -> datetime | None:
    return as_utc(value) if value is not None else None


@dataclass(frozen=True, slots=True)
class PolicyInput:
    global_mode: str
    kill_switch: bool
    whitelisted: bool
    ai_enabled: bool
    importance: str
    channel_enabled: bool = True
    relationship_label: str = "朋友"
    is_group: bool = False
    group_allowed: bool = False
    mentioned_user: bool = False
    conversation_mode: str = ConversationMode.AUTO_READY
    human_until: datetime | None = None
    cooldown_until: datetime | None = None
    managed_rounds: int = 0
    now: datetime | None = None
    auto_start: time = time(10, 0)
    auto_end: time = time(23, 30)
    enforce_time_window: bool = True
    max_managed_rounds: int = 40
    # Retained for backward-compatible construction. Business time is always
    # China Standard Time and cannot drift with the host configuration.
    utc_offset_hours: int = 8
    inbound_content: str = ""


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    code: str
    reason: str
    needs_attention: bool = False


class PolicyEngine:
    """Deterministic send permission. No model output can override this class."""

    def evaluate(self, item: PolicyInput) -> PolicyDecision:
        now = _as_utc(item.now) or utc_now()
        is_admin = item.relationship_label.strip().lower() == "管理员"
        if item.kill_switch:
            return self._deny("KILL_SWITCH", "全局急停已开启")
        if item.global_mode != GlobalMode.AUTO:
            return self._deny("GLOBAL_MODE", f"当前模式 {item.global_mode} 禁止发送")
        if not item.channel_enabled:
            return self._deny("CHANNEL_DISABLED", "当前消息通道已关闭")
        if not item.ai_enabled:
            return self._deny("AI_DISABLED", "该联系人未启用 AI")
        if not item.whitelisted:
            return self._deny("NOT_WHITELISTED", "非白名单联系人")
        if item.importance == Importance.MANUAL_ONLY:
            return self._deny("MANUAL_ONLY", "该联系人仅允许人工处理", True)
        if item.is_group and not item.group_allowed:
            return self._deny("GROUP_NOT_ALLOWED", "群聊不在允许列表")
        if item.is_group and not item.mentioned_user:
            return self._deny("GROUP_NOT_MENTIONED", "群聊未 @ 用户")
        local_time = now.astimezone(BEIJING_TZ).time().replace(tzinfo=None)
        if item.auto_start < item.auto_end:
            inside_time_window = item.auto_start <= local_time < item.auto_end
        else:
            # A start later than the end represents an intentional overnight window.
            inside_time_window = local_time >= item.auto_start or local_time < item.auto_end
        if item.enforce_time_window and not inside_time_window:
            window = f"{item.auto_start.strftime('%H:%M')}–{item.auto_end.strftime('%H:%M')}"
            return self._deny("OUTSIDE_TIME_WINDOW", f"当前不在 {window} LIVE 自动发送时段")
        if item.conversation_mode == ConversationMode.HUMAN:
            return self._deny("HUMAN_TAKEOVER", "人工接管冷却期内")
        if item.conversation_mode == ConversationMode.CONTACT_COOLDOWN and not is_admin:
            return self._deny("CONTACT_COOLDOWN", "联系人对话已进入安全冷却", True)
        if item.managed_rounds >= item.max_managed_rounds and not is_admin:
            return self._deny("MAX_MANAGED_ROUNDS", "连续托管达到 40 轮上限", True)
        if any(pattern.search(item.inbound_content) for pattern in SECRET_PATTERNS):
            return self._deny("INBOUND_SECRET", "消息疑似包含密钥或令牌，禁止发送到模型并转人工", True)
        return PolicyDecision(True, "PASS", "全部确定性策略通过")

    @staticmethod
    def _deny(code: str, reason: str, needs_attention: bool = False) -> PolicyDecision:
        return PolicyDecision(False, code, reason, needs_attention)


@dataclass(frozen=True, slots=True)
class GuardDecision:
    allowed: bool
    code: str
    reason: str
    content: str


class ResponseGuard:
    def inspect(
        self,
        content: str,
        inbound_content: str = "",
        *,
        strict: bool = False,
        truncate_limit: int | None = 800,
        strict_length_limit: int | None = 400,
    ) -> GuardDecision:
        # Provider prompts are advisory. This local normalization is the final,
        # deterministic language boundary before a reply can be sent.
        normalized = to_simplified_chinese(content.strip())
        if not normalized:
            return GuardDecision(False, "EMPTY_RESPONSE", "模型返回空内容", "")
        if truncate_limit is not None and len(normalized) > truncate_limit:
            normalized = normalized[: max(1, truncate_limit - 3)].rstrip() + "…"
        for pattern in SECRET_PATTERNS:
            if pattern.search(normalized):
                return GuardDecision(False, "SECRET_EXPOSURE", "回复疑似包含密钥或令牌", "")
        if any(pattern.search(normalized) for pattern in MONEY_COMMITMENTS):
            return GuardDecision(False, "MONEY_COMMITMENT", "禁止自动作出金钱或付款承诺", "")
        if any(pattern.search(normalized) for pattern in FORMAL_COMMITMENTS):
            return GuardDecision(False, "FORMAL_COMMITMENT", "禁止自动作出正式事务承诺", "")
        if strict and any(pattern.search(normalized) for pattern in IMPORTANT_COMMITMENTS):
            return GuardDecision(False, "IMPORTANT_COMMITMENT", "重要联系人回复不得作出未经本人确认的强承诺", "")
        if strict and strict_length_limit is not None and len(normalized) > strict_length_limit:
            normalized = normalized[: max(1, strict_length_limit - 1)].rstrip() + "…"
        # Keep the cat persona subtle even if a provider ignores the prompt.
        if normalized.count("喵") > 2:
            first = normalized.find("喵")
            normalized = normalized[: first + 1] + normalized[first + 1 :].replace("喵", "")
        return GuardDecision(True, "PASS", "内容安全检查通过", normalized)


def contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def requires_human_for_formal_topic(text: str) -> bool:
    """Distinguish factual questions about institutions from personal formal business."""

    normalized = " ".join((text or "").casefold().split())
    if any(topic in normalized for topic in FORMAL_HIGH_RISK_TOPICS):
        return True
    return any(pattern.search(normalized) for pattern in FORMAL_INTERACTION_PATTERNS)
