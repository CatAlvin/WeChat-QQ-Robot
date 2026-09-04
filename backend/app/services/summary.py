from __future__ import annotations

import re

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.core.enums import MessageAuthor, MessageStatus
from app.models.entities import Conversation, ConversationSummary, Message


_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)\b(api[_ -]?key|access[_ -]?token|session[_ -]?token|password)\s*[:=]\s*\S+"),
    re.compile(r"\b\d{6}\b"),
)
_EXPLICIT_ATTITUDE = re.compile(
    r"我(?:很|有点|特别|真的|非常)?(?:开心|高兴|难过|伤心|生气|担心|焦虑|满意|不满意|同意|不同意|支持|反对|喜欢|讨厌)[^。！？!?]{0,80}"
)


def _safe_excerpt(value: str, limit: int = 180) -> str:
    text = " ".join(value.split())
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[敏感信息已隐藏]", text)
    return text[:limit]


def refresh_conversation_summary(db: Session, conversation: Conversation) -> ConversationSummary:
    """Maintain one local, deterministic, non-hallucinated summary per conversation."""
    recent = list(
        db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(desc(Message.created_at))
            .limit(40)
        )
    )
    ordered = list(reversed(recent))
    transcript = [f"- {item.author}: {_safe_excerpt(item.content)}" for item in ordered[-8:]]
    unfinished = [
        f"- {_safe_excerpt(item.content)}"
        for item in ordered
        if any(marker in item.content for marker in ("？", "?", "待", "记得", "需要", "之后", "下次"))
    ][-5:]
    attention = [
        f"- {item.status}: {_safe_excerpt(item.content)}"
        for item in ordered
        if item.status in {MessageStatus.BLOCKED, MessageStatus.FAILED, MessageStatus.CANCELLED}
    ][-5:]
    explicit_attitudes: list[str] = []
    for item in ordered:
        if item.author != MessageAuthor.CONTACT:
            continue
        for match in _EXPLICIT_ATTITUDE.finditer(item.content):
            excerpt = _safe_excerpt(match.group(0))
            if excerpt not in explicit_attitudes:
                explicit_attitudes.append(excerpt)
    attitude_lines = [f"- 联系人明确表达：{value}" for value in explicit_attitudes[-3:]]
    content = "\n".join(
        [
            "今天主要聊了什么：",
            *(transcript or ["- 暂无可摘要消息"]),
            "",
            "重要事实：",
            "- 长期事实只由当前联系人的独立 Memory 管理；摘要不会自动扩散为长期记忆。",
            "",
            "尚未完成事项：",
            *(unfinished or ["- 暂未识别"]),
            "",
            "联系人情绪/态度：",
            *(attitude_lines or ["- 未发现联系人明确表达；系统不自动推断情绪标签。"]),
            "",
            "可能需要用户关注：",
            *(attention or ["- 暂无"]),
        ]
    )[:8000]

    summary = db.scalar(
        select(ConversationSummary)
        .where(ConversationSummary.conversation_id == conversation.id)
        .order_by(desc(ConversationSummary.created_at))
        .limit(1)
    )
    if summary is None:
        summary = ConversationSummary(conversation_id=conversation.id, content=content)
        db.add(summary)
    else:
        summary.content = content
    db.flush()
    return summary
