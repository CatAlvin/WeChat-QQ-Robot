from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True, slots=True)
class InboundAttachment:
    kind: str
    segment_type: str
    segment_index: int
    file_name: str
    source_ref: str | None = None
    file_id: str | None = None
    size_bytes: int | None = None
    mime_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class InboundEvent:
    platform: str
    message_id: str
    conversation_id: str
    sender_id: str
    sender_name: str
    content: str
    account_id: str | None = None
    is_group: bool = False
    group_id: str | None = None
    group_name: str | None = None
    mentioned_user: bool = False
    author: str = "CONTACT"
    message_type: str = "TEXT"
    reply_to_message_id: str | None = None
    attachments: tuple[InboundAttachment, ...] = ()
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class OutboundAttachment:
    kind: str
    file_path: str
    file_name: str
    mime_type: str | None = None


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    platform: str
    target_id: str
    content: str
    reply_to_message_id: str
    is_group: bool = False
    attachments: tuple[OutboundAttachment, ...] = ()


@dataclass(frozen=True, slots=True)
class SendPermit:
    target_id: str
    whitelisted: bool
    policy_passed: bool
    guard_passed: bool
    rate_limit_passed: bool
    global_auto: bool
    kill_switch_off: bool
    scheduled_allowed: bool = False

    def assert_valid_for(self, message: OutboundMessage) -> None:
        checks = (
            self.target_id == message.target_id,
            self.whitelisted or self.scheduled_allowed,
            self.policy_passed,
            self.guard_passed,
            self.rate_limit_passed,
            self.global_auto,
            self.kill_switch_off,
        )
        if not all(checks):
            raise PermissionError("Connector refused an invalid send permit")


@dataclass(frozen=True, slots=True)
class SendResult:
    success: bool
    external_message_id: str | None = None
    error_code: str | None = None
    error_detail: str | None = None


class ChannelConnector(ABC):
    platform: str
    real_channel: bool = True

    @abstractmethod
    async def send(self, message: OutboundMessage, permit: SendPermit) -> SendResult: ...

    @abstractmethod
    async def status(self) -> dict[str, Any]: ...
