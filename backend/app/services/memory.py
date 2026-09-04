from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.entities import Contact, Memory, Message
from app.policy.engine import contains_secret


MEMORY_KINDS = {"PREFERENCE", "PERSONAL_FACT", "RELATIONSHIP_CONTEXT", "ONGOING_TOPIC", "CONVERSATION_CONVENTION"}
SENSITIVE_PATTERNS = [
    re.compile(r"\b\d{15,19}\b"),
    re.compile(r"身份证|银行卡|支付密码|验证码|private key", re.I),
]
EXTRACTION_RULES = (
    ("PREFERENCE", re.compile(r"(?:我)?(?:喜欢|偏好|爱吃|不喜欢|讨厌|最爱)[^，,。！？!?]{1,100}")),
    ("RELATIONSHIP_CONTEXT", re.compile(r"(?:我们是|我是你的|你是我的|大学同学|高中同学|同事|室友|老朋友)[^，,。！？!?]{0,80}")),
    ("ONGOING_TOPIC", re.compile(r"(?:我)?(?:正在|最近在|目前在|一直在)[^，,。！？!?]{1,100}")),
    ("CONVERSATION_CONVENTION", re.compile(r"(?:以后|叫我|习惯|咱们可以|互相开玩笑)[^，,。！？!?]{1,100}")),
    ("PERSONAL_FACT", re.compile(r"(?:我)?(?:下周|下个月|明天|准备|计划|打算|住在|来自)[^，,。！？!?]{1,100}")),
)


def safe_memory(kind: str, content: str) -> tuple[bool, str]:
    normalized_kind = kind.upper()
    normalized = content.strip()
    if normalized_kind not in MEMORY_KINDS:
        return False, "不支持的记忆类型"
    if not normalized or len(normalized) > 500:
        return False, "记忆内容为空或过长"
    if contains_secret(normalized) or any(pattern.search(normalized) for pattern in SENSITIVE_PATTERNS):
        return False, "敏感内容禁止写入长期记忆"
    return True, normalized


def extract_candidate_memories(content: str) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for kind, pattern in EXTRACTION_RULES:
        for match in pattern.finditer(content):
            allowed, normalized = safe_memory(kind, match.group(0).strip(" ，,。！？!?"))
            key = (kind, normalized)
            if allowed and key not in seen:
                seen.add(key)
                candidates.append(key)
            if len(candidates) >= 6:
                return candidates
    return candidates


def store_extracted_memories(db: Session, contact: Contact, source: Message, max_items: int = 100) -> int:
    if not contact.whitelisted or not contact.memory_enabled:
        return 0
    existing_count = db.scalar(select(func.count(Memory.id)).where(Memory.contact_id == contact.id)) or 0
    remaining = max(0, max_items - existing_count)
    if remaining == 0:
        return 0
    created = 0
    for kind, content in extract_candidate_memories(source.content):
        duplicate = db.scalar(
            select(Memory.id).where(Memory.contact_id == contact.id, Memory.kind == kind, Memory.content == content).limit(1)
        )
        if duplicate:
            continue
        # This personal installation writes safe, deterministic extractions
        # immediately.  The local dashboard remains the correction boundary:
        # every item can still be edited, rejected, pinned, or deleted.
        db.add(
            Memory(
                contact_id=contact.id,
                kind=kind,
                content=content,
                source_message_id=source.id,
                review_status="APPROVED",
            )
        )
        created += 1
        if created >= remaining:
            break
    if created:
        db.flush()
    return created
