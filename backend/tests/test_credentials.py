from __future__ import annotations

import base64
import sys

import pytest

from app.core.config import Settings
from app.core.config import get_settings
from app.core.security import (
    CredentialVault,
    _dpapi_encrypt,
    create_access_token,
    decode_access_token,
    hash_password,
    mask_secret,
    verify_password,
)


def test_credential_is_encrypted_and_masked(tmp_path):
    settings = Settings(data_dir=tmp_path, secret_key="a" * 40)
    vault = CredentialVault(settings)
    raw = "sk-super-secret-value-123456"
    encrypted = vault.encrypt(raw)
    assert raw not in encrypted
    assert encrypted.startswith("fernet:")
    assert vault.decrypt(encrypted) == raw
    assert raw not in mask_secret(raw)
    assert mask_secret(raw).endswith("3456")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI compatibility")
def test_credential_vault_can_read_legacy_user_scoped_dpapi(tmp_path):
    raw = "legacy-user-scoped-secret"
    legacy = "dpapi:" + base64.urlsafe_b64encode(_dpapi_encrypt(raw.encode("utf-8"))).decode("ascii")
    assert CredentialVault(Settings(data_dir=tmp_path)).decrypt(legacy) == raw


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI compatibility")
def test_credential_vault_rewraps_legacy_dpapi_for_stable_restarts(tmp_path):
    raw = "legacy-machine-secret"
    legacy = "dpapi-machine:" + base64.urlsafe_b64encode(
        _dpapi_encrypt(raw.encode("utf-8"), machine_scope=True)
    ).decode("ascii")
    vault = CredentialVault(Settings(data_dir=tmp_path))

    replacement, changed = vault.rewrap_if_legacy(legacy)

    assert changed
    assert replacement.startswith("fernet:")
    assert vault.decrypt(replacement) == raw


def test_password_and_session_token():
    password_hash, salt = hash_password("a-long-safe-password")
    assert verify_password("a-long-safe-password", password_hash, salt)
    assert not verify_password("wrong-password", password_hash, salt)
    settings = Settings(secret_key="b" * 40)
    token = create_access_token("user-1", settings)
    assert decode_access_token(token, settings)["sub"] == "user-1"


def test_missing_auth_secret_is_random_and_persistent(tmp_path, monkeypatch):
    # Keep the test independent from a real project's local .env file.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NEKO_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("NEKO_SECRET_KEY", raising=False)
    try:
        get_settings.cache_clear()
        first = get_settings().secret_key
        get_settings.cache_clear()
        second = get_settings().secret_key
        assert len(first) >= 32
        assert first == second
        assert (tmp_path / ".auth_secret").read_text(encoding="utf-8").strip() == first
    finally:
        get_settings.cache_clear()
