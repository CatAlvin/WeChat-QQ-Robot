from __future__ import annotations

import asyncio

import httpx
from sqlalchemy import text

from app.core.config import Settings
from app.core.security import CredentialVault
from app.models.entities import Account, ApiCredential
from app.services import system_health


class _Response:
    def __init__(self, status_code: int = 200, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> dict:
        return self._body


def test_repository_migration_head_is_discovered_dynamically() -> None:
    heads = system_health._expected_migration_heads()
    assert heads
    assert all(value.strip() for value in heads)


def test_self_check_accepts_the_discovered_database_head(db, monkeypatch) -> None:
    db.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
    db.execute(text("INSERT INTO alembic_version(version_num) VALUES ('current-head')"))
    db.commit()
    monkeypatch.setattr(system_health, "_expected_migration_heads", lambda: {"current-head"})

    class Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, url: str, **kwargs):
            return _Response(body={"models": []})

    async def media_status(self, state):
        return {
            "vision": {"service_online": True, "model_installed": True},
            "speech": {"package_installed": True},
            "documents": {"pdf": True},
        }

    monkeypatch.setattr(system_health.httpx, "AsyncClient", Client)
    monkeypatch.setattr(system_health.MediaUnderstandingService, "status", media_status)
    result = asyncio.run(system_health.run_self_check(db, Settings()))
    database = next(item for item in result["checks"] if item["key"] == "mysql")
    assert database["status"] == "WARNING"
    assert "current-head" in database["detail"]


def test_napcat_monitor_replaces_stale_online_status(db, tmp_path, monkeypatch) -> None:
    settings = Settings(data_dir=tmp_path)
    credential = ApiCredential(
        label="NapCat test token",
        provider="QQ_NAPCAT",
        encrypted_secret=CredentialVault(settings).encrypt("test-token-123456"),
        masked_hint="test",
    )
    db.add(credential)
    db.flush()
    account = Account(
        platform="QQ_NAPCAT",
        display_name="Test NapCat",
        connector_kind="NAPCAT_ONEBOT11",
        status="ONLINE",
        enabled=True,
        experimental=True,
        credential_id=credential.id,
        config={"api_base": "http://127.0.0.1:3001"},
    )
    db.add(account)
    db.commit()

    class OfflineClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, url: str, **kwargs):
            request = httpx.Request("GET", url)
            raise httpx.ConnectError("offline", request=request)

    monkeypatch.setattr(system_health.httpx, "AsyncClient", OfflineClient)
    result = asyncio.run(system_health.inspect_active_napcat(db, settings, update_account=True))
    assert result.changed is True
    assert result.check["status"] == "ERROR"
    assert account.status == "DISCONNECTED"
    assert account.last_error == "ConnectError"

    class OnlineClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, url: str, **kwargs):
            return _Response(body={"status": "ok", "retcode": 0, "data": {"online": True}})

    monkeypatch.setattr(system_health.httpx, "AsyncClient", OnlineClient)
    recovered = asyncio.run(system_health.inspect_active_napcat(db, settings, update_account=True))
    assert recovered.changed is True
    assert recovered.check["status"] == "OK"
    assert account.status == "ONLINE"
    assert account.last_error is None
