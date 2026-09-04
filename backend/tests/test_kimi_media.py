from __future__ import annotations

import hashlib
import shutil
import struct
import subprocess

import pytest

from app.core.config import Settings
from app.models.entities import MessageAttachment, RuntimeState
from app.services.cloud_media import build_kimi_attachments, plan_kimi_attachments, prepare_kimi_attachments


def _row(*, kind: str, name: str, mime: str, local_path: str) -> MessageAttachment:
    return MessageAttachment(
        message_id="message-id",
        kind=kind,
        segment_type=kind.casefold(),
        segment_index=0,
        file_name=name,
        mime_type=mime,
        local_path=local_path,
        status="SAVED",
        sha256=hashlib.sha256(name.encode()).hexdigest(),
    )


def test_kimi_media_requires_explicit_switch_and_accepts_saved_image_and_video(tmp_path):
    media = tmp_path / "media" / "contact"
    media.mkdir(parents=True)
    (media / "photo.png").write_bytes(b"image")
    (media / "clip.mp4").write_bytes(b"video")
    rows = [
        _row(kind="IMAGE", name="photo.png", mime="image/png", local_path="media/contact/photo.png"),
        _row(kind="VIDEO", name="clip.mp4", mime="video/mp4", local_path="media/contact/clip.mp4"),
    ]
    state = RuntimeState(id=1, kimi_media_upload_enabled=False)
    settings = Settings(data_dir=tmp_path, secret_key="x" * 48)

    assert build_kimi_attachments(rows, state, settings) == ()
    state.kimi_media_upload_enabled = True
    result = build_kimi_attachments(rows, state, settings)

    assert [item.kind for item in result] == ["IMAGE", "VIDEO"]
    assert all(item.local_path.startswith(str(media)) for item in result)


