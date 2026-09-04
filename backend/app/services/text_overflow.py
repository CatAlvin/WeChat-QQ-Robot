from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from html import escape
from pathlib import Path

from sqlalchemy.orm import Session

from app.channels.base import OutboundAttachment
from app.core.clock import as_beijing, utc_now
from app.core.config import Settings
from app.models.entities import Message, MessageAttachment


@dataclass(frozen=True, slots=True)
class TextDelivery:
    content: str
    attachments: tuple[OutboundAttachment, ...] = ()
    overflowed: bool = False
    full_characters: int = 0
    format: str | None = None
    document_mode: bool = False


def _safe_title(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return (cleaned or "Neko 长文本分析")[:60]


def _pdf_font_path() -> Path:
    candidates = (
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )
    for item in candidates:
        if item.is_file():
            return item
    raise RuntimeError("PDF_FONT_NOT_FOUND")


def _write_pdf(destination: Path, *, title: str, content: str) -> None:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    font_name = "NekoLongFormCJK"
    if font_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(font_name, str(_pdf_font_path())))
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "NekoLongFormTitle",
        parent=styles["Title"],
        fontName=font_name,
        fontSize=18,
        leading=26,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#174a32"),
        spaceAfter=8 * mm,
    )
    body_style = ParagraphStyle(
        "NekoLongFormBody",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=10.5,
        leading=18,
        wordWrap="CJK",
        textColor=colors.HexColor("#28362e"),
        spaceAfter=3 * mm,
    )
    heading_style = ParagraphStyle(
        "NekoLongFormHeading",
        parent=body_style,
        fontSize=13,
        leading=20,
        textColor=colors.HexColor("#1d6041"),
        spaceBefore=3 * mm,
        spaceAfter=2 * mm,
    )
    document = SimpleDocTemplate(
        str(destination),
        pagesize=A4,
        rightMargin=19 * mm,
        leftMargin=19 * mm,
        topMargin=17 * mm,
        bottomMargin=17 * mm,
        title=title,
        author="Neko AI",
    )

    def draw_footer(canvas, current_document) -> None:
        canvas.saveState()
        canvas.setFont(font_name, 8)
        canvas.setFillColor(colors.HexColor("#718078"))
        canvas.drawRightString(A4[0] - 19 * mm, 10 * mm, f"第 {current_document.page} 页")
        canvas.restoreState()

    story: list[object] = [Paragraph(escape(title), title_style)]
    blocks = re.split(r"\n\s*\n", content.strip())
    for block in blocks:
        value = block.strip()
        if not value:
            continue
        heading = re.fullmatch(r"#{1,4}\s+(.+)", value)
        if heading:
            story.append(Paragraph(escape(heading.group(1)), heading_style))
            continue
        rendered = escape(value).replace("\n", "<br/>")
        rendered = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", rendered)
        story.append(Paragraph(rendered, body_style))
    story.append(Spacer(1, 2 * mm))
    document.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)


def prepare_text_delivery(
    db: Session,
    *,
    settings: Settings,
    message: Message,
    content: str,
    segment_index: int = 0,
    document_mode: bool = False,
    document_format: str = "PDF",
    document_title: str = "Neko 长文本分析",
) -> TextDelivery:
    """Turn an explicit long-form task (or oversized reply) into one readable local document."""
    if message.platform.upper() != "QQ_NAPCAT" or (
        not document_mode and len(content) <= settings.napcat_text_safe_limit
    ):
        message.content = content
        return TextDelivery(content=content, full_characters=len(content))

    data_root = settings.data_dir.resolve()
    destination_dir = data_root / "media" / "outbound" / message.id
    destination_dir.mkdir(parents=True, exist_ok=True)
    local_time = as_beijing(utc_now())
    requested_format = document_format.upper() if document_mode else "TXT"
    actual_format = requested_format if requested_format in {"PDF", "TXT"} else "PDF"
    title = _safe_title(document_title)
    stem = _safe_title(f"{title}-{local_time.strftime('%Y%m%d-%H%M%S')}")
    destination = destination_dir / f"{stem}.{actual_format.casefold()}"
    if actual_format == "PDF":
        try:
            _write_pdf(destination, title=title, content=content)
        except Exception:
            actual_format = "TXT"
            destination = destination_dir / f"{stem}.txt"
            destination.write_text(content, encoding="utf-8-sig")
    else:
        destination.write_text(content, encoding="utf-8-sig")
    file_name = destination.name
    size_bytes = destination.stat().st_size
    sha256 = hashlib.sha256(destination.read_bytes()).hexdigest()
    relative_path = destination.relative_to(data_root).as_posix()

    excerpt = " ".join(content.split())[:220].rstrip()
    if document_mode:
        delivery_content = f"分析完成啦，我已经把完整内容整理成 {actual_format} 文件发给你了～正文只放在文件里，聊天窗口就不重复啦。"
    else:
        delivery_content = (
            "这次回复比较长，我把完整内容整理成 TXT 文件发给你啦～\n"
            f"先说重点：{excerpt}{'…' if len(excerpt) < len(' '.join(content.split())) else ''}"
        )
    mime_type = "application/pdf" if actual_format == "PDF" else "text/plain; charset=utf-8"
    source = "AI_DOCUMENT_RESPONSE" if document_mode else "AI_TEXT_OVERFLOW"
    row = MessageAttachment(
        message_id=message.id,
        kind="FILE",
        segment_type="file",
        segment_index=segment_index,
        file_name=file_name,
        mime_type=mime_type,
        size_bytes=size_bytes,
        local_path=relative_path,
        sha256=sha256,
        status="SAVED",
        analysis_status="COMPLETED",
        analysis_provider="NEKO_GENERATED_DOCUMENT",
        analysis_model=message.model,
        analysis_text=content,
        analyzed_at=utc_now(),
        attachment_metadata={
            "source": source,
            "full_characters": len(content),
            "transport_limit": settings.napcat_text_safe_limit,
            "format": actual_format,
            "document_mode": document_mode,
            "title": title,
        },
    )
    db.add(row)
    envelope = dict(message.raw_envelope or {})
    envelope["text_overflow"] = {
        "sha256": sha256,
        "full_characters": len(content),
        "transport_limit": settings.napcat_text_safe_limit,
        "format": actual_format,
    }
    if document_mode:
        envelope["document_delivery"] = {
            "sha256": sha256,
            "full_characters": len(content),
            "format": actual_format,
            "title": title,
        }
    message.raw_envelope = envelope
    message.content = delivery_content
    message.message_type = "MIXED"
    return TextDelivery(
        content=delivery_content,
        attachments=(OutboundAttachment("FILE", str(destination), file_name, mime_type),),
        overflowed=True,
        full_characters=len(content),
        format=actual_format,
        document_mode=document_mode,
    )
