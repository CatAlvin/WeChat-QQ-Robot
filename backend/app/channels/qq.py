from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.channels.base import ChannelConnector, OutboundMessage, SendPermit, SendResult


class QQOfficialBotConnector(ChannelConnector):
    """QQ Official Bot v2 adapter. It never receives an unrestricted send primitive."""

    platform = "QQ"
    real_channel = True

    def __init__(
        self,
        *,
        app_id: str | None = None,
        app_secret: str | None = None,
        api_base: str = "https://api.sgroup.qq.com",
        token_url: str = "https://bots.qq.com/app/getAppAccessToken",
        timeout: float = 15.0,
        enabled: bool = False,
        observed_status: str = "DISCONNECTED",
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.api_base = api_base.rstrip("/")
        self.token_url = token_url
        self.timeout = timeout
        self.enabled = enabled
        self.observed_status = observed_status
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._token_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.app_secret)

    async def _token(self) -> str:
        if not self.enabled:
            raise RuntimeError("QQ Bot 连接器已关闭")
        if self._access_token and time.monotonic() < self._expires_at - 60:
            return self._access_token
        if not self.configured:
            raise RuntimeError("QQ Bot AppID/AppSecret 未配置")
        async with self._token_lock:
            if self._access_token and time.monotonic() < self._expires_at - 60:
                return self._access_token
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(self.token_url, json={"appId": self.app_id, "clientSecret": self.app_secret})
                response.raise_for_status()
                data = response.json()
            self._access_token = data["access_token"]
            self._expires_at = time.monotonic() + int(data.get("expires_in", 7200))
            return self._access_token

    async def send(self, message: OutboundMessage, permit: SendPermit) -> SendResult:
        permit.assert_valid_for(message)
        if not self.enabled:
            return SendResult(False, error_code="QQ_DISABLED", error_detail="QQ Bot 连接器已关闭")
        try:
            token = await self._token()
            if message.is_group:
                endpoint = f"{self.api_base}/v2/groups/{message.target_id}/messages"
            else:
                endpoint = f"{self.api_base}/v2/users/{message.target_id}/messages"
            payload = {"content": message.content, "msg_type": 0, "msg_id": message.reply_to_message_id}
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    endpoint,
                    headers={"Authorization": f"QQBot {token}", "Content-Type": "application/json"},
                    json=payload,
                )
            if not response.is_success:
                return SendResult(False, error_code=f"QQ_HTTP_{response.status_code}", error_detail="QQ 官方 API 拒绝发送")
            data = response.json()
            return SendResult(True, str(data.get("id") or data.get("message_id") or "qq-accepted"))
        except httpx.HTTPError as exc:
            return SendResult(False, error_code="QQ_NETWORK_ERROR", error_detail=type(exc).__name__)
        except (KeyError, RuntimeError) as exc:
            return SendResult(False, error_code="QQ_NOT_CONFIGURED", error_detail=str(exc))

    async def status(self) -> dict[str, Any]:
        if not self.enabled:
            status = "DISABLED"
        elif not self.configured:
            status = "NOT_CONFIGURED"
        else:
            status = self.observed_status
        return {
            "platform": self.platform,
            "status": status,
            "real_channel": True,
            "credential_present": self.configured,
            "enabled": self.enabled,
        }
