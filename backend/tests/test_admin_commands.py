from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path
import re

import pytest
from sqlalchemy import func, select

from app.channels.base import ChannelConnector, InboundAttachment, InboundEvent, OutboundMessage, SendPermit, SendResult
from app.core.config import Settings
from app.core.enums import ConversationMode, GlobalMode, MessageAuthor, MessageStatus
from app.llm.base import LLMMessage, LLMResponse
from app.llm.gateway import LLMGateway
from app.llm.providers import SimulatorProvider
from app.models.entities import Account, AdminMemoryImportBatch, AdminMemoryImportItem, AuditLog, Contact, Conversation, Memory, Message, MessageAttachment, PersonaProfile, ProviderConfig, RuntimeState
from app.services.admin_commands import parse_admin_command
from app.services.media_understanding import MediaInsight, MediaUnderstandingService
from app.services.pipeline import MessagePipeline


class RecordingConnector(ChannelConnector):
    platform = "QQ_NAPCAT"
    real_channel = False

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def send(self, message: OutboundMessage, permit: SendPermit) -> SendResult:
        permit.assert_valid_for(message)
        self.sent.append(message)
        return SendResult(True, external_message_id=f"admin-test-{len(self.sent)}")

    async def status(self) -> dict[str, object]:
        return {"status": "ONLINE"}


class FixedProvider:
    name = "fixed-test-provider"

    def __init__(self, content: str, model: str = "fixed-test-model") -> None:
        self.content = content
        self.model = model
        self.calls = 0

    async def chat(self, messages: list[LLMMessage]) -> LLMResponse:
        self.calls += 1
        return LLMResponse(self.content, self.model, self.name, 1, 20, 40)

    async def test_connection(self) -> bool:
        return True

    async def models(self) -> list[str]:
        return [self.model]


class SequentialProvider(FixedProvider):
    def __init__(self, contents: list[str]) -> None:
        super().__init__(contents[-1])
        self.contents = contents
        self.calls = 0

    async def chat(self, messages: list[LLMMessage]) -> LLMResponse:
        content = self.contents[min(self.calls, len(self.contents) - 1)]
        self.calls += 1
        return LLMResponse(content, self.model, self.name, 1, 20, 40)


def _seed_admin(db, *, gate: str = "LIVE", mode: str = "AUTO", kill: bool = False, data_dir: Path | None = None) -> tuple[Contact, RecordingConnector, MessagePipeline]:
    db.add(RuntimeState(id=1, global_mode=mode, release_gate=gate, kill_switch=kill, live_time_window_enabled=False))
    db.add(Account(platform="QQ_NAPCAT", display_name="测试托管号", connector_kind="NAPCAT_ONEBOT11", status="ONLINE", enabled=True, credential_id="test", config={"managed_qq_id": "1032556054", "risk_acknowledged": True, "api_base": "http://127.0.0.1:3001"}))
    contact = Contact(platform="QQ_NAPCAT", platform_user_id="1032556054", display_name="程澜喵！", relationship_label="管理员", whitelisted=True, ai_enabled=True)
    db.add_all([contact, PersonaProfile(id=1)])
    db.commit()
    connector = RecordingConnector()
    pipeline = MessagePipeline(gateway=LLMGateway([SimulatorProvider()]), connectors={"QQ_NAPCAT": connector}, settings=Settings(secret_key="x" * 40, data_dir=data_dir or Path("data")))
    return contact, connector, pipeline


def _event(
    contact: Contact,
    message_id: str,
    content: str,
    *,
    is_group: bool = False,
    message_type: str = "TEXT",
    attachments: tuple[InboundAttachment, ...] = (),
) -> InboundEvent:
    return InboundEvent(
        platform="QQ_NAPCAT",
        message_id=message_id,
        conversation_id=f"napcat:private:{contact.platform_user_id}",
        sender_id=contact.platform_user_id,
        sender_name=contact.display_name,
        content=content,
        is_group=is_group,
        message_type=message_type,
        attachments=attachments,
        timestamp=datetime.now(timezone.utc),
        raw={"event_type": "ONEBOT11_MESSAGE", "text_content": "" if attachments else content},
    )


