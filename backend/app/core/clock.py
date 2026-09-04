from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


UTC = timezone.utc
BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
BEIJING_TIME_ZONE_NAME = "Asia/Shanghai"


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Return an aware UTC value; database-naive values are stored UTC."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def as_beijing(value: datetime) -> datetime:
    return as_utc(value).astimezone(BEIJING_TZ)


def beijing_now() -> datetime:
    return utc_now().astimezone(BEIJING_TZ)


def beijing_start_of_day_utc(value: datetime | None = None) -> datetime:
    local = as_beijing(value or utc_now())
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def beijing_day_bounds_utc(day: datetime) -> tuple[datetime, datetime]:
    local = as_beijing(day)
    start = datetime.combine(local.date(), time.min, tzinfo=BEIJING_TZ)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Store UTC without an offset and restore an aware UTC datetime.

    MySQL DATETIME and SQLite both discard timezone metadata even when
    ``timezone=True`` is requested. This adapter keeps the physical schema
    unchanged while preventing API clients from interpreting UTC rows as local
    Beijing wall-clock values.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        return as_utc(value).replace(tzinfo=None) if value is not None else None

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        return as_utc(value) if value is not None else None
