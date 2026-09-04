from __future__ import annotations

import codecs
from pathlib import Path


def test_unicode_powershell_scripts_have_utf8_bom() -> None:
    """Windows PowerShell 5.1 otherwise decodes UTF-8 scripts as ANSI."""

    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    incompatible: list[str] = []

    for script in sorted(scripts_dir.glob("*.ps1")):
        data = script.read_bytes()
        payload = data[len(codecs.BOM_UTF8) :] if data.startswith(codecs.BOM_UTF8) else data
        if any(byte >= 0x80 for byte in payload) and not data.startswith(codecs.BOM_UTF8):
            incompatible.append(script.name)

    assert not incompatible, (
        "PowerShell scripts containing Unicode must be UTF-8 with BOM for Windows "
        f"PowerShell 5.1 compatibility: {', '.join(incompatible)}"
    )


def test_supervisor_checks_http_health_instead_of_trusting_only_recorded_pids() -> None:
    supervisor = (
        Path(__file__).resolve().parents[2] / "scripts" / "neko-supervisor.ps1"
    ).read_text(encoding="utf-8-sig")

    assert "http://127.0.0.1:8000/health" in supervisor
    assert "$BackendHealthFailures -ge 3" in supervisor
    assert "HEALTH_RECOVERY" in supervisor
    assert "backend_process_running" in supervisor


def test_tray_uses_neko_brand_icon_with_safe_fallback() -> None:
    project_root = Path(__file__).resolve().parents[2]
    tray = (project_root / "scripts" / "neko-tray.ps1").read_text(encoding="utf-8-sig")

    assert (project_root / "assets" / "neko-ai.ico").is_file()
    assert "assets\\neko-ai.ico" in tray
    assert "[Drawing.Icon]::new($TrayIconFile)" in tray
    assert "[Drawing.SystemIcons]::Application" in tray
    assert "Local\\NekoAiTray" in tray


def test_supervisor_keeps_tray_running_and_reports_its_status() -> None:
    supervisor = (
        Path(__file__).resolve().parents[2] / "scripts" / "neko-supervisor.ps1"
    ).read_text(encoding="utf-8-sig")

    assert "function Start-NekoTray" in supervisor
    assert "tray_running" in supervisor
    assert "tray_pid" in supervisor
