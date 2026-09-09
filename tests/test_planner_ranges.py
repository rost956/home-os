from datetime import date, timedelta

from sqlalchemy import create_engine, inspect, text

import app.main as main_module
from app.database import Base
from app.models import PlannerItem
from app.services.planner import (
    calendar_occurrences,
    duration_days,
    event_overlaps_range,
    is_event_on_date,
)
from app.timezone import today_msk


def make_event(start: date, end: date | None = None) -> PlannerItem:
    return PlannerItem(owner_id=1, title="Trip", scheduled_for=start, end_date=end)


def test_interval_helpers_are_inclusive_and_bounded():
    event = make_event(date(2026, 9, 12), date(2026, 9, 14))

    assert duration_days(event) == 3
    assert is_event_on_date(event, date(2026, 9, 12))
    assert is_event_on_date(event, date(2026, 9, 13))
    assert is_event_on_date(event, date(2026, 9, 14))
    assert not is_event_on_date(event, date(2026, 9, 11))
    assert not is_event_on_date(event, date(2026, 9, 15))
    assert event_overlaps_range(event, date(2026, 9, 1), date(2026, 9, 30))
    assert not event_overlaps_range(event, date(2026, 10, 1), date(2026, 10, 31))

    occurrences = calendar_occurrences([event], date(2026, 9, 13), date(2026, 9, 14))
    assert list(occurrences) == [date(2026, 9, 13), date(2026, 9, 14)]
    assert [occurrences[day][0].item.id for day in occurrences] == [event.id, event.id]
    assert occurrences[date(2026, 9, 13)][0].range_position == "middle"
    assert occurrences[date(2026, 9, 14)][0].range_position == "end"


def test_single_day_and_cross_year_range_helpers():
    single = make_event(date(2026, 9, 12))
    cross_year = make_event(date(2026, 12, 30), date(2027, 1, 3))
    leap = make_event(date(2028, 2, 28), date(2028, 3, 1))

    assert duration_days(single) == 1
    assert is_event_on_date(single, date(2026, 9, 12))
    assert event_overlaps_range(cross_year, date(2027, 1, 1), date(2027, 1, 31))
    assert duration_days(leap) == 3


def test_planner_create_edit_and_rejects_reversed_range(client, db, make_user, login):
    make_user("alice")
    login("alice")
    created = client.post(
        "/planner",
        data={"title": "Trip", "scheduled_for": "2026-09-12", "end_date": "2026-09-14"},
        follow_redirects=False,
    )
    item = db.query(PlannerItem).one()

    rejected = client.post(
        f"/planner/{item.id}/update",
        data={"title": "Trip", "scheduled_for": "2026-09-15", "end_date": "2026-09-12"},
    )
    db.refresh(item)
    assert item.end_date == date(2026, 9, 14)
    assert item.scheduled_for == date(2026, 9, 12)
    edited = client.post(
        f"/planner/{item.id}/update",
        data={"title": "Trip", "scheduled_for": "2026-09-12", "end_date": "2026-09-12"},
        follow_redirects=False,
    )

    assert created.status_code == 303
    assert rejected.status_code == 200
    assert "Дата окончания не может быть раньше даты начала" in rejected.text
    assert edited.status_code == 303
    db.refresh(item)
    assert item.end_date is None


def test_month_page_uses_overlap_and_renders_each_visible_day(client, db, make_user, login):
    user = make_user("alice")
    item = PlannerItem(
        owner_id=user.id, title="Cross-month Trip", scheduled_for=date(2026, 8, 30), end_date=date(2026, 9, 3)
    )
    db.add(item)
    db.commit()
    login("alice")

    response = client.get("/planner?month=2026-09&day=2026-09-02")

    assert response.status_code == 200
    assert response.text.count("Cross-month Trip") >= 4
    assert "event-range-middle" in response.text
    assert "02.09.2026" in response.text


def test_active_range_stays_in_upcoming_once(client, db, make_user, login):
    user = make_user("alice")
    today = today_msk()
    item = PlannerItem(
        owner_id=user.id,
        title="Active range",
        scheduled_for=today - timedelta(days=1),
        end_date=today + timedelta(days=1),
    )
    db.add(item)
    db.commit()
    login("alice")

    response = client.get(f"/planner?month={today:%Y-%m}&day={today.isoformat()}")

    assert response.status_code == 200
    # Three visible calendar cells plus one selected-day and one upcoming card;
    # the two list representations stay canonical rather than expanding by day.
    assert response.text.count('>Active range</b>') == 5


def test_runtime_migration_adds_nullable_planner_end_date(tmp_path, monkeypatch):
    legacy_engine = create_engine(f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}")
    Base.metadata.create_all(bind=legacy_engine)
    with legacy_engine.begin() as connection:
        connection.execute(text("DROP TABLE planner_items"))
        connection.execute(text("CREATE TABLE planner_items (id INTEGER PRIMARY KEY, scheduled_for DATE NOT NULL)"))
    monkeypatch.setattr(main_module, "engine", legacy_engine)

    main_module.ensure_runtime_schema()

    assert "end_date" in {column["name"] for column in inspect(legacy_engine).get_columns("planner_items")}
