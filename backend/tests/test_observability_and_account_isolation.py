from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.channels.base import InboundEvent
from app.core.config import Settings
from app.llm.base import LLMProvider, LLMResponse
from app.llm.gateway import LLMGateway
from app.models.entities import Account, Contact, Conversation, Message, MessageAttachment, PersonaProfile, RuntimeState
from app.services.observability import contact_detail, media_library, message_diagnostics, reference_preview
from app.services.pipeline import MessagePipeline


class LocalProvider(LLMProvider):
    name = "local-test"

    async def chat(self, messages):
        return LLMResponse("收到，我会简短回复。", "local-model", self.name, 3)

    async def test_connection(self) -> bool:
        return True

    async def models(self) -> list[str]:
        return ["local-model"]


@pytest.mark.asyncio
async def test_same_platform_identifiers_are_isolated_by_managed_account(db, tmp_path) -> None:
    first = Account(platform="QQ_NAPCAT", display_name="账号 A", connector_kind="NAPCAT_ONEBOT11", enabled=True)
    second = Account(platform="QQ_NAPCAT", display_name="账号 B", connector_kind="NAPCAT_ONEBOT11", enabled=False)
    db.add_all([first, second, RuntimeState(id=1, release_gate="SHADOW"), PersonaProfile(id=1)])
    db.flush()
    db.add_all(
        [
            Contact(platform="QQ_NAPCAT", account_id=first.id, platform_user_id="1001", display_name="A 的联系人", whitelisted=True, ai_enabled=True),
            Contact(platform="QQ_NAPCAT", account_id=second.id, platform_user_id="1001", display_name="B 的联系人", whitelisted=True, ai_enabled=True),
        ]
    )
    db.commit()
    pipeline = MessagePipeline(
        gateway=LLMGateway([LocalProvider()]),
        connectors={},
        settings=Settings(data_dir=tmp_path, secret_key="i" * 40),
    )
    for account in (first, second):
        result = await pipeline.handle(
            InboundEvent(
                platform="QQ_NAPCAT",
                account_id=account.id,
                message_id="same-message-id",
                conversation_id="napcat:private:1001",
                sender_id="1001",
                sender_name="联系人",
                content="你好",
                timestamp=datetime.now(timezone.utc),
                raw={"event_type": "ONEBOT11_MESSAGE", "text_content": "你好"},
            ),
            db,
        )
        assert result.shadowed is True

    messages = list(db.scalars(select(Message).where(Message.external_message_id == "same-message-id")))
    conversations = list(db.scalars(select(Conversation).where(Conversation.external_id == "napcat:private:1001")))
    assert {item.account_id for item in messages} == {first.id, second.id}
    assert {item.account_id for item in conversations} == {first.id, second.id}


@pytest.mark.asyncio
async def test_diagnostics_reference_contact_detail_and_media_library(db, tmp_path) -> None:
    account = Account(platform="QQ_NAPCAT", display_name="测试账号", connector_kind="NAPCAT_ONEBOT11", enabled=True)
    contact = Contact(
        platform="QQ_NAPCAT",
        platform_user_id="2002",
        display_name="测试联系人",
        whitelisted=True,
        ai_enabled=False,
        reply_time_window_enabled=False,
        media_storage_enabled=True,
    )
    db.add_all([account, RuntimeState(id=1, release_gate="LIVE", live_time_window_enabled=True), PersonaProfile(id=1)])
    db.flush()
    contact.account_id = account.id
    db.add(contact)
    db.flush()
    conversation = Conversation(platform="QQ_NAPCAT", account_id=account.id, external_id="napcat:private:2002", contact_id=contact.id)
    db.add(conversation)
    db.flush()
    quoted = Message(
        platform="QQ_NAPCAT",
        account_id=account.id,
        external_message_id="quoted",
        conversation_id=conversation.id,
        contact_id=contact.id,
        content="PDF 中的原始段落",
    )
    inbound = Message(
        platform="QQ_NAPCAT",
        account_id=account.id,
        external_message_id="incoming",
        conversation_id=conversation.id,
        contact_id=contact.id,
        content="请总结",
        reply_to_external_id="quoted",
    )
    db.add_all([quoted, inbound])
    db.flush()
    attachment = MessageAttachment(
        message_id=quoted.id,
        kind="FILE",
        segment_type="file",
        segment_index=0,
        file_name="paper.pdf",
        mime_type="application/pdf",
        status="SAVED",
        local_path="media/paper.pdf",
        sha256="a" * 64,
        analysis_status="COMPLETED",
        analysis_provider="LOCAL_DOCUMENT",
        analysis_model="pypdf",
        analysis_text="本地提取的 PDF 文本",
    )
    db.add(attachment)
    db.commit()

    diagnostic = message_diagnostics(db, inbound.id, Settings(data_dir=tmp_path, secret_key="d" * 40))
    assert diagnostic["steps"][0]["status"] == "PASS"
    assert next(item for item in diagnostic["steps"] if item["key"] == "ai")["status"] == "BLOCK"
    assert diagnostic["outcome"]["replied"] is False

    preview = reference_preview(db, inbound.id)
    assert preview["has_reference"] is True
    assert preview["message"]["content"] == "PDF 中的原始段落"
    assert preview["attachments"][0]["analysis_text"] == "本地提取的 PDF 文本"

    detail = contact_detail(db, contact.id, Settings(data_dir=tmp_path, secret_key="c" * 40))
    assert detail["account"]["id"] == account.id
    assert detail["effective_policy"]["reply_time_window_enabled"] is False

    library = media_library(db, account_id=account.id, contact_id=contact.id, kind="FILE")
    assert len(library) == 1
    assert library[0]["sha256"] == "a" * 64
    assert library[0]["contact"]["display_name"] == "测试联系人"
