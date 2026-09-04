from __future__ import annotations

from dataclasses import dataclass
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm.base import LLMMessage
from app.models.entities import Contact, Message, TodoItem


CONTACT_RELAY_KIND = "CONTACT_RELAY"
RELAY_PROVIDER = "CONTACT_RELAY"
_OWNER = re.compile(r"主人|管理员|创造者|创建者|你的主人|你主人|橙蓝.{0,3}(?:主人|管理员)", re.I)
_REQUEST = re.compile(r"帮我|麻烦|请(?:你)?|能不能|可以不可以|替我|代我|问问|问一下|转告|告诉|通知|提醒|(?:跟|和|给).{0,12}说")
_ACTION = re.compile(r"问|说|转告|告诉|通知|提醒")
_DIRECT_OWNER_QUESTION = re.compile(
    r"(?:主人|管理员|创造者|创建者).{0,48}"
    r"(?:什么|啥|哪(?:里|儿|个)?|谁|怎么|怎样|如何|为什么|为啥|咋|是否|有没有|会不会|"
    r"能不能|可不可以|吗(?:[，。！？!?]|$)|[?？])",
    re.I,
)


@dataclass(frozen=True, slots=True)
class DelegatedTodoIntent:
    original: str
    asks_question: bool
    title: str
    notification: str
    acknowledgement: str


def detect_delegated_todo(
    contact: Contact,
    content: str,
    *,
    is_group: bool,
    message_type: str,
) -> DelegatedTodoIntent | None:
    if (
        is_group
        or message_type not in {"TEXT", "MIXED"}
        or not contact.whitelisted
        or contact.relationship_label.strip() == "管理员"
    ):
        return None
    normalized = " ".join((content or "").split()).strip()
    if not normalized or len(normalized) > 2000:
        return None
    if not _OWNER.search(normalized):
        return None
    direct_question = bool(_DIRECT_OWNER_QUESTION.search(normalized))
    explicit_relay = bool(_REQUEST.search(normalized) and _ACTION.search(normalized))
    if not direct_question and not explicit_relay:
        return None
    asks_question = direct_question or "问" in normalized or normalized.endswith(("?", "？"))
    purpose = "询问主人" if asks_question else "转告主人"
    title = f"{contact.display_name}{purpose}"
    request_label = "想问主人" if asks_question else "想转告主人"
    notification = (
        f"【联系人转交待办】\n{contact.display_name}{request_label}：\n“{normalized}”\n\n"
        f"要怎样回复呢？请直接引用这条消息回复，我会整理后转达给{contact.display_name}；"
        "也可以在后台手动处理。"
    )
    acknowledgement = (
        "好，我已经帮你问主人了，也把这件事记进后台待办；有回复后就告诉你。"
        if asks_question
        else "好，我已经替你转告主人了，也把这件事记进后台待办；有回复后就告诉你。"
    )
    return DelegatedTodoIntent(normalized, asks_question, title, notification, acknowledgement)


def find_account_admin(db: Session, source: Contact) -> Contact | None:
    query = select(Contact).where(
        Contact.platform == "QQ_NAPCAT",
        Contact.relationship_label == "管理员",
        Contact.whitelisted.is_(True),
    )
    query = query.where(Contact.account_id == source.account_id)
    return db.scalar(query.order_by(Contact.created_at.asc()))


def create_delegated_todo(
    db: Session,
    *,
    source: Contact,
    source_message: Message,
    intent: DelegatedTodoIntent,
    admin: Contact | None,
) -> TodoItem:
    item = TodoItem(
        title=intent.title,
        detail=intent.original,
        status="OPEN",
        priority="NORMAL",
        reminder_enabled=False,
        contact_id=source.id,
        source_message_id=source_message.id,
        kind=CONTACT_RELAY_KIND,
        account_id=source.account_id,
        admin_contact_id=admin.id if admin else None,
        delivery_status="PENDING" if admin else "ADMIN_MISSING",
        last_error=None if admin else "当前托管账号没有可用的白名单管理员联系人",
    )
    db.add(item)
    db.flush()
    return item


def referenced_delegated_todo(
    db: Session,
    *,
    admin: Contact,
    reply_to_external_id: str | None,
) -> tuple[TodoItem, Message] | None:
    if not reply_to_external_id or admin.relationship_label.strip() != "管理员" or not admin.whitelisted:
        return None
    notification = db.scalar(
        select(Message).where(
            Message.platform == "QQ_NAPCAT",
            Message.account_id == admin.account_id,
            Message.external_message_id == reply_to_external_id,
            Message.receiver_id == admin.platform_user_id,
        )
    )
    if notification is None:
        return None
    detail = (notification.raw_envelope or {}).get("contact_relay")
    if not isinstance(detail, dict) or not detail.get("todo_id"):
        return None
    todo = db.get(TodoItem, str(detail["todo_id"]))
    if (
        todo is None
        or todo.kind != CONTACT_RELAY_KIND
        or todo.status != "OPEN"
        or todo.admin_contact_id != admin.id
        or todo.admin_notification_message_id != notification.id
    ):
        return None
    return todo, notification


def build_relay_response_messages(*, target: Contact, original: str, admin_reply: str) -> list[LLMMessage]:
    return [
        LLMMessage(
            role="system",
            content=(
                "你是橙蓝。管理员正在回复一条由联系人转交给主人处理的待办。"
                "把管理员的答复整理成一条可直接发给原联系人的自然简体中文消息。"
                "必须忠实保留答复含义，不补充事实，不提后台、待办、模型或系统。"
                "可以自然写成“主人说……”或“我问过主人啦，他说……”，不要固定套话，最多三句。"
            ),
        ),
        LLMMessage(
            role="user",
            content=f"原联系人：{target.display_name}\n原请求：{original}\n管理员答复：{admin_reply}\n请只输出要发给原联系人的正文。",
        ),
    ]


def fallback_relay_response(admin_reply: str) -> str:
    normalized = " ".join((admin_reply or "").split()).strip()
    if normalized.startswith("我现在"):
        normalized = "他现在" + normalized[3:]
    elif normalized.startswith("我刚才"):
        normalized = "他刚才" + normalized[3:]
    elif normalized.startswith("我"):
        normalized = "他" + normalized[1:]
    return f"主人说{normalized.rstrip('。！？!?')}。" if normalized else "主人暂时还没有留下具体答复。"
