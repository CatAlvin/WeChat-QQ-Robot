from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.core.clock import utc_now
from app.llm.base import LLMMessage, LLMProvider, LLMResponse
from app.models.entities import (
    BackgroundTask,
    ConnectorEvent,
    Contact,
    Conversation,
    KnowledgeDocument,
    Message,
    RecoveryItem,
    StickerAsset,
)
from app.services.knowledge import content_sha256, search_knowledge
from app.services.model_router import route_providers
from app.services.stickers import select_auto_sticker
from app.services.task_center import enqueue_task, prune_background_tasks, retry_recovery_item, run_next_task, sync_recovery_items


class Provider(LLMProvider):
    def __init__(self, name: str, *, media: bool = False):
        self.name = name
        self.supports_attachments = media

    async def chat(self, messages):
        return LLMResponse("ok", "test", self.name, 1)

    async def test_connection(self):
        return True

    async def models(self):
        return ["test"]


def test_local_knowledge_only_returns_matching_enabled_documents(db):
    matching = KnowledgeDocument(title="Neko 部署", source_name="test", content="服务端口是 8000，前端端口是 3000。", sha256=content_sha256("服务端口是 8000，前端端口是 3000。"))
    disabled = KnowledgeDocument(title="私密草稿", source_name="test", content="服务端口是 9999。", sha256=content_sha256("服务端口是 9999。"), enabled=False)
    db.add_all([matching, disabled])
    db.flush()

    result = search_knowledge(db, "Neko 的服务端口是什么？")

    assert "Neko 部署" in result
    assert "8000" in result
    assert "9999" not in result


def test_task_center_generates_digest_and_persists_progress(db):
    contact = Contact(platform="SIMULATOR", platform_user_id="digest-user", display_name="摘要测试", whitelisted=True)
    db.add(contact)
    db.flush()
    conversation = Conversation(platform="SIMULATOR", external_id="digest-conversation", contact_id=contact.id)
    db.add(conversation)
    db.flush()
    db.add(Message(platform="SIMULATOR", external_message_id="digest-message", conversation_id=conversation.id, contact_id=contact.id, content="今天聊一下摘要", event_at=utc_now()))
    task = enqueue_task(db, kind="DAILY_DIGEST", title="测试摘要")
    db.flush()

    processed = run_next_task(db)

    assert processed is task
    assert task.status == "DONE"
    assert task.progress == 100
    assert task.result["digest_id"]


def test_task_center_prunes_old_terminal_history_to_fifty(db):
    base = utc_now()
    rows = [
        BackgroundTask(
            kind="KNOWLEDGE_REFRESH",
            title=f"历史任务 {index}",
            status="DONE",
            available_at=base + timedelta(seconds=index),
            created_at=base + timedelta(seconds=index),
        )
        for index in range(60)
    ]
    db.add_all(rows)
    db.flush()

    assert prune_background_tasks(db) == 10
    remaining = list(db.scalars(select(BackgroundTask).order_by(BackgroundTask.created_at.asc())))
    assert len(remaining) == 50
    assert remaining[0].title == "历史任务 10"


def test_task_center_never_prunes_pending_work(db):
    base = utc_now()
    pending = BackgroundTask(kind="RECOVERY_SYNC", title="仍在等待", status="PENDING", available_at=base, created_at=base)
    db.add(pending)
    db.add_all(
        BackgroundTask(
            kind="KNOWLEDGE_REFRESH",
            title=f"已完成 {index}",
            status="DONE",
            available_at=base + timedelta(seconds=index + 1),
            created_at=base + timedelta(seconds=index + 1),
        )
        for index in range(55)
    )
    db.flush()

    prune_background_tasks(db)

    assert db.get(BackgroundTask, pending.id) is pending
    assert db.query(BackgroundTask).count() == 50


def test_recovery_sync_allows_inbound_retry_but_not_outbound_resend(db):
    event = ConnectorEvent(platform="QQ_NAPCAT", external_event_id="failed-event", event_type="message", normalized_payload={}, status="FAILED", last_error="NameError")
    db.add(event)
    db.flush()
    assert sync_recovery_items(db) == 1
    item = db.query(RecoveryItem).one()

    retry_recovery_item(db, item)

    assert event.status == "PENDING"
    assert item.status == "RETRYING"
    assert item.retry_count == 1


def test_smart_route_preserves_chat_order_and_selects_specialists():
    deepseek = Provider("DeepSeek")
    kimi = Provider("Kimi Cloud", media=True)
    local = Provider("Ollama Local")

    general = route_providers([deepseek, kimi, local], [LLMMessage("user", "你好")])
    reasoning = route_providers([kimi, deepseek, local], [LLMMessage("user", "请分析代码报错原因")])
    private = route_providers([deepseek, kimi, local], [LLMMessage("user", "这条消息只在本地处理")])
    old_long_form = route_providers(
        [deepseek, kimi, local],
        [LLMMessage("user", "最近会话：\nCONTACT: 给我一篇长文本分析\n\n当前消息：下午好")],
    )

    assert general.task_type == "GENERAL_CHAT"
    assert [item.name for item in general.providers] == ["DeepSeek", "Kimi Cloud", "Ollama Local"]
    assert reasoning.task_type == "REASONING"
    assert [item.name for item in reasoning.providers[:2]] == ["Kimi Cloud", "DeepSeek"]
    assert private.task_type == "PRIVATE_LOCAL"
    assert private.providers[0].name == "Ollama Local"
    assert old_long_form.task_type == "GENERAL_CHAT"


def test_sticker_requires_explicit_auto_enable_and_exact_tag_match(db):
    disabled = StickerAsset(label="关闭", tags=["开心"], local_path="stickers/a.png", mime_type="image/png", sha256="a" * 64, auto_reply_enabled=False)
    enabled = StickerAsset(label="开心猫", tags=["太好了", "开心"], local_path="stickers/b.png", mime_type="image/png", sha256="b" * 64, auto_reply_enabled=True)
    db.add_all([disabled, enabled])
    db.flush()

    assert select_auto_sticker(db, "这真是太好了！") is enabled
    assert select_auto_sticker(db, "普通回复") is None
