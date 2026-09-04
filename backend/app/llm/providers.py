from __future__ import annotations

import asyncio
from pathlib import Path
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.llm.base import LLMAttachment, LLMMessage, LLMProvider, LLMResponse
from app.llm.budget import output_token_budget


KIMI_RESPONSE_STYLE = """Kimi 专属输出风格硬要求：
不要使用固定签名、固定落款或每次重复相同的表情符号，尤其不要把“🐾”“✨”组合放在回复末尾。
不要连续使用多个 Emoji；普通回复整条最多使用一个 Emoji，并且只在确实符合当前语境时偶尔使用。
大多数回复不需要 Emoji。不要为了表现可爱而强行添加表情，活泼和亲切应主要通过自然措辞体现。
如果用户明确要求原样输出某段内容或指定 Emoji，则严格服从该格式要求。"""


def _kimi_styled_messages(messages: list[LLMMessage]) -> list[LLMMessage]:
    """Attach Kimi-only style guidance without mutating caller-owned messages."""

    styled: list[LLMMessage] = []
    injected = False
    for message in messages:
        if message.role == "system" and not injected:
            styled.append(
                LLMMessage(
                    role=message.role,
                    content=f"{message.content.rstrip()}\n\n{KIMI_RESPONSE_STYLE}",
                    attachments=message.attachments,
                )
            )
            injected = True
        else:
            styled.append(message)
    if not injected:
        styled.insert(0, LLMMessage(role="system", content=KIMI_RESPONSE_STYLE))
    return styled


class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, *, name: str, base_url: str, model: str, api_key: str, timeout: float = 25.0) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    async def chat(self, messages: list[LLMMessage]) -> LLMResponse:
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={
                    "model": self.model,
                    "messages": [{"role": item.role, "content": item.content} for item in messages],
                    "stream": False,
                    "max_tokens": output_token_budget(messages),
                },
            )
            response.raise_for_status()
            data = response.json()
        usage = data.get("usage") or {}
        choice = data["choices"][0]
        return LLMResponse(
            content=(choice.get("message") or {}).get("content") or "",
            model=data.get("model") or self.model,
            provider=self.name,
            latency_ms=int((time.perf_counter() - started) * 1000),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            finish_reason=choice.get("finish_reason") or "stop",
        )

    async def test_connection(self) -> bool:
        # Connection errors are commonly transient on residential networks.
        # Retry the network handshake without retrying authentication failures.
        last_error: Exception | None = None
        for attempt, delay in enumerate((0.0, 0.5, 1.5)):
            if delay:
                await asyncio.sleep(delay)
            try:
                response = await self._request_models()
                response.raise_for_status()
                return True
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
                last_error = exc
                if attempt == 2:
                    raise
        if last_error is not None:
            raise last_error
        return False

    async def models(self) -> list[str]:
        response = await self._request_models()
        response.raise_for_status()
        return [
            str(model_id)
            for item in response.json().get("data", [])
            if (model_id := item.get("id") or item.get("model") or item.get("name"))
        ]

    def _models_url(self) -> str:
        return f"{self.base_url}/models"

    async def _request_models(self) -> httpx.Response:
        async with httpx.AsyncClient(timeout=min(self.timeout, 10)) as client:
            return await client.get(
                self._models_url(),
                headers={"Authorization": f"Bearer {self.api_key}"},
            )


class QwenProvider(OpenAICompatibleProvider):
    """Alibaba Model Studio's OpenAI-compatible chat endpoint.

    Model Studio exposes model discovery under ``/api/v1/models`` even when
    chat completions use ``/compatible-mode/v1``. Keeping that distinction in
    one provider prevents the dashboard connection test from falsely failing.
    """

    def _models_url(self) -> str:
        parsed = urlsplit(self.base_url)
        compatible_suffix = "/compatible-mode/v1"
        path = parsed.path.rstrip("/")
        if path.endswith(compatible_suffix):
            path = f"{path[:-len(compatible_suffix)]}/api/v1/models"
            return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))
        return super()._models_url()


