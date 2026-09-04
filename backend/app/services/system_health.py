from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.channels.experimental import normalize_loopback_base_url
from app.core.config import Settings
from app.core.security import CredentialVault
from app.models.entities import Account, ApiCredential, AuditLog, ProviderConfig, RuntimeState
from app.services.account_selection import get_active_account
from app.services.media_understanding import MediaUnderstandingService


def _check(key: str, label: str, status: str, detail: str, suggestion: str = "") -> dict[str, str]:
    return {"key": key, "label": label, "status": status, "detail": detail, "suggestion": suggestion}


def _expected_migration_heads() -> set[str]:
    """Read the repository's Alembic heads instead of duplicating a revision."""

    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    return set(ScriptDirectory.from_config(config).get_heads())


@dataclass(frozen=True, slots=True)
class NapCatHealth:
    check: dict[str, str]
    observed_status: str | None
    observed_error: str | None
    changed: bool = False


async def inspect_active_napcat(
    db: Session,
    settings: Settings,
    *,
    update_account: bool = False,
) -> NapCatHealth:
    """Probe the active loopback OneBot API without sending a chat message."""

    account = get_active_account(db, "QQ_NAPCAT")
    if account is None or not account.enabled:
        return NapCatHealth(
            _check("napcat", "NapCat", "WARNING", "没有启用的 NapCat 托管账号。", "在“连接账号”选择一个 NapCat 账号并启用。"),
            None,
            None,
        )

    config = account.config or {}
    credential = db.get(ApiCredential, account.credential_id) if account.credential_id else None
    observed_status = "ERROR"
    observed_error: str | None = None
    if not credential or not config.get("api_base"):
        observed_error = "NAPCAT_CONFIG_INCOMPLETE"
        check = _check("napcat", "NapCat", "ERROR", "活动账号缺少本机地址或 Token。", "在账号资料中重新填写并加密保存。")
    else:
        try:
            api_base = normalize_loopback_base_url(str(config["api_base"]))
            token = CredentialVault(settings).decrypt(credential.encrypted_secret)
            response = None
            last_http_error: httpx.HTTPError | None = None
            for attempt in range(2):
                try:
                    # A loopback control plane must never inherit an HTTP proxy
                    # from the login shell or scheduled-task environment.
                    async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
                        response = await client.get(
                            f"{api_base}/get_status",
                            headers={"Authorization": f"Bearer {token}"},
                        )
                    if response.status_code not in {502, 503, 504} or attempt > 0:
                        break
                except (httpx.TimeoutException, httpx.ConnectError) as exc:
                    last_http_error = exc
                    if attempt > 0:
                        raise
                await asyncio.sleep(0.3)
            if response is None:
                if last_http_error is not None:
                    raise last_http_error
                raise httpx.ConnectError("NapCat health probe returned no response")
            body = response.json() if response.is_success else {}
            onebot_data = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else {}
            healthy = (
                response.is_success
                and isinstance(body, dict)
                and str(body.get("status")) == "ok"
                and onebot_data.get("online") is not False
            )
            if healthy:
                observed_status = "ONLINE"
                check = _check("napcat", "NapCat", "OK", f"{account.display_name} · HTTP {response.status_code} · OneBot 在线。")
            else:
                observed_error = f"NAPCAT_HTTP_{response.status_code}" if not response.is_success else "NAPCAT_ONEBOT_NOT_READY"
                check = _check(
                    "napcat",
                    "NapCat",
                    "ERROR",
                    f"{account.display_name} · HTTP {response.status_code} · OneBot 未就绪。",
                    "确认 NapCat 已登录对应 QQ，HTTP 服务地址、端口和 Token 与后台一致。",
                )
        except Exception as exc:
            observed_status = "DISCONNECTED" if isinstance(exc, httpx.HTTPError) else "ERROR"
            observed_error = type(exc).__name__
            check = _check(
                "napcat",
                "NapCat",
                "ERROR",
                f"本机 OneBot 检查失败：{type(exc).__name__}",
                "确认 NapCat 正在运行且 HTTP 服务监听 127.0.0.1。",
            )

    changed = account.status != observed_status or account.last_error != observed_error
    if update_account and changed:
        account.status = observed_status
        account.last_error = observed_error
        db.add(
            AuditLog(
                event="CONNECTOR_HEALTH_CHANGED",
                level="INFO" if observed_status == "ONLINE" else "WARNING",
                detail={"account_id": account.id, "platform": account.platform, "status": observed_status, "error": observed_error},
            )
        )
        db.flush()
    return NapCatHealth(check, observed_status, observed_error, changed if update_account else False)


