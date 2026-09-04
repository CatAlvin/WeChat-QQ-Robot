from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import threading
import wave
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings


class LocalTTSError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class LocalTTSResult:
    absolute_path: Path
    relative_path: str
    sha256: str
    size_bytes: int
    voice: str
    engine: str


_SAPI_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$text = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($encodedText))
$output = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($encodedOutput))
$requested = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($encodedVoice))
$synth = New-Object -ComObject SAPI.SpVoice
$stream = New-Object -ComObject SAPI.SpFileStream
try {
  $voices = @($synth.GetVoices())
  $selected = $null
  # Neko's character voice is explicitly male. Never fall back to an
  # arbitrary Chinese SAPI voice because Windows commonly picks Huihui, which
  # is female. Kangkang is the only legacy Chinese male SAPI voice we accept.
  $selected = $voices | Where-Object { $_.GetDescription() -like '*Kangkang*' } | Select-Object -First 1
  if (-not $selected) { throw 'NO_VERIFIED_CHINESE_MALE_SAPI_VOICE' }
  $synth.Voice = $selected
  $synth.Rate = [Math]::Max(-3, [Math]::Min(4, [int]$requestedRate))
  $synth.Volume = [Math]::Max(1, [Math]::Min(100, [int]$requestedVolume))
  $stream.Open($output, 3, $false)
  $synth.AudioOutputStream = $stream
  [void]$synth.Speak($text, 0)
  $stream.Close()
  [Console]::Out.Write($selected.GetDescription())
} finally {
  try { $stream.Close() } catch {}
}
""".strip()


_SHERPA_LOCK = threading.Lock()
_SHERPA_ENGINES: dict[str, object] = {}


@dataclass(frozen=True, slots=True)
class _MaleVoiceProfile:
    key: str
    pitch_ratio: float
    speed_bias: float


_DEFAULT_MALE_VOICE = "male-youth-cute"
_MALE_VOICE_PROFILES = {
    "male-youth-cute": _MaleVoiceProfile("male-youth-cute", pitch_ratio=1.18, speed_bias=0.05),
    "male-youth-bright": _MaleVoiceProfile("male-youth-bright", pitch_ratio=1.15, speed_bias=0.11),
    "male-youth-soft": _MaleVoiceProfile("male-youth-soft", pitch_ratio=1.13, speed_bias=-0.02),
}


def _male_voice_profile(value: str) -> _MaleVoiceProfile:
    """Resolve only reviewed male presets; legacy/unknown values stay male."""
    return _MALE_VOICE_PROFILES.get(value.strip().casefold(), _MALE_VOICE_PROFILES[_DEFAULT_MALE_VOICE])


def _kokoro_root(settings: Settings) -> Path:
    return (settings.data_dir.resolve() / "models" / "tts" / "kokoro-multi-lang-v1_0").resolve()


def _kokoro_ready(settings: Settings) -> bool:
    root = _kokoro_root(settings)
    required = (
        "model.onnx",
        "voices.bin",
        "tokens.txt",
        "lexicon-us-en.txt",
        "lexicon-zh.txt",
        "date-zh.fst",
        "number-zh.fst",
        "phone-zh.fst",
    )
    return (root / "espeak-ng-data").is_dir() and all((root / name).is_file() for name in required)


def _kokoro_engine(settings: Settings):
    try:
        import sherpa_onnx
    except ImportError as exc:
        raise LocalTTSError("TTS_SHERPA_NOT_INSTALLED") from exc
    root = _kokoro_root(settings)
    if not _kokoro_ready(settings):
        raise LocalTTSError("TTS_KOKORO_MODEL_MISSING")
    key = str(root)
    with _SHERPA_LOCK:
        engine = _SHERPA_ENGINES.get(key)
        if engine is not None:
            return engine
        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                    model=str(root / "model.onnx"),
                    voices=str(root / "voices.bin"),
                    tokens=str(root / "tokens.txt"),
                    data_dir=str(root / "espeak-ng-data"),
                    lexicon=",".join((str(root / "lexicon-us-en.txt"), str(root / "lexicon-zh.txt"))),
                ),
                provider="cpu",
                debug=False,
                num_threads=max(1, min(4, os.cpu_count() or 2)),
            ),
            rule_fsts=",".join(str(root / name) for name in ("date-zh.fst", "number-zh.fst", "phone-zh.fst")),
            max_num_sentences=1,
        )
        if not config.validate():
            raise LocalTTSError("TTS_KOKORO_CONFIG_INVALID")
        try:
            engine = sherpa_onnx.OfflineTts(config)
        except Exception as exc:
            raise LocalTTSError("TTS_KOKORO_LOAD_FAILED") from exc
        _SHERPA_ENGINES[key] = engine
        return engine


def _write_youthful_wave(
    samples: object,
    sample_rate: int,
    destination: Path,
    profile: _MaleVoiceProfile,
    volume: int,
) -> None:
    import numpy as np

    values = np.asarray(samples, dtype=np.float32)
    if values.size == 0:
        raise ValueError("empty audio")
    # Raising both pitch and formants is intentional here: unlike a generic
    # pitch shifter this reduces mature chest resonance and moves the perceived
    # age toward a boyish timbre. The limit stays below 20% so Mandarin remains
    # clear.
    if profile.pitch_ratio > 1.0 and values.size > 1:
        target_size = max(1, int(values.size / profile.pitch_ratio))
        source_positions = np.arange(values.size, dtype=np.float32)
        target_positions = np.linspace(0, values.size - 1, target_size, dtype=np.float32)
        values = np.interp(target_positions, source_positions, values).astype(np.float32)
    amplitude = max(1, min(100, int(volume))) / 100.0
    pcm = (np.clip(values * amplitude, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    with wave.open(str(destination), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(int(sample_rate))
        output.writeframes(pcm)


def _synthesize_kokoro(settings: Settings, text: str, destination: Path, voice: str, rate: int, volume: int) -> str:
    try:
        import sherpa_onnx
    except ImportError as exc:
        raise LocalTTSError("TTS_SHERPA_NOT_INSTALLED") from exc
    profile = _male_voice_profile(voice)
    generation = sherpa_onnx.GenerationConfig()
    generation.sid = 50  # zm_yunxi: expressive Mandarin male voice
    generation.speed = max(0.82, min(1.35, 1.03 + rate * 0.07 + profile.speed_bias))
    generation.silence_scale = 0.14
    try:
        audio = _kokoro_engine(settings).generate(text, generation)
        _write_youthful_wave(audio.samples, int(audio.sample_rate), destination, profile, volume)
    except Exception as exc:
        destination.unlink(missing_ok=True)
        if isinstance(exc, LocalTTSError):
            raise
        raise LocalTTSError("TTS_KOKORO_SYNTHESIS_FAILED") from exc
    return f"kokoro-multi-lang-v1_0/zm_yunxi/{profile.key}"


def _sherpa_root(settings: Settings) -> Path:
    return (settings.data_dir.resolve() / "models" / "tts" / "vits-zh-hf-fanchen-wnj").resolve()


def _sherpa_model_path(settings: Settings) -> Path:
    return _sherpa_root(settings) / "vits-zh-hf-fanchen-wnj.onnx"


def _sherpa_ready(settings: Settings) -> bool:
    root = _sherpa_root(settings)
    required = ("lexicon.txt", "tokens.txt", "phone.fst", "date.fst", "number.fst")
    return _sherpa_model_path(settings).is_file() and all((root / name).is_file() for name in required)


def _sherpa_engine(settings: Settings):
    try:
        import sherpa_onnx
    except ImportError as exc:
        raise LocalTTSError("TTS_SHERPA_NOT_INSTALLED") from exc
    root = _sherpa_root(settings)
    if not _sherpa_ready(settings):
        raise LocalTTSError("TTS_SHERPA_MODEL_MISSING")
    key = str(root)
    with _SHERPA_LOCK:
        engine = _SHERPA_ENGINES.get(key)
        if engine is not None:
            return engine
        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=str(_sherpa_model_path(settings)),
                    lexicon=str(root / "lexicon.txt"),
                    tokens=str(root / "tokens.txt"),
                ),
                provider="cpu",
                debug=False,
                num_threads=max(1, min(4, os.cpu_count() or 2)),
            ),
            rule_fsts=",".join(str(root / name) for name in ("phone.fst", "date.fst", "number.fst")),
            max_num_sentences=1,
        )
        if not config.validate():
            raise LocalTTSError("TTS_SHERPA_CONFIG_INVALID")
        try:
            engine = sherpa_onnx.OfflineTts(config)
        except Exception as exc:
            raise LocalTTSError("TTS_SHERPA_LOAD_FAILED") from exc
        _SHERPA_ENGINES[key] = engine
        return engine


def _synthesize_sherpa(settings: Settings, text: str, destination: Path, voice: str, rate: int, volume: int) -> str:
    try:
        import sherpa_onnx
    except ImportError as exc:
        raise LocalTTSError("TTS_SHERPA_NOT_INSTALLED") from exc
    engine = _sherpa_engine(settings)
    profile = _male_voice_profile(voice)
    generation = sherpa_onnx.GenerationConfig()
    generation.sid = 0
    generation.speed = max(0.75, min(1.35, 1.0 + rate * 0.08 + profile.speed_bias))
    generation.silence_scale = 0.2
    try:
        audio = engine.generate(text, generation)
        _write_youthful_wave(audio.samples, int(audio.sample_rate), destination, profile, volume)
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise LocalTTSError("TTS_SHERPA_SYNTHESIS_FAILED") from exc
    return f"vits-zh-hf-fanchen-wnj/{profile.key}"


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def synthesize_local_speech(
    settings: Settings,
    text: str,
    *,
    voice: str = "",
    rate: int = 1,
    volume: int = 92,
    max_chars: int = 300,
) -> LocalTTSResult:
    """Create a local WAV with offline Sherpa VITS, then SAPI fallback."""
    normalized = " ".join(text.replace("\x00", " ").split()).strip()[:max_chars]
    if not normalized:
        raise LocalTTSError("TTS_EMPTY_TEXT")
    if os.name != "nt":
        raise LocalTTSError("TTS_WINDOWS_ONLY")
    profile = _male_voice_profile(voice)
    if _kokoro_ready(settings):
        engine_key = "KOKORO_MALE_YUNXI_V1"
    elif _sherpa_ready(settings):
        engine_key = "SHERPA_MALE_WNJ"
    else:
        engine_key = "SAPI_MALE_KANGKANG"
    digest = hashlib.sha256(f"{engine_key}|{profile.key}|{rate}|{volume}|{normalized}".encode("utf-8")).hexdigest()
    directory = (settings.data_dir / "tts").resolve()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"chenglan-{digest[:20]}.wav"
    selected_voice = profile.key
    if _kokoro_ready(settings):
        selected_engine = "SHERPA_ONNX_KOKORO_MALE_YUNXI"
    elif _sherpa_ready(settings):
        selected_engine = "SHERPA_ONNX_VITS_MALE"
    else:
        selected_engine = "WINDOWS_SAPI_MALE"
    if not destination.exists():
        if _kokoro_ready(settings):
            selected_voice = _synthesize_kokoro(settings, normalized, destination, profile.key, rate, volume)
        elif _sherpa_ready(settings):
            selected_voice = _synthesize_sherpa(settings, normalized, destination, profile.key, rate, volume)
        else:
            selected_voice = _synthesize_sapi(normalized, destination, profile.key, rate, volume)
    try:
        payload = destination.read_bytes()
    except OSError as exc:
        raise LocalTTSError("TTS_OUTPUT_MISSING") from exc
    if len(payload) < 44 or payload[:4] != b"RIFF" or payload[8:12] != b"WAVE":
        destination.unlink(missing_ok=True)
        raise LocalTTSError("TTS_INVALID_WAVE")
    return LocalTTSResult(
        absolute_path=destination,
        relative_path=destination.relative_to(settings.data_dir.resolve()).as_posix(),
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        voice=selected_voice,
        engine=selected_engine,
    )


def _synthesize_sapi(text: str, destination: Path, voice: str, rate: int, volume: int) -> str:
    """Fallback for machines where a working Windows SAPI voice is present."""
    normalized = text
    selected_voice = voice
    invocation_script = (
        f"$encodedText = '{_b64(normalized)}'\n"
        f"$encodedOutput = '{_b64(str(destination))}'\n"
        f"$encodedVoice = '{_b64(voice)}'\n"
        f"$requestedRate = {int(rate)}\n"
        f"$requestedVolume = {int(volume)}\n"
        f"{_SAPI_SCRIPT}"
    )
    encoded_script = base64.b64encode(invocation_script.encode("utf-16-le")).decode("ascii")
    try:
        result = subprocess.run(
            [
                str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded_script,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LocalTTSError("TTS_ENGINE_UNAVAILABLE") from exc
    if result.returncode != 0:
        destination.unlink(missing_ok=True)
        raise LocalTTSError("TTS_SYNTHESIS_FAILED")
    selected_voice = result.stdout.strip() or voice
    return selected_voice


def local_tts_status(settings: Settings | None = None) -> dict[str, object]:
    settings = settings or Settings()
    kokoro_ready = _kokoro_ready(settings)
    sherpa_ready = _sherpa_ready(settings)
    try:
        import sherpa_onnx  # noqa: F401
        sherpa_installed = True
    except ImportError:
        sherpa_installed = False
    if kokoro_ready and sherpa_installed:
        return {
            "supported": True,
            "engine": "SHERPA_ONNX_KOKORO_MALE_YUNXI",
            "detail": "本地云希少年男声已就绪；默认活泼可爱，内容不离开本机",
        }
    if sherpa_ready and sherpa_installed:
        return {
            "supported": True,
            "engine": "SHERPA_ONNX_VITS_MALE",
            "detail": "少年男声主模型未就绪；当前使用较成熟的本地男声兜底",
        }
    return {
        "supported": False,
        "engine": "SHERPA_ONNX_VITS_MALE",
        "detail": "经过确认的中文男声模型尚未安装；为避免误用女声，当前自动降级为纯文字",
        "package_installed": sherpa_installed,
        "model_ready": kokoro_ready or sherpa_ready,
        "kokoro_ready": kokoro_ready,
    }
