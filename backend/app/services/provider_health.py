from __future__ import annotations

import time
from typing import Any

import httpx
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.core.clock import utc_now
from app.core.security import CredentialVault
from app.llm.base import LLMProvider, LLMResponse
from app.llm.providers import KimiProvider, OllamaProvider, OpenAICompatibleProvider, QwenProvider
from app.models.entities import ApiCredential, AuditLog, ProviderConfig, ProviderHealth


def _health_row(db: Session, config: ProviderConfig) -> ProviderHealth:
    row = db.scalar(select(ProviderHealth).where(ProviderHealth.provider_id == config.id))
    if row is None:
        row = ProviderHealth(provider_id=config.id)
        db.add(row)
        db.flush()
    return row


def _safe_error(exc: Exception) -> tuple[str, str]:
    code = type(exc).__name__
    if isinstance(exc, httpx.HTTPStatusError):
        code = f"HTTP_{exc.response.status_code}"
    detail = str(exc).replace("\r", " ").replace("\n", " ")[:500]
    # Provider errors sometimes echo request headers. Persist only a generic
    # description when authorization-like text appears.
    if any(marker in detail.casefold() for marker in ("authorization", "api-key", "bearer ", "token=")):
        detail = "提供方拒绝或中断了请求；详细响应因可能包含凭据而未保存。"
    return code, detail or code


def record_provider_failure(db: Session, provider_name: str, exc: Exception) -> None:
    config = db.scalar(select(ProviderConfig).where(ProviderConfig.name == provider_name))
    if config is None:
        return
    row = _health_row(db, config)
    code, detail = _safe_error(exc)
    now = utc_now()
    row.status = "DEGRADED"
    row.consecutive_failures += 1
    row.last_attempt_at = now
    row.last_failure_at = now
    row.last_error_code = code
    row.last_error_detail = detail
    db.flush()


def record_provider_success(db: Session, response: LLMResponse) -> None:
    config = db.scalar(select(ProviderConfig).where(ProviderConfig.name == response.provider))
    if config is None:
        return
    row = _health_row(db, config)
    now = utc_now()
    row.status = "HEALTHY"
    row.consecutive_failures = 0
    row.last_attempt_at = now
    row.last_success_at = now
    row.last_error_code = None
    row.last_error_detail = None
    row.last_latency_ms = response.latency_ms
    row.last_model = response.model
    db.flush()


def build_single_provider(db: Session, config: ProviderConfig, settings) -> LLMProvider:
    if config.provider_type == "OLLAMA":
        return OllamaProvider(base_url=config.base_url, model=config.model, timeout=config.timeout_seconds)
    credential = db.get(ApiCredential, config.credential_id) if config.credential_id else None
    if credential is None:
        raise ValueError("模型没有绑定可用凭据")
    secret = CredentialVault(settings).decrypt(credential.encrypted_secret)
    provider_class = QwenProvider if config.provider_type == "QWEN" else KimiProvider if config.provider_type == "KIMI" else OpenAICompatibleProvider
    return provider_class(
        name=config.name,
        base_url=config.base_url,
        model=config.model,
        api_key=secret,
        timeout=config.timeout_seconds,
    )


async def test_provider(db: Session, config: ProviderConfig, settings) -> ProviderHealth:
    row = _health_row(db, config)
    row.status = "CHECKING"
    row.last_attempt_at = utc_now()
    db.flush()
    started = time.perf_counter()
    try:
        provider = build_single_provider(db, config, settings)
        ok = await provider.test_connection()
        if not ok:
            raise ConnectionError("连接测试未返回成功状态")
    except Exception as exc:
        record_provider_failure(db, config.name, exc)
        db.add(AuditLog(event="PROVIDER_HEALTH_RETRY_FAILED", level="WARNING", detail={"provider_id": config.id, "provider": config.name, "error": _safe_error(exc)[0]}))
    else:
        now = utc_now()
        row.status = "HEALTHY"
        row.consecutive_failures = 0
        row.last_attempt_at = now
        row.last_success_at = now
        row.last_error_code = None
        row.last_error_detail = None
        row.last_latency_ms = int((time.perf_counter() - started) * 1000)
        row.last_model = config.model
        db.add(AuditLog(event="PROVIDER_HEALTH_RETRY_SUCCEEDED", detail={"provider_id": config.id, "provider": config.name, "model": config.model}))
        db.flush()
    return row


def model_health_snapshot(db: Session) -> dict[str, Any]:
    configs = list(db.scalars(select(ProviderConfig).order_by(ProviderConfig.priority.asc(), ProviderConfig.name.asc())))
    health_by_provider = {
        item.provider_id: item for item in db.scalars(select(ProviderHealth))
    }
    latest_response = db.scalar(
        select(AuditLog).where(AuditLog.event == "LLM_RESPONSE").order_by(desc(AuditLog.created_at)).limit(1)
    )
    latest_detail = latest_response.detail if latest_response else {}
    enabled = [item for item in configs if item.enabled]
    rows = []
    for index, config in enumerate(configs):
        health = health_by_provider.get(config.id)
        is_local = config.provider_type == "OLLAMA" and config.base_url.startswith(("http://127.0.0.1", "http://localhost"))
        rows.append(
            {
                "id": config.id,
                "name": config.name,
                "model": config.model,
                "provider_type": config.provider_type,
                "enabled": config.enabled,
                "priority": config.priority,
                "is_primary": bool(config.enabled and enabled and config.id == enabled[0].id),
                "is_local": is_local,
                "status": health.status if health else "UNKNOWN",
                "consecutive_failures": health.consecutive_failures if health else 0,
                "last_attempt_at": health.last_attempt_at if health else None,
                "last_success_at": health.last_success_at if health else None,
                "last_failure_at": health.last_failure_at if health else None,
                "last_error_code": health.last_error_code if health else None,
                "last_error_detail": health.last_error_detail if health else None,
                "last_latency_ms": health.last_latency_ms if health else None,
                "position": index + 1,
            }
        )
    actual_provider = latest_detail.get("provider")
    primary_name = enabled[0].name if enabled else None
    return {
        "primary_provider": primary_name,
        "primary_model": enabled[0].model if enabled else None,
        "actual_provider": actual_provider,
        "actual_model": latest_detail.get("model"),
        "fallback_active": bool(actual_provider and primary_name and actual_provider != primary_name),
        "last_response_at": latest_response.created_at if latest_response else None,
        "pure_local": bool(enabled) and all(row["is_local"] for row in rows if row["enabled"]),
        "providers": rows,
    }
