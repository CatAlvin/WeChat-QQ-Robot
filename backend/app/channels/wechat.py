from __future__ import annotations

from typing import Any

from app.channels.base import ChannelConnector, OutboundMessage, SendPermit, SendResult


class WeChatDisabledConnector(ChannelConnector):
    platform = "WECHAT"
    real_channel = True

    async def send(self, message: OutboundMessage, permit: SendPermit) -> SendResult:
        permit.assert_valid_for(message)
        return SendResult(
            False,
            error_code="WECHAT_CONNECTOR_BLOCKED",
            error_detail="个人微信未发现可接受的官方接入方式；非官方连接器默认禁用。",
        )

    async def status(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "status": "BLOCKED",
            "real_channel": True,
            "experimental": True,
            "reason": "个人微信安全接入门禁未通过",
        }

