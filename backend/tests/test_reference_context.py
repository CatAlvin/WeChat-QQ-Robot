from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.channels.base import InboundEvent
from app.core.config import Settings
from app.llm.base import LLMMessage, LLMProvider, LLMResponse
from app.llm.gateway import LLMGateway
from app.models.entities import AuditLog, Contact, Conversation, Message, MessageAttachment, PersonaProfile, RuntimeState
from app.services.pipeline import MessagePipeline


class CapturingProvider(LLMProvider):
    name = "capture"

    def __init__(self) -> None:
        self.messages: list[LLMMessage] = []

    async def chat(self, messages: list[LLMMessage]) -> LLMResponse:
        self.messages = messages
        return LLMResponse("这份论文讨论了 AI Agent 的记忆形式、功能与动态。", "capture-v1", self.name, 1)

    async def test_connection(self) -> bool:
        return True

    async def models(self) -> list[str]:
        return ["capture-v1"]


def _seed_pdf_history(db) -> tuple[Contact, Conversation]:
    contact = Contact(platform="QQ_NAPCAT", platform_user_id="1001", display_name="管理员", relationship_label="管理员", whitelisted=True, ai_enabled=True, memory_enabled=False)
    conversation = Conversation(platform="QQ_NAPCAT", external_id="napcat:private:1001", contact=contact)
    db.add_all([RuntimeState(id=1, release_gate="SHADOW", media_understanding_enabled=True), contact, conversation, PersonaProfile(id=1)])
    db.flush()
    message = Message(platform="QQ_NAPCAT", external_message_id="pdf-1", conversation_id=conversation.id, contact_id=contact.id, content="[文件：paper.pdf]", message_type="FILE")
    db.add(message)
    db.flush()
    db.add(MessageAttachment(message_id=message.id, kind="FILE", segment_type="file", segment_index=0, file_name="paper.pdf", mime_type="application/pdf", status="SAVED", local_path="media/fake/paper.pdf", analysis_status="COMPLETED", analysis_provider="LOCAL_DOCUMENT", analysis_model="pypdf", analysis_text="Memory in the Age of AI Agents. 本文系统分析智能体记忆的形式、功能、工作机制与动态。"))
    db.commit()
    return contact, conversation


@pytest.mark.asyncio
async def test_recent_pdf_analysis_is_added_to_followup_without_formal_false_positive(db, tmp_path) -> None:
    contact, conversation = _seed_pdf_history(db)
    provider = CapturingProvider()
    pipeline = MessagePipeline(gateway=LLMGateway([provider]), connectors={}, settings=Settings(data_dir=tmp_path, secret_key="r" * 40))
    event = InboundEvent(platform="QQ_NAPCAT", message_id="followup-1", conversation_id=conversation.external_id, sender_id=contact.platform_user_id, sender_name=contact.display_name, content="请总结上面的 PDF", timestamp=datetime.now(timezone.utc), raw={"event_type": "ONEBOT11_MESSAGE", "text_content": "请总结上面的 PDF"})

    result = await pipeline.handle(event, db)

    assert result.shadowed is True
    assert result.code == "SHADOWED"
    prompt = provider.messages[-1].content
    assert "Memory in the Age of AI Agents" in prompt
    assert prompt.index("Memory in the Age of AI Agents") < prompt.index("当前消息：请总结上面的 PDF")
    assert "不要把当前消息改写或复述成答案" in prompt
    audit = db.scalar(select(AuditLog).where(AuditLog.event == "REFERENCE_CONTEXT_RESOLVED"))
    assert audit is not None and audit.detail["source"] == "RECENT_ATTACHMENT"


@pytest.mark.asyncio
async def test_explicit_reply_text_is_available_to_model_and_relation_is_stored(db, tmp_path) -> None:
    contact, conversation = _seed_pdf_history(db)
    quoted = Message(platform="QQ_NAPCAT", external_message_id="quoted-text", conversation_id=conversation.id, contact_id=contact.id, content="需要分析的引用正文", message_type="TEXT")
    db.add(quoted)
    db.commit()
    provider = CapturingProvider()
    pipeline = MessagePipeline(gateway=LLMGateway([provider]), connectors={}, settings=Settings(data_dir=tmp_path, secret_key="s" * 40))
    event = InboundEvent(platform="QQ_NAPCAT", message_id="reply-1", conversation_id=conversation.external_id, sender_id=contact.platform_user_id, sender_name=contact.display_name, content="分析一下", reply_to_message_id="quoted-text", timestamp=datetime.now(timezone.utc), raw={"event_type": "ONEBOT11_MESSAGE", "text_content": "分析一下"})

    result = await pipeline.handle(event, db)

    assert result.shadowed is True
    prompt = provider.messages[-1].content
    assert "需要分析的引用正文" in prompt
    assert prompt.index("需要分析的引用正文") < prompt.index("当前消息：分析一下")
    stored = db.scalar(select(Message).where(Message.external_message_id == "reply-1"))
    assert stored is not None and stored.reply_to_external_id == "quoted-text"


@pytest.mark.asyncio
async def test_explicit_reply_to_generic_mime_video_passes_saved_video_to_model(db, tmp_path) -> None:
    contact, conversation = _seed_pdf_history(db)
    state = db.get(RuntimeState, 1)
    state.kimi_media_upload_enabled = True
    state.kimi_media_upload_videos = True
    state.kimi_media_max_file_mb = 50
    media_dir = tmp_path / "media" / "quoted"
    media_dir.mkdir(parents=True)
    video_path = media_dir / "clip.mp4"
    video_path.write_bytes(b"napcat-video")
    quoted = Message(
        platform="QQ_NAPCAT",
        external_message_id="quoted-video",
        conversation_id=conversation.id,
        contact_id=contact.id,
        content="[视频：clip.mp4]",
        message_type="VIDEO",
    )
    db.add(quoted)
    db.flush()
    db.add(
        MessageAttachment(
            message_id=quoted.id,
            kind="VIDEO",
            segment_type="video",
            segment_index=0,
            file_name="clip.mp4",
            mime_type="application/octet-stream",
            size_bytes=video_path.stat().st_size,
            status="SAVED",
            local_path="media/quoted/clip.mp4",
            sha256="d" * 64,
            analysis_status="SKIPPED_TYPE",
        )
    )
    db.commit()
    provider = CapturingProvider()
    pipeline = MessagePipeline(
        gateway=LLMGateway([provider]),
        connectors={},
        settings=Settings(data_dir=tmp_path, secret_key="v" * 40),
    )
    event = InboundEvent(
        platform="QQ_NAPCAT",
        message_id="reply-video",
        conversation_id=conversation.external_id,
        sender_id=contact.platform_user_id,
        sender_name=contact.display_name,
        content="请总结这个视频",
        reply_to_message_id="quoted-video",
        timestamp=datetime.now(timezone.utc),
        raw={"event_type": "ONEBOT11_MESSAGE", "text_content": "请总结这个视频"},
    )

    result = await pipeline.handle(event, db)

    assert result.shadowed is True
    attachments = provider.messages[-1].attachments
    assert len(attachments) == 1
    assert attachments[0].kind == "VIDEO"
    assert attachments[0].mime_type == "video/mp4"
    request = db.scalar(select(AuditLog).where(AuditLog.event == "LLM_REQUEST"))
    assert request is not None
    assert request.detail["kimi_media_attachment_count"] == 1
