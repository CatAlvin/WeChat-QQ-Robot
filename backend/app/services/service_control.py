from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


ALLOWED_ACTIONS = {"start", "stop", "restart"}


def _runtime_paths() -> tuple[Path, Path]:
    project_root = Path(__file__).resolve().parents[3]
    runtime_dir = project_root / "data" / "runtime"
    return runtime_dir / "supervisor-status.json", runtime_dir / "supervisor-command.json"


def supervisor_status() -> dict[str, Any]:
    status_path, _ = _runtime_paths()
    fallback = {
        "installed": False,
        "online": False,
        "status": "NOT_RUNNING",
        "pid": None,
        "restart_count": 0,
        "last_restart_reason": None,
        "last_heartbeat_at": None,
    }
    try:
        # Windows PowerShell 5.1 writes `-Encoding utf8` files with a BOM.
        # utf-8-sig accepts those files as well as BOM-less UTF-8 produced by
        # PowerShell 7, so the dashboard can observe either supervisor host.
        payload = json.loads(status_path.read_text(encoding="utf-8-sig"))
        heartbeat = datetime.fromisoformat(
            str(payload.get("heartbeat_at") or payload.get("last_heartbeat_at") or "").replace("Z", "+00:00")
        )
        fresh = (datetime.now(timezone.utc) - heartbeat.astimezone(timezone.utc)).total_seconds() < 12
        pid = int(payload.get("pid") or 0)
        # A fresh heartbeat is the portable liveness signal. os.kill(pid, 0)
        # is not a probe on Windows and raises WinError 87 even for a live PID.
        return {**fallback, **payload, "installed": True, "online": bool(fresh and pid > 0)}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {**fallback, "installed": status_path.exists()}


def _send_supervisor_command(action: str) -> bool:
    status = supervisor_status()
    if not status["online"]:
        return False
    _, command_path = _runtime_paths()
    command_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "action": action,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "request_id": os.urandom(12).hex(),
    }
    with NamedTemporaryFile("w", encoding="utf-8", dir=command_path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False)
        temporary = Path(handle.name)
    temporary.replace(command_path)
    return True


def dispatch_service_action(action: str) -> None:
    normalized = action.lower().strip()
    if normalized not in ALLOWED_ACTIONS:
        raise ValueError("不支持的服务操作")
    if _send_supervisor_command(normalized):
        return
    project_root = Path(__file__).resolve().parents[3]
    helper = project_root / "scripts" / "control-helper.ps1"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(helper), "-Action", normalized],
        cwd=str(project_root),
        creationflags=flags,
        close_fds=True,
    )
