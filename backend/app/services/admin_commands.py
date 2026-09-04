from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import re

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.clock import as_beijing, utc_now
from app.core.enums import GlobalMode, MessageAuthor, MessageStatus
from app.models.entities import Account, AuditLog, Contact, Incident, Message, ProviderConfig, RuntimeState, TodoItem
from app.services.account_selection import get_active_account


ADMIN_RELATIONSHIP = "管理员"
ADMIN_PREFIX = "/neko"


@dataclass(frozen=True, slots=True)
class AdminCommand:
    name: str
    arguments: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AdminCommandPlan:
    code: str
    response: str
    state_updates: dict[str, object] = field(default_factory=dict)
    cancel_queued: bool = False
    acknowledge_before_apply: bool = False
    target_contact_id: str | None = None
    contact_updates: dict[str, object] = field(default_factory=dict)
    preferred_provider_id: str | None = None
    cancel_target_queued: bool = False


def parse_admin_command(contact: Contact, content: str, *, is_group: bool, message_type: str) -> AdminCommand | None:
    if is_group or message_type not in {"TEXT", "MIXED"} or contact.relationship_label.strip() != ADMIN_RELATIONSHIP or not contact.whitelisted:
        return None
    normalized = " ".join(content.strip().split())
    if not normalized.casefold().startswith(ADMIN_PREFIX):
        return None
    remainder = normalized[len(ADMIN_PREFIX) :].strip()
    if not remainder:
        return AdminCommand("帮助")
    parts = remainder.split(" ")
    if parts[0].casefold() in {"模式", "mode"} and len(parts) > 1:
        return AdminCommand(parts[1], tuple(parts[2:]))
    return AdminCommand(parts[0], tuple(parts[1:]))


