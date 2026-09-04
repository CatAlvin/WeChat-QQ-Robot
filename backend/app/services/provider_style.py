from __future__ import annotations

from dataclasses import dataclass
import re

from app.core.enums import MessageAuthor
from app.models.entities import Message


_EMOJI_RE = re.compile(
    r"(?:[\U0001F1E6-\U0001F1FF]{2}|"
    r"[\U0001F300-\U0001FAFF\u2600-\u27BF]"
    r"(?:\uFE0E|\uFE0F)?"
    r"(?:\u200D[\U0001F300-\U0001FAFF\u2600-\u27BF](?:\uFE0E|\uFE0F)?)*)"
)
_EXACT_OUTPUT_RE = re.compile(
    r"(?:只|仅)(?:回复|输出|发送)|原样(?:回复|重复|输出|发送)|"
    r"(?:回复|输出|发送|使用).{0,10}(?:emoji|表情符号|表情)",
    re.IGNORECASE,
)
_KNOWN_FIXED_SIGNATURES = (("🐾", "✨"),)


@dataclass(frozen=True, slots=True)
class ProviderStyleResult:
    content: str
    changed: bool
    removed_emoji_count: int = 0
    reason: str | None = None


def contains_emoji(content: str) -> bool:
    return bool(_EMOJI_RE.search(content or ""))


def normalize_provider_reply(
    *,
    content: str,
    provider: str | None,
    inbound_content: str,
    recent: list[Message],
) -> ProviderStyleResult:
    """Enforce a restrained Kimi emoji style after generation.

    The model prompt is the primary control. This deterministic final check
    prevents a learned signature from leaking through while retaining one
    contextual emoji occasionally. Explicit formatting requests are left
    untouched.
    """

    original = content or ""
    if "kimi" not in (provider or "").casefold() or _EXACT_OUTPUT_RE.search(inbound_content or ""):
        return ProviderStyleResult(original, False)

    matches = list(_EMOJI_RE.finditer(original))
    if not matches:
        return ProviderStyleResult(original, False)

    has_fixed_signature = any(all(marker in original for marker in signature) for signature in _KNOWN_FIXED_SIGNATURES)
    recent_kimi_ai = sorted(
        (
            item
            for item in recent
            if str(item.author) == str(MessageAuthor.AI)
            and "kimi" in (item.provider or "").casefold()
        ),
        key=lambda item: item.created_at,
        reverse=True,
    )[:6]
    emoji_on_cooldown = any(contains_emoji(item.content) for item in recent_kimi_ai[:2])
    current_first_emoji = matches[0].group(0)
    repeated_emoji = any(
        current_first_emoji in {match.group(0) for match in _EMOJI_RE.finditer(item.content or "")}
        for item in recent_kimi_ai
    )
    keep_limit = 0 if has_fixed_signature or emoji_on_cooldown or repeated_emoji else 1

    parts: list[str] = []
    cursor = 0
    kept = 0
    for match in matches:
        parts.append(original[cursor : match.start()])
        if kept < keep_limit:
            parts.append(match.group(0))
            kept += 1
        cursor = match.end()
    parts.append(original[cursor:])
    normalized = "".join(parts)
    normalized = re.sub(r"[ \t]+(?=[，。！？、；：,.!?])", "", normalized)
    normalized = re.sub(r"[ \t]{2,}", " ", normalized)
    normalized = re.sub(r"(?m)^[ \t]+|[ \t]+$", "", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    if not normalized:
        normalized = "收到。"
    reason = (
        "FIXED_EMOJI_SIGNATURE"
        if has_fixed_signature
        else "EMOJI_COOLDOWN"
        if emoji_on_cooldown
        else "REPEATED_EMOJI"
        if repeated_emoji
        else "MULTIPLE_EMOJI"
    )
    return ProviderStyleResult(
        normalized,
        normalized != original,
        removed_emoji_count=max(0, len(matches) - kept),
        reason=reason,
    )
