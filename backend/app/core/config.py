from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
import secrets
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_prefix="NEKO_",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Neko AI Core"
    env: str = "development"
    api_prefix: str = "/api/v1"
    database_url: str = "sqlite:///./data/neko.db"
    secret_key: str = ""
    allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://127.0.0.1:3000", "http://localhost:3000"]
    )
    release_gate: str = "SIMULATION"
    connector_shared_token: str | None = None
    auto_start: str = "10:00"
    auto_end: str = "23:30"
    contact_per_minute: int = 5
    max_consecutive_sends: int = 3
    daily_send_limit: int = 500
    daily_send_hard_limit: int = 1000
    human_takeover_minutes: int = 10
    conversation_warning_rounds: int = 20
    conversation_stop_rounds: int = 40
    contact_cooldown_minutes: int = 30
    llm_timeout_seconds: float = 25.0
    napcat_text_safe_limit: int = Field(default=1200, ge=500, le=4000)
    failure_circuit_breaker_count: int = 3
    failure_circuit_breaker_window_minutes: int = 10
    log_retention_days: int = 7
    data_dir: Path = Path("data")

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def parse_origins(cls, value: object) -> object:
        if isinstance(value, str) and not value.lstrip().startswith("["):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("release_gate")
    @classmethod
    def validate_release_gate(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"SIMULATION", "SHADOW", "LIVE"}:
            raise ValueError("release_gate must be SIMULATION, SHADOW or LIVE")
        return normalized


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if len(settings.secret_key) < 32:
        key_path = settings.data_dir / ".auth_secret"
        local_secret = key_path.read_text(encoding="utf-8").strip() if key_path.exists() else ""
        if len(local_secret) < 32:
            local_secret = secrets.token_urlsafe(48)
            key_path.write_text(local_secret, encoding="utf-8")
            try:
                os.chmod(key_path, 0o600)
            except OSError:
                pass
        settings.secret_key = local_secret
    return settings
