from __future__ import annotations

import json

import httpx
import pytest

from app.llm.base import LLMMessage, LLMResponse
from app.llm.gateway import LLMGateway
from app.llm.providers import OpenAICompatibleProvider, OllamaProvider
from app.services.long_form import (
    LongFormIncompleteError,
    add_long_form_instructions,
    generate_long_form,
    output_token_budget,
    plan_long_form_request,
)


class ChunkedProvider:
    name = "chunked-provider"
    model = "chunked-model"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: list[LLMMessage]) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return LLMResponse("第一部分已经完整展开。", self.model, self.name, 10, 100, 4096, "length")
        return LLMResponse("第二部分补齐例子和结论。", self.model, self.name, 8, 120, 300, "stop")

    async def test_connection(self) -> bool:
        return True

    async def models(self) -> list[str]:
        return [self.model]


def _mock_clients(monkeypatch, handler):
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def test_long_form_intent_is_explicit_without_turning_every_detailed_reply_into_a_file() -> None:
    plan = plan_long_form_request("给我一篇长文本分析，介绍 MCP 服务器并举例")
    assert plan.requested is True
    assert plan.format == "PDF"
    assert plan_long_form_request("请用 TXT 生成一份完整教程").format == "TXT"
    assert plan_long_form_request("请详细说明这个普通话题").requested is False


def test_long_form_messages_receive_larger_cloud_and_local_output_budgets() -> None:
    normal = [LLMMessage("user", "你好")]
    plan = plan_long_form_request("写一篇长文本分析")
    document = add_long_form_instructions([LLMMessage("user", "写一篇长文本分析")], plan)

    assert output_token_budget(normal) == 500
    assert output_token_budget(normal, local=True) == 512
    assert output_token_budget(document) == 4096
    assert output_token_budget(document, local=True) == 2048


@pytest.mark.asyncio
async def test_cloud_and_ollama_protocols_use_dynamic_document_budgets(monkeypatch) -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.read()))
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                json={
                    "model": "test-model",
                    "choices": [{"message": {"content": "完整文档"}, "finish_reason": "stop"}],
                },
            )
        return httpx.Response(
            200,
            json={"model": "qwen3:8b", "message": {"content": "本地完整文档"}, "done_reason": "stop"},
        )

    _mock_clients(monkeypatch, handler)
    plan = plan_long_form_request("给我一篇长文本分析")
    messages = add_long_form_instructions([LLMMessage("user", "给我一篇长文本分析")], plan)
    cloud = OpenAICompatibleProvider(
        name="DeepSeek",
        base_url="https://api.example.test/v1",
        model="test-model",
        api_key="test-key",
    )
    local = OllamaProvider(model="qwen3:8b")

    await cloud.chat(messages)
    await local.chat(messages)

    assert payloads[0]["max_tokens"] == 4096
    assert payloads[1]["options"]["num_predict"] == 2048


@pytest.mark.asyncio
async def test_length_stopped_document_is_continued_and_merged() -> None:
    provider = ChunkedProvider()
    gateway = LLMGateway([provider])
    plan = plan_long_form_request("给我一篇长文本分析")
    messages = add_long_form_instructions([LLMMessage("user", "给我一篇长文本分析")], plan)

    result = await generate_long_form(gateway, messages)

    assert provider.calls == 2
    assert result.continued is True
    assert result.chunks == 2
    assert "第一部分已经完整展开" in result.response.content
    assert "第二部分补齐例子和结论" in result.response.content
    assert result.response.output_tokens == 4396
    assert result.response.finish_reason == "stop"


@pytest.mark.asyncio
async def test_document_is_not_returned_when_every_chunk_is_still_truncated() -> None:
    class NeverFinishes(ChunkedProvider):
        async def chat(self, messages: list[LLMMessage]) -> LLMResponse:
            self.calls += 1
            return LLMResponse(f"未完成部分 {self.calls}", self.model, self.name, 1, 10, 4096, "length")

    provider = NeverFinishes()
    plan = plan_long_form_request("给我一篇长文本分析")
    messages = add_long_form_instructions([LLMMessage("user", "给我一篇长文本分析")], plan)

    with pytest.raises(LongFormIncompleteError):
        await generate_long_form(LLMGateway([provider]), messages, max_chunks=2)

    assert provider.calls == 2
