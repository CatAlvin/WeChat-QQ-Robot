from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.services import service_control


def _write_status(path, *, heartbeat: datetime) -> None:
    path.write_text(
        json.dumps(
            {
                "pid": 43210,
                "heartbeat_at": heartbeat.isoformat(),
                "desired_state": "RUNNING",
                "services_running": True,
                "restart_count": 3,
            }
        ),
        encoding="utf-8-sig",
    )


def test_supervisor_status_accepts_windows_powershell_bom(tmp_path, monkeypatch):
    status_path = tmp_path / "supervisor-status.json"
    command_path = tmp_path / "supervisor-command.json"
    _write_status(status_path, heartbeat=datetime.now(timezone.utc))
    monkeypatch.setattr(service_control, "_runtime_paths", lambda: (status_path, command_path))

    status = service_control.supervisor_status()

    assert status["installed"] is True
    assert status["online"] is True
    assert status["pid"] == 43210
    assert status["restart_count"] == 3


def test_supervisor_status_keeps_installed_state_for_stale_heartbeat(tmp_path, monkeypatch):
    status_path = tmp_path / "supervisor-status.json"
    command_path = tmp_path / "supervisor-command.json"
    _write_status(status_path, heartbeat=datetime.now(timezone.utc) - timedelta(minutes=1))
    monkeypatch.setattr(service_control, "_runtime_paths", lambda: (status_path, command_path))

    status = service_control.supervisor_status()

    assert status["installed"] is True
    assert status["online"] is False
