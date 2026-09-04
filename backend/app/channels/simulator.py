from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any
from uuid import uuid4

from app.channels.base import ChannelConnector, OutboundMessage, SendPermit, SendResult


class SimulatorConnector(ChannelConnector):
    platform = "SIMULATOR"
    real_channel = False

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    async def send(self, message: OutboundMessage, permit: SendPermit) -> SendResult:
        permit.assert_valid_for(message)
        async with self._lock:
            external_id = f"sim-out-{uuid4()}"
            self.sent.append({**asdict(message), "external_message_id": external_id})
        return SendResult(True, external_id)

    async def status(self) -> dict[str, Any]:
        return {"platform": self.platform, "status": "ONLINE", "real_channel": False, "sent_count": len(self.sent)}

    async def clear(self) -> None:
        async with self._lock:
            self.sent.clear()

