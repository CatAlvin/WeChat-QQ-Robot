from __future__ import annotations

import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.enums import MessageAuthor, MessageDirection, MessageStatus
from app.models.entities import Contact, Conversation, Message, MessageAttachment
from app.services.chat_export import ChatExportError, ChatExportOptions, ChatExportService


def _seed_chat(db, data_dir: Path) -> tuple[Contact, Contact]:
    first = Contact(
        platform="QQ_NAPCAT",
        platform_user_id="10001",
        display_name="小林",
        relationship_label="朋友",
        whitelisted=True,
    )
    second = Contact(
        platform="QQ_NAPCAT",
        platform_user_id="10002",
        display_name="阿月",
        relationship_label="朋友",
        whitelisted=True,
    )
    db.add_all([first, second])
    db.flush()
    first_conversation = Conversation(platform="QQ_NAPCAT", external_id="napcat:private:10001", contact_id=first.id)
    second_conversation = Conversation(platform="QQ_NAPCAT", external_id="napcat:private:10002", contact_id=second.id)
    db.add_all([first_conversation, second_conversation])
    db.flush()
    now = datetime.now(timezone.utc)
    messages = [
        Message(platform="QQ_NAPCAT", external_message_id="export-contact", event_at=now - timedelta(days=2), conversation_id=first_conversation.id, contact_id=first.id, direction=MessageDirection.INBOUND, author=MessageAuthor.CONTACT, content="对方正文", message_type="TEXT", status=MessageStatus.RECEIVED, raw_envelope={"token": "must-not-export"}),
        Message(platform="QQ_NAPCAT", external_message_id="export-ai", event_at=now - timedelta(days=2, minutes=-1), conversation_id=first_conversation.id, contact_id=first.id, direction=MessageDirection.OUTBOUND, author=MessageAuthor.AI, content="AI 正文", message_type="TEXT", status=MessageStatus.SENT),
        Message(platform="QQ_NAPCAT", external_message_id="export-system", event_at=now - timedelta(days=2, minutes=-2), conversation_id=first_conversation.id, contact_id=first.id, direction=MessageDirection.OUTBOUND, author=MessageAuthor.SYSTEM, content="系统正文", message_type="TEXT", status=MessageStatus.SENT),
        Message(platform="QQ_NAPCAT", external_message_id="export-human", event_at=now - timedelta(days=2, minutes=-3), conversation_id=first_conversation.id, contact_id=first.id, direction=MessageDirection.OUTBOUND, author=MessageAuthor.HUMAN, content="本人正文", message_type="TEXT", status=MessageStatus.SENT),
        Message(platform="QQ_NAPCAT", external_message_id="export-second", event_at=now - timedelta(days=1), conversation_id=second_conversation.id, contact_id=second.id, direction=MessageDirection.INBOUND, author=MessageAuthor.CONTACT, content="第二位联系人", message_type="IMAGE", status=MessageStatus.RECEIVED),
        Message(platform="QQ_NAPCAT", external_message_id="export-old", event_at=now - timedelta(days=45), conversation_id=first_conversation.id, contact_id=first.id, direction=MessageDirection.INBOUND, author=MessageAuthor.CONTACT, content="过期正文", message_type="TEXT", status=MessageStatus.RECEIVED),
    ]
    db.add_all(messages)
    db.flush()
    saved = data_dir / "media" / "10002" / "picture.png"
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_bytes(b"safe-image-bytes")
    db.add_all(
        [
            MessageAttachment(message_id=messages[4].id, kind="IMAGE", segment_type="image", segment_index=0, file_name="照片.png", mime_type="image/png", local_path="media/10002/picture.png", status="SAVED", analysis_status="COMPLETED", analysis_text="一只猫"),
            MessageAttachment(message_id=messages[4].id, kind="FILE", segment_type="file", segment_index=1, file_name="缺失.txt", local_path="media/10002/missing.txt", status="SAVED"),
        ]
    )
    db.commit()
    return first, second


def _options(first: Contact, second: Contact, output: Path, *, format: str = "MARKDOWN", **overrides) -> ChatExportOptions:
    now = datetime.now(timezone.utc)
    values = {
        "contact_ids": [first.id, second.id],
        "start_at": now - timedelta(days=30),
        "end_at": now,
        "format": format,
        "output_directory": str(output),
    }
    values.update(overrides)
    return ChatExportOptions(**values)


def test_markdown_export_filters_range_and_packages_saved_media(db, tmp_path) -> None:
    data_dir = tmp_path / "data"
    first, second = _seed_chat(db, data_dir)
    result = ChatExportService(Settings(data_dir=data_dir, secret_key="x" * 40)).export(
        db,
        _options(first, second, tmp_path / "exports"),
    )

    assert result.contact_count == 2
    assert result.message_count == 5
    assert result.attachment_count == 1
    assert result.missing_attachment_count == 1
    assert result.archive_file is not None
    assert Path(result.archive_file).is_file()
    combined = "\n".join(Path(item).read_text(encoding="utf-8") for item in result.record_files)
    assert all(value in combined for value in ("对方正文", "AI 正文", "系统正文", "本人正文", "第二位联系人", "一只猫"))
    assert "过期正文" not in combined
    assert "must-not-export" not in combined
    manifest = json.loads((Path(result.output_directory) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["message_count"] == 5
    assert manifest["time_zone"] == "Asia/Shanghai (UTC+08:00)"
    with zipfile.ZipFile(result.archive_file) as archive:
        names = archive.namelist()
        assert any(name.endswith("照片.png") for name in names)
        assert "manifest.json" in names


def test_export_source_filters_and_message_limit(db, tmp_path) -> None:
    data_dir = tmp_path / "data"
    first, second = _seed_chat(db, data_dir)
    result = ChatExportService(Settings(data_dir=data_dir, secret_key="x" * 40)).export(
        db,
        _options(
            first,
            second,
            tmp_path / "exports",
            format="TXT",
            include_ai_messages=False,
            include_human_messages=False,
            include_attachments=False,
            max_messages=1,
        ),
    )
    assert result.message_count == 1
    assert result.truncated is True
    assert result.archive_file is None
    combined = "\n".join(Path(item).read_text(encoding="utf-8-sig") for item in result.record_files)
    assert "对方正文" in combined
    assert "AI 正文" not in combined
    assert "本人正文" not in combined


def test_pdf_export_creates_a_real_pdf(db, tmp_path) -> None:
    data_dir = tmp_path / "data"
    first, second = _seed_chat(db, data_dir)
    result = ChatExportService(Settings(data_dir=data_dir, secret_key="x" * 40)).export(
        db,
        _options(first, second, tmp_path / "exports", format="PDF", include_attachments=False),
    )
    assert len(result.record_files) == 2
    assert all(Path(item).read_bytes().startswith(b"%PDF") for item in result.record_files)


def test_export_rejects_missing_contact_and_empty_sources(db, tmp_path) -> None:
    service = ChatExportService(Settings(data_dir=tmp_path / "data", secret_key="x" * 40))
    now = datetime.now(timezone.utc)
    with pytest.raises(ChatExportError, match="联系人不存在"):
        service.export(db, ChatExportOptions(["missing"], now - timedelta(days=1), now))
    with pytest.raises(ChatExportError, match="至少选择一种"):
        service.export(db, ChatExportOptions(["missing"], now - timedelta(days=1), now, False, False, False))
