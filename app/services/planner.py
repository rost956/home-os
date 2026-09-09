"""Date-interval semantics for canonical Planner events."""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol


class PlannerDatedItem(Protocol):
    scheduled_for: date
    end_date: date | None


@dataclass(frozen=True)
class PlannerOccurrence:
    """One bounded calendar-cell representation of a canonical event."""

    item: PlannerDatedItem
    day: date
    range_position: str


def effective_end_date(item: PlannerDatedItem) -> date:
    return item.end_date or item.scheduled_for


def duration_days(item: PlannerDatedItem) -> int:
    return (effective_end_date(item) - item.scheduled_for).days + 1


def is_event_on_date(item: PlannerDatedItem, value: date) -> bool:
    return item.scheduled_for <= value <= effective_end_date(item)


def event_overlaps_range(item: PlannerDatedItem, range_start: date, range_end: date) -> bool:
    return item.scheduled_for <= range_end and effective_end_date(item) >= range_start


def calendar_occurrences(
    items: list[PlannerDatedItem], range_start: date, range_end: date
) -> dict[date, list[PlannerOccurrence]]:
    """Expand only the intersection of events and the displayed calendar range."""
    occurrences: dict[date, list[PlannerOccurrence]] = {}
    for item in items:
        if not event_overlaps_range(item, range_start, range_end):
            continue
        visible_start = max(item.scheduled_for, range_start)
        visible_end = min(effective_end_date(item), range_end)
        current_day = visible_start
        while current_day <= visible_end:
            if item.scheduled_for == effective_end_date(item):
                range_position = "single"
            elif current_day == item.scheduled_for:
                range_position = "start"
            elif current_day == effective_end_date(item):
                range_position = "end"
            else:
                range_position = "middle"
            occurrences.setdefault(current_day, []).append(
                PlannerOccurrence(item=item, day=current_day, range_position=range_position)
            )
            current_day += timedelta(days=1)
    return occurrences


def format_planner_date_range(item: PlannerDatedItem) -> str:
    start = item.scheduled_for.strftime("%d.%m.%Y")
    end = effective_end_date(item)
    return start if end == item.scheduled_for else f"{start} — {end.strftime('%d.%m.%Y')}"
