from __future__ import annotations

import hashlib
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import KnowledgeDocument


def content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _terms(value: str) -> set[str]:
    lowered = value.casefold()
    words = set(re.findall(r"[a-z0-9_+-]{2,}|[\u4e00-\u9fff]{2,}", lowered))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
    words.update(chinese[index : index + 2] for index in range(max(0, len(chinese) - 1)))
    return {item for item in words if item}


def search_knowledge(db: Session, query: str, *, limit: int = 3, max_chars: int = 3600) -> str:
    query_terms = _terms(query)
    if not query_terms:
        return ""
    rows = list(db.scalars(select(KnowledgeDocument).where(KnowledgeDocument.enabled.is_(True))))
    ranked: list[tuple[int, KnowledgeDocument]] = []
    for row in rows:
        haystack = _terms(f"{row.title}\n{row.content}")
        score = len(query_terms & haystack)
        if score:
            ranked.append((score, row))
    ranked.sort(key=lambda item: (item[0], item[1].updated_at), reverse=True)
    chunks: list[str] = []
    used = 0
    for score, row in ranked[:limit]:
        excerpt = " ".join(row.content.split())[:1400]
        chunk = f"[本地知识：{row.title}｜匹配 {score}]\n{excerpt}"
        if used + len(chunk) > max_chars:
            chunk = chunk[: max(0, max_chars - used)]
        if chunk:
            chunks.append(chunk)
            used += len(chunk)
    return "\n\n".join(chunks)
