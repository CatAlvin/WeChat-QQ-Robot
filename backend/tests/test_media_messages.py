from __future__ import annotations

import base64
from pathlib import Path

import pytest
from sqlalchemy import select

from app.api.routes import conversation_messages
from app.channels.base import InboundAttachment, InboundEvent
from app.core.config import Settings
from app.core.security import CredentialVault, mask_secret
from app.llm.gateway import LLMGateway
from app.llm.providers import SimulatorProvider
from app.models.entities import Account, ApiCredential, AuditLog, Contact, Message, MessageAttachment, PersonaProfile, RuntimeState
from app.services.media import MediaStore, MediaStoreError
from app.services.observability import message_diagnostics
from app.services.pipeline import MessagePipeline


@pytest.mark.asyncio
async def test_whitelisted_media_is_saved_and_pure_media_does_not_call_ai_by_default(db, tmp_path: Path) -> None:
    state = RuntimeState(id=1, release_gate="SHADOW", media_ai_reply_enabled=False)
    contact = Contact(
        platform="QQ_NAPCAT",
        platform_user_id="1001",
        display_name="媒体联系人",
        whitelisted=True,
        ai_enabled=True,
        memory_enabled=False,
    )
    db.add_all([state, contact, PersonaProfile(id=1)])
    db.commit()
    png = b"\x89PNG\r\n\x1a\nmedia-test"
    source = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    event = InboundEvent(
        platform="QQ_NAPCAT",
        message_id="media-1",
        conversation_id="napcat:private:1001",
        sender_id="1001",
        sender_name="媒体联系人",
        content="[图片]",
        message_type="IMAGE",
        attachments=(
            InboundAttachment(
                kind="IMAGE",
                segment_type="image",
                segment_index=0,
                file_name="photo.png",
                source_ref=source,
                size_bytes=len(png),
                mime_type="image/png",
            ),
        ),
        raw={"event_type": "ONEBOT11_MESSAGE", "text_content": "", "onebot": {"message_id": 1}},
    )
    pipeline = MessagePipeline(
        gateway=LLMGateway([SimulatorProvider()]),
        connectors={},
        settings=Settings(data_dir=tmp_path, secret_key="m" * 40),
    )

    result = await pipeline.handle(event, db)

    assert result.code == "MEDIA_STORED_ONLY"
    message = db.scalar(select(Message).where(Message.external_message_id == "media-1"))
    assert message is not None
    assert message.message_type == "IMAGE"
    assert message.raw_envelope["event_type"] == "ONEBOT11_MESSAGE"
    attachment = db.scalar(select(MessageAttachment).where(MessageAttachment.message_id == message.id))
    assert attachment is not None
    assert attachment.status == "SAVED"
    assert attachment.sha256
    saved_path = tmp_path / str(attachment.local_path)
    assert saved_path.read_bytes() == png
    response = conversation_messages(message.conversation_id, db)
    assert response[0]["attachments"][0]["download_url"] == f"/media/{attachment.id}"


@pytest.mark.asyncio
async def test_media_type_switch_keeps_metadata_without_writing_file(db, tmp_path: Path) -> None:
    state = RuntimeState(id=1, release_gate="SHADOW", media_save_files=False)
    contact = Contact(
        platform="QQ_NAPCAT",
        platform_user_id="1002",
        display_name="文件联系人",
        whitelisted=True,
        ai_enabled=True,
        memory_enabled=False,
    )
    db.add_all([state, contact, PersonaProfile(id=1)])
    db.commit()
    event = InboundEvent(
        platform="QQ_NAPCAT",
        message_id="media-2",
        conversation_id="napcat:private:1002",
        sender_id="1002",
        sender_name="文件联系人",
        content="[文件：notes.txt]",
        message_type="FILE",
        attachments=(
            InboundAttachment(
                kind="FILE",
                segment_type="file",
                segment_index=0,
                file_name="notes.txt",
                source_ref="data:text/plain;base64," + base64.b64encode(b"trusted notes").decode("ascii"),
            ),
        ),
        raw={"event_type": "ONEBOT11_MESSAGE", "text_content": ""},
    )
    pipeline = MessagePipeline(
        gateway=LLMGateway([SimulatorProvider()]),
        connectors={},
        settings=Settings(data_dir=tmp_path, secret_key="n" * 40),
    )

    result = await pipeline.handle(event, db)

    assert result.code == "MEDIA_STORED_ONLY"
    attachment = db.scalar(select(MessageAttachment))
    assert attachment is not None
    assert attachment.status == "SKIPPED_DISABLED"
    assert attachment.local_path is None
    assert not (tmp_path / "media").exists()