def plan_admin_command(
    db: Session, state: RuntimeState, command: AdminCommand, *, account_id: str | None = None
) -> AdminCommandPlan:
    name = command.name.casefold()
    aliases = {
        "help": "帮助",
        "?": "帮助",
        "status": "状态",
        "auto": "自动",
        "silent": "静默",
        "readonly": "只读",
        "read-only": "只读",
        "stop": "停止",
        "stopped": "停止",
        "kill": "急停",
        "shadow": "影子",
        "accounts": "账号",
        "models": "模型",
        "contacts": "联系人",
        "contact": "联系人",
        "whitelist": "白名单",
        "memory": "记忆",
        "hours": "时段",
        "schedule": "时段",
        "preferred-model": "模型优先",
        "model-priority": "模型优先",
        "history": "记录",
        "recent": "记录",
        "incidents": "风险",
        "export": "导出",
        "todo": "待办",
        "todos": "待办",
    }
    name = aliases.get(name, name)
    if "|" in command.name or any("|" in item for item in command.arguments):
        return AdminCommandPlan(
            "LEGACY_SEPARATOR",
            "管理员指令的参数分隔符已统一改为“-”，不再接受“|”。请把原命令中的“|”替换为“-”后重试。",
        )
    if name in {"帮助", "菜单"}:
        return AdminCommandPlan("HELP", _help_text())
    if name in {"状态", "汇报"}:
        return AdminCommandPlan("STATUS", _status_text(db, state))
    if name in {"账号", "通道"}:
        return AdminCommandPlan("ACCOUNTS", _accounts_text(db))
    if name in {"模型", "模型状态"}:
        return AdminCommandPlan("MODELS", _models_text(db))
    if name in {"联系人", "联系人状态"}:
        if not command.arguments:
            return AdminCommandPlan("CONTACTS", _contacts_text(db, account_id=account_id))
        target, error = _resolve_contact(db, " ".join(_clean_arguments(command.arguments)), account_id=account_id)
        if error:
            return error
        assert target is not None
        return AdminCommandPlan("CONTACT", _contact_text(target))
    if name in {"白名单", "名单"}:
        return _contact_toggle_plan(db, command, field_name="whitelisted", label="白名单", account_id=account_id)
    if name in {"记忆", "联系人记忆"}:
        return _contact_toggle_plan(db, command, field_name="memory_enabled", label="记忆", account_id=account_id)
    if name in {"时段", "回复时段", "自动回复时段"}:
        return _time_window_plan(state, command)
    if name in {"模型优先", "优先模型", "切换模型"}:
        return _preferred_model_plan(db, command)
    if name in {"记录", "聊天记录", "最近消息"}:
        return _recent_messages_plan(db, command, account_id=account_id)
    if name in {"风险", "最近风险"}:
        return AdminCommandPlan("INCIDENTS", _incidents_text(db))
    if name in {"待办", "待办事项"}:
        return AdminCommandPlan("TODOS", _todos_text(db, account_id=account_id))
    if name in {"导出", "导出帮助"}:
        return AdminCommandPlan(
            "EXPORT_HELP",
            "聊天记录导出请打开本机后台 → 消息导出。可多选联系人、日期、消息来源、格式和附件。"
            "主动发送本机文件只读取 Neko data/sendbox 目录，并且必须通过确认代码二次确认。",
        )
    if name in {"自动", "恢复自动"}:
        return AdminCommandPlan("MODE_AUTO", "已切换为 AUTO。后续消息仍会经过白名单、LIVE 时段、频率、内容和急停复检。", {"global_mode": GlobalMode.AUTO})
    if name in {"静默", "暂停回复"}:
        return AdminCommandPlan("MODE_SILENT", "即将切换为 SILENT：继续记录消息，但不再自动回复。发送本确认后生效；恢复请发 /neko 自动。", {"global_mode": GlobalMode.SILENT}, True, True)
    if name in {"只读", "仅读"}:
        return AdminCommandPlan("MODE_READ_ONLY", "即将切换为 READ_ONLY：只记录原始消息，不调用模型、不更新记忆、不回复。发送本确认后生效；恢复请发 /neko 自动。", {"global_mode": GlobalMode.READ_ONLY}, True, True)
    if name in {"停止", "停止托管"}:
        return AdminCommandPlan("MODE_STOPPED", "即将切换为 STOPPED 并取消所有待发消息。发送本确认后生效；恢复请发 /neko 自动。", {"global_mode": GlobalMode.STOPPED}, True, True)
    if name in {"急停", "紧急停止"}:
        return AdminCommandPlan("KILL_SWITCH_ON", "急停已立即开启。根据安全规则，急停开启后 Neko 不会发送确认；解除需发送：/neko 解除急停 确认", {"kill_switch": True}, True, False)
    if name in {"解除急停", "取消急停"}:
        confirmed = any(item.casefold() in {"确认", "confirm", "yes"} for item in command.arguments)
        if not confirmed:
            return AdminCommandPlan("CONFIRM_REQUIRED", "这是高风险操作。请完整发送：/neko 解除急停 确认")
        return AdminCommandPlan("KILL_SWITCH_OFF", "急停已解除。当前运行模式仍为 %s；只有 AUTO 才会自动回复。" % state.global_mode, {"kill_switch": False})
    if name in {"影子", "降级", "shadow"}:
        if state.release_gate == "SIMULATION":
            return AdminCommandPlan("GATE_UNCHANGED", "当前已是 SIMULATION，不能通过聊天提高发布门禁。")
        return AdminCommandPlan("GATE_SHADOW", "即将降级到 SHADOW 并取消全部待发消息。发送本确认后生效；重新进入 LIVE 必须在本机后台操作。", {"release_gate": "SHADOW"}, True, True)
    if name in {"live", "上线"}:
        return AdminCommandPlan("LIVE_DASHBOARD_ONLY", "出于安全考虑，聊天指令不能把系统升级到 LIVE。请在本机后台完成验收和确认。")
    return AdminCommandPlan("UNKNOWN", "没有识别这个指令。发送 /neko 帮助 查看可用命令；不以 /neko 开头的消息会作为普通聊天。")


