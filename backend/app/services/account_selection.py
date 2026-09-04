from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Account


def get_active_account(db: Session, platform: str) -> Account | None:
    """Return the only enabled account for a platform, otherwise fail closed.

    Personal-account connectors may have multiple stored profiles while only one
    is allowed to be active.  Returning ``None`` for both zero and multiple
    enabled rows prevents an arbitrary stale profile from being used for auth,
    policy checks, media access, or outbound sends.
    """

    items = list(
        db.scalars(
            select(Account)
            .where(Account.platform == platform, Account.enabled.is_(True))
            .order_by(Account.updated_at.desc(), Account.id.asc())
            .limit(2)
            .execution_options(populate_existing=True)
        )
    )
    return items[0] if len(items) == 1 else None


def lock_platform_accounts(db: Session, platform: str) -> list[Account]:
    """Lock every stored profile while changing which one is active."""

    return list(
        db.scalars(
            select(Account)
            .where(Account.platform == platform)
            .order_by(Account.created_at.asc(), Account.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
