from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.core.config import Settings, get_settings
from app.llm.base import LLMAttachment
from app.models.entities import MessageAttachment, RuntimeState
from app.services.media import MediaStoreError, normalize_media_mime_type, resolve_saved_media_path


MAX_KIMI_ATTACHMENTS = 8
MAX_KIMI_FILE_BYTES = 100 * 1024 * 1024
MAX_KIMI_TOTAL_BYTES = 100 * 1024 * 1024
MEDIA_COMPRESSION_TIMEOUT_SECONDS = 180
KIMI_IMAGE_MIME_TYPES = {"image/bmp", "image/gif", "image/jpeg", "image/png", "image/webp"}
KIMI_VIDEO_MIME_TYPES = {
    "video/mp4",
    "video/mpeg",
    "video/quicktime",
    "video/webm",
    "video/x-matroska",
    "video/x-msvideo",
}


@dataclass(frozen=True, slots=True)
class KimiMediaCandidate:
    kind: str
    file_name: str
    mime_type: str
    local_path: Path
    size_bytes: int
    sha256: str | None
    target_bytes: int
    needs_compression: bool


@dataclass(frozen=True, slots=True)
class KimiMediaPlan:
    candidates: tuple[KimiMediaCandidate, ...] = ()

    @property
    def compression_required(self) -> bool:
        return any(item.needs_compression for item in self.candidates)


@dataclass(frozen=True, slots=True)
class KimiMediaPreparation:
    attachments: tuple[LLMAttachment, ...] = ()
    compression_required: bool = False
    compressed_count: int = 0
    failed_count: int = 0
    failure_codes: tuple[str, ...] = ()


def plan_kimi_attachments(
    rows: list[MessageAttachment],
    state: RuntimeState,
    settings: Settings | None = None,
) -> KimiMediaPlan:
    """Select managed media and mark oversized items for local compression.

    Planning is side-effect free. It never downloads, compresses, or uploads a
    file, so policy checks can run before expensive processing or notifications.
    """

    if not state.kimi_media_upload_enabled:
        return KimiMediaPlan()
    config = settings or get_settings()
    per_file_limit = min(
        max(1, int(state.kimi_media_max_file_mb or 50)) * 1024 * 1024,
        MAX_KIMI_FILE_BYTES,
    )
    output: list[KimiMediaCandidate] = []
    seen: set[str] = set()
    reserved_bytes = 0
    for row in rows:
        if len(output) >= MAX_KIMI_ATTACHMENTS or row.status != "SAVED":
            continue
        try:
            path = resolve_saved_media_path(row, config)
            size = path.stat().st_size
            with path.open("rb") as handle:
                prefix = handle.read(16)
        except (MediaStoreError, OSError):
            continue
        if size <= 0:
            continue
        mime_type = normalize_media_mime_type(row.mime_type, file_name=path.name, prefix=prefix)
        kind = _kimi_kind(mime_type)
        if kind == "IMAGE" and state.kimi_media_upload_images is False:
            continue
        if kind == "VIDEO" and state.kimi_media_upload_videos is False:
            continue
        if kind is None:
            continue
        identity = row.sha256 or str(path).casefold()
        if identity in seen:
            continue
        remaining_total = MAX_KIMI_TOTAL_BYTES - reserved_bytes
        if remaining_total <= 0:
            break
        target_bytes = min(per_file_limit, remaining_total)
        output.append(
            KimiMediaCandidate(
                kind=kind,
                file_name=_safe_file_name(row.file_name, path),
                mime_type=mime_type,
                local_path=path,
                size_bytes=size,
                sha256=row.sha256,
                target_bytes=target_bytes,
                needs_compression=size > target_bytes,
            )
        )
        seen.add(identity)
        reserved_bytes += min(size, target_bytes)
    return KimiMediaPlan(tuple(output))


def build_kimi_attachments(
    rows: list[MessageAttachment],
    state: RuntimeState,
    settings: Settings | None = None,
) -> tuple[LLMAttachment, ...]:
    """Return already-small media references without doing compression."""

    plan = plan_kimi_attachments(rows, state, settings)
    return tuple(_to_attachment(item) for item in plan.candidates if not item.needs_compression)


async def prepare_kimi_attachments(
    plan: KimiMediaPlan,
    settings: Settings | None = None,
) -> KimiMediaPreparation:
    """Create bounded Kimi attachments, compressing oversized media locally.

    Originals stay immutable. Derived files are cached below
    ``data/media/_derived/kimi`` and reused after a retry or restart.
    """

    config = settings or get_settings()
    attachments: list[LLMAttachment] = []
    failures: list[str] = []
    compressed_count = 0
    total_bytes = 0
    for item in plan.candidates:
        prepared = item
        if item.needs_compression:
            try:
                prepared = await _compress_candidate(item, config)
            except MediaStoreError as exc:
                failures.append(str(exc))
                continue
            compressed_count += 1
        if prepared.size_bytes <= 0 or prepared.size_bytes > prepared.target_bytes:
            failures.append("MEDIA_COMPRESSION_LIMIT_NOT_MET")
            continue
        if total_bytes + prepared.size_bytes > MAX_KIMI_TOTAL_BYTES:
            failures.append("KIMI_TOTAL_SIZE_LIMIT")
            continue
        attachments.append(_to_attachment(prepared))
        total_bytes += prepared.size_bytes
    return KimiMediaPreparation(
        attachments=tuple(attachments),
        compression_required=plan.compression_required,
        compressed_count=compressed_count,
        failed_count=len(failures),
        failure_codes=tuple(failures),
    )


