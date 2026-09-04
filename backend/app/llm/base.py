from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LLMAttachment:
    kind: str
    file_name: str
    mime_type: str
    local_path: str
    size_bytes: int
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class LLMMessage:
    role: str
    content: str
    attachments: tuple[LLMAttachment, ...] = ()


@dataclass(frozen=True, slots=True)
class LLMResponse:
    content: str
    model: str
    provider: str
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str = "stop"
    error: str | None = None
    provider_errors: tuple[str, ...] = ()


class LLMProvider(ABC):
    name: str

    @abstractmethod
    async def chat(self, messages: list[LLMMessage]) -> LLMResponse: ...

    @abstractmethod
    async def test_connection(self) -> bool: ...

    @abstractmethod
    async def models(self) -> list[str]: ...