def test_parser_only_accepts_private_whitelisted_admin_text() -> None:
    admin = Contact(platform="QQ_NAPCAT", platform_user_id="1", display_name="管理员", relationship_label="管理员", whitelisted=True)
    normal = Contact(platform="QQ_NAPCAT", platform_user_id="2", display_name="朋友", relationship_label="朋友", whitelisted=True)
    assert parse_admin_command(admin, "/neko 状态", is_group=False, message_type="TEXT") is not None
    assert parse_admin_command(admin, "普通聊天", is_group=False, message_type="TEXT") is None
    assert parse_admin_command(admin, "/neko 状态", is_group=True, message_type="TEXT") is None
    assert parse_admin_command(admin, "/neko 状态", is_group=False, message_type="IMAGE") is None
    assert parse_admin_command(admin, "/neko 发送文件 好友", is_group=False, message_type="MIXED") is not None
    assert parse_admin_command(normal, "/neko 急停", is_group=False, message_type="TEXT") is None


@pytest.mark.asyncio
async def test_status_command_uses_local_deterministic_reply_without_llm(db) -> None:
    contact, connector, pipeline = _seed_admin(db)
    result = await pipeline.handle(_event(contact, "admin-status", "/neko 状态"), db)
    assert result.code == "ADMIN_STATUS"
    assert result.sent is True
    assert len(connector.sent) == 1
    outbound = db.scalar(select(Message).where(Message.external_message_id == "admin-test-1"))
    assert outbound is not None
    assert outbound.author == MessageAuthor.SYSTEM
    assert outbound.provider == "LOCAL_COMMAND"
    assert "发布门禁：LIVE" in outbound.content


@pytest.mark.asyncio
async def test_silent_command_acknowledges_then_applies_and_cancels_queue(db) -> None:
    contact, connector, pipeline = _seed_admin(db)
    result = await pipeline.handle(_event(contact, "admin-silent", "/neko 静默"), db)
    state = db.get(RuntimeState, 1)
    assert result.code == "ADMIN_MODE_SILENT"
    assert result.sent is True
    assert state is not None and state.global_mode == GlobalMode.SILENT
    assert len(connector.sent) == 1


@pytest.mark.asyncio
async def test_kill_switch_is_immediate_and_never_sends_confirmation(db) -> None:
    contact, connector, pipeline = _seed_admin(db)
    result = await pipeline.handle(_event(contact, "admin-kill", "/neko 急停"), db)
    state = db.get(RuntimeState, 1)
    assert result.code == "ADMIN_KILL_SWITCH_ON"
    assert state is not None and state.kill_switch is True
    assert connector.sent == []
    response = db.scalar(select(Message).where(Message.author == MessageAuthor.SYSTEM))
    assert response is not None and response.status == MessageStatus.BLOCKED


@pytest.mark.asyncio
async def test_live_cannot_be_enabled_by_chat_and_normal_admin_chat_still_works(db) -> None:
    contact, connector, pipeline = _seed_admin(db, gate="SHADOW")
    refused = await pipeline.handle(_event(contact, "admin-live", "/neko LIVE"), db)
    assert refused.code == "ADMIN_LIVE_DASHBOARD_ONLY"
    assert db.get(RuntimeState, 1).release_gate == "SHADOW"
    assert connector.sent == []

    normal = await pipeline.handle(_event(contact, "admin-normal", "今天也正常聊聊天"), db)
    assert not normal.code.startswith("ADMIN_")
    assert normal.shadowed is True


@pytest.mark.asyncio
async def test_non_admin_command_cannot_mutate_runtime(db) -> None:
    contact, _, pipeline = _seed_admin(db, gate="SHADOW")
    contact.relationship_label = "朋友"
    db.commit()
    result = await pipeline.handle(_event(contact, "friend-command", "/neko 急停"), db)
    assert result.code != "ADMIN_KILL_SWITCH_ON"
    assert db.get(RuntimeState, 1).kill_switch is False


@pytest.mark.asyncio
async def test_admin_text_delivery_requires_confirmation_and_whitelist(db, tmp_path: Path) -> None:
    admin, connector, pipeline = _seed_admin(db, data_dir=tmp_path)
    target = Contact(platform="QQ_NAPCAT", platform_user_id="2002", display_name="测试好友", relationship_label="朋友", whitelisted=True, ai_enabled=False)
    db.add(target)
    db.commit()

    prepared = await pipeline.handle(_event(admin, "delivery-draft", "/neko 发送 测试好友-这是一条确认后才发送的消息"), db)
    assert prepared.code == "ADMIN_DELIVERY_DRAFTED"
    assert [item.target_id for item in connector.sent] == [admin.platform_user_id]
    code = re.search(r"确认发送 ([A-F0-9]{6})", prepared.reply or "").group(1)

    confirmed = await pipeline.handle(_event(admin, "delivery-confirm", f"/neko 确认发送 {code}"), db)
    assert confirmed.code == "ADMIN_DELIVERY_SENT"
    assert connector.sent[-2].target_id == target.platform_user_id
    assert connector.sent[-2].content == "这是一条确认后才发送的消息"
    assert connector.sent[-1].target_id == admin.platform_user_id


