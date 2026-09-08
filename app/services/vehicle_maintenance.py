from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

STATUS_ORDER = {"overdue": 0, "soon": 1, "ok": 2}


@dataclass(frozen=True)
class MaintenanceState:
    status: str
    next_odometer: int | None
    next_date: date | None
    remaining_km: int | None
    remaining_days: int | None


def add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year, month = value.year + month_index // 12, month_index % 12 + 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def warning_km(interval_km: int) -> int:
    return min(1_000, max(100, interval_km // 10))


def warning_days(interval_months: int) -> int:
    return min(30, max(7, (interval_months * 30) // 10))


def calculate_maintenance(*, current_odometer: int, today: date, last_odometer: int | None, last_date: date | None, interval_km: int | None, interval_months: int | None) -> MaintenanceState:
    next_odometer = last_odometer + interval_km if interval_km is not None and last_odometer is not None else None
    next_date = add_months(last_date, interval_months) if interval_months is not None and last_date is not None else None
    remaining_km = next_odometer - current_odometer if next_odometer is not None else None
    remaining_days = (next_date - today).days if next_date is not None else None
    overdue = (remaining_km is not None and remaining_km <= 0) or (remaining_days is not None and remaining_days <= 0)
    soon = (remaining_km is not None and remaining_km <= warning_km(interval_km or 1)) or (remaining_days is not None and remaining_days <= warning_days(interval_months or 1))
    return MaintenanceState("overdue" if overdue else "soon" if soon else "ok", next_odometer, next_date, remaining_km, remaining_days)