def test_kimi_media_respects_type_switch_size_limit_and_managed_path(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    (media / "small.png").write_bytes(b"ok")
    (media / "large.mp4").write_bytes(b"x" * (1024 * 1024 + 1))
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    state = RuntimeState(
        id=1,
        kimi_media_upload_enabled=True,
        kimi_media_upload_images=True,
        kimi_media_upload_videos=True,
        kimi_media_max_file_mb=1,
    )
    rows = [
        _row(kind="IMAGE", name="small.png", mime="image/png", local_path="media/small.png"),
        _row(kind="VIDEO", name="large.mp4", mime="video/mp4", local_path="media/large.mp4"),
        _row(kind="IMAGE", name="outside.png", mime="image/png", local_path="../outside.png"),
    ]

    result = build_kimi_attachments(rows, state, Settings(data_dir=tmp_path, secret_key="y" * 48))

    assert len(result) == 1
    assert result[0].file_name == "small.png"


def test_kimi_media_recovers_image_and_video_from_generic_napcat_mime(tmp_path):
    media = tmp_path / "media" / "napcat"
    media.mkdir(parents=True)
    (media / "photo.webp").write_bytes(b"generic-image-payload")
    (media / "clip.mp4").write_bytes(b"generic-video-payload")
    rows = [
        _row(
            kind="IMAGE",
            name="photo.webp",
            mime="application/octet-stream",
            local_path="media/napcat/photo.webp",
        ),
        _row(
            kind="VIDEO",
            name="clip.mp4",
            mime="application/octet-stream",
            local_path="media/napcat/clip.mp4",
        ),
    ]
    state = RuntimeState(
        id=1,
        kimi_media_upload_enabled=True,
        kimi_media_upload_images=True,
        kimi_media_upload_videos=True,
        kimi_media_max_file_mb=10,
    )

    result = build_kimi_attachments(rows, state, Settings(data_dir=tmp_path, secret_key="z" * 48))

    assert [(item.kind, item.mime_type) for item in result] == [
        ("IMAGE", "image/webp"),
        ("VIDEO", "video/mp4"),
    ]


def test_kimi_media_rejects_unknown_binary_and_deduplicates_same_saved_asset(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    (media / "asset.bin").write_bytes(b"unknown")
    (media / "vector.svg").write_text("<svg></svg>", encoding="utf-8")
    (media / "photo.png").write_bytes(b"image")
    unknown = _row(
        kind="FILE",
        name="asset.bin",
        mime="application/octet-stream",
        local_path="media/asset.bin",
    )
    unsupported_image = _row(
        kind="IMAGE",
        name="vector.svg",
        mime="application/octet-stream",
        local_path="media/vector.svg",
    )
    first = _row(
        kind="IMAGE",
        name="photo.png",
        mime="application/octet-stream",
        local_path="media/photo.png",
    )
    duplicate = _row(
        kind="IMAGE",
        name="photo-copy.png",
        mime="application/octet-stream",
        local_path="media/photo.png",
    )
    duplicate.sha256 = first.sha256
    state = RuntimeState(id=1, kimi_media_upload_enabled=True, kimi_media_max_file_mb=10)

    result = build_kimi_attachments(
        [unknown, unsupported_image, first, duplicate],
        state,
        Settings(data_dir=tmp_path, secret_key="w" * 48),
    )

    assert len(result) == 1
    assert result[0].kind == "IMAGE"
    assert result[0].mime_type == "image/png"


def _write_large_bmp(path, width: int = 1024, height: int = 1024) -> None:
    row_size = (width * 3 + 3) & ~3
    pixel_bytes = row_size * height
    header = b"BM" + struct.pack("<IHHI", 54 + pixel_bytes, 0, 0, 54)
    dib = struct.pack("<IIIHHIIIIII", 40, width, height, 1, 24, 0, pixel_bytes, 2835, 2835, 0, 0)
    row = bytes((40, 120, 220)) * width + b"\0" * (row_size - width * 3)
    path.write_bytes(header + dib + row * height)


@pytest.mark.asyncio
async def test_oversized_image_is_compressed_to_cached_managed_copy_without_overwriting_original(tmp_path):
    media = tmp_path / "media" / "contact"
    media.mkdir(parents=True)
    original = media / "large.bmp"
    _write_large_bmp(original)
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    row = _row(kind="IMAGE", name="large.bmp", mime="image/bmp", local_path="media/contact/large.bmp")
    row.sha256 = original_hash
    state = RuntimeState(
        id=1,
        kimi_media_upload_enabled=True,
        kimi_media_upload_images=True,
        kimi_media_max_file_mb=1,
    )
    settings = Settings(data_dir=tmp_path, secret_key="c" * 48)

    plan = plan_kimi_attachments([row], state, settings)
    prepared = await prepare_kimi_attachments(plan, settings)

    assert plan.compression_required is True
    assert prepared.compressed_count == 1
    assert prepared.failed_count == 0
    assert len(prepared.attachments) == 1
    attachment = prepared.attachments[0]
    assert attachment.kind == "IMAGE"
    assert attachment.mime_type == "image/jpeg"
    assert attachment.size_bytes <= 1024 * 1024
    assert "_derived" in attachment.local_path
    assert hashlib.sha256(original.read_bytes()).hexdigest() == original_hash
    cached = await prepare_kimi_attachments(plan, settings)
    assert cached.attachments[0].local_path == attachment.local_path


@pytest.mark.asyncio
async def test_oversized_video_is_transcoded_to_mp4_under_limit(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is required for the media compression feature")
    media = tmp_path / "media" / "contact"
    media.mkdir(parents=True)
    original = media / "large.mkv"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30",
            "-t",
            "3",
            "-c:v",
            "ffv1",
            str(original),
        ],
        check=True,
    )
    assert original.stat().st_size > 1024 * 1024
    row = _row(kind="VIDEO", name="large.mkv", mime="video/x-matroska", local_path="media/contact/large.mkv")
    row.sha256 = hashlib.sha256(original.read_bytes()).hexdigest()
    state = RuntimeState(
        id=1,
        kimi_media_upload_enabled=True,
        kimi_media_upload_videos=True,
        kimi_media_max_file_mb=1,
    )
    settings = Settings(data_dir=tmp_path, secret_key="d" * 48)

    prepared = await prepare_kimi_attachments(plan_kimi_attachments([row], state, settings), settings)

    assert prepared.compressed_count == 1
    assert prepared.failed_count == 0
    assert len(prepared.attachments) == 1
    assert prepared.attachments[0].mime_type == "video/mp4"
    assert prepared.attachments[0].size_bytes <= 1024 * 1024
