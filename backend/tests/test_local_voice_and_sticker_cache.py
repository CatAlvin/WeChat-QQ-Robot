from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest

from app.core.config import Settings
from app.schemas import MediaPolicyUpdate
from app.services import local_tts
from app.services.local_tts import synthesize_local_speech
from app.services.stickers import import_qq_cache_sticker, qq_cache_candidate_path, resolve_sticker_path, scan_qq_sticker_cache


def test_media_policy_accepts_legacy_median_alias() -> None:
    payload = MediaPolicyUpdate(
        storage_enabled=True,
        save_images=True,
        save_audio=True,
        save_files=True,
        ai_reply_enabled=True,
        max_file_mb=50,
        whisper_model="median",
    )
    assert payload.whisper_model == "medium"


def test_windows_sapi_tts_writes_a_valid_local_wave(tmp_path: Path, monkeypatch) -> None:
    class Result:
        returncode = 0
        stdout = "Microsoft Kangkang Desktop"
        stderr = ""

    def fake_run(args, **_kwargs):
        script = base64.b64decode(args[-1]).decode("utf-16-le")
        match = re.search(r"\$encodedOutput = '([^']+)'", script)
        assert match is not None
        destination = Path(base64.b64decode(match.group(1)).decode("utf-8"))
        destination.write_bytes(b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 64)
        return Result()

    monkeypatch.setattr(local_tts.os, "name", "nt")
    monkeypatch.setattr(local_tts.subprocess, "run", fake_run)
    result = synthesize_local_speech(Settings(data_dir=tmp_path), "好呀，我来啦。", rate=2)
    assert result.absolute_path.is_file()
    assert result.relative_path.startswith("tts/")
    assert result.voice == "Microsoft Kangkang Desktop"
    assert result.engine == "WINDOWS_SAPI_MALE"


def test_qq_cache_scan_and_import_only_use_discovered_emoji_roots(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "Tencent Files" / "10001" / "nt_qq" / "nt_data" / "Emoji" / "personal_emoji"
    root.mkdir(parents=True)
    source = root / "cute.cache"
    source.write_bytes(b"GIF89a" + b"x" * 2048)
    monkeypatch.setattr("app.services.stickers.discover_qq_emoji_roots", lambda: [root.resolve()])

    candidates, roots = scan_qq_sticker_cache(limit=10)
    assert len(candidates) == 1
    assert roots == [str(root.resolve())]
    assert qq_cache_candidate_path(candidates[0].id) == source.resolve()

    local_path, digest, mime_type, source_ref = import_qq_cache_sticker(Settings(data_dir=tmp_path / "data"), candidates[0].id)
    assert mime_type == "image/gif"
    assert len(digest) == 64
    assert local_path.startswith("stickers/")
    assert source_ref == str(source.resolve())


def test_sticker_preview_path_cannot_escape_sticker_directory(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    sticker = settings.data_dir / "stickers" / "preview.gif"
    sticker.parent.mkdir(parents=True)
    sticker.write_bytes(b"GIF89a")
    outside = settings.data_dir / "secret.txt"
    outside.write_text("secret", encoding="utf-8")

    assert resolve_sticker_path(settings, "stickers/preview.gif") == sticker.resolve()
    with pytest.raises(ValueError, match="路径无效"):
        resolve_sticker_path(settings, "secret.txt")
