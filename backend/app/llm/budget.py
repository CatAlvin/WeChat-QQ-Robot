from __future__ import annotations

from app.llm.base import LLMMessage


LONG_FORM_MARKER = "[NEKO_LONG_FORM_DOCUMENT]"


def output_token_budget(messages: list[LLMMessage], *, local: bool = False) -> int:
    is_document = any(LONG_FORM_MARKER in item.content for item in messages)
    if is_document:
        return 2048 if local else 4096
    return 512 if local else 500