@pytest.mark.asyncio
async def test_admin_can_ask_neko_to_start_a_topic_with_a_whitelisted_contact(db, tmp_path: Path) -> None:
    admin, connector, pipeline = _seed_admin(db, data_dir=tmp_path)
    target = Contact(
        platform="QQ_NAPCAT",
        platform_user_id="2009",
        display_name="技术好友",
        relationship_label="朋友",
        whitelisted=True,
        ai_enabled=True,
    )
    db.add(target)
    db.commit()
    natural_opener = "主人觉得我们俩应该会对 SAM 挺有共同话题，就让我来问问你～它可以用提示点或框做通用分割，你更关注交互标注还是自动分割呀？"
    pipeline.gateway = LLMGateway([FixedProvider(natural_opener)])

    result = await pipeline.handle(
        _event(admin, "topic-start", "/neko 找 技术好友-图像识别中 SAM 技术的运用"),
        db,
    )

    assert result.code == "ADMIN_PROACTIVE_TOPIC_SENT"
    assert result.sent is True
    assert len(connector.sent) == 2
    topic_send, acknowledgement = connector.sent
    assert topic_send.target_id == target.platform_user_id
    assert topic_send.content == natural_opener
    assert "主人让我来找技术好友聊聊关于" not in topic_send.content
    assert "交互标注还是自动分割" in topic_send.content
    assert acknowledgement.target_id == admin.platform_user_id
    stored = db.scalar(
        select(Message).where(
            Message.contact_id == target.id,
            Message.author == MessageAuthor.AI,
        )
    )
    assert stored is not None
    assert stored.provider == "fixed-test-provider"
    assert stored.model == "fixed-test-model"
    assert stored.status == MessageStatus.SENT
    assert target.account_id is not None


@pytest.mark.asyncio
async def test_admin_can_add_approved_memory_with_hyphen_and_legacy_pipe_is_rejected(db, tmp_path: Path) -> None:
    admin, _, pipeline = _seed_admin(db, data_dir=tmp_path)
    target = Contact(
        platform="QQ_NAPCAT",
        platform_user_id="2088",
        display_name="记忆好友",
        relationship_label="朋友",
        whitelisted=True,
        memory_enabled=True,
    )
    db.add(target)
    db.commit()

    result = await pipeline.handle(
        _event(admin, "memory-add", "/neko 记住 记忆好友-喜欢研究视觉识别"),
        db,
    )
    legacy = await pipeline.handle(
        _event(admin, "memory-old-separator", "/neko 记住 记忆好友 | 不应写入"),
        db,
    )
    legacy_start = await pipeline.handle(
        _event(admin, "memory-import-old-separator", "/neko 截图开始 记忆好友|另一批"),
        db,
    )

    assert result.code == "ADMIN_MEMORY_ADDED"
    stored = db.scalar(select(Memory).where(Memory.contact_id == target.id))
    assert stored is not None
    assert stored.content == "喜欢研究视觉识别"
    assert stored.review_status == "APPROVED"
    assert legacy.code == "ADMIN_LEGACY_SEPARATOR"
    assert "分隔符已统一改为“-”" in (legacy.reply or "")
    assert legacy_start.code == "ADMIN_LEGACY_SEPARATOR"
    assert "分隔符已统一改为“-”" in (legacy_start.reply or "")
    assert db.scalar(select(func.count(Memory.id)).where(Memory.contact_id == target.id)) == 1


