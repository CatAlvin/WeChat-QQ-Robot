from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace

import httpx

from app.llm.base import LLMMessage, LLMProvider, LLMResponse
from app.llm.providers import ProviderConfigurationError
from app.services.model_router import RouteDecision, route_providers


class AllProvidersFailed(RuntimeError):
    pass


class LLMGateway:
    def __init__(
        self,
        providers: list[LLMProvider],
        *,
        timeout_seconds: float = 25.0,
        on_error: Callable[[str, Exception], Awaitable[None]] | None = None,
        on_success: Callable[[LLMResponse], Awaitable[None]] | None = None,
    ) -> None:
        self.providers = providers
        self.timeout_seconds = timeout_seconds
        self.on_error = on_error
        self.on_success = on_success

    async def chat(
        self,
        messages: list[LLMMessage],
        *,
        preferred_models: tuple[str, ...] = (),
    ) -> LLMResponse:
        errors: list[str] = []
        for provider in self.providers_for(messages, preferred_models=preferred_models):
            provider_timeout = float(getattr(provider, "timeout", self.timeout_seconds))
            for attempt in range(2):
                try:
                    result = await asyncio.wait_for(provider.chat(messages), timeout=provider_timeout)
                    if not result.content.strip():
                        raise ValueError("provider returned an empty response")
                    if self.on_success:
                        await self.on_success(result)
                    return replace(result, provider_errors=tuple(errors))
                except (Exception, asyncio.TimeoutError) as exc:
                    errors.append(f"{provider.name} attempt {attempt + 1}: {type(exc).__name__}")
                    if self.on_error:
                        await self.on_error(provider.name, exc)
                    if attempt == 0 and self._retryable(exc):
                        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, asyncio.TimeoutError)):
                            await asyncio.sleep(0.5)
                        continue
                    break
            # At most one application-level retry, then deterministic fallback.
        raise AllProvidersFailed("; ".join(errors) or "No enabled providers")

    def providers_for(
        self,
        messages: list[LLMMessage],
        *,
        preferred_models: tuple[str, ...] = (),
    ) -> list[LLMProvider]:
        return list(self.route_decision(messages, preferred_models=preferred_models).providers)

    def route_decision(
        self,
        messages: list[LLMMessage],
        *,
        preferred_models: tuple[str, ...] = (),
    ) -> RouteDecision:
        decision = route_providers(self.providers, messages)
        normalized = tuple(model.strip().casefold() for model in preferred_models if model.strip())
        if not normalized:
            return decision

        priorities = {model: index for index, model in enumerate(normalized)}
        matched = [
            provider
            for provider in decision.providers
            if str(getattr(provider, "model", "")).strip().casefold() in priorities
        ]
        if not matched:
            return decision

        matched.sort(
            key=lambda provider: priorities[str(getattr(provider, "model", "")).strip().casefold()]
        )
        ordered = tuple([*matched, *[provider for provider in decision.providers if provider not in matched]])
        return replace(
            decision,
            reason=f"{decision.reason}；当前任务优先模型：{', '.join(preferred_models)}",
            providers=ordered,
        )

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        if isinstance(exc, ProviderConfigurationError):
            return False
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            return status >= 500 or status in {408, 409, 425, 429}
        return True
