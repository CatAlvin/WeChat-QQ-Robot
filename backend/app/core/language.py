from __future__ import annotations

import re
from functools import lru_cache

from opencc import OpenCC


# Do not rewrite literal material that must remain byte-for-byte usable. Natural
# language around these spans is still normalized to Simplified Chinese.
_PROTECTED_SPAN = re.compile(
    r"(```[\s\S]*?```|`[^`\r\n]*`|https?://[^\s<>()]+|[A-Za-z]:\\[^\s<>]+)"
)


@lru_cache(maxsize=1)
def _traditional_to_simplified() -> OpenCC:
    # tw2sp also normalizes common Traditional/Taiwan terminology (for
    # example “網路/軟體”) to Mainland Simplified usage (“网络/软件”).
    return OpenCC("tw2sp")


def to_simplified_chinese(text: str) -> str:
    """Normalize natural-language spans to Simplified Chinese locally.

    Code blocks, inline code, URLs, and Windows paths are preserved so that the
    language guarantee cannot make executable examples or file references
    unusable.
    """

    if not text:
        return text
    converter = _traditional_to_simplified()
    parts: list[str] = []
    cursor = 0
    for match in _PROTECTED_SPAN.finditer(text):
        parts.append(converter.convert(text[cursor : match.start()]))
        parts.append(match.group(0))
        cursor = match.end()
    parts.append(converter.convert(text[cursor:]))
    return "".join(parts)
