# ruff: noqa: E701, E702
"""Interval and bounded recurrence semantics for canonical Planner events."""
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol

RECURRENCE_FREQUENCIES = {"daily", "weekly", "monthly", "yearly"}

class PlannerDatedItem(Protocol):
    id: int; scheduled_for: date; end_date: date | None
    recurrence_frequency: str | None; recurrence_interval: int; recurrence_until: date | None

@dataclass(frozen=True)
class DerivedPlannerOccurrence:
    item: PlannerDatedItem; start_date: date; end_date: date; key: str

    def __getattr__(self, name):
        if name == "scheduled_for": return self.start_date
        return getattr(self.item, name)

@dataclass(frozen=True)
class PlannerOccurrence:
    item: PlannerDatedItem; day: date; range_position: str; occurrence_key: str

def effective_end_date(item): return item.end_date or item.scheduled_for
def duration_days(item): return (effective_end_date(item) - item.scheduled_for).days + 1
def is_event_on_date(item, value): return item.scheduled_for <= value <= effective_end_date(item)
def event_overlaps_range(item, start, end): return item.scheduled_for <= end and effective_end_date(item) >= start
def is_recurring(item): return item.recurrence_frequency in RECURRENCE_FREQUENCIES

def _month_date(anchor, months):
    year, index = divmod(anchor.year * 12 + anchor.month - 1 + months, 12)
    return date(year, index + 1, min(anchor.day, monthrange(year, index + 1)[1]))
def _start_at(item, index):
    interval = item.recurrence_interval or 1
    if item.recurrence_frequency == "daily": return item.scheduled_for + timedelta(days=index * interval)
    if item.recurrence_frequency == "weekly": return item.scheduled_for + timedelta(days=index * interval * 7)
    if item.recurrence_frequency == "monthly": return _month_date(item.scheduled_for, index * interval)
    if item.recurrence_frequency == "yearly": return _month_date(item.scheduled_for, index * interval * 12)
    return item.scheduled_for
def _first_index(item, target):
    if not is_recurring(item) or target <= item.scheduled_for: return 0
    interval = item.recurrence_interval or 1
    if item.recurrence_frequency == "daily": return max(0, (target-item.scheduled_for).days // interval)
    if item.recurrence_frequency == "weekly": return max(0, (target-item.scheduled_for).days // (interval*7))
    months = (target.year-item.scheduled_for.year)*12 + target.month-item.scheduled_for.month
    return max(0, months // interval) if item.recurrence_frequency == "monthly" else max(0, (target.year-item.scheduled_for.year)//interval)
def iter_occurrences_in_range(item, range_start, range_end):
    duration = timedelta(days=duration_days(item)-1)
    if not is_recurring(item):
        if event_overlaps_range(item, range_start, range_end): yield DerivedPlannerOccurrence(item,item.scheduled_for,effective_end_date(item),f"{item.id}@{item.scheduled_for.isoformat()}")
        return
    index = _first_index(item, range_start-duration)
    while True:
        start = _start_at(item,index)
        if start > range_end or (item.recurrence_until and start > item.recurrence_until): break
        end=start+duration
        if end >= range_start: yield DerivedPlannerOccurrence(item,start,end,f"{item.id}@{start.isoformat()}")
        index += 1
def calendar_occurrences(items, range_start, range_end):
    result={}
    for item in items:
        for occurrence in iter_occurrences_in_range(item,range_start,range_end):
            current=max(occurrence.start_date,range_start); last=min(occurrence.end_date,range_end)
            while current <= last:
                position="single" if occurrence.start_date==occurrence.end_date else "start" if current==occurrence.start_date else "end" if current==occurrence.end_date else "middle"
                result.setdefault(current,[]).append(PlannerOccurrence(item,current,position,occurrence.key)); current += timedelta(days=1)
    return result
def format_planner_date_range(item):
    start=item.scheduled_for.strftime("%d.%m.%Y"); end=effective_end_date(item)
    return start if end==item.scheduled_for else f"{start} — {end.strftime('%d.%m.%Y')}"
