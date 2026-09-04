from __future__ import annotations

from dataclasses import dataclass

from app.llm.base import LLMMessage, LLMProvider
from app.services.long_form import plan_long_form_request


@dataclass(frozen=True, slots=True)
class RouteDecision:
    task_type: str
    reason: str
    providers: tuple[LLMProvider, ...]


def route_providers(providers: list[LLMProvider], messages: list[LLMMessage]) -> RouteDecision:
    """Apply conservative task-aware routing while preserving configured fallback order."""

    if any(message.attachments for message in messages):
        media = [provider for provider in providers if getattr(provider, "supports_attachments", False)]
        return RouteDecision("MULTIMODAL", "消息包含图片或视频，优先原生多模态模型", tuple([*media, *[p for p in providers if p not in media]]))
    text = _current_user_request(messages).casefold()
    if any(marker in text for marker in ("只在本地", "不要联网", "隐私模式", "本地处理")):
        local = [
            provider
            for provider in providers
            if provider.__class__.__name__.casefold().startswith("ollama") or "ollama" in provider.name.casefold()
        ]
        return RouteDecision("PRIVATE_LOCAL", "用户明确要求本地处理", tuple([*local, *[p for p in providers if p not in local]]))
    if len(text) > 5000 or plan_long_form_request(text).requested:
        preferred = [provider for provider in providers if "kimi" in provider.name.casefold()]
        return RouteDecision("LONG_CONTEXT", "长文本或文档任务，优先长上下文模型", tuple([*preferred, *[p for p in providers if p not in preferred]]))
    if any(marker in text for marker in ("代码", "报错", "算法", "推理", "分析原因")):
        preferred = [provider for provider in providers if any(name in provider.name.casefold() for name in ("deepseek", "qwen", "kimi"))]
        return RouteDecision("REASONING", "推理或代码任务，优先分析能力更强的模型", tuple([*preferred, *[p for p in providers if p not in preferred]]))
    return RouteDecision("GENERAL_CHAT", "普通聊天沿用管理员配置的模型顺序", tuple(providers))


def _current_user_request(messages: list[LLMMessage]) -> str:
    text = next((item.content for item in reversed(messages) if item.role == "user"), "")
    marker = "\n\n当前消息："
    return text.rsplit(marker, 1)[-1].strip() if marker in text else text.strip()