class KimiProvider(OpenAICompatibleProvider):
    """Moonshot Kimi chat provider with short-lived image/video uploads.

    Kimi's native multimodal protocol uploads each local asset to ``/files``
    and references it with an ``ms://`` URL in the chat content array.  The
    remote copies are deleted in ``finally`` so a failed chat or fallback does
    not silently leave the user's media in the cloud.
    """

    supports_attachments = True

    @staticmethod
    def _completion_budget(messages: list[LLMMessage]) -> int:
        """Reserve room for Kimi K3's mandatory reasoning before its answer.

        K3 counts ``reasoning_content`` and the user-visible ``content``
        against the same completion budget.  Neko's generic 500-token chat
        budget can therefore return HTTP 200 with an empty final answer even
        for a healthy provider.  Long-form jobs need proportionally more room.
        """

        return 8192 if output_token_budget(messages) > 500 else 2048

    async def chat(self, messages: list[LLMMessage]) -> LLMResponse:
        started = time.perf_counter()
        uploaded_ids: list[str] = []
        styled_messages = _kimi_styled_messages(messages)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                payload_messages: list[dict[str, object]] = []
                for message in styled_messages:
                    if not message.attachments:
                        payload_messages.append({"role": message.role, "content": message.content})
                        continue
                    parts: list[dict[str, object]] = []
                    for attachment in message.attachments:
                        file_id = await self._upload_attachment(client, attachment)
                        uploaded_ids.append(file_id)
                        content_type = "image_url" if attachment.kind.upper() == "IMAGE" else "video_url"
                        parts.append({"type": content_type, content_type: {"url": f"ms://{file_id}"}})
                    if message.content:
                        parts.append({"type": "text", "text": message.content})
                    payload_messages.append({"role": message.role, "content": parts})

                completion_budget = self._completion_budget(styled_messages)
                data: dict[str, object] = {}
                for attempt, current_budget in enumerate((completion_budget, completion_budget * 2)):
                    payload = {
                        "model": self.model,
                        "messages": payload_messages,
                        "stream": False,
                        # Kimi K3 always reasons.  The current Moonshot API
                        # deprecates max_tokens in favour of this field.
                        "max_completion_tokens": current_budget,
                        "reasoning_effort": "low",
                    }
                    response: httpx.Response | None = None
                    for network_attempt, delay in enumerate((0.0, 0.5, 1.5)):
                        if delay:
                            await asyncio.sleep(delay)
                        try:
                            response = await client.post(
                                f"{self.base_url}/chat/completions",
                                headers={
                                    "Authorization": f"Bearer {self.api_key}",
                                    "Content-Type": "application/json",
                                },
                                json=payload,
                            )
                            break
                        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                            if network_attempt == 2:
                                raise exc
                    assert response is not None
                    response.raise_for_status()
                    data = response.json()
                    choice = (data.get("choices") or [{}])[0]
                    message = choice.get("message") or {}
                    if str(message.get("content") or "").strip() or choice.get("finish_reason") != "length":
                        break
                    # A healthy K3 request may spend its whole first budget on
                    # mandatory reasoning. Retry the chat only, reusing the
                    # already-uploaded ms:// assets, then clean them up once.
                    if attempt == 1:
                        break
            finally:
                for file_id in uploaded_ids:
                    try:
                        await client.delete(
                            f"{self.base_url}/files/{file_id}",
                            headers={"Authorization": f"Bearer {self.api_key}"},
                        )
                    except httpx.HTTPError:
                        # The answer (or primary error) must not be hidden by a
                        # best-effort cleanup failure. Kimi also expires files.
                        pass

        usage = data.get("usage") or {}
        choice = data["choices"][0]
        message = choice.get("message") or {}
        content = str(message.get("content") or "").strip()
        if not content:
            raise ProviderConfigurationError("KIMI_EMPTY_FINAL_ANSWER")
        return LLMResponse(
            content=content,
            model=data.get("model") or self.model,
            provider=self.name,
            latency_ms=int((time.perf_counter() - started) * 1000),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            finish_reason=choice.get("finish_reason") or "stop",
        )

    async def _upload_attachment(self, client: httpx.AsyncClient, attachment: LLMAttachment) -> str:
        kind = attachment.kind.upper()
        if kind not in {"IMAGE", "VIDEO"}:
            raise ProviderConfigurationError("KIMI_MEDIA_TYPE_UNSUPPORTED")
        path = Path(attachment.local_path)
        if not path.is_file():
            raise ProviderConfigurationError("KIMI_MEDIA_FILE_MISSING")
        if attachment.size_bytes <= 0 or attachment.size_bytes > 100 * 1024 * 1024:
            raise ProviderConfigurationError("KIMI_MEDIA_TOO_LARGE")
        purpose = "image" if kind == "IMAGE" else "video"
        response: httpx.Response | None = None
        for attempt, delay in enumerate((0.0, 0.5, 1.5)):
            if delay:
                await asyncio.sleep(delay)
            try:
                # Reopen on every attempt so a failed multipart request can
                # never retry from an already-consumed file position.
                with path.open("rb") as handle:
                    response = await client.post(
                        f"{self.base_url}/files",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        data={"purpose": purpose},
                        files={"file": (attachment.file_name, handle, attachment.mime_type)},
                    )
                break
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if attempt == 2:
                    raise exc
        assert response is not None
        response.raise_for_status()
        file_id = str(response.json().get("id") or "").strip()
        if not file_id:
            raise ProviderConfigurationError("KIMI_MEDIA_UPLOAD_EMPTY_ID")
        return file_id