@pytest.mark.asyncio
async def test_admin_screenshot_batch_collects_without_reply_then_writes_approved_memories(
    db,
    tmp_path: Path,
    monkeypatch,
) -> None:
    admin, connector, pipeline = _seed_admin(db, data_dir=tmp_path)
    state = db.get(RuntimeState, 1)
    state.media_understanding_enabled = False
    state.media_understand_images = False
    admin.media_storage_enabled = False
    target = Contact(
        platform="QQ_NAPCAT",
        platform_user_id="2077",
        display_name="截图好友",
        relationship_label="朋友",
        whitelisted=True,
        memory_enabled=True,
    )
    db.add(target)
    db.commit()
    provider = FixedProvider(
        '{"summary":"对方喜欢摄影，也在学习分割模型。","memories":['
        '{"kind":"PREFERENCE","content":"喜欢摄影"},'
        '{"kind":"ONGOING_TOPIC","content":"正在学习图像分割模型"}]}',
        model="kimi-k3",
    )
    provider.name = "Kimi Cloud"
    pipeline.gateway = LLMGateway([provider])

    async def fake_analyze(self, request_db, rows, runtime_state, force_images=False):
        assert force_images is True
        insights = []
        for row in rows:
            row.analysis_status = "COMPLETED"
            row.analysis_provider = "TEST_OCR"
            row.analysis_model = "test-vision"
            row.analysis_text = "聊天截图：对方说自己很喜欢摄影，最近正在学习图像分割模型。"
            insights.append(MediaInsight(row.id, "IMAGE", row.file_name, row.analysis_text, "TEST_OCR", "test-vision"))
        request_db.flush()
        return insights

    monkeypatch.setattr(MediaUnderstandingService, "analyze", fake_analyze)

    started = await pipeline.handle(
        _event(admin, "memory-import-start", "/neko 截图开始 截图好友"),
        db,
    )
    image = InboundAttachment(
        kind="IMAGE",
        segment_type="image",
        segment_index=0,
        file_name="chat.png",
        source_ref="data:image/png;base64," + base64.b64encode(b"fake-png").decode(),
        mime_type="image/png",
    )
    captured = await pipeline.handle(
        _event(admin, "memory-import-image", "[图片]", message_type="IMAGE", attachments=(image,)),
        db,
    )
    completed = await pipeline.handle(
        _event(admin, "memory-import-end", "/neko 截图结束"),
        db,
    )

    assert started.code == "ADMIN_MEMORY_IMPORT_STARTED"
    assert captured.code == "ADMIN_MEMORY_IMPORT_SCREENSHOT_CAPTURED"
    assert completed.code == "ADMIN_MEMORY_IMPORT_COMPLETED"
    assert "实际模型：Kimi Cloud / kimi-k3" in (completed.reply or "")
    assert len(connector.sent) == 2
    batch = db.scalar(select(AdminMemoryImportBatch))
    assert batch is not None
    assert (batch.status, batch.screenshot_count, batch.memory_count) == ("COMPLETED", 1, 2)
    assert batch.summary == "对方喜欢摄影，也在学习分割模型。"
    assert db.scalar(select(func.count(AdminMemoryImportItem.id))) == 1
    memories = list(db.scalars(select(Memory).where(Memory.contact_id == target.id).order_by(Memory.kind)))
    assert [(item.kind, item.content, item.review_status) for item in memories] == [
        ("ONGOING_TOPIC", "正在学习图像分割模型", "APPROVED"),
        ("PREFERENCE", "喜欢摄影", "APPROVED"),
    ]


@pytest.mark.asyncio
async def test_admin_screenshot_batch_does_not_fall_through_after_reaching_limit(db, tmp_path: Path) -> None:
    admin, connector, pipeline = _seed_admin(db, data_dir=tmp_path)
    target = Contact(
        platform="QQ_NAPCAT",
        platform_user_id="2078",
        display_name="截图上限好友",
        relationship_label="朋友",
        whitelisted=True,
        memory_enabled=True,
    )
    db.add(target)
    db.commit()
    await pipeline.handle(
        _event(admin, "memory-limit-start", "/neko 截图开始 截图上限好友"),
        db,
    )
    batch = db.scalar(select(AdminMemoryImportBatch).where(AdminMemoryImportBatch.status == "ACTIVE"))
    assert batch is not None
    batch.screenshot_count = 40
    db.commit()
    image = InboundAttachment(
        kind="IMAGE",
        segment_type="image",
        segment_index=0,
        file_name="over-limit.png",
        source_ref="data:image/png;base64," + base64.b64encode(b"fake-png").decode(),
        mime_type="image/png",
    )

    result = await pipeline.handle(
        _event(admin, "memory-limit-image", "[图片]", message_type="IMAGE", attachments=(image,)),
        db,
    )

    assert result.code == "ADMIN_MEMORY_IMPORT_SCREENSHOT_LIMIT"
    assert "40 张上限" in result.reason
    assert len(connector.sent) == 1
    assert db.scalar(select(func.count(AdminMemoryImportItem.id))) == 0


