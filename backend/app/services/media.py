from __future__ import annotations

import base64
import binascii
import asyncio
import hashlib
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.channels.base import InboundAttachment, InboundEvent
from app.channels.experimental import normalize_loopback_base_url
from app.core.config import Settings, get_settings
from app.core.clock import as_beijing
from app.core.security import CredentialVault
from app.models.entities import Account, ApiCredential, Message, MessageAttachment, RuntimeState
from app.services.account_selection import get_active_account


class MediaStoreError(RuntimeError):
    def __init__(self, code: str, *, attempts: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.attempts = attempts


_GENERIC_MIME_TYPES = {
    "application/octet-stream",
    "application/binary",
    "application/x-binary",
    "binary/octet-stream",
    "unknown/unknown",
}

_MEDIA_SUFFIX_MIME_TYPES = {
    ".aac": "audio/aac",
    ".amr": "audio/amr",
    ".avi": "video/x-msvideo",
    ".bmp": "image/bmp",
    ".flac": "audio/flac",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".m4a": "audio/mp4",
    ".m4v": "video/mp4",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".png": "image/png",
    ".wav": "audio/wav",
    ".webm": "video/webm",
    ".webp": "image/webp",
}


@dataclass(frozen=True, slots=True)
class StoredMedia:
    local_path: str
    size_bytes: int
    sha256: str
    mime_type: str | None
    storage_source: str


def _safe_file_name(value: str, kind: str, index: int) -> str:
    leaf = Path(value.replace("\\", "/")).name.strip().strip(".")
    leaf = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", leaf)
    if not leaf:
        suffix = {"IMAGE": ".jpg", "AUDIO": ".mp3", "VIDEO": ".mp4"}.get(kind, "")
        leaf = f"附件-{index + 1}{suffix}"
    return leaf[:180]


def _decode_embedded(source: str, max_bytes: int) -> tuple[bytes, str | None] | None:
    encoded: str
    mime_type: str | None = None
    if source.startswith("base64://"):
        encoded = source[len("base64://") :]
    elif source.startswith("data:") and ";base64," in source:
        header, encoded = source.split(",", 1)
        mime_type = header[5:].split(";", 1)[0] or None
    else:
        return None
    if len(encoded) > ((max_bytes + 2) // 3) * 4 + 16:
        raise MediaStoreError("MEDIA_TOO_LARGE")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MediaStoreError("MEDIA_BASE64_INVALID") from exc
    if len(content) > max_bytes:
        raise MediaStoreError("MEDIA_TOO_LARGE")
    return content, mime_type


def normalize_media_mime_type(
    fallback: str | None,
    *,
    file_name: str | None = None,
    prefix: bytes = b"",
) -> str | None:
    """Return a useful MIME type without trusting generic CDN metadata.

    QQ/NapCat commonly serves images and videos as
    ``application/octet-stream``.  Treating that value as authoritative made
    otherwise valid media disappear before it reached a multimodal provider.
    Strong file signatures win; otherwise a specific declared type is kept
    and generic/empty values fall back to the managed file name.
    """

    declared = (fallback or "").split(";", 1)[0].strip().casefold()
    if prefix.startswith(b"#!AMR\n"):
        return "audio/amr"
    if prefix.startswith(b"ID3") or prefix[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}:
        return "audio/mpeg"
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if prefix.startswith(b"BM"):
        return "image/bmp"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        return "image/webp"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WAVE":
        return "audio/wav"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"AVI ":
        return "video/x-msvideo"
    if prefix.startswith(b"%PDF-"):
        return "application/pdf"

    if declared and declared not in _GENERIC_MIME_TYPES:
        return declared

    suffix = Path(file_name or "").suffix.casefold()
    guessed = _MEDIA_SUFFIX_MIME_TYPES.get(suffix) or mimetypes.guess_type(file_name or "")[0]
    if guessed:
        return guessed.casefold()

    # ISO BMFF covers MP4/MOV/M4A. With no trustworthy file name, video/mp4
    # is the safest useful type for the only cloud-media kinds we accept.
    if len(prefix) >= 12 and prefix[4:8] == b"ftyp":
        return "video/mp4"
    if prefix.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    if prefix.startswith(b"PK\x03\x04"):
        return "application/zip"
    return declared or None


def _sniff_mime_type(prefix: bytes, fallback: str | None, file_name: str | None = None) -> str | None:
    return normalize_media_mime_type(fallback, file_name=file_name, prefix=prefix)


class MediaStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.data_root = self.settings.data_dir.resolve()
        self.media_root = (self.data_root / "media").resolve()

    async def persist(
        self,
        db: Session,
        message: Message,
        event: InboundEvent,
        state: RuntimeState,
        *,
        storage_enabled: bool | None = None,
    ) -> list[MessageAttachment]:
        rows: list[MessageAttachment] = []
        max_bytes = state.media_max_file_mb * 1024 * 1024
        for attachment in event.attachments:
            row = MessageAttachment(
                message_id=message.id,
                kind=attachment.kind,
                segment_type=attachment.segment_type,
                segment_index=attachment.segment_index,
                file_name=_safe_file_name(attachment.file_name, attachment.kind, attachment.segment_index),
                mime_type=attachment.mime_type,
                size_bytes=attachment.size_bytes,
                source_ref=attachment.source_ref,
                file_id=attachment.file_id,
                status="METADATA_ONLY",
                attachment_metadata=attachment.metadata,
            )
            db.add(row)
            rows.append(row)
            effective_storage = state.media_storage_enabled if storage_enabled is None else storage_enabled
            if not effective_storage or not self._kind_enabled(state, attachment.kind):
                row.status = "SKIPPED_DISABLED"
                continue
            if attachment.size_bytes is not None and attachment.size_bytes > max_bytes:
                row.status = "SKIPPED_TOO_LARGE"
                row.error_code = "MEDIA_TOO_LARGE"
                continue
            try:
                stored = await self._store_one(db, message, attachment, row.file_name, max_bytes)
            except MediaStoreError as exc:
                row.status = "SKIPPED_TOO_LARGE" if exc.code == "MEDIA_TOO_LARGE" else "METADATA_ONLY"
                row.error_code = exc.code
                if exc.attempts:
                    row.attachment_metadata = {
                        **(row.attachment_metadata or {}),
                        "neko_storage_attempts": list(exc.attempts),
                    }
                continue
            except OSError:
                # A disappearing NapCat temporary file or local disk error
                # must not make the complete connector event retry/fail.
                row.status = "METADATA_ONLY"
                row.error_code = "MEDIA_LOCAL_IO_ERROR"
                continue
            row.local_path = stored.local_path
            row.size_bytes = stored.size_bytes
            row.sha256 = stored.sha256
            row.mime_type = stored.mime_type or row.mime_type
            row.status = "SAVED"
            row.attachment_metadata = {
                **(row.attachment_metadata or {}),
                "neko_storage_source": stored.storage_source,
            }
        return rows

    @staticmethod
    def _kind_enabled(state: RuntimeState, kind: str) -> bool:
        if kind == "IMAGE":
            return state.media_save_images
        if kind == "AUDIO":
            return state.media_save_audio
        return state.media_save_files

    async def _store_one(
        self,
        db: Session,
        message: Message,
        attachment: InboundAttachment,
        file_name: str,
        max_bytes: int,
    ) -> StoredMedia:
        source = attachment.source_ref or ""
        embedded = _decode_embedded(source, max_bytes)
        if embedded is not None:
            content, mime_type = embedded
            return await self._write_bytes(message, attachment, file_name, content, mime_type)

        audio_resolution_error: MediaStoreError | None = None
        if attachment.kind == "AUDIO":
            try:
                resolved = await self._resolve_from_napcat(db, attachment, message.account_id)
                audio_name = str(Path(file_name).with_suffix(".mp3"))
                return await self._store_resolved(
                    message,
                    attachment,
                    audio_name,
                    resolved,
                    max_bytes,
                    default_mime_type="audio/mpeg",
                )
            except MediaStoreError as exc:
                # A raw local/remote source is still worth preserving when
                # NapCat cannot transcode it. The row records the real MIME
                # type, so the dashboard will not claim conversion succeeded.
                audio_resolution_error = exc

        direct_error: MediaStoreError | None = None
        if source.startswith(("http://", "https://")):
            try:
                return await self._download(message, attachment, file_name, source, max_bytes)
            except MediaStoreError as exc:
                if exc.code == "MEDIA_TOO_LARGE":
                    raise
                # QQ CDN URLs are temporary and can fail intermittently. The
                # account-bound OneBot API remains the authoritative fallback.
                direct_error = exc

        local_source = self._existing_local_path(source)
        if local_source is not None:
            return await self._copy(message, attachment, file_name, local_source, max_bytes)

        if audio_resolution_error is not None:
            if direct_error is not None:
                raise MediaStoreError(
                    "MEDIA_ALL_SOURCES_FAILED",
                    attempts=(audio_resolution_error.code, direct_error.code),
                ) from direct_error
            raise audio_resolution_error
        try:
            resolved = await self._resolve_from_napcat(db, attachment, message.account_id)
            return await self._store_resolved(message, attachment, file_name, resolved, max_bytes)
        except MediaStoreError as fallback_error:
            if fallback_error.code == "MEDIA_TOO_LARGE":
                raise
            if direct_error is not None:
                raise MediaStoreError(
                    "MEDIA_ALL_SOURCES_FAILED",
                    attempts=(direct_error.code, fallback_error.code),
                ) from fallback_error
            raise

    async def _store_resolved(
        self,
        message: Message,
        attachment: InboundAttachment,
        file_name: str,
        resolved: dict[str, Any],
        max_bytes: int,
        *,
        default_mime_type: str | None = None,
    ) -> StoredMedia:
        last_error: MediaStoreError | None = None
        # NapCat commonly returns both an expiring QQ CDN URL and a local file.
        # Prefer local material and continue to the next candidate after a
        # transient source failure instead of failing the whole attachment.
        for key in ("base64", "path", "file", "url"):
            candidate = str(resolved.get(key) or "").strip()
            if not candidate:
                continue
            if key == "base64" and not candidate.startswith(("base64://", "data:")):
                candidate = "base64://" + candidate
            embedded = _decode_embedded(candidate, max_bytes)
            if embedded is not None:
                content, mime_type = embedded
                return await self._write_bytes(
                    message,
                    attachment,
                    file_name,
                    content,
                    mime_type or default_mime_type,
                    storage_source="NAPCAT_API",
                )
            if candidate.startswith(("http://", "https://")):
                try:
                    stored = await self._download(
                        message,
                        attachment,
                        file_name,
                        candidate,
                        max_bytes,
                        storage_source="NAPCAT_API",
                    )
                except MediaStoreError as exc:
                    if exc.code == "MEDIA_TOO_LARGE":
                        raise
                    last_error = exc
                    continue
                return StoredMedia(
                    stored.local_path,
                    stored.size_bytes,
                    stored.sha256,
                    stored.mime_type or default_mime_type,
                    stored.storage_source,
                )
            local_source = self._existing_local_path(candidate)
            if local_source is not None:
                stored = await self._copy(
                    message,
                    attachment,
                    file_name,
                    local_source,
                    max_bytes,
                    storage_source="NAPCAT_API",
                )
                return StoredMedia(
                    stored.local_path,
                    stored.size_bytes,
                    stored.sha256,
                    stored.mime_type or default_mime_type,
                    stored.storage_source,
                )
        if last_error is not None:
            raise last_error
        raise MediaStoreError("MEDIA_SOURCE_UNAVAILABLE")

    @staticmethod
    def _existing_local_path(value: str) -> Path | None:
        if not value or value.startswith("file://"):
            return None
        candidate = Path(unquote(value))
        try:
            return candidate.resolve() if candidate.is_file() else None
        except OSError:
            return None

    async def _resolve_from_napcat(
        self, db: Session, attachment: InboundAttachment, account_id: str | None = None
    ) -> dict[str, Any]:
        account = db.get(Account, account_id) if account_id else get_active_account(db, "QQ_NAPCAT")
        if account is None or not account.credential_id:
            raise MediaStoreError("NAPCAT_MEDIA_NOT_CONFIGURED")
        credential = db.get(ApiCredential, account.credential_id)
        if credential is None:
            raise MediaStoreError("NAPCAT_MEDIA_NOT_CONFIGURED")
        try:
            token = CredentialVault(self.settings).decrypt(credential.encrypted_secret)
            api_base = normalize_loopback_base_url(str((account.config or {}).get("api_base") or ""))
        except Exception as exc:
            raise MediaStoreError("NAPCAT_MEDIA_CREDENTIAL") from exc
        sources = attachment.metadata.get("napcat_sources") if isinstance(attachment.metadata, dict) else None
        sources = sources if isinstance(sources, dict) else {}
        napcat_file = str(sources.get("file") or sources.get("path") or "").strip()
        if attachment.kind == "IMAGE":
            action = "get_image"
            payload: dict[str, Any] = {"file": napcat_file or attachment.source_ref or attachment.file_id}
        elif attachment.kind == "AUDIO":
            action = "get_record"
            payload = {"file": napcat_file or attachment.source_ref or attachment.file_id, "out_format": "mp3"}
        else:
            action = "get_file"
            payload = {"file_id": attachment.file_id} if attachment.file_id else {"file": attachment.source_ref}
        if not next((value for value in payload.values() if value and value != "mp3"), None):
            raise MediaStoreError("MEDIA_SOURCE_UNAVAILABLE")
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{api_base}/{action}",
                    headers={"Authorization": f"Bearer {token}"},
                    json=payload,
                )
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MediaStoreError("NAPCAT_MEDIA_API_ERROR") from exc
        if not response.is_success or not isinstance(body, dict) or body.get("status") != "ok":
            raise MediaStoreError("NAPCAT_MEDIA_API_REJECTED")
        data = body.get("data")
        if not isinstance(data, dict):
            raise MediaStoreError("NAPCAT_MEDIA_API_EMPTY")
        return data

    def _destination(self, message: Message, attachment: InboundAttachment, file_name: str) -> Path:
        at = as_beijing(message.event_at)
        directory = self.media_root / at.strftime("%Y") / at.strftime("%m") / at.strftime("%d") / message.id
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{attachment.segment_index:02d}-{file_name}"

    async def _write_bytes(
        self,
        message: Message,
        attachment: InboundAttachment,
        file_name: str,
        content: bytes,
        mime_type: str | None,
        *,
        storage_source: str = "EMBEDDED",
    ) -> StoredMedia:
        destination = self._destination(message, attachment, file_name)

        def write() -> None:
            temporary = destination.with_suffix(destination.suffix + ".part")
            temporary.write_bytes(content)
            os.replace(temporary, destination)

        await asyncio.to_thread(write)
        return self._stored(
            destination,
            len(content),
            hashlib.sha256(content).hexdigest(),
            _sniff_mime_type(content[:16], mime_type, file_name),
            storage_source,
        )

    async def _copy(
        self,
        message: Message,
        attachment: InboundAttachment,
        file_name: str,
        source: Path,
        max_bytes: int,
        *,
        storage_source: str = "LOCAL_PATH",
    ) -> StoredMedia:
        if source.stat().st_size > max_bytes:
            raise MediaStoreError("MEDIA_TOO_LARGE")
        destination = self._destination(message, attachment, file_name)

        def copy() -> tuple[int, str, bytes]:
            temporary = destination.with_suffix(destination.suffix + ".part")
            digest = hashlib.sha256()
            total = 0
            prefix = b""
            try:
                with source.open("rb") as reader, temporary.open("wb") as writer:
                    while chunk := reader.read(1024 * 1024):
                        if not prefix:
                            prefix = chunk[:16]
                        total += len(chunk)
                        if total > max_bytes:
                            raise MediaStoreError("MEDIA_TOO_LARGE")
                        digest.update(chunk)
                        writer.write(chunk)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            return total, digest.hexdigest(), prefix

        total, digest, prefix = await asyncio.to_thread(copy)
        mime_type = mimetypes.guess_type(source.name)[0]
        return self._stored(
            destination,
            total,
            digest,
            _sniff_mime_type(prefix, mime_type, file_name),
            storage_source,
        )

    async def _download(
        self,
        message: Message,
        attachment: InboundAttachment,
        file_name: str,
        url: str,
        max_bytes: int,
        *,
        storage_source: str = "DIRECT_URL",
    ) -> StoredMedia:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise MediaStoreError("MEDIA_URL_INVALID")
        destination = self._destination(message, attachment, file_name)
        temporary = destination.with_suffix(destination.suffix + ".part")
        digest = hashlib.sha256()
        total = 0
        mime_type: str | None = None
        prefix = b""
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                async with client.stream("GET", url) as response:
                    if not response.is_success:
                        raise MediaStoreError("MEDIA_DOWNLOAD_REJECTED")
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise MediaStoreError("MEDIA_TOO_LARGE")
                    mime_type = response.headers.get("content-type", "").split(";", 1)[0] or None
                    with temporary.open("wb") as writer:
                        async for chunk in response.aiter_bytes(1024 * 256):
                            if not prefix:
                                prefix = chunk[:16]
                            total += len(chunk)
                            if total > max_bytes:
                                raise MediaStoreError("MEDIA_TOO_LARGE")
                            digest.update(chunk)
                            writer.write(chunk)
            os.replace(temporary, destination)
        except MediaStoreError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise MediaStoreError("MEDIA_DOWNLOAD_FAILED") from exc
        finally:
            temporary.unlink(missing_ok=True)
        return self._stored(
            destination,
            total,
            digest.hexdigest(),
            _sniff_mime_type(prefix, mime_type, file_name),
            storage_source,
        )

    def _stored(
        self,
        destination: Path,
        size: int,
        digest: str,
        mime_type: str | None,
        storage_source: str,
    ) -> StoredMedia:
        try:
            relative = destination.resolve().relative_to(self.data_root)
        except ValueError as exc:
            destination.unlink(missing_ok=True)
            raise MediaStoreError("MEDIA_PATH_INVALID") from exc
        return StoredMedia(relative.as_posix(), size, digest, mime_type, storage_source)


def resolve_saved_media_path(item: MessageAttachment, settings: Settings | None = None) -> Path:
    config = settings or get_settings()
    root = config.data_dir.resolve()
    if not item.local_path:
        raise MediaStoreError("MEDIA_NOT_SAVED")
    candidate = (root / item.local_path).resolve()
    try:
        candidate.relative_to((root / "media").resolve())
    except ValueError as exc:
        raise MediaStoreError("MEDIA_PATH_INVALID") from exc
    if not candidate.is_file():
        raise MediaStoreError("MEDIA_FILE_MISSING")
    return candidate