async def _compress_candidate(item: KimiMediaCandidate, settings: Settings) -> KimiMediaCandidate:
    ffmpeg = _find_ffmpeg()
    identity = item.sha256 or _path_identity(item.local_path)
    suffix = ".jpg" if item.kind == "IMAGE" else ".mp4"
    mime_type = "image/jpeg" if item.kind == "IMAGE" else "video/mp4"
    derived = (
        settings.data_dir
        / "media"
        / "_derived"
        / "kimi"
        / identity[:2]
        / f"{identity}-{item.target_bytes}{suffix}"
    ).resolve()
    media_root = (settings.data_dir / "media").resolve()
    try:
        derived.relative_to(media_root)
    except ValueError as exc:
        raise MediaStoreError("MEDIA_COMPRESSION_PATH_INVALID") from exc
    derived.parent.mkdir(parents=True, exist_ok=True)
    if derived.is_file() and 0 < derived.stat().st_size <= item.target_bytes:
        return _derived_candidate(item, derived, mime_type)

    commands = (
        _image_commands(ffmpeg, item.local_path, derived)
        if item.kind == "IMAGE"
        else _video_commands(ffmpeg, item.local_path, derived)
    )
    last_code = "MEDIA_COMPRESSION_FAILED"
    for command in commands:
        temporary = derived.with_name(f".{derived.stem}.{uuid4().hex}.tmp{derived.suffix}")
        try:
            run_command = [*command[:-1], str(temporary)]
            return_code, _ = await _run_process(run_command)
            if return_code != 0 or not temporary.is_file() or temporary.stat().st_size <= 0:
                last_code = "MEDIA_COMPRESSION_TOOL_FAILED"
                continue
            if temporary.stat().st_size > item.target_bytes:
                last_code = "MEDIA_COMPRESSION_LIMIT_NOT_MET"
                continue
            os.replace(temporary, derived)
            return _derived_candidate(item, derived, mime_type)
        except TimeoutError:
            last_code = "MEDIA_COMPRESSION_TIMEOUT"
        except OSError:
            last_code = "MEDIA_COMPRESSION_IO_ERROR"
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    raise MediaStoreError(last_code)


def _image_commands(ffmpeg: str, source: Path, destination: Path) -> tuple[list[str], ...]:
    common = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-map_metadata",
        "-1",
    ]
    return tuple(
        [
            *common,
            "-vf",
            f"scale='min({dimension},iw)':-2:force_original_aspect_ratio=decrease,format=yuvj420p",
            "-c:v",
            "mjpeg",
            "-q:v",
            str(quality),
            str(destination),
        ]
        for dimension, quality in ((2048, 5), (1600, 8), (1280, 11), (960, 14), (640, 18))
    )


def _video_commands(ffmpeg: str, source: Path, destination: Path) -> tuple[list[str], ...]:
    common = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-map_metadata",
        "-1",
    ]
    return tuple(
        [
            *common,
            "-vf",
            f"scale=-2:'min({height},ih)':force_original_aspect_ratio=decrease",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(crf),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            audio_rate,
            "-movflags",
            "+faststart",
            str(destination),
        ]
        for height, crf, audio_rate in ((1080, 28, "96k"), (720, 31, "80k"), (540, 34, "64k"), (360, 37, "48k"))
    )


async def _run_process(command: list[str]) -> tuple[int, str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        creationflags=flags,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=MEDIA_COMPRESSION_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise TimeoutError from exc
    return int(process.returncode or 0), stderr.decode("utf-8", errors="replace")[-2000:]


def _find_ffmpeg() -> str:
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    raise MediaStoreError("FFMPEG_UNAVAILABLE")


def _derived_candidate(item: KimiMediaCandidate, path: Path, mime_type: str) -> KimiMediaCandidate:
    return KimiMediaCandidate(
        kind=item.kind,
        file_name=f"{Path(item.file_name).stem}-compressed{path.suffix}",
        mime_type=mime_type,
        local_path=path,
        size_bytes=path.stat().st_size,
        sha256=_sha256_file(path),
        target_bytes=item.target_bytes,
        needs_compression=False,
    )


def _path_identity(path: Path) -> str:
    stat = path.stat()
    value = f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8", errors="replace")
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _to_attachment(item: KimiMediaCandidate) -> LLMAttachment:
    return LLMAttachment(
        kind=item.kind,
        file_name=item.file_name,
        mime_type=item.mime_type,
        local_path=str(item.local_path),
        size_bytes=item.size_bytes,
        sha256=item.sha256,
    )


def _kimi_kind(mime_type: str | None) -> str | None:
    normalized = (mime_type or "").casefold()
    if normalized in KIMI_IMAGE_MIME_TYPES:
        return "IMAGE"
    if normalized in KIMI_VIDEO_MIME_TYPES:
        return "VIDEO"
    return None


def _safe_file_name(value: str, path: Path) -> str:
    name = Path(value or path.name).name.strip()
    return name[:255] or path.name[:255]
