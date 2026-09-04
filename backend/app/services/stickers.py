from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.entities import StickerAsset


EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
_CACHE_CANDIDATES: dict[str, Path] = {}
_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class StickerCacheCandidate:
    id: str
    file_name: str
    mime_type: str
    size_bytes: int
    modified_at: datetime
    source_category: str


def _image_mime(path: Path) -> str | None:
    try:
        with path.open("rb") as source:
            head = source.read(16)
    except OSError:
        return None
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _documents_directory() -> Path:
    if os.name == "nt":
        try:
            import ctypes

            buffer = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer) == 0 and buffer.value:
                return Path(buffer.value)
        except (AttributeError, OSError):
            pass
    return Path.home() / "Documents"


def discover_qq_emoji_roots() -> list[Path]:
    """Return only QQ folders explicitly dedicated to emoji assets."""
    roots: list[Path] = []
    documents_candidates = {_documents_directory(), Path.home() / "Documents"}
    if os.name == "nt":
        username = Path.home().name
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            documents_candidates.add(Path(f"{letter}:/{username}/Documents"))
            documents_candidates.add(Path(f"{letter}:/Users/{username}/Documents"))
    for documents in documents_candidates:
        tencent = documents / "Tencent Files"
        if not tencent.is_dir():
            continue
        for account in tencent.iterdir():
            if not account.is_dir() or not account.name.isdecimal():
                continue
            emoji = account / "nt_qq" / "nt_data" / "Emoji"
            for name in ("personal_emoji", "marketface", "emoji-recv"):
                candidate = emoji / name
                if candidate.is_dir():
                    roots.append(candidate.resolve())
            legacy = account / "RecommendFace"
            if legacy.is_dir():
                roots.append(legacy.resolve())
    return list(dict.fromkeys(roots))


def scan_qq_sticker_cache(*, limit: int = 120) -> tuple[list[StickerCacheCandidate], list[str]]:
    roots = discover_qq_emoji_roots()
    paths: list[Path] = []
    for root in roots:
        try:
            for path in root.rglob("*"):
                if path.is_file() and 1024 <= path.stat().st_size <= 6 * 1024 * 1024:
                    paths.append(path)
        except OSError:
            continue
    paths.sort(key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True)
    candidates: list[StickerCacheCandidate] = []
    seen: set[str] = set()
    candidate_paths: dict[str, Path] = {}
    for path in paths:
        mime = _image_mime(path)
        if mime is None:
            continue
        try:
            stat = path.stat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        if digest in seen:
            continue
        seen.add(digest)
        identifier = digest[:24]
        candidate_paths[identifier] = path.resolve()
        candidates.append(
            StickerCacheCandidate(
                id=identifier,
                file_name=f"{path.stem[:80]}{EXTENSIONS[mime]}",
                mime_type=mime,
                size_bytes=stat.st_size,
                modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                source_category=next((root.name for root in roots if path.is_relative_to(root)), "QQ_EMOJI"),
            )
        )
        if len(candidates) >= limit:
            break
    with _CACHE_LOCK:
        _CACHE_CANDIDATES.clear()
        _CACHE_CANDIDATES.update(candidate_paths)
    return candidates, [str(root) for root in roots]


def qq_cache_candidate_path(candidate_id: str) -> Path:
    with _CACHE_LOCK:
        path = _CACHE_CANDIDATES.get(candidate_id)
    if path is None or not path.is_file() or _image_mime(path) is None:
        raise ValueError("QQ 缓存候选已失效，请重新扫描")
    roots = discover_qq_emoji_roots()
    if not any(path.is_relative_to(root) for root in roots):
        raise ValueError("候选文件不在 QQ 表情缓存中")
    return path


def import_qq_cache_sticker(settings: Settings, candidate_id: str) -> tuple[str, str, str, str]:
    source = qq_cache_candidate_path(candidate_id)
    mime = _image_mime(source)
    if mime is None:
        raise ValueError("QQ 缓存文件不是受支持的贴图")
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    directory = (settings.data_dir / "stickers").resolve()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"qq-cache-{digest[:16]}{EXTENSIONS[mime]}"
    if not destination.exists():
        shutil.copyfile(source, destination)
    relative = destination.relative_to(settings.data_dir.resolve()).as_posix()
    return relative, digest, mime, str(source)


def save_sticker(settings: Settings, *, file_name: str, mime_type: str, data_base64: str) -> tuple[str, str]:
    try:
        payload = base64.b64decode(data_base64, validate=True)
    except Exception as exc:
        raise ValueError("贴图内容不是有效 Base64") from exc
    if not payload or len(payload) > 6 * 1024 * 1024:
        raise ValueError("贴图必须小于 6 MB")
    extension = EXTENSIONS.get(mime_type)
    if not extension:
        raise ValueError("只支持 PNG、JPEG、GIF 或 WebP")
    digest = hashlib.sha256(payload).hexdigest()
    directory = settings.data_dir / "stickers"
    directory.mkdir(parents=True, exist_ok=True)
    safe_stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", Path(file_name).stem).strip("-")[:40] or "sticker"
    destination = directory / f"{safe_stem}-{digest[:12]}{extension}"
    if not destination.exists():
        destination.write_bytes(payload)
    return destination.resolve().relative_to(settings.data_dir.resolve()).as_posix(), digest


def resolve_sticker_path(settings: Settings, local_path: str) -> Path:
    root = (settings.data_dir / "stickers").resolve()
    candidate = (settings.data_dir / local_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("贴图路径无效") from exc
    if not candidate.is_file():
        raise ValueError("贴图文件不存在")
    return candidate


def select_auto_sticker(db: Session, response_text: str) -> StickerAsset | None:
    """Return one explicitly enabled sticker by exact tag match.

    There is no random sending: an administrator must enable both the asset
    and its auto-reply switch, and at least one configured tag must occur in
    the generated response.
    """

    rows = list(db.scalars(select(StickerAsset).where(StickerAsset.enabled.is_(True), StickerAsset.auto_reply_enabled.is_(True))))
    lowered = response_text.casefold()
    candidates = [row for row in rows if any(str(tag).casefold() in lowered for tag in (row.tags or []))]
    return min(candidates, key=lambda row: (row.use_count, row.created_at)) if candidates else None
