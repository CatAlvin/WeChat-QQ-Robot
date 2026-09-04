from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.enums import GlobalMode, MessageStatus
from app.models.entities import Message, RuntimeState


class RuntimeControl:
    def __init__(self) -> None:
        self.send_lock = asyncio.Lock()

    @staticmethod
    def get(db: Session) -> RuntimeState:
        state = db.get(RuntimeState, 1)
        if state is None:
            state = RuntimeState(id=1)
            db.add(state)
            db.flush()
        return state

    async def set_mode(self, db: Session, mode: GlobalMode) -> RuntimeState:
        async with self.send_lock:
            state = self.get(db)
            state.global_mode = mode.value
            if mode == GlobalMode.STOPPED:
                db.execute(update(Message).where(Message.status == MessageStatus.QUEUED).values(status=MessageStatus.CANCELLED))
            state.updated_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(state)
            return state

    async def set_kill_switch(self, db: Session, enabled: bool) -> RuntimeState:
        async with self.send_lock:
            state = self.get(db)
            state.kill_switch = enabled
            if enabled:
                db.execute(update(Message).where(Message.status == MessageStatus.QUEUED).values(status=MessageStatus.CANCELLED))
            state.updated_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(state)
            return state


runtime_control = RuntimeControl()


def cancel_queued_messages(db: Session) -> int:
    """Crash/restart invariant: a draft from an earlier process can never send."""
    result = db.execute(update(Message).where(Message.status == MessageStatus.QUEUED).values(status=MessageStatus.CANCELLED))
    return int(result.rowcount or 0)
