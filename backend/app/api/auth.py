from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import create_access_token, hash_password, verify_password
from app.database import get_db
from app.models.entities import AuditLog, User
from app.schemas import BootstrapRequest, LoginRequest, TokenResponse


router = APIRouter(prefix="/auth", tags=["auth"])
_attempts: dict[str, deque[float]] = defaultdict(deque)


def _check_throttle(key: str) -> None:
    now = time.monotonic()
    events = _attempts[key]
    while events and events[0] < now - 900:
        events.popleft()
    if len(events) >= 8:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="登录尝试过多，请稍后再试")
    events.append(now)


@router.get("/bootstrap-status")
def bootstrap_status(db: Session = Depends(get_db)) -> dict[str, bool]:
    return {"configured": bool(db.scalar(select(func.count(User.id))))}


@router.post("/bootstrap", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def bootstrap(payload: BootstrapRequest, db: Session = Depends(get_db)) -> TokenResponse:
    if db.scalar(select(func.count(User.id))):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="后台用户已经创建")
    try:
        password_hash, salt = hash_password(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    user = User(username=payload.username.strip(), password_hash=password_hash, password_salt=salt)
    db.add(user)
    db.flush()
    db.add(AuditLog(event="ADMIN_BOOTSTRAPPED", detail={"username": user.username}))
    db.commit()
    return TokenResponse(access_token=create_access_token(user.id))


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> TokenResponse:
    throttle_key = f"{request.client.host if request.client else 'local'}:{payload.username.casefold()}"
    _check_throttle(throttle_key)
    user = db.scalar(select(User).where(User.username == payload.username.strip()))
    if user is None or not verify_password(payload.password, user.password_hash, user.password_salt):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    _attempts.pop(throttle_key, None)
    db.add(AuditLog(event="ADMIN_LOGIN", detail={"username": user.username}))
    db.commit()
    return TokenResponse(access_token=create_access_token(user.id))

