from __future__ import annotations

import httpx
import pytest

from app.llm.base import LLMAttachment, LLMMessage
from app.llm.providers import KimiProvider, OllamaProvider, OpenAICompatibleProvider, QwenProvider


def _mock_clients(monkeypatch, handler):
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


@pytest.mark.asyncio
@pytest.mark.parametrize("name,base_url", [("DeepSeek", "https://api.deepseek.com"), ("OpenAI", "https://api.openai.com/v1")])
async def test_openai_compatible_connection_models_chat_and_token_usage(monkeypatch, name, base_url):
    seen_authorization = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_authorization.append(request.headers.get("Authorization"))
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "model-a"}, {"id": "model-b"}]})
        assert request.url.path.endswith("/chat/completions")
        return httpx.Response(
            200,
            json={
                "model": "model-a",
                "choices": [{"message": {"content": "安全回复"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 7},
            },
        )

    _mock_clients(monkeypatch, handler)
    provider = OpenAICompatibleProvider(name=name, base_url=base_url, model="model-a", api_key="test-secret-key")
    assert await provider.test_connection()
    assert await provider.models() == ["model-a", "model-b"]
    response = await provider.chat([LLMMessage("user", "你好")])
    assert response.content == "安全回复"
    assert response.input_tokens == 12 and response.output_tokens == 7
    assert seen_authorization == ["Bearer test-secret-key"] * 3


@pytest.mark.asyncio
async def test_openai_compatible_connection_check_recovers_from_transient_network_errors(monkeypatch):
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectError("temporary", request=request)
        return httpx.Response(200, json={"data": [{"id": "model-a"}]})

    _mock_clients(monkeypatch, handler)
    provider = OpenAICompatibleProvider(
        name="DeepSeek",
        base_url="https://api.deepseek.com",
        model="model-a",
        api_key="test-secret-key",
    )

    assert await provider.test_connection()
    assert attempts == 3


@pytest.mark.asyncio
async def test_qwen_uses_native_model_discovery_and_openai_compatible_chat(monkeypatch):
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        assert request.headers.get("Authorization") == "Bearer qwen-test-key"
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json={"data": [{"id": "qwen-plus"}]})
        assert request.url.path == "/compatible-mode/v1/chat/completions"
        return httpx.Response(
            200,
            json={
                "model": "qwen-plus",
                "choices": [{"message": {"content": "千问兜底回复"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 5},
            },
        )

    _mock_clients(monkeypatch, handler)
    provider = QwenProvider(
        name="Qwen Cloud",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen-plus",
        api_key="qwen-test-key",
    )

    assert await provider.test_connection()
    assert await provider.models() == ["qwen-plus"]
    response = await provider.chat([LLMMessage("user", "你好")])
    assert response.content == "千问兜底回复"
    assert seen_paths == ["/api/v1/models", "/api/v1/models", "/compatible-mode/v1/chat/completions"]


@pytest.mark.asyncio
async def test_kimi_uploads_image_and_video_uses_ms_urls_and_deletes_remote_files(monkeypatch, tmp_path):
    image = tmp_path / "photo.png"
    video = tmp_path / "clip.mp4"
    image.write_bytes(b"png-test")
    video.write_bytes(b"mp4-test")
    uploads: list[bytes] = []
    deleted: list[str] = []
    chat_payload: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_payload
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "kimi-k3"}]})
        if request.url.path == "/v1/files" and request.method == "POST":
            uploads.append(request.read())
            return httpx.Response(200, json={"id": f"file-{len(uploads)}"})
        if request.url.path == "/v1/chat/completions":
            chat_payload = __import__("json").loads(request.read())
            return httpx.Response(
                200,
                json={
                    "model": "kimi-k3",
                    "choices": [{"message": {"content": "图片和视频已理解"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 25, "completion_tokens": 9},
                },
            )
        if request.method == "DELETE" and request.url.path.startswith("/v1/files/"):
            deleted.append(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"deleted": True})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    _mock_clients(monkeypatch, handler)
    provider = KimiProvider(
        name="Kimi Cloud",
        base_url="https://api.moonshot.cn/v1",
        model="kimi-k3",
        api_key="kimi-test-key",
    )
    assert await provider.test_connection()
    response = await provider.chat(
        [
            LLMMessage(
                "user",
                "请分析这些媒体",
                (
                    LLMAttachment("IMAGE", image.name, "image/png", str(image), image.stat().st_size, "a" * 64),
                    LLMAttachment("VIDEO", video.name, "video/mp4", str(video), video.stat().st_size, "b" * 64),
                ),
            )
        ]
    )

    assert response.content == "图片和视频已理解"
    assert len(uploads) == 2
    assert b'name="purpose"' in uploads[0] and b"image" in uploads[0]
    assert b'name="purpose"' in uploads[1] and b"video" in uploads[1]
    assert chat_payload["messages"][0]["role"] == "system"
    assert "不要使用固定签名" in chat_payload["messages"][0]["content"]
    assert "整条最多使用一个 Emoji" in chat_payload["messages"][0]["content"]
    assert chat_payload["reasoning_effort"] == "low"
    assert chat_payload["max_completion_tokens"] == 2048
    assert "max_tokens" not in chat_payload
    parts = chat_payload["messages"][1]["content"]
    assert parts == [
        {"type": "image_url", "image_url": {"url": "ms://file-1"}},
        {"type": "video_url", "video_url": {"url": "ms://file-2"}},
        {"type": "text", "text": "请分析这些媒体"},
    ]
    assert deleted == ["file-1", "file-2"]


@pytest.mark.asyncio
async def test_kimi_retries_empty_reasoning_only_response_without_reuploading_media(monkeypatch, tmp_path):
    image = tmp_path / "photo.png"
    image.write_bytes(b"png-test")
    uploads = 0
    deleted: list[str] = []
    budgets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal uploads
        if request.url.path == "/v1/files" and request.method == "POST":
            uploads += 1
            return httpx.Response(200, json={"id": "file-once"})
        if request.url.path == "/v1/chat/completions":
            payload = __import__("json").loads(request.read())
            budgets.append(payload["max_completion_tokens"])
            if len(budgets) == 1:
                return httpx.Response(
                    200,
                    json={
                        "model": "kimi-k3",
                        "choices": [
                            {
                                "message": {"content": "", "reasoning_content": "仍在分析"},
                                "finish_reason": "length",
                            }
                        ],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "model": "kimi-k3",
                    "choices": [{"message": {"content": "最终答案"}, "finish_reason": "stop"}],
                },
            )
        if request.method == "DELETE" and request.url.path == "/v1/files/file-once":
            deleted.append("file-once")
            return httpx.Response(200, json={"deleted": True})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    _mock_clients(monkeypatch, handler)
    provider = KimiProvider(
        name="Kimi Cloud",
        base_url="https://api.moonshot.cn/v1",
        model="kimi-k3",
        api_key="kimi-test-key",
    )
    response = await provider.chat(
        [
            LLMMessage(
                "user",
                "描述图片",
                (LLMAttachment("IMAGE", image.name, "image/png", str(image), image.stat().st_size),),
            )
        ]
    )

    assert response.content == "最终答案"
    assert budgets == [2048, 4096]
    assert uploads == 1
    assert deleted == ["file-once"]


@pytest.mark.asyncio
async def test_kimi_recovers_from_transient_chat_connect_errors(monkeypatch):
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        assert request.url.path == "/v1/chat/completions"
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectError("temporary", request=request)
        return httpx.Response(
            200,
            json={
                "model": "kimi-k3",
                "choices": [{"message": {"content": "已恢复"}, "finish_reason": "stop"}],
            },
        )

    _mock_clients(monkeypatch, handler)
    provider = KimiProvider(
        name="Kimi Cloud",
        base_url="https://api.moonshot.cn/v1",
        model="kimi-k3",
        api_key="kimi-test-key",
    )

    response = await provider.chat([LLMMessage("user", "你好")])

    assert response.content == "已恢复"
    assert attempts == 3


@pytest.mark.asyncio
async def test_kimi_deletes_completed_upload_when_a_later_upload_fails(monkeypatch, tmp_path):
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    upload_count = 0
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upload_count
        if request.url.path == "/v1/files" and request.method == "POST":
            upload_count += 1
            if upload_count == 1:
                return httpx.Response(200, json={"id": "file-first"})
            return httpx.Response(413, json={"error": {"message": "too large"}})
        if request.method == "DELETE":
            deleted.append(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"deleted": True})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    _mock_clients(monkeypatch, handler)
    provider = KimiProvider(
        name="Kimi Cloud",
        base_url="https://api.moonshot.cn/v1",
        model="kimi-k3",
        api_key="kimi-test-key",
    )
    attachments = tuple(
        LLMAttachment("IMAGE", path.name, "image/png", str(path), path.stat().st_size)
        for path in (first, second)
    )

    with pytest.raises(httpx.HTTPStatusError):
        await provider.chat([LLMMessage("user", "分析", attachments)])

    assert deleted == ["file-first"]