@pytest.mark.asyncio
async def test_admin_proactive_topic_prefers_kimi_k3_and_records_the_actual_route(db, tmp_path: Path) -> None:
    admin, connector, pipeline = _seed_admin(db, data_dir=tmp_path)
    target = Contact(
        platform="QQ_NAPCAT",
        platform_user_id="2099",
        display_name="Kimi话题好友",
        relationship_label="朋友",
        whitelisted=True,
        ai_enabled=True,
    )
    db.add(target)
    db.commit()
    deepseek = FixedProvider(
        "主人提过这个话题，我用 DeepSeek 来问你怎么看？",
        model="deepseek-v4-flash",
    )
    deepseek.name = "DeepSeek"
    kimi = FixedProvider(
        "主人觉得我们可以聊聊视觉模型，就托我来问问～你更看重识别精度还是响应速度呀？",
        model="kimi-k3",
    )
    kimi.name = "Kimi Cloud"
    pipeline.gateway = LLMGateway([deepseek, kimi])

    result = await pipeline.handle(
        _event(admin, "topic-kimi-first", "/neko 聊聊 Kimi话题好友-视觉模型的实际运用"),
        db,
    )

    assert result.code == "ADMIN_PROACTIVE_TOPIC_SENT"
    assert connector.sent[0].content == kimi.content
    assert "实际模型：Kimi Cloud / kimi-k3" in connector.sent[1].content
    assert [deepseek.calls, kimi.calls] == [0, 1]
    stored = db.scalar(
        select(Message).where(
            Message.contact_id == target.id,
            Message.author == MessageAuthor.AI,
        )
    )
    assert stored is not None
    assert (stored.provider, stored.model) == ("Kimi Cloud", "kimi-k3")
    audit = db.scalar(
        select(AuditLog).where(
            AuditLog.message_id == result.inbound_message_id,
            AuditLog.event == "LLM_REQUEST",
        )
    )
    assert audit is not None
    assert audit.detail["preferred_models"] == ["kimi-k3"]
    assert audit.detail["provider_order"][:2] == ["Kimi Cloud", "DeepSeek"]


@pytest.mark.asyncio
async def test_topic_opener_is_regenerated_as_a_whole_instead_of_getting_a_fixed_prefix(db, tmp_path: Path) -> None:
    admin, connector, pipeline = _seed_admin(db, data_dir=tmp_path)
    target = Contact(
        platform="QQ_NAPCAT",
        platform_user_id="2011",
        display_name="自然聊天好友",
        relationship_label="朋友",
        whitelisted=True,
        ai_enabled=True,
    )
    db.add(target)
    db.commit()
    provider = SequentialProvider(
        [
            "SAM 用提示点做交互分割挺有意思的，你更关注哪种用法？",
            "主人说你可能也对 SAM 感兴趣，就让我来问问～我觉得提示点交互特别适合快速标注，你更关注标注效率还是分割精度呀？",
        ]
    )
    pipeline.gateway = LLMGateway([provider])

    result = await pipeline.handle(
        _event(admin, "topic-natural-rewrite", "/neko 找 自然聊天好友-SAM 的实际应用"),
        db,
    )

    assert result.code == "ADMIN_PROACTIVE_TOPIC_SENT"
    assert provider.calls == 2
    assert connector.sent[0].content == provider.contents[1]
    assert "主人让我来找自然聊天好友聊聊关于" not in connector.sent[0].content
    stored = db.scalar(
        select(Message).where(
            Message.contact_id == target.id,
            Message.author == MessageAuthor.AI,
        )
    )
    assert stored is not None
    assert stored.raw_envelope["opener_regenerated"] is True


@pytest.mark.asyncio
async def test_admin_topic_command_rejects_non_whitelisted_target_without_model_call(db, tmp_path: Path) -> None:
    admin, connector, pipeline = _seed_admin(db, data_dir=tmp_path)
    db.add(
        Contact(
            platform="QQ_NAPCAT",
            platform_user_id="2010",
            display_name="非白名单好友",
            relationship_label="朋友",
            whitelisted=False,
            ai_enabled=True,
        )
    )
    db.commit()

    result = await pipeline.handle(
        _event(admin, "topic-denied", "/neko 找 非白名单好友-不应该发送的话题"),
        db,
    )

    assert result.code == "ADMIN_TARGET_NOT_WHITELISTED"
    assert all(item.target_id == admin.platform_user_id for item in connector.sent)