def apply_admin_plan(db: Session, state: RuntimeState, plan: AdminCommandPlan, *, contact: Contact, message_id: str) -> int:
    for key, value in plan.state_updates.items():
        setattr(state, key, value.value if isinstance(value, GlobalMode) else value)
    target = db.get(Contact, plan.target_contact_id) if plan.target_contact_id else None
    if plan.contact_updates:
        if target is None:
            raise ValueError("管理员指令目标联系人不存在")
        for key, value in plan.contact_updates.items():
            setattr(target, key, value)
    preferred = db.get(ProviderConfig, plan.preferred_provider_id) if plan.preferred_provider_id else None
    if plan.preferred_provider_id:
        if preferred is None or not preferred.enabled:
            raise ValueError("管理员指令目标模型不存在或已停用")
        providers = list(
            db.scalars(
                select(ProviderConfig)
                .where(ProviderConfig.enabled.is_(True))
                .order_by(ProviderConfig.priority.asc(), ProviderConfig.name.asc())
            )
        )
        preferred.priority = 0
        next_priority = 10
        for provider in providers:
            if provider.id == preferred.id:
                continue
            provider.priority = next_priority
            next_priority += 10
    cancelled = 0
    if plan.cancel_queued:
        result = db.execute(update(Message).where(Message.status == MessageStatus.QUEUED).values(status=MessageStatus.CANCELLED))
        cancelled = int(result.rowcount or 0)
    elif plan.cancel_target_queued and target is not None:
        result = db.execute(
            update(Message)
            .where(Message.status == MessageStatus.QUEUED, Message.contact_id == target.id)
            .values(status=MessageStatus.CANCELLED)
        )
        cancelled = int(result.rowcount or 0)
    if plan.state_updates:
        state.updated_at = utc_now()
    db.add(
        AuditLog(
            event="ADMIN_COMMAND_APPLIED",
            level="WARNING" if (plan.state_updates or plan.contact_updates or plan.preferred_provider_id) else "INFO",
            message_id=message_id,
            detail={
                "code": plan.code,
                "admin_contact_id": contact.id,
                "target_contact_id": plan.target_contact_id,
                "state_updates": sorted(plan.state_updates),
                "contact_updates": sorted(plan.contact_updates),
                "preferred_provider_id": plan.preferred_provider_id,
                "queued_cancelled": cancelled,
            },
        )
    )
    db.commit()
    db.refresh(state)
    return cancelled


def _help_text() -> str:
    return (
        "Neko 管理指令（仅管理员私聊可用）\n"
        "• /neko 状态 — 汇报门禁、模式、急停、通道和待处理风险\n"
        "• /neko 账号、/neko 模型、/neko 风险 — 查看局部状态\n"
        "• /neko 联系人 [名称或QQ] — 列出联系人或查看指定联系人策略\n"
        "• /neko 白名单 联系人 开启/关闭 确认 — 修改消息采集白名单\n"
        "• /neko 记忆 联系人 开启/关闭 确认 — 修改该联系人的记忆读写开关\n"
        "• /neko 时段 — 查看 LIVE 回复时段\n"
        "• /neko 时段 09:00-23:30 确认；/neko 时段 开启/关闭 确认\n"
        "• /neko 模型优先 模型名 确认 — 切换首选回复模型；/neko 模型 查看顺序\n"
        "• /neko 记录 联系人 [条数] — 查看最近聊天记录，默认 8 条、最多 20 条\n"
        "• /neko 待办 — 查看本账号待处理事项；引用联系人转交通知即可答复\n"
        "• /neko 自动、/neko 静默、/neko 只读、/neko 停止 — 切换运行模式\n"
        "• /neko 急停 — 立即停止；安全起见不会回复确认\n"
        "• /neko 解除急停 确认 — 解除急停\n"
        "• /neko 影子 — 从 LIVE 安全降级，不能用聊天升级 LIVE\n"
        "• /neko 导出 — 查看导出入口说明\n"
        "• /neko 记住 联系人-记忆内容 — 直接写入该联系人的长期记忆\n"
        "• /neko 截图开始 联系人；/neko 截图结束；/neko 截图取消 — 批量导入聊天截图并自动整理记忆\n"
        "• /neko 找 联系人-话题 — 让橙蓝自然说明受主人委托，并主动找该白名单联系人聊天\n"
        "• /neko 发送 联系人-内容 — 生成主动发送预览（仅白名单）\n"
        "• /neko 发送文件 联系人-文件名 — 发送 data/sendbox 内文件；也可引用已保存附件后省略文件名\n"
        "• /neko 确认发送 代码；/neko 取消发送 代码 — 10 分钟内确认或撤销主动发送\n"
        "普通聊天不要以 /neko 开头，Neko 会照常陪你聊天。"
    )


