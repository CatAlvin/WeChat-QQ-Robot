from __future__ import annotations

from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.core.enums import MessageDirection
from app.models.entities import Account, ApiCredential, Contact, Message, ProviderConfig
from app.services.account_selection import get_active_account
from app.services.provider_health import model_health_snapshot
from app.services.service_control import supervisor_status
from app.services.system_health import run_self_check


def _item(
    check_id: str,
    section: str,
    status: str,
    title: str,
    detail: str,
    suggestion: str = "",
    *,
    can_retry: bool = False,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "section": section,
        "status": status,
        "title": title,
        "detail": detail,
        "suggestion": suggestion,
        "can_retry": can_retry,
    }


async def configuration_report(db: Session, settings) -> dict[str, Any]:
    base = await run_self_check(db, settings)
    base_by_key = {item["key"]: item for item in base["checks"]}
    items: list[dict[str, Any]] = []

    supervisor = supervisor_status()
    items.append(
        _item(
            "supervisor",
            "运行方式",
            "OK" if supervisor["online"] else "WARNING",
            "Windows 常驻监督",
            (
                f"监督进程在线，已自动恢复 {supervisor.get('restart_count', 0)} 次。"
                if supervisor["online"]
                else "当前仍由普通启动脚本运行，无法保证权限一致或崩溃自动恢复。"
            ),
            "运行 scripts\\install-startup.ps1 安装当前用户开机自启和托盘。" if not supervisor["online"] else "",
        )
    )

    for key, section in (("mysql", "数据库"), ("frontend", "Neko"), ("ollama", "媒体与本地模型"), ("media", "媒体与本地模型")):
        source = base_by_key.get(key)
        if source:
            items.append(_item(key, section, source["status"], source["label"], source["detail"], source["suggestion"], can_retry=True))

    napcat_accounts = list(db.scalars(select(Account).where(Account.platform == "QQ_NAPCAT").order_by(Account.created_at.asc())))
    active = get_active_account(db, "QQ_NAPCAT")
    if active is None:
        enabled_count = sum(1 for item in napcat_accounts if item.enabled)
        detail = "没有活动账号。" if enabled_count == 0 else f"检测到 {enabled_count} 个账号同时启用，系统已按安全规则拒绝任选其一。"
        items.append(_item("napcat-account", "NapCat", "ERROR", "活动托管账号", detail, "在“连接账号”中只激活一个正确的 NapCat 配置。"))
    else:
        config = active.config or {}
        credential = db.get(ApiCredential, active.credential_id) if active.credential_id else None
        missing = []
        if not str(config.get("managed_qq_id") or "").isdigit():
            missing.append("托管 QQ 号")
        if not config.get("api_base"):
            missing.append("OneBot HTTP 地址")
        if credential is None:
            missing.append("Access Token")
        items.append(
            _item(
                "napcat-account",
                "NapCat",
                "ERROR" if missing else "OK",
                "活动托管账号",
                f"{active.display_name}（{config.get('managed_qq_id') or 'QQ 未填写'}）" + (f"缺少：{'、'.join(missing)}。" if missing else "配置字段完整。"),
                "编辑该账号并重新保存缺失项。" if missing else "",
            )
        )
        napcat = base_by_key.get("napcat")
        if napcat:
            items.append(_item("napcat", "NapCat", napcat["status"], "OneBot HTTP 连通性", napcat["detail"], napcat["suggestion"], can_retry=True))
        latest_inbound = db.scalar(
            select(Message)
            .where(Message.account_id == active.id, Message.direction == MessageDirection.INBOUND)
            .order_by(desc(Message.created_at))
            .limit(1)
        )
        bound_contacts = db.scalar(select(func.count(Contact.id)).where(Contact.account_id == active.id)) or 0
        items.append(
            _item(
                "napcat-inbound",
                "NapCat",
                "OK" if latest_inbound else "WARNING",
                "账号切换后的入站归属",
                (
                    f"最近入站已归属当前账号，已有 {bound_contacts} 位联系人绑定。"
                    if latest_inbound
                    else f"当前账号已有 {bound_contacts} 位联系人绑定，但尚未收到可验证的新入站消息。"
                ),
                "切换 QQ 后，请由专用测试联系人发送一条消息，确认新事件携带正确账号。" if not latest_inbound else "",
            )
        )

    model_snapshot = model_health_snapshot(db)
    enabled_providers = [item for item in model_snapshot["providers"] if item["enabled"]]
    if not enabled_providers:
        items.append(_item("models", "模型", "ERROR", "回复模型", "没有启用的回复模型。", "至少启用一个已正确配置的模型。"))
    for provider in enabled_providers:
        credential_required = provider["provider_type"] != "OLLAMA"
        config = db.get(ProviderConfig, provider["id"])
        missing_credential = bool(credential_required and config and not config.credential_id)
        status = "ERROR" if missing_credential else provider["status"]
        items.append(
            _item(
                f"provider:{provider['id']}",
                "模型",
                status if status in {"OK", "WARNING", "ERROR"} else "OK" if status == "HEALTHY" else "WARNING",
                f"{provider['name']} / {provider['model']}",
                "缺少加密凭据。" if missing_credential else f"状态 {provider['status']}，连续失败 {provider['consecutive_failures']} 次。",
                "重新绑定凭据后测试。" if missing_credential else "点击重试只执行连接测试，不会发送聊天消息。",
                can_retry=not missing_credential,
            )
        )

    rank = {"OK": 0, "WARNING": 1, "ERROR": 2}
    worst = max((rank.get(item["status"], 1) for item in items), default=0)
    passed = sum(1 for item in items if item["status"] == "OK")
    return {
        "status": "OK" if worst == 0 else "WARNING" if worst == 1 else "ERROR",
        "passed": passed,
        "total": len(items),
        "items": items,
        "supervisor": supervisor,
    }