@pytest.mark.asyncio
async def test_long_ai_reply_becomes_txt_attachment_and_file_echo_does_not_take_over(db, tmp_path: Path) -> None:
    admin, connector, pipeline = _seed_admin(db, data_dir=tmp_path)
    long_reply = "这是完整的长回复内容。" * 90
    pipeline.gateway = LLMGateway([FixedProvider(long_reply)])
    pipeline.settings.napcat_text_safe_limit = 500

    result = await pipeline.handle(_event(admin, "long-reply", "请详细说明这个普通话题"), db)

    assert result.sent is True
    assert len(connector.sent) == 1
    sent = connector.sent[0]
    assert sent.content.startswith("这次回复比较长")
    assert len(sent.attachments) == 1
    assert sent.attachments[0].kind == "FILE"
    assert sent.attachments[0].file_name.endswith(".txt")
    saved_text = Path(sent.attachments[0].file_path).read_text(encoding="utf-8-sig")
    assert saved_text == long_reply
    outbound = db.get(Message, result.outbound_message_id)
    assert outbound is not None
    assert outbound.message_type == "MIXED"
    attachment = db.scalar(select(MessageAttachment).where(MessageAttachment.message_id == outbound.id))
    assert attachment is not None
    assert attachment.attachment_metadata["source"] == "AI_TEXT_OVERFLOW"

    conversation = db.get(Conversation, outbound.conversation_id)
    assert conversation is not None and conversation.mode == ConversationMode.AUTO_READY
    file_echo = InboundEvent(
        platform="QQ_NAPCAT",
        message_id="long-reply-file-echo",
        conversation_id=conversation.external_id,
        sender_id=admin.platform_user_id,
        sender_name=admin.display_name,
        content=f"[文件：{attachment.file_name}]",
        author=MessageAuthor.HUMAN,
        message_type="FILE",
        attachments=(
            InboundAttachment(
                kind="FILE",
                segment_type="file",
                segment_index=0,
                file_name=attachment.file_name,
            ),
        ),
        timestamp=datetime.now(timezone.utc),
        raw={"event_type": "ONEBOT11_MESSAGE_SENT"},
    )
    echo_result = await pipeline.handle(file_echo, db)
    assert echo_result.duplicate is True
    assert echo_result.code == "AI_SEND_ECHO"
    db.refresh(conversation)
    assert conversation.mode == ConversationMode.AUTO_READY


@pytest.mark.asyncio
async def test_admin_long_form_analysis_uses_pdf_without_important_contact_truncation(db, tmp_path: Path) -> None:
    admin, connector, pipeline = _seed_admin(db, data_dir=tmp_path)
    admin.importance = "IMPORTANT"
    db.commit()
    long_reply = (
        "# MCP 服务器的定义\n\n"
        "MCP 服务器负责把工具、资源和提示能力通过统一协议提供给 AI 客户端。\n\n"
        "# 具体功能与例子\n\n"
        + "例如，一个本地文件 MCP 服务器可以让获得授权的助手检索指定目录，而不暴露其他路径。" * 80
    )
    pipeline.gateway = LLMGateway([FixedProvider(long_reply)])

    result = await pipeline.handle(
        _event(admin, "admin-long-document", "给我一篇长文本分析，告诉我 MCP 服务器的定义和功能，并用具体例子解释"),
        db,
    )

    assert result.sent is True
    assert len(connector.sent) == 1
    sent = connector.sent[0]
    assert sent.content.startswith("分析完成啦")
    assert "先看重点" not in sent.content
    assert "MCP 服务器负责" not in sent.content
    assert "正文只放在文件里" in sent.content
    assert len(sent.attachments) == 1
    assert sent.attachments[0].file_name.endswith(".pdf")
    assert Path(sent.attachments[0].file_path).read_bytes().startswith(b"%PDF")
    outbound = db.get(Message, result.outbound_message_id)
    assert outbound is not None
    assert outbound.message_type == "MIXED"
    assert outbound.raw_envelope["long_form"]["requested"] is True
    assert outbound.raw_envelope["document_delivery"]["format"] == "PDF"
    attachment = db.scalar(select(MessageAttachment).where(MessageAttachment.message_id == outbound.id))
    assert attachment is not None
    assert attachment.attachment_metadata["source"] == "AI_DOCUMENT_RESPONSE"
    assert attachment.attachment_metadata["full_characters"] == len(long_reply)
    assert attachment.attachment_metadata["full_characters"] > 400