def _status_text(db: Session, state: RuntimeState) -> str:
    active = get_active_account(db, "QQ_NAPCAT")
    unresolved = db.scalar(select(func.count(Incident.id)).where(Incident.resolved.is_(False))) or 0
    queued = db.scalar(select(func.count(Message.id)).where(Message.status == MessageStatus.QUEUED)) or 0
    account = f"{active.display_name}（{(active.config or {}).get('managed_qq_id') or '未填写QQ号'}）" if active else "无唯一活动 NapCat 账号"
    return (
        f"Neko 当前状态\n发布门禁：{state.release_gate}\n运行模式：{state.global_mode}\n"
        f"LIVE 回复时段：{'开启' if state.live_time_window_enabled else '关闭'}"
        f"（{state.live_auto_start}–{state.live_auto_end}）\n"
        f"急停：{'已开启' if state.kill_switch else '已关闭'}\n活动账号：{account}\n"
        f"待发队列：{queued}\n未处理风险：{unresolved}\n"
        "提示：聊天只能安全降级；进入 LIVE 必须在本机后台确认。"
    )


def _accounts_text(db: Session) -> str:
    rows = list(db.scalars(select(Account).where(Account.platform == "QQ_NAPCAT").order_by(Account.enabled.desc(), Account.created_at.asc())))
    if not rows:
        return "尚未配置 NapCat 账号。"
    lines = ["NapCat 账号："]
    for item in rows:
        lines.append(f"• {'当前' if item.enabled else '停用'} · {item.display_name} · {(item.config or {}).get('managed_qq_id') or '未填写QQ号'} · {item.status}")
    return "\n".join(lines)


def _models_text(db: Session) -> str:
    rows = list(db.scalars(select(ProviderConfig).where(ProviderConfig.enabled.is_(True)).order_by(ProviderConfig.priority.asc())))
    if not rows:
        return "当前没有启用的模型提供方。"
    lines = ["已启用模型（按优先级）："]
    for index, item in enumerate(rows):
        lines.append(f"• {'当前优先 · ' if index == 0 else ''}P{item.priority} · {item.name} · {item.model}")
    lines.append("切换：/neko 模型优先 模型名 确认")
    return "\n".join(lines)


def _incidents_text(db: Session) -> str:
    rows = list(db.scalars(select(Incident).where(Incident.resolved.is_(False)).order_by(Incident.created_at.desc()).limit(5)))
    if not rows:
        return "当前没有未处理风险事件。"
    return "最近未处理风险：\n" + "\n".join(f"• {item.severity} · {item.title}" for item in rows)


def _todos_text(db: Session, *, account_id: str | None = None) -> str:
    query = select(TodoItem).where(TodoItem.status == "OPEN")
    if account_id is not None:
        query = query.where(or_(TodoItem.account_id == account_id, TodoItem.account_id.is_(None)))
    rows = list(db.scalars(query.order_by(TodoItem.created_at.desc()).limit(10)))
    if not rows:
        return "当前没有未完成待办。"
    lines = ["未完成待办（最多显示 10 条）："]
    for item in rows:
        kind = "联系人转交" if item.kind == "CONTACT_RELAY" else "手动待办"
        state = item.delivery_status if item.kind == "CONTACT_RELAY" else item.priority
        lines.append(f"• {kind} · {item.title} · {state}")
    lines.append("联系人转交待办：请引用我发给你的待办通知并填写答复；也可在本机后台直接标记完成。")
    return "\n".join(lines)