async def run_self_check(db: Session, settings: Settings) -> dict[str, Any]:
    checks: list[dict[str, str]] = [
        _check("neko", "Neko 后端", "OK", "后台接口正在响应，核心进程可用。")
    ]

    try:
        db.execute(text("SELECT 1"))
        dialect = db.get_bind().dialect.name
        versions = {str(value) for value in db.scalars(text("SELECT version_num FROM alembic_version"))}
        expected_heads = _expected_migration_heads()
        head_ok = bool(expected_heads) and versions == expected_heads
        version = ", ".join(sorted(versions)) or "未知"
        expected = ", ".join(sorted(expected_heads)) or "未知"
        database_status = "OK" if dialect == "mysql" and head_ok else "WARNING" if head_ok else "ERROR"
        database_detail = f"{dialect.upper()} 已连接，迁移版本 {version}，代码 Head {expected}。"
        database_suggestion = "" if dialect == "mysql" and head_ok else (
            "当前是 SQLite 开发库；正式长期运行建议在设置完成后切换到专用 MySQL。"
            if head_ok else "请停止服务后执行数据库迁移，再重新启动。"
        )
        checks.append(_check("mysql", "MySQL / 数据库", database_status, database_detail, database_suggestion))
    except Exception as exc:
        checks.append(_check("mysql", "MySQL / 数据库", "ERROR", f"数据库检查失败：{type(exc).__name__}", "检查数据库服务、连接地址和账号权限。"))

    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            response = await client.get("http://127.0.0.1:3000")
        checks.append(_check("frontend", "Neko 后台页面", "OK" if response.is_success else "ERROR", f"本机页面返回 HTTP {response.status_code}。", "页面未就绪时使用“重启”恢复前后端。" if not response.is_success else ""))
    except httpx.HTTPError:
        checks.append(_check("frontend", "Neko 后台页面", "ERROR", "本机 3000 端口没有正常响应。", "点击“重启”；若页面已离线，请运行 scripts\\start.ps1 -NoBrowser。"))

    checks.append((await inspect_active_napcat(db, settings)).check)

    ollama_providers = list(db.scalars(select(ProviderConfig).where(ProviderConfig.provider_type == "OLLAMA")))
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get("http://127.0.0.1:11434/api/tags")
        body = response.json() if response.is_success else {}
        installed = {str(item.get("name") or "") for item in body.get("models", []) if isinstance(item, dict)}
        requested = {item.model for item in ollama_providers if item.enabled}
        missing = sorted(model for model in requested if model not in installed and f"{model}:latest" not in installed)
        checks.append(_check("ollama", "Ollama", "OK" if response.is_success and not missing else "ERROR", f"服务在线，已发现 {len(installed)} 个模型。" + (f" 缺少：{', '.join(missing)}" if missing else ""), "先在 Ollama 下载缺失模型，模型名必须与后台完全一致。" if missing else ""))
    except Exception:
        checks.append(_check("ollama", "Ollama", "ERROR", "无法连接本机 11434 端口。", "启动 Ollama 后再点击全面自检。"))

    state = db.get(RuntimeState, 1)
    if state is not None:
        media = await MediaUnderstandingService(settings).status(state)
        vision = media["vision"]
        speech = media["speech"]
        documents = media["documents"]
        ready = bool(documents.get("pdf")) and (not state.media_transcribe_audio or bool(speech.get("package_installed")))
        detail = (
            f"图片模型{'就绪' if vision.get('service_online') and vision.get('model_installed') else '未就绪'}；"
            f"语音组件{'就绪' if speech.get('package_installed') else '未安装'}；"
            f"PDF {'可提取' if documents.get('pdf') else '不可提取'}。"
        )
        checks.append(_check("media", "媒体模型", "OK" if ready else "WARNING", detail, "按需安装语音组件并下载本机图片模型；关闭的媒体能力不会影响纯文本回复。" if not ready else ""))

    rank = {"OK": 0, "WARNING": 1, "ERROR": 2}
    worst = max((rank[item["status"]] for item in checks), default=0)
    return {"status": "OK" if worst == 0 else "WARNING" if worst == 1 else "ERROR", "checks": checks}