@pytest.mark.asyncio
async def test_whitelisted_file_is_saved_with_original_name_and_bytes(db, tmp_path: Path) -> None:
    state = RuntimeState(id=1, release_gate="SHADOW", media_save_files=True)
    contact = Contact(
        platform="QQ_NAPCAT",
        platform_user_id="1004",
        display_name="文件保存联系人",
        whitelisted=True,
        ai_enabled=True,
        memory_enabled=False,
    )
    db.add_all([state, contact, PersonaProfile(id=1)])
    db.commit()
    content = b"controlled-file-payload"
    event = InboundEvent(
        platform="QQ_NAPCAT",
        message_id="media-file-1",
        conversation_id="napcat:private:1004",
        sender_id="1004",
        sender_name="文件保存联系人",
        content="[文件：notes.txt]",
        message_type="FILE",
        attachments=(
            InboundAttachment(
                kind="FILE",
                segment_type="file",
                segment_index=0,
                file_name="notes.txt",
                source_ref="data:text/plain;base64," + base64.b64encode(content).decode("ascii"),
                size_bytes=len(content),
                mime_type="text/plain",
            ),
        ),
        raw={"event_type": "ONEBOT11_MESSAGE", "text_content": ""},
    )
    pipeline = MessagePipeline(
        gateway=LLMGateway([SimulatorProvider()]),
        connectors={},
        settings=Settings(data_dir=tmp_path, secret_key="f" * 40),
    )

    result = await pipeline.handle(event, db)

    assert result.code == "MEDIA_STORED_ONLY"
    attachment = db.scalar(select(MessageAttachment))
    assert attachment is not None and attachment.status == "SAVED"
    assert attachment.file_name == "notes.txt"
    assert attachment.mime_type == "text/plain"
    assert attachment.local_path and attachment.local_path.endswith("notes.txt")
    assert (tmp_path / attachment.local_path).read_bytes() == content