_ON = {"开", "开启", "启用", "on", "enable", "enabled"}
_OFF = {"关", "关闭", "停用", "off", "disable", "disabled"}
_CONFIRM = {"确认", "confirm", "yes"}
_CLOCK = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _clean_arguments(arguments: tuple[str, ...], *, drop_confirmation: bool = False) -> list[str]:
    values = [item.strip() for item in arguments if item.strip()]
    if drop_confirmation:
        values = [item for item in values if item.casefold() not in _CONFIRM]
    return values


def _confirmed(arguments: tuple[str, ...]) -> bool:
    return any(item.casefold() in _CONFIRM for item in _clean_arguments(arguments))


def _resolve_contact(
    db: Session, value: str, *, account_id: str | None = None
) -> tuple[Contact | None, AdminCommandPlan | None]:
    target_text = value.strip()
    if not target_text:
        return None, AdminCommandPlan("CONTACT_REQUIRED", "请填写后台显示的完整联系人名称或 QQ 号。")
    query = select(Contact).where(
        Contact.platform == "QQ_NAPCAT",
        or_(Contact.platform_user_id == target_text, Contact.display_name == target_text),
    )
    if account_id is not None:
        query = query.where(Contact.account_id == account_id)
    rows = list(
        db.scalars(
            query
        )
    )
    if not rows:
        return None, AdminCommandPlan("CONTACT_NOT_FOUND", "没有找到该 NapCat 联系人；请先发送 /neko 联系人 查看名称和 QQ 号。")
    if len(rows) > 1:
        return None, AdminCommandPlan("CONTACT_AMBIGUOUS", "联系人名称不唯一，请改用 QQ 号。")
    return rows[0], None


def _contacts_text(db: Session, *, account_id: str | None = None) -> str:
    query = select(Contact).where(Contact.platform == "QQ_NAPCAT")
    if account_id is not None:
        query = query.where(Contact.account_id == account_id)
    rows = list(db.scalars(query.order_by(Contact.display_name.asc()).limit(30)))
    if not rows:
        return "尚未记录 NapCat 联系人。"
    lines = ["NapCat 联系人（最多显示 30 位）："]
    for item in rows:
        lines.append(
            f"• {item.display_name} · {item.platform_user_id} · "
            f"白名单{'开' if item.whitelisted else '关'} · 记忆{'开' if item.memory_enabled else '关'} · AI{'开' if item.ai_enabled else '关'}"
        )
    lines.append("详情：/neko 联系人 名称或QQ")
    return "\n".join(lines)


def _contact_text(contact: Contact) -> str:
    return (
        f"联系人：{contact.display_name}\nQQ：{contact.platform_user_id}\n关系：{contact.relationship_label}\n"
        f"白名单：{'开启' if contact.whitelisted else '关闭'}\n"
        f"记忆：{'开启' if contact.memory_enabled else '关闭'}\n"
        f"AI 回复：{'开启' if contact.ai_enabled else '关闭'}"
    )


