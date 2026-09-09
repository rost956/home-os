"""Planner reminder configuration and deterministic due-time expansion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Iterable, Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import PlannerItem, PlannerReminder
from app.services.planner import iter_occurrences_in_range
from app.timezone import MSK

REMINDER_UNITS = {"minutes": 60, "hours": 60 * 60, "days": 24 * 60 * 60}
REMINDER_RELATION = "before_start"
MAX_REMINDER_OFFSET = timedelta(days=365)


@dataclass(frozen=True)
class ReminderConfigInput:
    offset_value: int
    offset_unit: str
    relation: str = REMINDER_RELATION

    @property
    def offset(self) -> timedelta:
        return timedelta(seconds=self.offset_value * REMINDER_UNITS[self.offset_unit])


@dataclass(frozen=True)
class DuePlannerReminder:
    key: str
    reminder_id: int
    planner_item_id: int
    occurrence_key: str
    occurrence_start: datetime
    due_at: datetime
    title: str
    offset_value: int
    offset_unit: str
    relation: str


def parse_reminder_configs(
    offset_values: Sequence[str | int],
    offset_units: Sequence[str],
) -> list[ReminderConfigInput]:
    if len(offset_values) != len(offset_units):
        raise ValueError("Некорректный набор напоминаний")
    configs: list[ReminderConfigInput] = []
    effective_offsets: set[int] = set()
    for raw_value, raw_unit in zip(offset_values, offset_units, strict=True):
        value_text = str(raw_value).strip()
        if not value_text:
            continue
        try:
            value = int(value_text)
        except ValueError as exc:
            raise ValueError("Интервал напоминания должен быть целым числом") from exc
        unit = str(raw_unit).strip().lower()
        if unit not in REMINDER_UNITS:
            raise ValueError("Выберите допустимую единицу напоминания")
        if value < 0:
            raise ValueError("Интервал напоминания не может быть отрицательным")
        seconds = value * REMINDER_UNITS[unit]
        if seconds > int(MAX_REMINDER_OFFSET.total_seconds()):
            raise ValueError("Напоминание можно установить не более чем за 365 дней")
        if seconds in effective_offsets:
            raise ValueError("Одинаковые напоминания нельзя добавлять дважды")
        effective_offsets.add(seconds)
        configs.append(ReminderConfigInput(value, unit))
    return configs


def replace_reminder_configs(item: PlannerItem, configs: Iterable[ReminderConfigInput]) -> None:
    existing = {
        (reminder.offset_value, reminder.offset_unit, reminder.relation): reminder
        for reminder in item.reminders
    }
    item.reminders = [
        existing.get((config.offset_value, config.offset_unit, config.relation))
        or PlannerReminder(
            offset_value=config.offset_value,
            offset_unit=config.offset_unit,
            relation=config.relation,
        )
        for config in configs
    ]


def reminder_offset(reminder: PlannerReminder) -> timedelta:
    return timedelta(seconds=reminder.offset_value * REMINDER_UNITS[reminder.offset_unit])


def occurrence_start_at(occurrence) -> datetime:
    start_time = time.fromisoformat(occurrence.item.start_time) if occurrence.item.start_time else time.min
    return datetime.combine(occurrence.start_date, start_time, tzinfo=MSK)


def _aware_msk(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Reminder query bounds must be timezone-aware")
    return value.astimezone(MSK)


def expand_due_reminders(
    items: Iterable[PlannerItem],
    window_start: datetime,
    window_end: datetime,
) -> list[DuePlannerReminder]:
    start = _aware_msk(window_start)
    end = _aware_msk(window_end)
    if end <= start:
        raise ValueError("Reminder query window must be non-empty")
    due: list[DuePlannerReminder] = []
    for item in items:
        if not item.reminders:
            continue
        max_offset = max(reminder_offset(reminder) for reminder in item.reminders)
        occurrence_end = (end + max_offset).date()
        for occurrence in iter_occurrences_in_range(item, start.date(), occurrence_end):
            occurrence_start = occurrence_start_at(occurrence)
            for reminder in item.reminders:
                due_at = occurrence_start - reminder_offset(reminder)
                if start <= due_at < end:
                    due.append(
                        DuePlannerReminder(
                            key=f"{reminder.id}:{occurrence.key}",
                            reminder_id=reminder.id,
                            planner_item_id=item.id,
                            occurrence_key=occurrence.key,
                            occurrence_start=occurrence_start,
                            due_at=due_at,
                            title=item.title,
                            offset_value=reminder.offset_value,
                            offset_unit=reminder.offset_unit,
                            relation=reminder.relation,
                        )
                    )
    return sorted(due, key=lambda value: (value.due_at, value.reminder_id, value.occurrence_key))


def due_planner_reminders(
    db: Session,
    owner_id: int,
    window_start: datetime,
    window_end: datetime,
) -> list[DuePlannerReminder]:
    """Return owner-scoped reminders due in the half-open interval [start, end)."""
    start = _aware_msk(window_start)
    end = _aware_msk(window_end)
    if end <= start:
        raise ValueError("Reminder query window must be non-empty")
    latest_occurrence_start = end + MAX_REMINDER_OFFSET
    items = db.scalars(
        select(PlannerItem)
        .join(PlannerItem.reminders)
        .options(selectinload(PlannerItem.reminders))
        .where(
            PlannerItem.owner_id == owner_id,
            PlannerItem.scheduled_for <= latest_occurrence_start.date(),
            or_(
                and_(
                    PlannerItem.recurrence_frequency.is_(None),
                    PlannerItem.scheduled_for >= start.date(),
                ),
                and_(
                    PlannerItem.recurrence_frequency.is_not(None),
                    or_(PlannerItem.recurrence_until.is_(None), PlannerItem.recurrence_until >= start.date()),
                ),
            ),
        )
        .distinct()
    ).all()
    return expand_due_reminders(items, start, end)


def reminder_label(reminder: PlannerReminder) -> str:
    if reminder.offset_value == 0:
        return "В момент начала"
    labels = {"minutes": "мин", "hours": "ч", "days": "дн"}
    return f"За {reminder.offset_value} {labels[reminder.offset_unit]}"
