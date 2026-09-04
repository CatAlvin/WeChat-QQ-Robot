from __future__ import annotations

from collections.abc import Iterable

from app.models.entities import ProviderConfig


def capability_order(providers: Iterable[ProviderConfig]) -> list[ProviderConfig]:
    """Return a stable, explainable strongest-first fallback order.

    The ranking is intentionally coarse.  Administrators can still move any
    item manually, while known flagship/cloud models stay ahead of small local
    fallbacks when the one-click recommendation is used.
    """

    return sorted(providers, key=lambda item: (_capability_rank(item), item.priority, item.name.casefold()))


def apply_provider_order(providers: list[ProviderConfig]) -> None:
    """Normalize a complete ordered provider list to distinct priorities."""

    for index, provider in enumerate(providers, start=1):
        provider.priority = index * 10


def _capability_rank(provider: ProviderConfig) -> int:
    provider_type = provider.provider_type.upper()
    model = provider.model.casefold()
    if "gpt-5" in model:
        return 0
    if provider_type == "KIMI" and "kimi-k3" in model:
        return 10
    if provider_type == "DEEPSEEK":
        return 20
    if provider_type == "QWEN":
        return 30
    if provider_type == "OPENAI":
        return 35
    if provider_type == "KIMI":
        return 40
    if provider_type == "OPENAI_COMPATIBLE":
        return 50
    if provider_type == "OLLAMA" and "qwen3.5" in model:
        return 80
    if provider_type == "OLLAMA":
        return 90
    return 70