def _contact_toggle_plan(
    db: Session,
    command: AdminCommand,
    *,
    field_name: str,
    label: str,
    account_id: str | None = None,
) -> AdminCommandPlan:
    values = _clean_arguments(command.arguments, drop_confirmation=True)
    if len(values) < 2:
        return AdminCommandPlan("CONTACT_TOGGLE_USAGE", f"格式：/neko {label} 联系人名称或QQ 开启/关闭 确认")
    action = values[-1].casefold()
    if action not in _ON | _OFF:
        return AdminCommandPlan("CONTACT_TOGGLE_USAGE", f"请明确填写“开启”或“关闭”：/neko {label} 联系人 开启/关闭 确认")
    target, error = _resolve_contact(db, " ".join(values[:-1]), account_id=account_id)
    if error:
        return error
    assert target is not None
    enabled = action in _ON
    current = bool(getattr(target, field_name))
    if current == enabled:
        return AdminCommandPlan("CONTACT_UNCHANGED", f"{target.display_name} 的{label}已经是{'开启' if enabled else '关闭'}状态。")
    if field_name == "whitelisted" and not enabled and target.relationship_label.strip() == ADMIN_RELATIONSHIP:
        return AdminCommandPlan("ADMIN_WHITELIST_PROTECTED", "不能通过聊天关闭管理员自己的白名单，否则会失去远程管理入口。请在本机后台处理。")
    exact = f"/neko {label} {target.platform_user_id} {'开启' if enabled else '关闭'} 确认"
    if not _confirmed(command.arguments):
        impact = "开启后将开始接收并保存其新消息。" if field_name == "whitelisted" and enabled else "关闭后将不再接收或保存其新消息。" if field_name == "whitelisted" else "原有记忆保留，但停止读取和新增。" if not enabled else "将恢复读取和新增该联系人的记忆。"
        return AdminCommandPlan("CONFIRM_REQUIRED", f"待修改：{target.display_name} 的{label} → {'开启' if enabled else '关闭'}。{impact}\n确认请发送：{exact}")
    return AdminCommandPlan(
        f"CONTACT_{field_name.upper()}_{'ON' if enabled else 'OFF'}",
        f"已将 {target.display_name} 的{label}设为{'开启' if enabled else '关闭'}。",
        target_contact_id=target.id,
        contact_updates={field_name: enabled},
        cancel_target_queued=field_name == "whitelisted" and not enabled,
    )


def _time_window_plan(state: RuntimeState, command: AdminCommand) -> AdminCommandPlan:
    values = _clean_arguments(command.arguments, drop_confirmation=True)
    if not values:
        status = "开启" if state.live_time_window_enabled else "关闭（LIVE 将不限制回复时间）"
        return AdminCommandPlan(
            "TIME_WINDOW",
            f"LIVE 回复时段：{status}\n当前时间：{state.live_auto_start}–{state.live_auto_end}\n"
            "设置：/neko 时段 09:00-23:30 确认\n开关：/neko 时段 开启/关闭 确认",
        )
    action = values[0].casefold()
    if len(values) == 1 and action in _ON | _OFF:
        enabled = action in _ON
        if state.live_time_window_enabled == enabled:
            return AdminCommandPlan("TIME_WINDOW_UNCHANGED", f"LIVE 回复时段开关已经是{'开启' if enabled else '关闭'}。")
        if not _confirmed(command.arguments):
            warning = "关闭后 LIVE 将全天允许自动回复。" if not enabled else "开启后只在已设置时段内自动回复。"
            return AdminCommandPlan("CONFIRM_REQUIRED", f"待修改：回复时段开关 → {'开启' if enabled else '关闭'}。{warning}\n确认请发送：/neko 时段 {'开启' if enabled else '关闭'} 确认")
        return AdminCommandPlan(
            f"TIME_WINDOW_{'ON' if enabled else 'OFF'}",
            f"LIVE 回复时段已{'开启' if enabled else '关闭'}；当前设置为 {state.live_auto_start}–{state.live_auto_end}。",
            {"live_time_window_enabled": enabled},
            cancel_queued=True,
        )
    joined = " ".join(values).replace("–", " ").replace("—", " ").replace("-", " ").replace("~", " ").replace("至", " ")
    clocks = [item for item in joined.split() if item]
    if len(clocks) != 2 or not all(_CLOCK.fullmatch(item) for item in clocks) or clocks[0] == clocks[1]:
        return AdminCommandPlan("TIME_WINDOW_INVALID", "时间格式无效，起止时间不能相同。示例：/neko 时段 09:00-23:30 确认；支持跨午夜。")
    start, end = clocks
    if state.live_time_window_enabled and state.live_auto_start == start and state.live_auto_end == end:
        return AdminCommandPlan("TIME_WINDOW_UNCHANGED", f"LIVE 回复时段已经是 {start}–{end}。")
    if not _confirmed(command.arguments):
        return AdminCommandPlan("CONFIRM_REQUIRED", f"待修改：LIVE 回复时段 → {start}–{end} 并开启。\n确认请发送：/neko 时段 {start}-{end} 确认")
    return AdminCommandPlan(
        "TIME_WINDOW_SET",
        f"LIVE 回复时段已设为 {start}–{end} 并开启。",
        {"live_time_window_enabled": True, "live_auto_start": start, "live_auto_end": end},
        cancel_queued=True,
    )


