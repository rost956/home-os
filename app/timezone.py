from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
try:
    MSK = ZoneInfo("Europe/Moscow")
except Exception:  # pragma: no cover - tzdata is installed in production
    MSK = timezone(timedelta(hours=3), name="MSK")


def now_utc() -> datetime:
    """Naive UTC timestamp for database storage."""
    return datetime.now(UTC).replace(tzinfo=None)


def now_msk() -> datetime:
    return datetime.now(MSK)


def today_msk() -> date:
    return datetime.now(MSK).date()


def to_msk(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(MSK)


def msk_date(value: datetime | None) -> date | None:
    local_value = to_msk(value)
    return local_value.date() if local_value else None


def format_msk(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    local_value = to_msk(value)
    return local_value.strftime(fmt) if local_value else ""


def msk_day_bounds_utc(day: date) -> tuple[datetime, datetime]:
    start_msk = datetime.combine(day, time.min, tzinfo=MSK)
    end_msk = datetime.combine(day, time.max, tzinfo=MSK)
    start_utc = start_msk.astimezone(UTC).replace(tzinfo=None)
    end_utc = end_msk.astimezone(UTC).replace(tzinfo=None)
    return start_utc, end_utc


def msk_date_to_utc_naive(day: date, keep_time_from: datetime | None = None) -> datetime:
    """Store a Moscow calendar date as naive UTC, preserving local time when possible."""
    local_time = to_msk(keep_time_from).time() if keep_time_from else now_msk().time()
    value_msk = datetime.combine(day, local_time, tzinfo=MSK)
    return value_msk.astimezone(UTC).replace(tzinfo=None)


def msk_month_bounds(day: date) -> tuple[date, date]:
    start = day.replace(day=1)
    if start.month == 12:
        next_month = start.replace(year=start.year + 1, month=1)
    else:
        next_month = start.replace(month=start.month + 1)
    return start, next_month - timedelta(days=1)
