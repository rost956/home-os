from datetime import date

from app.models import PlannerItem
from app.services.planner import iter_occurrences_in_range


def series(start, frequency, interval=1, until=None, end=None):
    return PlannerItem(owner_id=1, title="Series", scheduled_for=start, end_date=end, recurrence_frequency=frequency, recurrence_interval=interval, recurrence_until=until)


def starts(item, start, end):
    return [value.start_date for value in iter_occurrences_in_range(item, start, end)]


def test_anchor_recurrences_and_until_are_bounded():
    assert starts(series(date(2026, 9, 1), "daily", 2), date(2026, 9, 1), date(2026, 9, 8)) == [date(2026, 9, 1), date(2026, 9, 3), date(2026, 9, 5), date(2026, 9, 7)]
    assert starts(series(date(2026, 9, 7), "weekly", 2), date(2026, 9, 1), date(2026, 10, 31)) == [date(2026, 9, 7), date(2026, 9, 21), date(2026, 10, 5), date(2026, 10, 19)]
    assert starts(series(date(2026, 9, 1), "daily", until=date(2026, 9, 3)), date(2026, 9, 1), date(2026, 9, 10)) == [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]


def test_monthly_and_yearly_clamp_without_drift():
    monthly = series(date(2026, 1, 31), "monthly")
    assert starts(monthly, date(2026, 1, 1), date(2026, 5, 31)) == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30), date(2026, 5, 31)]
    leap = series(date(2028, 2, 29), "yearly")
    assert starts(leap, date(2028, 1, 1), date(2032, 12, 31)) == [date(2028, 2, 29), date(2029, 2, 28), date(2030, 2, 28), date(2031, 2, 28), date(2032, 2, 29)]


def test_recurring_multi_day_occurrence_keeps_duration_and_lookback():
    item = series(date(2026, 8, 30), "monthly", end=date(2026, 9, 1))
    occurrences = list(iter_occurrences_in_range(item, date(2026, 9, 1), date(2026, 9, 30)))
    assert occurrences[0].start_date == date(2026, 8, 30)
    assert occurrences[0].end_date == date(2026, 9, 1)
    assert occurrences[1].start_date == date(2026, 9, 30)
