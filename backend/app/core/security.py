from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import os
import secrets
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.database import get_db
from app.models.entities import User


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _to_blob(data: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_CRYPTPROTECT_LOCAL_MACHINE = 0x4


def _dpapi_encrypt(data: bytes, *, machine_scope: bool = False) -> bytes:
    in_blob, _buffer = _to_blob(data)
    out_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    flags = _CRYPTPROTECT_UI_FORBIDDEN
    if machine_scope:
        flags |= _CRYPTPROTECT_LOCAL_MACHINE
    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "Neko AI credential",
        None,
        None,
        None,
        flags,
        ctypes.byref(out_blob),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def _dpapi_decrypt(data: bytes) -> bytes:
    in_blob, _buffer = _to_blob(data)
    out_blob = _DataBlob()
    description = wintypes.LPWSTR()
    crypt32 = ctypes.windll.crypt32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        ctypes.byref(description),
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)
        if description:
            ctypes.windll.kernel32.LocalFree(description)


class CredentialVault:
    """Installation-local credential storage for a single-user workstation.

    New values use one Fernet key stored beside Neko's local data.  This keeps
    credentials stable across elevated/non-elevated shells and Windows account
    context changes.  Database and filesystem access are the intended boundary
    for this personal installation. Older DPAPI values remain readable and are
    automatically rewrapped by the startup preflight when possible.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._key_path = Path(self.settings.data_dir) / ".credential_key"

    def encrypt(self, secret: str) -> str:
        if not secret or len(secret) < 6:
            raise ValueError("Credential is too short")
        raw = secret.encode("utf-8")
        return "fernet:" + self._fernet().encrypt(raw).decode("ascii")

    def decrypt(self, encrypted: str) -> str:
        scheme, payload = encrypted.split(":", 1)
        if scheme in {"dpapi", "dpapi-machine"}:
            return _dpapi_decrypt(base64.urlsafe_b64decode(payload)).decode("utf-8")
        if scheme == "fernet":
            return self._fernet().decrypt(payload.encode("ascii")).decode("utf-8")
        raise ValueError("Unknown credential scheme")

    def rewrap_if_legacy(self, encrypted: str) -> tuple[str, bool]:
        """Convert a readable DPAPI value to the installation-local format."""

        scheme = encrypted.split(":", 1)[0]
        if scheme == "fernet":
            return encrypted, False
        secret = self.decrypt(encrypted)
        try:
            return self.encrypt(secret), True
        finally:
            secret = ""

    def _fernet(self) -> Fernet:
        if self._key_path.exists():
            key = self._key_path.read_bytes()
        else:
            self._key_path.parent.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key()
            self._key_path.write_bytes(key)
            try:
                os.chmod(self._key_path, 0o600)
            except OSError:
                pass
        return Fernet(key)


def mask_secret(secret: str) -> str:
    if len(secret) <= 8:
        return "•" * len(secret)
    prefix = secret[:3] if secret.startswith(("sk-", "ds-")) else secret[:2]
    return f"{prefix}{'•' * 12}{secret[-4:]}"


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    if len(password) < 10:
        raise ValueError("密码至少需要 10 个字符")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return base64.urlsafe_b64encode(digest).decode(), base64.urlsafe_b64encode(salt).decode()


def verify_password(password: str, expected_hash: str, salt: str) -> bool:
    actual, _ = hash_password(password, base64.urlsafe_b64decode(salt))
    return hmac.compare_digest(actual, expected_hash)


def create_access_token(user_id: str, settings: Settings | None = None, ttl_seconds: int = 8 * 3600) -> str:
    config = settings or get_settings()
    payload = {"sub": user_id, "exp": int(time.time()) + ttl_seconds, "nonce": secrets.token_hex(8)}
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=")
    signature = hmac.new(config.secret_key.encode(), body, hashlib.sha256).digest()
    return body.decode() + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()


def decode_access_token(token: str, settings: Settings | None = None) -> dict[str, Any]:
    config = settings or get_settings()
    try:
        body_text, signature_text = token.split(".", 1)
        body = body_text.encode()
        signature = base64.urlsafe_b64decode(signature_text + "=" * (-len(signature_text) % 4))
        expected = hmac.new(config.secret_key.encode(), body, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid signature")
        payload = json.loads(base64.urlsafe_b64decode(body_text + "=" * (-len(body_text) % 4)))
        if int(payload["exp"]) < int(time.time()):
            raise ValueError("expired")
        return payload
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效") from exc


bearer = HTTPBearer(auto_error=False)


def require_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    payload = decode_access_token(credentials.credentials)
    user = db.get(User, payload["sub"])
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在")
    return user