def _preferred_model_plan(db: Session, command: AdminCommand) -> AdminCommandPlan:
    values = _clean_arguments(command.arguments, drop_confirmation=True)
    if not values:
        return AdminCommandPlan("MODELS", _models_text(db))
    query = " ".join(values).casefold()
    rows = list(db.scalars(select(ProviderConfig).where(ProviderConfig.enabled.is_(True)).order_by(ProviderConfig.priority.asc(), ProviderConfig.name.asc())))
    exact = [item for item in rows if query in {item.name.casefold(), item.model.casefold(), item.provider_type.casefold()}]
    matches = exact or [item for item in rows if query in item.name.casefold() or query in item.model.casefold()]
    if not matches:
        return AdminCommandPlan("MODEL_NOT_FOUND", "没有找到已启用的模型。发送 /neko 模型 查看可选名称。")
    if len(matches) > 1:
        return AdminCommandPlan("MODEL_AMBIGUOUS", "模型名称不唯一，请使用 /neko 模型 中显示的完整名称。")
    selected = matches[0]
    if rows and rows[0].id == selected.id:
        return AdminCommandPlan("MODEL_UNCHANGED", f"{selected.name}（{selected.model}）已经是当前优先模型。")
    if not _confirmed(command.arguments):
        return AdminCommandPlan("CONFIRM_REQUIRED", f"待切换优先模型：{selected.name}（{selected.model}）。失败时仍会按列表顺序兜底。\n确认请发送：/neko 模型优先 {selected.name} 确认")
    return AdminCommandPlan(
        "PREFERRED_MODEL_CHANGED",
        f"已将 {selected.name}（{selected.model}）设为优先模型；其他启用模型仍作为失败兜底。",
        preferred_provider_id=selected.id,
    )


def _recent_messages_plan(
    db: Session, command: AdminCommand, *, account_id: str | None = None
) -> AdminCommandPlan:
    values = _clean_arguments(command.arguments)
    if not values:
        return AdminCommandPlan("RECENT_USAGE", "格式：/neko 记录 联系人名称或QQ [条数]；默认 8 条，最多 20 条。")
    limit = 8
    if values[-1].isdigit():
        limit = min(20, max(1, int(values.pop())))
    target, error = _resolve_contact(db, " ".join(values), account_id=account_id)
    if error:
        return error
    assert target is not None
    rows = list(
        db.scalars(
            select(Message)
            .where(
                Message.contact_id == target.id,
                Message.status.in_([MessageStatus.RECEIVED, MessageStatus.SENT, MessageStatus.SHADOWED]),
            )
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
    )
    if not rows:
        return AdminCommandPlan("RECENT_MESSAGES", f"尚未记录与 {target.display_name} 的聊天消息。")
    labels = {
        MessageAuthor.CONTACT: target.display_name,
        MessageAuthor.AI: "Neko",
        MessageAuthor.HUMAN: "我",
        MessageAuthor.SYSTEM: "Neko系统",
    }
    lines = [f"与 {target.display_name} 最近 {len(rows)} 条记录："]
    for item in reversed(rows):
        speaker = "我（管理员发送）" if item.provider == "LOCAL_ADMIN_DELIVERY" else labels.get(item.author, str(item.author))
        content = " ".join(item.content.split())
        if len(content) > 160:
            content = content[:157] + "…"
        shadow = " [SHADOW未发送]" if item.status == MessageStatus.SHADOWED else ""
        lines.append(f"• {as_beijing(item.created_at):%m-%d %H:%M} {speaker}{shadow}：{content}")
    return AdminCommandPlan("RECENT_MESSAGES", "\n".join(lines))
