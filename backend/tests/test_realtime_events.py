from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.events import EventHub
from app.core.security import create_access_token
from app.main import app


def test_websocket_requires_a_valid_session_token():
    client = TestClient(app)
    try:
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect("/api/v1/ws?token=invalid-token") as websocket:
                websocket.receive_json()
        assert rejected.value.code == 4401
    finally:
        client.close()


def test_websocket_accepts_a_valid_session_and_answers_heartbeat():
    client = TestClient(app)
    try:
        token = create_access_token("realtime-test-user")
        with client.websocket_connect(f"/api/v1/ws?token={token}") as websocket:
            websocket.send_text("ping")
            payload = websocket.receive_json()
        assert payload["event"] == "pong"
        assert payload["data"] == {}
        assert payload["timestamp"]
    finally:
        client.close()


class _Socket:
    def __init__(self, *, fail: bool = False) -> None:
        self.accepted = False
        self.fail = fail
        self.payloads: list[dict] = []

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict) -> None:
        if self.fail:
            raise RuntimeError("connection lost")
        self.payloads.append(payload)


@pytest.mark.asyncio
async def test_event_hub_broadcasts_and_removes_stale_connections():
    hub = EventHub()
    healthy = _Socket()
    stale = _Socket(fail=True)
    await hub.connect(healthy)  # type: ignore[arg-type]
    await hub.connect(stale)  # type: ignore[arg-type]

    await hub.broadcast("message_sent", {"message_id": "m-1"})

    assert healthy.accepted and stale.accepted
    assert healthy.payloads[0]["event"] == "message_sent"
    assert healthy.payloads[0]["data"] == {"message_id": "m-1"}
    assert healthy in hub._connections
    assert stale not in hub._connections