class OllamaProvider(LLMProvider):
    def __init__(self, *, base_url: str = "http://127.0.0.1:11434", model: str = "qwen3:8b", timeout: float = 60.0) -> None:
        self.name = "ollama"
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    async def chat(self, messages: list[LLMMessage]) -> LLMResponse:
        started = time.perf_counter()
        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "keep_alive": "30m",
            "options": {
                "temperature": 0.65,
                "top_p": 0.9,
                "repeat_penalty": 1.05,
                "num_ctx": 8192,
                "num_predict": output_token_budget(messages, local=True),
            },
        }
        if self.model.casefold().startswith("qwen3"):
            # Daily managed chat values concise answers more than exposed
            # reasoning traces. Apply this to text and multimodal Qwen 3 models;
            # complex evidence is still supplied in-context.
            payload["think"] = False
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/api/chat",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        return LLMResponse(
            content=(data.get("message") or {}).get("content") or "",
            model=data.get("model") or self.model,
            provider=self.name,
            latency_ms=int((time.perf_counter() - started) * 1000),
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
            finish_reason=data.get("done_reason") or "stop",
        )

    async def test_connection(self) -> bool:
        async with httpx.AsyncClient(timeout=5) as client:
            return (await client.get(f"{self.base_url}/api/tags")).is_success

    async def models(self) -> list[str]:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{self.base_url}/api/tags")
            response.raise_for_status()
            return [item["name"] for item in response.json().get("models", [])]


class SimulatorProvider(LLMProvider):
    name = "simulator"

    async def chat(self, messages: list[LLMMessage]) -> LLMResponse:
        await asyncio.sleep(0)
        current = messages[-1].content.rsplit("当前消息：", 1)[-1].strip()
        if "吃什么" in current:
            reply = "唔…要不要吃点热乎的？火锅或者拉面都不错呀。"
        elif "你好" in current:
            reply = "你好呀，今天过得怎么样？"
        else:
            reply = "收到啦，我在认真听。你想继续聊聊吗？"
        return LLMResponse(reply, "neko-simulator-v1", self.name, 1, 12, 18)

    async def test_connection(self) -> bool:
        return True

    async def models(self) -> list[str]:
        return ["neko-simulator-v1"]


class ProviderConfigurationError(RuntimeError):
    """A local provider configuration is unavailable and should not be retried."""


class UnavailableProvider(LLMProvider):
    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.reason = reason

    async def chat(self, messages: list[LLMMessage]) -> LLMResponse:
        raise ProviderConfigurationError(self.reason)

    async def test_connection(self) -> bool:
        return False

    async def models(self) -> list[str]:
        return []
