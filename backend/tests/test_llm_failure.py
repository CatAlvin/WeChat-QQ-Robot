from __future__ import annotations

import asyncio

import httpx
import pytest

from app.llm.base import LLMAttachment, LLMMessage, LLMProvider, LLMResponse
from app.llm.gateway import AllProvidersFailed, LLMGateway


class FailingProvider(LLMProvider):
    def __init__(self, name: str):
        self.name = name
        self.calls = 0

    async def chat(self, messages):
        self.calls += 1
        raise RuntimeError("redacted provider failure")

    async def test_connection(self):
        return False

    async def models(self):
        return []


class WorkingProvider(FailingProvider):
    async def chat(self, messages):
        self.calls += 1
        return LLMResponse("安全回复", "test", self.name, 1)


class MediaWorkingProvider(WorkingProvider):
    supports_attachments = True


class SlowConfiguredProvider(WorkingProvider):
    timeout = 0.1

    async def chat(self, messages):
        self.calls += 1
        await asyncio.sleep(0.03)
        return LLMResponse("本地模型回复", "local-test", self.name, 30)


class AuthenticationFailureProvider(FailingProvider):
    async def chat(self, messages):
        self.calls += 1
        request = httpx.Request("POST", "https://example.invalid/chat/completions")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("unauthorized", request=request, response=response)


class TransientNetworkProvider(WorkingProvider):
    async def chat(self, messages):
        self.calls += 1
        if self.calls == 1:
            raise httpx.ConnectError("temporary", request=httpx.Request("POST", "https://example.invalid"))
        return LLMResponse("网络恢复", "test", self.name, 1)


@pytest.mark.asyncio
async def test_retry_once_then_fallback():
    first = FailingProvider("first")
    second = WorkingProvider("second")
    result = await LLMGateway([first, second]).chat([LLMMessage("user", "hello")])
    assert result.content == "安全回复"
    assert first.calls == 2
    assert second.calls == 1
    assert result.provider_errors == ("first attempt 1: RuntimeError", "first attempt 2: RuntimeError")


@pytest.mark.asyncio
async def test_all_failures_end_without_response():
    providers = [FailingProvider("one"), FailingProvider("two")]
    with pytest.raises(AllProvidersFailed):
        await LLMGateway(providers).chat([LLMMessage("user", "hello")])
    assert [item.calls for item in providers] == [2, 2]


@pytest.mark.asyncio
async def test_provider_specific_timeout_overrides_shorter_gateway_default():
    provider = SlowConfiguredProvider("local")

    result = await LLMGateway([provider], timeout_seconds=0.01).chat([LLMMessage("user", "hello")])

    assert result.content == "本地模型回复"
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_authentication_failure_is_not_retried_before_fallback():
    first = AuthenticationFailureProvider("cloud")
    second = WorkingProvider("local")

    result = await LLMGateway([first, second]).chat([LLMMessage("user", "hello")])

    assert result.provider == "local"
    assert first.calls == 1
    assert result.provider_errors == ("cloud attempt 1: HTTPStatusError",)


@pytest.mark.asyncio
async def test_transient_network_failure_retries_and_recovers():
    provider = TransientNetworkProvider("cloud")

    result = await LLMGateway([provider]).chat([LLMMessage("user", "hello")])

    assert result.content == "网络恢复"
    assert provider.calls == 2
    assert result.provider_errors == ("cloud attempt 1: ConnectError",)


@pytest.mark.asyncio
async def test_deepseek_failure_uses_qwen_before_ollama():
    deepseek = AuthenticationFailureProvider("DeepSeek")
    qwen = WorkingProvider("Qwen Cloud")
    ollama = WorkingProvider("Ollama Local")

    result = await LLMGateway([deepseek, qwen, ollama]).chat([LLMMessage("user", "你好")])

    assert result.provider == "Qwen Cloud"
    assert [deepseek.calls, qwen.calls, ollama.calls] == [1, 1, 0]


@pytest.mark.asyncio
async def test_ollama_remains_last_when_both_cloud_providers_fail():
    deepseek = AuthenticationFailureProvider("DeepSeek")
    qwen = AuthenticationFailureProvider("Qwen Cloud")
    ollama = WorkingProvider("Ollama Local")

    result = await LLMGateway([deepseek, qwen, ollama]).chat([LLMMessage("user", "你好")])

    assert result.provider == "Ollama Local"
    assert [deepseek.calls, qwen.calls, ollama.calls] == [1, 1, 1]


@pytest.mark.asyncio
async def test_media_turn_prefers_native_media_provider_without_changing_text_priority():
    deepseek = WorkingProvider("DeepSeek")
    kimi = MediaWorkingProvider("Kimi Cloud")
    gateway = LLMGateway([deepseek, kimi])

    text_result = await gateway.chat([LLMMessage("user", "纯文字")])
    attachment = LLMAttachment("IMAGE", "photo.png", "image/png", "ignored", 10)
    media_result = await gateway.chat([LLMMessage("user", "看图", (attachment,))])

    assert text_result.provider == "DeepSeek"
    assert media_result.provider == "Kimi Cloud"
    assert [deepseek.calls, kimi.calls] == [1, 1]


@pytest.mark.asyncio
async def test_explicit_model_preference_moves_kimi_k3_first_for_one_task_only():
    deepseek = WorkingProvider("DeepSeek")
    deepseek.model = "deepseek-v4-flash"
    kimi = WorkingProvider("Kimi Cloud")
    kimi.model = "kimi-k3"
    gateway = LLMGateway([deepseek, kimi])
    messages = [LLMMessage("user", "帮我主动找朋友聊聊一个话题")]

    preferred_route = gateway.route_decision(messages, preferred_models=("kimi-k3",))
    preferred_result = await gateway.chat(messages, preferred_models=("kimi-k3",))
    normal_result = await gateway.chat(messages)

    assert [provider.name for provider in preferred_route.providers] == ["Kimi Cloud", "DeepSeek"]
    assert preferred_result.provider == "Kimi Cloud"
    assert normal_result.provider == "DeepSeek"
    assert [deepseek.calls, kimi.calls] == [1, 1]


@pytest.mark.asyncio
async def test_explicit_kimi_k3_preference_still_falls_back_safely():
    deepseek = WorkingProvider("DeepSeek")
    deepseek.model = "deepseek-v4-flash"
    kimi = AuthenticationFailureProvider("Kimi Cloud")
    kimi.model = "kimi-k3"

    result = await LLMGateway([deepseek, kimi]).chat(
        [LLMMessage("user", "主动话题")],
        preferred_models=("kimi-k3",),
    )

    assert result.provider == "DeepSeek"
    assert [kimi.calls, deepseek.calls] == [1, 1]
    assert result.provider_errors == ("Kimi Cloud attempt 1: HTTPStatusError",)