@pytest.mark.asyncio
async def test_admin_can_send_staged_file_only_from_sendbox(db, tmp_path: Path) -> None:
    admin, connector, pipeline = _seed_admin(db, data_dir=tmp_path)
    target = Contact(platform="QQ_NAPCAT", platform_user_id="2003", display_name="文件好友", relationship_label="朋友", whitelisted=True)
    db.add(target)
    db.commit()
    sendbox = tmp_path / "sendbox"
    sendbox.mkdir()
    (sendbox / "demo.pdf").write_bytes(b"safe-pdf")

    prepared = await pipeline.handle(_event(admin, "file-draft", "/neko 发送文件 文件好友-demo.pdf"), db)
    code = re.search(r"确认发送 ([A-F0-9]{6})", prepared.reply or "").group(1)
    confirmed = await pipeline.handle(_event(admin, "file-confirm", f"/neko 确认发送 {code}"), db)

    assert confirmed.code == "ADMIN_DELIVERY_SENT"
    file_send = connector.sent[-2]
    assert file_send.target_id == target.platform_user_id
    assert len(file_send.attachments) == 1
    assert file_send.attachments[0].file_name == "demo.pdf"
    assert Path(file_send.attachments[0].file_path).read_bytes() == b"safe-pdf"


@pytest.mark.asyncio
async def test_admin_delivery_rejects_non_whitelisted_target(db, tmp_path: Path) -> None:
    admin, connector, pipeline = _seed_admin(db, data_dir=tmp_path)
    db.add(Contact(platform="QQ_NAPCAT", platform_user_id="2004", display_name="未授权好友", whitelisted=False))
    db.commit()
    result = await pipeline.handle(_event(admin, "delivery-denied", "/neko 发送 未授权好友-不应发送"), db)
    assert result.code == "ADMIN_TARGET_NOT_WHITELISTED"
    assert all(item.target_id == admin.platform_user_id for item in connector.sent)


@pytest.mark.asyncio
async def test_admin_delivery_rechecks_active_account_after_draft(db, tmp_path: Path) -> None:
    admin, connector, pipeline = _seed_admin(db, data_dir=tmp_path)
    target = Contact(platform="QQ_NAPCAT", platform_user_id="2005", display_name="切换测试好友", whitelisted=True)
    db.add(target)
    db.commit()
    prepared = await pipeline.handle(_event(admin, "switch-draft", "/neko 发送 切换测试好友-不应从失效账号发出"), db)
    code = re.search(r"确认发送 ([A-F0-9]{6})", prepared.reply or "").group(1)
    account = db.scalar(select(Account).where(Account.platform == "QQ_NAPCAT"))
    account.enabled = False
    db.commit()

    result = await pipeline.handle(_event(admin, "switch-confirm", f"/neko 确认发送 {code}"), db)

    assert result.code == "ADMIN_CHANNEL_DISABLED"
    assert all(item.target_id == admin.platform_user_id for item in connector.sent)


@pytest.mark.asyncio
async def test_admin_contact_whitelist_and_memory_changes_require_confirmation(db) -> None:
    admin, connector, pipeline = _seed_admin(db)
    target = Contact(
        platform="QQ_NAPCAT",
        platform_user_id="2042360010",
        display_name="大臭狐",
        relationship_label="朋友",
        whitelisted=True,
        ai_enabled=True,
        memory_enabled=True,
    )
    db.add(target)
    db.commit()

    preview = await pipeline.handle(_event(admin, "whitelist-preview", "/neko 白名单 大臭狐 关闭"), db)
    db.refresh(target)
    assert preview.code == "ADMIN_CONFIRM_REQUIRED"
    assert target.whitelisted is True
    assert "确认请发送" in (preview.reply or "")

    applied = await pipeline.handle(_event(admin, "whitelist-apply", "/neko 白名单 2042360010 关闭 确认"), db)
    db.refresh(target)
    assert applied.code == "ADMIN_CONTACT_WHITELISTED_OFF"
    assert target.whitelisted is False

    memory = await pipeline.handle(_event(admin, "memory-apply", "/neko 记忆 大臭狐 关闭 确认"), db)
    db.refresh(target)
    assert memory.code == "ADMIN_CONTACT_MEMORY_ENABLED_OFF"
    assert target.memory_enabled is False
    audit = db.scalar(select(AuditLog).where(AuditLog.message_id == memory.inbound_message_id, AuditLog.event == "ADMIN_COMMAND_APPLIED"))
    assert audit is not None and audit.detail["target_contact_id"] == target.id
    assert len(connector.sent) == 3


