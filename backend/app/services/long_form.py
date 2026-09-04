from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.llm.base import LLMMessage, LLMResponse
from app.llm.budget import LONG_FORM_MARKER, output_token_budget

if TYPE_CHECKING:
    from app.llm.gateway import LLMGateway


_LONG_FORM_PATTERNS = (
    re.compile(r"(?:长文本|长篇|长文).{0,12}(?:分析|说明|介绍|总结|报告|教程)", re.I),
    re.compile(r"(?:详细|深入|完整).{0,12}(?:报告|文档|教程)", re.I),
    re.compile(r"(?:写|生成|整理|制作|输出).{0,16}(?:分析报告|研究报告|长文|文档|教程)", re.I),
    re.compile(r"(?:文档模式|生成\s*(?:PDF|TXT)|整理成\s*(?:PDF|TXT|文档))", re.I),
    re.compile(r"(?:总结文件|总结文档|逐段分析|长文总结)", re.I),
)
_LENGTH_FINISH_REASONS = {"length", "max_tokens", "max_token", "limit"}


@dataclass(frozen=True, slots=True)
class LongFormPlan:
    requested: bool
    format: str = "PDF"
    title: str = "Neko 长文本分析"


@dataclass(frozen=True, slots=True)
class LongFormGeneration:
    response: LLMResponse
    chunks: int = 1
    continued: bool = False


class LongFormIncompleteError(RuntimeError):
    pass


def plan_long_form_request(text: str) -> LongFormPlan:
    normalized = " ".join(text.split()).strip()
    requested = any(pattern.search(normalized) for pattern in _LONG_FORM_PATTERNS)
    if not requested:
        return LongFormPlan(False)
    preferred_format = "TXT" if re.search(r"\bTXT\b", normalized, re.I) and not re.search(r"\bPDF\b", normalized, re.I) else "PDF"
    title_source = re.sub(r"^(?:请|麻烦|帮我|给我|请你|帮忙)?(?:写|生成|整理|制作|输出)?(?:一篇|一份)?", "", normalized).strip(" ：:，,")
    title = (title_source or "长文本分析")[:60]
    return LongFormPlan(True, preferred_format, title)


def is_long_form_messages(messages: list[LLMMessage]) -> bool:
    return any(LONG_FORM_MARKER in item.content for item in messages) or any(
        plan_long_form_request(item.content).requested for item in messages if item.role == "user"
    )


def add_long_form_instructions(messages: list[LLMMessage], plan: LongFormPlan) -> list[LLMMessage]:
    if not plan.requested:
        return messages
    instruction = LLMMessage(
        role="system",
        content=(
            f"{LONG_FORM_MARKER}\n"
            "这是长文档任务。请完整回答，不要遵循普通聊天的 1–3 句限制。内容应结构清楚、信息充分，"
            "使用简体中文；先给出准确结论，再分节解释并提供具体例子。不要为了凑长度重复内容，"
            "不要声称已经创建文件，文件由本地程序在回答完成后生成。"
        ),
    )
    return [*messages[:-1], instruction, messages[-1]] if messages else [instruction]


def _needs_continuation(response: LLMResponse) -> bool:
    return response.finish_reason.strip().casefold() in _LENGTH_FINISH_REASONS


def _merge_chunk(existing: str, continuation: str) -> str:
    left = existing.rstrip()
    right = continuation.lstrip()
    max_overlap = min(600, len(left), len(right))
    for size in range(max_overlap, 19, -1):
        if left[-size:] == right[:size]:
            right = right[size:].lstrip()
            break
    return f"{left}\n\n{right}".strip() if right else left


async def generate_long_form(
    gateway: LLMGateway,
    messages: list[LLMMessage],
    *,
    max_chunks: int = 4,
) -> LongFormGeneration:
    """Generate a complete document, continuing only when a provider reports a length stop."""

    response = await gateway.chat(messages)
    chunks = [response.content.strip()]
    providers = [response.provider]
    models = [response.model]
    provider_errors = list(response.provider_errors)
    input_tokens = response.input_tokens
    output_tokens = response.output_tokens
    latency_ms = response.latency_ms
    finish_reason = response.finish_reason

    while _needs_continuation(response) and len(chunks) < max_chunks:
        combined = "\n\n".join(chunks)
        continuation_messages = [
            *messages,
            LLMMessage(role="assistant", content=combined),
            LLMMessage(
                role="user",
                content=(
                    f"{LONG_FORM_MARKER}\n从刚才被截断的位置继续完成文档。不要重写开头，不要重复已有段落；"
                    "补齐尚未完成的章节和结论，只输出续写正文。"
                ),
            ),
        ]
        response = await gateway.chat(continuation_messages)
        chunks.append(response.content.strip())
        providers.append(response.provider)
        models.append(response.model)
        provider_errors.extend(response.provider_errors)
        input_tokens += response.input_tokens
        output_tokens += response.output_tokens
        latency_ms += response.latency_ms
        finish_reason = response.finish_reason

    if _needs_continuation(response):
        raise LongFormIncompleteError(f"provider still reported {finish_reason!r} after {len(chunks)} chunks")

    combined = chunks[0]
    for chunk in chunks[1:]:
        combined = _merge_chunk(combined, chunk)
    unique_providers = list(dict.fromkeys(providers))
    unique_models = list(dict.fromkeys(models))
    merged_response = LLMResponse(
        content=combined,
        model=" → ".join(unique_models)[:120],
        provider=" → ".join(unique_providers)[:60],
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        finish_reason=finish_reason,
        provider_errors=tuple(provider_errors),
    )
    return LongFormGeneration(merged_response, len(chunks), len(chunks) > 1)
