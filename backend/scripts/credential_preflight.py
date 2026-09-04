from __future__ import annotations

from sqlalchemy import select

from app.core.security import CredentialVault
from app.database import SessionLocal
from app.models.entities import ApiCredential


def main() -> int:
    """Validate credentials and migrate readable legacy DPAPI values."""
    db = SessionLocal()
    failures: list[str] = []
    migrated = 0
    try:
        credentials = list(db.scalars(select(ApiCredential).order_by(ApiCredential.provider, ApiCredential.label)))
        vault = CredentialVault()
        for credential in credentials:
            try:
                replacement, changed = vault.rewrap_if_legacy(credential.encrypted_secret)
                if changed:
                    credential.encrypted_secret = replacement
                    migrated += 1
            except Exception:
                failures.append(credential.provider)
        if migrated:
            db.commit()
    finally:
        db.close()

    if failures:
        providers = ", ".join(sorted(set(failures)))
        print(
            "以下旧凭据无法读取："
            f"{providers}。Neko 仍会正常启动，只有对应连接暂不可用；"
            "请在本机后台重新填写一次，之后将不再绑定 Windows 用户上下文。"
        )
        return 2
    suffix = f"; migrated={migrated}" if migrated else ""
    print(f"Neko credential storage: OK{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