@pytest.mark.asyncio
async def test_admin_cannot_disable_own_whitelist_from_chat(db) -> None:
    admin, _, pipeline = _seed_admin(db)

    result = await pipeline.handle(_event(admin, "self-whitelist", "/neko 白名单 1032556054 关闭 确认"), db)

    db.refresh(admin)
    assert result.code == "ADMIN_ADMIN_WHITELIST_PROTECTED"
    assert admin.whitelisted is True


@pytest.mark.asyncio
async def test_admin_can_view_set_and_disable_live_reply_window(db) -> None:
    admin, _, pipeline = _seed_admin(db)

    viewed = await pipeline.handle(_event(admin, "window-view", "/neko 时段"), db)
    assert viewed.code == "ADMIN_TIME_WINDOW"
    assert "10:00–23:30" in (viewed.reply or "")

    preview = await pipeline.handle(_event(admin, "window-preview", "/neko 时段 22:00 02:00"), db)
    assert preview.code == "ADMIN_CONFIRM_REQUIRED"
    assert db.get(RuntimeState, 1).live_auto_start == "10:00"

    changed = await pipeline.handle(_event(admin, "window-set", "/neko 时段 22:00 02:00 确认"), db)
    state = db.get(RuntimeState, 1)
    assert changed.code == "ADMIN_TIME_WINDOW_SET"
    assert state.live_time_window_enabled is True
    assert state.live_auto_start == "22:00" and state.live_auto_end == "02:00"

    disabled = await pipeline.handle(_event(admin, "window-off", "/neko 时段 关闭 确认"), db)
    assert disabled.code == "ADMIN_TIME_WINDOW_OFF"
    assert db.get(RuntimeState, 1).live_time_window_enabled is False


@pytest.mark.asyncio
async def test_admin_can_view_and_switch_preferred_model(db) -> None:
    admin, _, pipeline = _seed_admin(db)
    ollama = ProviderConfig(name="Ollama", provider_type="OLLAMA", base_url="http://127.0.0.1:11434", model="llama3.1:8b", enabled=True, priority=5)
    deepseek = ProviderConfig(name="DeepSeek", provider_type="DEEPSEEK", base_url="https://api.deepseek.com", model="deepseek-chat", enabled=True, priority=10)
    db.add_all([ollama, deepseek])
    db.commit()

    viewed = await pipeline.handle(_event(admin, "models-view", "/neko 模型"), db)
    assert viewed.code == "ADMIN_MODELS"
    assert "当前优先" in (viewed.reply or "") and "Ollama" in (viewed.reply or "")

    preview = await pipeline.handle(_event(admin, "model-preview", "/neko 模型优先 DeepSeek"), db)
    assert preview.code == "ADMIN_CONFIRM_REQUIRED"
    db.refresh(ollama)
    assert ollama.priority == 5

    changed = await pipeline.handle(_event(admin, "model-apply", "/neko 模型优先 DeepSeek 确认"), db)
    db.refresh(ollama)
    db.refresh(deepseek)
    assert changed.code == "ADMIN_PREFERRED_MODEL_CHANGED"
    assert deepseek.priority == 0 and ollama.priority == 10


@pytest.mark.asyncio
async def test_admin_can_view_bounded_recent_contact_history(db) -> None:
    admin, connector, pipeline = _seed_admin(db)
    target = Contact(platform="QQ_NAPCAT", platform_user_id="2008", display_name="记录好友", whitelisted=True)
    conversation = Conversation(platform="QQ_NAPCAT", external_id="napcat:private:2008", contact=target)
    db.add_all([target, conversation])
    db.flush()
    db.add_all(
        [
            Message(platform="QQ_NAPCAT", external_message_id="history-in", conversation_id=conversation.id, contact_id=target.id, author=MessageAuthor.CONTACT, direction="INBOUND", content="第一条测试消息", status=MessageStatus.RECEIVED),
            Message(platform="QQ_NAPCAT", external_message_id="history-out", conversation_id=conversation.id, contact_id=target.id, author=MessageAuthor.AI, direction="OUTBOUND", content="这是 Neko 的回复", status=MessageStatus.SENT),
        ]
    )
    db.commit()

    result = await pipeline.handle(_event(admin, "history-view", "/neko 记录 记录好友 2"), db)

    assert result.code == "ADMIN_RECENT_MESSAGES"
    assert "第一条测试消息" in (result.reply or "")
    assert "Neko：这是 Neko 的回复" in (result.reply or "")
    assert connector.sent[-1].target_id == admin.platform_user_id
