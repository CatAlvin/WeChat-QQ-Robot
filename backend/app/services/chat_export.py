from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.clock import BEIJING_TIME_ZONE_NAME, BEIJING_TZ, as_beijing, as_utc, utc_now
from app.core.enums import MessageAuthor
from app.models.entities import Contact, Message, MessageAttachment
from app.services.media import MediaStoreError, resolve_saved_media_path


class ChatExportError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ChatExportOptions:
    contact_ids: list[str]
    start_at: datetime
    end_at: datetime
    include_contact_messages: bool = True
    include_ai_messages: bool = True
    include_human_messages: bool = True
    format: str = "MARKDOWN"
    include_attachments: bool = True
    output_directory: str | None = None
    max_messages: int = 5000


@dataclass(frozen=True, slots=True)
class ChatExportResult:
    output_directory: str
    record_files: tuple[str, ...]
    archive_file: str | None
    contact_count: int
    message_count: int
    attachment_count: int
    missing_attachment_count: int
    truncated: bool


class ChatExportService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.local_zone = BEIJING_TZ

    def export(self, db: Session, options: ChatExportOptions) -> ChatExportResult:
        if options.end_at < options.start_at:
            raise ChatExportError("结束时间不能早于开始时间")
        if not any((options.include_contact_messages, options.include_ai_messages, options.include_human_messages)):
            raise ChatExportError("至少选择一种消息来源")
        normalized_format = options.format.upper()
        extension = {"TXT": ".txt", "MARKDOWN": ".md", "PDF": ".pdf"}.get(normalized_format)
        if extension is None:
            raise ChatExportError("不支持的导出格式")

        contacts = list(db.scalars(select(Contact).where(Contact.id.in_(options.contact_ids))))
        if len(contacts) != len(set(options.contact_ids)):
            raise ChatExportError("部分联系人不存在，请刷新联系人列表后重试")
        contacts.sort(key=lambda item: (item.display_name.casefold(), item.id))

        authors: list[str] = []
        if options.include_contact_messages:
            authors.append(MessageAuthor.CONTACT)
        if options.include_ai_messages:
            authors.extend((MessageAuthor.AI, MessageAuthor.SYSTEM))
        if options.include_human_messages:
            authors.append(MessageAuthor.HUMAN)

        start_at = self._utc(options.start_at)
        end_at = self._utc(options.end_at)
        rows = list(
            db.scalars(
                select(Message)
                .where(
                    Message.contact_id.in_([item.id for item in contacts]),
                    Message.event_at >= start_at,
                    Message.event_at <= end_at,
                    Message.author.in_(authors),
                )
                .order_by(Message.event_at.asc(), Message.created_at.asc(), Message.id.asc())
                .limit(options.max_messages + 1)
            )
        )
        truncated = len(rows) > options.max_messages
        rows = rows[: options.max_messages]
        message_ids = [item.id for item in rows]
        attachment_rows = (
            list(
                db.scalars(
                    select(MessageAttachment)
                    .where(MessageAttachment.message_id.in_(message_ids))
                    .order_by(MessageAttachment.message_id.asc(), MessageAttachment.segment_index.asc())
                )
            )
            if message_ids
            else []
        )
        attachments: dict[str, list[MessageAttachment]] = {}
        for item in attachment_rows:
            attachments.setdefault(item.message_id, []).append(item)

        root = self._output_root(options.output_directory)
        stamp = as_beijing(utc_now()).strftime("%Y%m%d_%H%M%S")
        export_dir = root / f"Neko聊天导出_{stamp}_{uuid4().hex[:6]}"
        export_dir.mkdir(parents=False, exist_ok=False)

        copied_paths: dict[str, str] = {}
        copied = 0
        missing = 0
        if options.include_attachments:
            for contact in contacts:
                contact_dir = export_dir / "attachments" / self._contact_stem(contact)
                contact_message_ids = {item.id for item in rows if item.contact_id == contact.id}
                for attachment in (item for message_id in contact_message_ids for item in attachments.get(message_id, [])):
                    try:
                        source = resolve_saved_media_path(attachment, self.settings)
                    except MediaStoreError:
                        missing += 1
                        continue
                    contact_dir.mkdir(parents=True, exist_ok=True)
                    destination = contact_dir / f"{attachment.id[:8]}_{self._safe_name(attachment.file_name)}"
                    shutil.copy2(source, destination)
                    copied_paths[attachment.id] = destination.relative_to(export_dir).as_posix()
                    copied += 1

        record_files: list[str] = []
        for contact in contacts:
            contact_rows = [item for item in rows if item.contact_id == contact.id]
            destination = export_dir / f"{self._contact_stem(contact)}{extension}"
            if normalized_format == "PDF":
                self._write_pdf(destination, contact, contact_rows, attachments, copied_paths, options, truncated)
            else:
                content = self._render_text(
                    contact,
                    contact_rows,
                    attachments,
                    copied_paths,
                    options,
                    markdown=normalized_format == "MARKDOWN",
                    truncated=truncated,
                )
                destination.write_text(content, encoding="utf-8-sig" if normalized_format == "TXT" else "utf-8")
            record_files.append(str(destination))

        manifest = {
            "generated_at": utc_now().isoformat(),
            "time_zone": f"{BEIJING_TIME_ZONE_NAME} (UTC+08:00)",
            "start_at": start_at.isoformat(),
            "end_at": end_at.isoformat(),
            "format": normalized_format,
            "contact_count": len(contacts),
            "message_count": len(rows),
            "attachment_count": copied,
            "missing_attachment_count": missing,
            "truncated": truncated,
            "max_messages": options.max_messages,
            "contacts": [
                {"display_name": item.display_name, "platform": item.platform, "platform_user_id": item.platform_user_id}
                for item in contacts
            ],
        }
        (export_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        archive_file: str | None = None
        if options.include_attachments:
            archive_file = shutil.make_archive(str(export_dir), "zip", root_dir=export_dir)

        return ChatExportResult(
            output_directory=str(export_dir),
            record_files=tuple(record_files),
            archive_file=archive_file,
            contact_count=len(contacts),
            message_count=len(rows),
            attachment_count=copied,
            missing_attachment_count=missing,
            truncated=truncated,
        )

    def default_output_directory(self) -> str:
        return str((self.settings.data_dir / "exports").resolve())

    def _output_root(self, value: str | None) -> Path:
        root = Path(value.strip()).expanduser() if value and value.strip() else self.settings.data_dir / "exports"
        try:
            root = root.resolve()
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ChatExportError("导出目录无法创建或写入") from exc
        if not root.is_dir():
            raise ChatExportError("导出目录不是文件夹")
        return root

    def _render_text(
        self,
        contact: Contact,
        rows: list[Message],
        attachments: dict[str, list[MessageAttachment]],
        copied_paths: dict[str, str],
        options: ChatExportOptions,
        *,
        markdown: bool,
        truncated: bool,
    ) -> str:
        title = f"{contact.display_name} 的 Neko 聊天记录"
        lines = [f"# {title}" if markdown else title]
        lines.extend(
            [
                "",
                f"平台：{contact.platform} · {contact.platform_user_id}",
                f"时间范围：{self._local(options.start_at):%Y-%m-%d %H:%M:%S} — {self._local(options.end_at):%Y-%m-%d %H:%M:%S}",
                f"导出条数：{len(rows)}" + ("（已达到上限）" if truncated else ""),
                "",
            ]
        )
        for item in rows:
            sender = self._sender_name(item, contact)
            stamp = self._local(item.event_at).strftime("%Y-%m-%d %H:%M:%S")
            heading = f"## {stamp} · {sender} · {item.message_type} · {item.status}" if markdown else f"[{stamp}] {sender} · {item.message_type} · {item.status}"
            lines.extend([heading, "", item.content or "（无文字内容）"])
            for attachment in attachments.get(item.id, []):
                relative = copied_paths.get(attachment.id)
                suffix = f" → {relative}" if relative else f" · {attachment.status}"
                lines.append(f"- 附件：{attachment.file_name}（{attachment.kind}）{suffix}")
                if attachment.analysis_text:
                    lines.append(f"  本机识别：{attachment.analysis_text}")
            lines.extend(["", "---" if markdown else "-" * 60, ""])
        if not rows:
            lines.append("所选范围内没有符合条件的消息。")
        return "\n".join(lines).rstrip() + "\n"

    def _write_pdf(
        self,
        destination: Path,
        contact: Contact,
        rows: list[Message],
        attachments: dict[str, list[MessageAttachment]],
        copied_paths: dict[str, str],
        options: ChatExportOptions,
        truncated: bool,
    ) -> None:
        try:
            from reportlab.lib import colors
            from reportlab.lib.enums import TA_CENTER
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
            from reportlab.lib.units import mm
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
            from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer
        except ImportError as exc:
            raise ChatExportError("PDF 组件尚未安装，请重新运行安装脚本") from exc

        font_path = self._pdf_font_path()
        font_name = "NekoCJK"
        if font_name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(font_name, str(font_path)))
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle("NekoTitle", parent=styles["Title"], fontName=font_name, fontSize=18, leading=25, alignment=TA_CENTER, textColor=colors.HexColor("#174a32"))
        meta_style = ParagraphStyle("NekoMeta", parent=styles["BodyText"], fontName=font_name, fontSize=9, leading=15, textColor=colors.HexColor("#66736b"))
        body_style = ParagraphStyle("NekoBody", parent=styles["BodyText"], fontName=font_name, fontSize=10, leading=17, wordWrap="CJK")
        head_style = ParagraphStyle("NekoHead", parent=body_style, fontSize=9, textColor=colors.HexColor("#35644c"))
        document = SimpleDocTemplate(str(destination), pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm, title=f"{contact.display_name} 的聊天记录")
        story: list[object] = [
            Paragraph(escape(f"{contact.display_name} 的 Neko 聊天记录"), title_style),
            Spacer(1, 7 * mm),
            Paragraph(escape(f"平台：{contact.platform} · {contact.platform_user_id}"), meta_style),
            Paragraph(escape(f"时间范围：{self._local(options.start_at):%Y-%m-%d %H:%M:%S} — {self._local(options.end_at):%Y-%m-%d %H:%M:%S}"), meta_style),
            Paragraph(escape(f"导出条数：{len(rows)}" + ("（已达到上限）" if truncated else "")), meta_style),
            Spacer(1, 6 * mm),
        ]
        for index, item in enumerate(rows):
            stamp = self._local(item.event_at).strftime("%Y-%m-%d %H:%M:%S")
            story.append(Paragraph(escape(f"{stamp} · {self._sender_name(item, contact)} · {item.message_type} · {item.status}"), head_style))
            story.append(Spacer(1, 1.5 * mm))
            story.append(Paragraph(escape(item.content or "（无文字内容）").replace("\n", "<br/>"), body_style))
            for attachment in attachments.get(item.id, []):
                relative = copied_paths.get(attachment.id)
                suffix = f" → {relative}" if relative else f" · {attachment.status}"
                story.append(Paragraph(escape(f"附件：{attachment.file_name}（{attachment.kind}）{suffix}"), meta_style))
                if attachment.analysis_text:
                    story.append(Paragraph(escape(f"本机识别：{attachment.analysis_text}").replace("\n", "<br/>"), meta_style))
            story.append(Spacer(1, 4 * mm))
            if index and index % 80 == 0:
                story.append(PageBreak())
        if not rows:
            story.append(Paragraph("所选范围内没有符合条件的消息。", body_style))
        document.build(story)

    def _pdf_font_path(self) -> Path:
        candidates = (
            Path("C:/Windows/Fonts/simhei.ttf"),
            Path("C:/Windows/Fonts/msyh.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        )
        for item in candidates:
            if item.is_file():
                return item
        raise ChatExportError("未找到可用于 PDF 的本机字体")

    def _contact_stem(self, contact: Contact) -> str:
        return self._safe_name(f"{contact.display_name}_{contact.platform}_{contact.platform_user_id}")

    @staticmethod
    def _safe_name(value: str) -> str:
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
        return (cleaned or "未命名")[:120]

    def _local(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=self.local_zone)
        return as_beijing(value)

    def _utc(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return as_utc(value.replace(tzinfo=self.local_zone))
        return as_utc(value)

    @staticmethod
    def _sender_name(item: Message, contact: Contact) -> str:
        if item.author == MessageAuthor.CONTACT:
            return f"对方 · {contact.display_name}"
        if item.author == MessageAuthor.HUMAN:
            return "本人"
        if item.author == MessageAuthor.SYSTEM:
            return "Neko 系统"
        return "Neko AI"