@pytest.mark.asyncio
async def test_ollama_connection_models_chat_and_token_usage(monkeypatch):
    seen_chat_payload = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_chat_payload
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}]})
        assert request.url.path == "/api/chat"
        seen_chat_payload = request.read()
        return httpx.Response(
            200,
            json={
                "model": "qwen3:8b",
                "message": {"content": "本地安全回复"},
                "prompt_eval_count": 9,
                "eval_count": 6,
                "done_reason": "stop",
            },
        )

    _mock_clients(monkeypatch, handler)
    provider = OllamaProvider(model="qwen3:8b")
    assert await provider.test_connection()
    assert await provider.models() == ["qwen3:8b"]
    response = await provider.chat([LLMMessage("user", "你好")])
    assert response.content == "本地安全回复"
    assert response.input_tokens == 9 and response.output_tokens == 6
    assert seen_chat_payload is not None
    payload = __import__("json").loads(seen_chat_payload)
    assert payload["keep_alive"] == "30m"
    assert payload["options"]["num_ctx"] == 8192
    assert payload["options"]["temperature"] == 0.65


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["qwen3.5:9b", "qwen3-vl:4b"])
async def test_qwen_local_disables_thinking_for_managed_chat(monkeypatch, model):
    seen_payload = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_payload
        seen_payload = __import__("json").loads(request.read())
        return httpx.Response(
            200,
            json={"model": model, "message": {"content": "简体中文回复"}, "done_reason": "stop"},
        )

    _mock_clients(monkeypatch, handler)
    provider = OllamaProvider(model=model)

    response = await provider.chat([LLMMessage("user", "你好")])

    assert response.content == "简体中文回复"
    assert seen_payload["think"] is False
