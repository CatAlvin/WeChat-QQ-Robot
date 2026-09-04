from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.routes import update_live_time_window
from app.models.entities import AuditLog, RuntimeState
from app.schemas import LiveTimeWindowUpdate


@pytest.mark.asyncio
async def test_live_time_window_requires_confirmation_to_disable(db) -> None:
    db.add(RuntimeState(id=1))
    db.commit()
    with pytest.raises(HTTPException, match="明确确认"):
        await update_live_time_window(
            LiveTimeWindowUpdate(enabled=False, start="00:00", end="23:59"),
            db,
        )


@pytest.mark.asyncio
async def test_live_time_window_persists_cross_midnight_and_disable_state(db) -> None:
    db.add(RuntimeState(id=1))
    db.commit()
    enabled = await update_live_time_window(
        LiveTimeWindowUpdate(enabled=True, start="22:00", end="02:00"),
        db,
    )
    assert enabled.live_time_window_enabled
    assert enabled.live_auto_start == "22:00"
    assert enabled.live_auto_end == "02:00"

    disabled = await update_live_time_window(
        LiveTimeWindowUpdate(
            enabled=False,
            start="22:00",
            end="02:00",
            disable_confirmed=True,
        ),
        db,
    )
    assert not disabled.live_time_window_enabled
    audit = db.scalar(select(AuditLog).where(AuditLog.event == "LIVE_TIME_WINDOW_CHANGED").order_by(AuditLog.created_at.desc()))
    assert audit is not None
    assert audit.level == "WARNING"
    assert audit.detail["enabled"] is False


def test_live_time_window_rejects_invalid_or_zero_length_values() -> None:
    with pytest.raises(ValueError):
        LiveTimeWindowUpdate(enabled=True, start="9:00", end="23:30")
    with pytest.raises(ValueError):
        LiveTimeWindowUpdate(enabled=True, start="10:00", end="10:00")