@pytest.mark.asyncio
async def test_voice_uses_local_napcat_api_to_save_playable_mp3(db, tmp_path: Path, monkeypatch) -> None:
    settings = Settings(data_dir=tmp_path, secret_key="v" * 40)
    token = "media-token-1234"
    credential = ApiCredential(
        label="NapCat media token",
        provider="QQ_NAPCAT",
        encrypted_secret=CredentialVault(settings).encrypt(token),
        masked_hint=mask_secret(token),
    )
    account = Account(
        platform="QQ_NAPCAT",
        display_name="NapCat",
        connector_kind="NAPCAT_ONEBOT11",
        enabled=True,
        config={"api_base": "http://127.0.0.1:3001"},
    )
    state = RuntimeState(id=1, release_gate="SHADOW", media_ai_reply_enabled=False)
    contact = Contact(
        platform="QQ_NAPCAT",
        platform_user_id="1003",
        display_name="语音联系人",
        whitelisted=True,
        ai_enabled=True,
        memory_enabled=False,
    )
    db.add_all([credential, account, state, contact, PersonaProfile(id=1)])
    db.flush()
    account.credential_id = credential.id
    db.commit()
    captured: dict[str, object] = {}
    mp3 = b"ID3-playable-audio"

    class FakeResponse:
        is_success = True

        @staticmethod
        def json():
            return {"status": "ok", "retcode": 0, "data": {"base64": base64.b64encode(mp3).decode("ascii")}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return FakeResponse()

    monkeypatch.setattr("app.services.media.httpx.AsyncClient", FakeClient)
    event = InboundEvent(
        platform="QQ_NAPCAT",
        message_id="media-voice-1",
        conversation_id="napcat:private:1003",
        sender_id="1003",
        sender_name="语音联系人",
        content="[语音]",
        message_type="AUDIO",
        attachments=(
            InboundAttachment(
                kind="AUDIO",
                segment_type="record",
                segment_index=0,
                file_name="voice.silk",
                source_ref="https://example.invalid/temporary-voice",
                metadata={
                    "napcat_sources": {
                        "file": "voice-resource-id",
                        "path": "D:/NapCat/voice.silk",
                        "url": "https://example.invalid/temporary-voice",
                    }
                },
            ),
        ),
        raw={"event_type": "ONEBOT11_MESSAGE", "text_content": ""},
    )
    pipeline = MessagePipeline(gateway=LLMGateway([SimulatorProvider()]), connectors={}, settings=settings)

    result = await pipeline.handle(event, db)

    assert result.code == "MEDIA_STORED_ONLY"
    attachment = db.scalar(select(MessageAttachment))
    assert attachment is not None and attachment.status == "SAVED"
    assert attachment.local_path and attachment.local_path.endswith("voice.mp3")
    assert attachment.mime_type == "audio/mpeg"
    assert (tmp_path / attachment.local_path).read_bytes() == mp3
    assert str(captured["url"]).endswith("/get_record")
    assert captured["json"] == {"file": "voice-resource-id", "out_format": "mp3"}
    assert captured["headers"] == {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    "kind,segment_type,file_name,mime_type",
    [
        ("IMAGE", "image", "photo.png", "image/png"),
        ("FILE", "file", "notes.txt", "text/plain"),
        ("VIDEO", "video", "clip.mp4", "video/mp4"),
    ],
)
@pytest.mark.asyncio
async def test_expired_remote_media_falls_back_to_the_exact_account_napcat_source(
    db,
    tmp_path: Path,
    monkeypatch,
    kind: str,
    segment_type: str,
    file_name: str,
    mime_type: str,
) -> None:
    account = Account(
        platform="QQ_NAPCAT",
        display_name="媒体账号 A",
        connector_kind="NAPCAT_ONEBOT11",
        enabled=True,
    )
    state = RuntimeState(
        id=1,
        release_gate="SHADOW",
        media_ai_reply_enabled=False,
        media_save_images=True,
        media_save_files=True,
    )
    contact = Contact(
        platform="QQ_NAPCAT",
        platform_user_id="fallback-contact",
        display_name="回退联系人",
        whitelisted=True,
        ai_enabled=True,
        memory_enabled=False,
    )
    db.add_all([account, state, contact, PersonaProfile(id=1)])
    db.flush()
    contact.account_id = account.id
    db.commit()
    cached = tmp_path / "napcat-cache" / file_name
    cached.parent.mkdir()
    cached.write_bytes(b"napcat-local-media")
    captured: dict[str, object] = {}

    async def fail_remote(self, message, attachment, saved_name, url, max_bytes, **kwargs):
        captured["remote_attempted"] = True
        raise MediaStoreError("MEDIA_DOWNLOAD_FAILED")

    async def resolve_local(self, request_db, attachment, account_id=None):
        captured["account_id"] = account_id
        # The API can return both values. Local material must be preferred over
        # retrying the same temporary CDN URL.
        return {"url": "https://temporary.invalid/expired", "file": str(cached)}

    monkeypatch.setattr(MediaStore, "_download", fail_remote)
    monkeypatch.setattr(MediaStore, "_resolve_from_napcat", resolve_local)
    event = InboundEvent(
        platform="QQ_NAPCAT",
        account_id=account.id,
        message_id=f"fallback-{kind.lower()}",
        conversation_id="napcat:private:fallback-contact",
        sender_id="fallback-contact",
        sender_name="回退联系人",
        content=f"[{kind}]",
        message_type=kind,
        attachments=(
            InboundAttachment(
                kind=kind,
                segment_type=segment_type,
                segment_index=0,
                file_name=file_name,
                source_ref="https://temporary.invalid/expired",
                mime_type=mime_type,
                metadata={"napcat_sources": {"file": file_name, "url": "https://temporary.invalid/expired"}},
            ),
        ),
        raw={"event_type": "ONEBOT11_MESSAGE", "text_content": ""},
    )
    pipeline = MessagePipeline(
        gateway=LLMGateway([SimulatorProvider()]),
        connectors={},
        settings=Settings(data_dir=tmp_path, secret_key="r" * 40),
    )

    result = await pipeline.handle(event, db)

    attachment = db.scalar(select(MessageAttachment))
    assert result.code == "MEDIA_STORED_ONLY"
    assert captured == {"remote_attempted": True, "account_id": account.id}
    assert attachment is not None and attachment.status == "SAVED"
    assert attachment.error_code is None
    assert attachment.attachment_metadata["neko_storage_source"] == "NAPCAT_API"
    assert attachment.local_path
    assert (tmp_path / attachment.local_path).read_bytes() == b"napcat-local-media"


@pytest.mark.asyncio
async def test_all_media_source_failures_are_bounded_and_keep_attempt_evidence(db, tmp_path: Path, monkeypatch) -> None:
    account = Account(platform="QQ_NAPCAT", display_name="账号", connector_kind="NAPCAT_ONEBOT11", enabled=True)
    contact = Contact(
        platform="QQ_NAPCAT",
        platform_user_id="all-failed",
        display_name="失败联系人",
        whitelisted=True,
        ai_enabled=True,
        memory_enabled=False,
    )
    db.add_all(
        [
            account,
            RuntimeState(
                id=1,
                release_gate="SHADOW",
                media_ai_reply_enabled=True,
                media_understanding_enabled=True,
                media_understand_images=True,
            ),
            contact,
            PersonaProfile(id=1),
        ]
    )
    db.flush()
    contact.account_id = account.id
    db.commit()

    async def fail_remote(self, message, attachment, saved_name, url, max_bytes, **kwargs):
        raise MediaStoreError("MEDIA_DOWNLOAD_FAILED")

    async def fail_napcat(self, request_db, attachment, account_id=None):
        raise MediaStoreError("NAPCAT_MEDIA_API_REJECTED")

    monkeypatch.setattr(MediaStore, "_download", fail_remote)
    monkeypatch.setattr(MediaStore, "_resolve_from_napcat", fail_napcat)
    event = InboundEvent(
        platform="QQ_NAPCAT",
        account_id=account.id,
        message_id="fallback-all-failed",
        conversation_id="napcat:private:all-failed",
        sender_id="all-failed",
        sender_name="失败联系人",
        content="[图片]",
        message_type="IMAGE",
        attachments=(InboundAttachment(kind="IMAGE", segment_type="image", segment_index=0, file_name="photo.png", source_ref="https://temporary.invalid/expired"),),
        raw={"event_type": "ONEBOT11_MESSAGE", "text_content": ""},
    )
    pipeline = MessagePipeline(
        gateway=LLMGateway([SimulatorProvider()]),
        connectors={},
        settings=Settings(data_dir=tmp_path, secret_key="x" * 40),
    )

    result = await pipeline.handle(event, db)

    attachment = db.scalar(select(MessageAttachment))
    assert result.code == "MEDIA_UNDERSTANDING_UNAVAILABLE"
    assert attachment is not None and attachment.status == "METADATA_ONLY"
    assert attachment.error_code == "MEDIA_ALL_SOURCES_FAILED"
    assert attachment.attachment_metadata["neko_storage_attempts"] == [
        "MEDIA_DOWNLOAD_FAILED",
        "NAPCAT_MEDIA_API_REJECTED",
    ]
    audit = db.scalar(select(AuditLog).where(AuditLog.event == "MEDIA_UNDERSTANDING_COMPLETED"))
    assert audit is not None
    assert audit.detail["completed"] == 0
    assert audit.detail["failed"] == 0
    assert audit.detail["unavailable"] == 1
    message = db.scalar(select(Message).where(Message.external_message_id == "fallback-all-failed"))
    assert message is not None
    diagnostic = message_diagnostics(db, message.id, Settings(data_dir=tmp_path, secret_key="x" * 40))
    assert diagnostic["outcome"]["code"] == "MEDIA_UNDERSTANDING_UNAVAILABLE"
    assert "可靠内容" in diagnostic["outcome"]["reason"]
