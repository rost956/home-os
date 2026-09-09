import json
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect

import app.main as main_module
from app.database import Base
from app.models import PlannerItem, PlannerReminder
from app.services.planner_reminders import (
    due_planner_reminders,
    expand_due_reminders,
    parse_reminder_configs,
)
from app.timezone import MSK


def at(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=MSK)


def add_event(db, owner_id, **values):
    item = PlannerItem(
        owner_id=owner_id,
        title=values.pop("title", "Event"),
        scheduled_for=values.pop("scheduled_for"),
        **values,
    )
    db.add(item)
    db.flush()
    return item


def add_reminder(item, value=15, unit="minutes"):
    reminder = PlannerReminder(offset_value=value, offset_unit=unit)
    item.reminders.append(reminder)
    return reminder


def test_reminder_validation_accepts_zero_and_rejects_invalid_or_duplicate_values():
    assert parse_reminder_configs(["0", "2"], ["minutes", "days"])[0].offset_value == 0
    with pytest.raises(ValueError, match="целым"):
        parse_reminder_configs(["1.5"], ["hours"])
    with pytest.raises(ValueError, match="отрицательным"):
        parse_reminder_configs(["-1"], ["minutes"])
    with pytest.raises(ValueError, match="единицу"):
        parse_reminder_configs(["1"], ["weeks"])
    with pytest.raises(ValueError, match="365"):
        parse_reminder_configs(["366"], ["days"])
    with pytest.raises(ValueError, match="дважды"):
        parse_reminder_configs(["60", "1"], ["minutes", "hours"])


def test_planner_reminder_crud_multiple_edit_removal_and_cascade(client, db, make_user, login):
    make_user("alice")
    login("alice")
    created = client.post(
        "/planner",
        data={
            "title": "Dentist",
            "scheduled_for": "2026-09-12",
            "end_date": "2026-09-12",
            "start_time": "10:00",
            "reminder_offset_value": ["15", "2"],
            "reminder_offset_unit": ["minutes", "hours"],
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    item = db.query(PlannerItem).one()
    db.refresh(item)
    assert [(value.offset_value, value.offset_unit) for value in item.reminders] == [(15, "minutes"), (2, "hours")]
    first_reminder_id = item.reminders[0].id
    page = client.get("/planner?month=2026-09&day=2026-09-12")
    assert 'name="reminder_offset_value"' in page.text
    assert "За 15 мин" in page.text

    unchanged = client.post(
        f"/planner/{item.id}/update",
        data={
            "title": "Dentist rescheduled",
            "scheduled_for": "2026-09-12",
            "end_date": "2026-09-12",
            "start_time": "10:00",
            "reminder_offset_value": ["15", "2"],
            "reminder_offset_unit": ["minutes", "hours"],
        },
        follow_redirects=False,
    )
    assert unchanged.status_code == 303
    db.refresh(item)
    assert item.reminders[0].id == first_reminder_id

    edited = client.post(
        f"/planner/{item.id}/update",
        data={
            "title": "Dentist rescheduled",
            "scheduled_for": "2026-09-12",
            "end_date": "2026-09-12",
            "start_time": "10:00",
            "reminder_offset_value": "0",
            "reminder_offset_unit": "minutes",
        },
        follow_redirects=False,
    )
    assert edited.status_code == 303
    db.refresh(item)
    assert [(value.offset_value, value.offset_unit) for value in item.reminders] == [(0, "minutes")]

    removed = client.post(
        f"/planner/{item.id}/update",
        data={"title": "Dentist rescheduled", "scheduled_for": "2026-09-12", "end_date": "2026-09-12"},
        follow_redirects=False,
    )
    assert removed.status_code == 303
    db.refresh(item)
    assert item.reminders == []
    assert db.get(PlannerItem, item.id) is not None

    add_reminder(item)
    db.commit()
    item_id = item.id
    reminder_id = item.reminders[0].id
    deleted = client.post(f"/planner/{item.id}/delete", follow_redirects=False)
    assert deleted.status_code == 303
    db.expire_all()
    assert db.get(PlannerItem, item_id) is None
    assert db.get(PlannerReminder, reminder_id) is None


def test_invalid_duplicate_reminders_do_not_create_event(client, db, make_user, login):
    make_user("alice")
    login("alice")
    response = client.post(
        "/planner",
        data={
            "title": "Duplicate",
            "scheduled_for": "2026-09-12",
            "end_date": "2026-09-12",
            "reminder_offset_value": ["60", "1"],
            "reminder_offset_unit": ["minutes", "hours"],
        },
    )
    assert response.status_code == 200
    assert "Одинаковые напоминания" in response.text
    assert db.query(PlannerItem).count() == 0


def test_due_window_is_half_open_and_all_day_uses_moscow_midnight(db, make_user):
    user = make_user("alice")
    timed = add_event(db, user.id, scheduled_for=date(2026, 9, 12), start_time="10:00")
    first = add_reminder(timed, 60, "minutes")
    add_reminder(timed, 30, "minutes")
    all_day = add_event(db, user.id, title="All day", scheduled_for=date(2026, 9, 13))
    add_reminder(all_day, 1, "days")
    db.commit()

    due = expand_due_reminders([timed, all_day], at(2026, 9, 12), at(2026, 9, 12, 9, 30))
    assert [(value.title, value.due_at) for value in due] == [
        ("All day", at(2026, 9, 12)),
        ("Event", at(2026, 9, 12, 9)),
    ]
    # The second timed reminder is due exactly at the exclusive end boundary.
    assert expand_due_reminders([timed], at(2026, 9, 12, 9), at(2026, 9, 12, 9, 30))[0].reminder_id == first.id
    assert due[1].key == f"{first.id}:{timed.id}@2026-09-12"
    assert due[0].occurrence_start == at(2026, 9, 13)


@pytest.mark.parametrize(
    ("event_values", "occurrence_start"),
    [
        ({"scheduled_for": date(2026, 9, 10), "recurrence_frequency": "daily"}, at(2026, 9, 12, 8)),
        ({"scheduled_for": date(2026, 9, 1), "recurrence_frequency": "daily", "recurrence_interval": 3}, at(2026, 9, 13, 8)),
        ({"scheduled_for": date(2026, 9, 1), "recurrence_frequency": "weekly", "recurrence_interval": 2}, at(2026, 9, 15, 8)),
        ({"scheduled_for": date(2026, 1, 31), "recurrence_frequency": "monthly"}, at(2026, 2, 28, 8)),
        ({"scheduled_for": date(2028, 2, 29), "recurrence_frequency": "yearly"}, at(2029, 2, 28, 8)),
    ],
)
def test_due_reminders_follow_daily_weekly_monthly_and_yearly_occurrences(
    db, make_user, event_values, occurrence_start
):
    user = make_user("alice")
    item = add_event(db, user.id, start_time="08:00", **event_values)
    add_reminder(item, 1, "hours")
    db.commit()

    due = expand_due_reminders(
        [item],
        occurrence_start - timedelta(hours=1),
        occurrence_start - timedelta(minutes=59),
    )
    assert len(due) == 1
    assert due[0].occurrence_start == occurrence_start
    assert due[0].due_at == occurrence_start - timedelta(hours=1)


def test_due_reminders_respect_until_and_use_multiday_occurrence_start(db, make_user):
    user = make_user("alice")
    item = add_event(
        db,
        user.id,
        scheduled_for=date(2026, 9, 10),
        end_date=date(2026, 9, 12),
        start_time="08:00",
        recurrence_frequency="daily",
        recurrence_until=date(2026, 9, 11),
    )
    add_reminder(item, 1, "hours")
    db.commit()

    assert len(expand_due_reminders([item], at(2026, 9, 11, 7), at(2026, 9, 11, 7, 1))) == 1
    assert expand_due_reminders([item], at(2026, 9, 12, 7), at(2026, 9, 12, 7, 1)) == []


def test_due_query_is_owner_scoped_and_accepts_distant_recurrence_anchor(db, make_user):
    alice = make_user("alice")
    bob = make_user("bob")
    for owner in (alice, bob):
        item = add_event(
            db,
            owner.id,
            title=owner.username,
            scheduled_for=date(2020, 1, 1),
            start_time="10:00",
            recurrence_frequency="daily",
        )
        add_reminder(item, 15, "minutes")
    db.commit()

    due = due_planner_reminders(db, alice.id, at(2026, 9, 12, 9, 45), at(2026, 9, 12, 9, 46))
    assert [value.title for value in due] == ["alice"]


def test_reminders_round_trip_in_export_and_old_backup_stays_compatible(client, db, make_user, login):
    user = make_user("alice")
    item = add_event(db, user.id, scheduled_for=date(2026, 9, 12), start_time="10:00")
    add_reminder(item, 15, "minutes")
    db.commit()
    login("alice")

    exported = client.get("/export/data.json").json()
    assert exported["planner"][0]["reminders"] == [
        {"offset_value": 15, "offset_unit": "minutes", "relation": "before_start"}
    ]
    old_backup = {
        "planner": [
            {"title": "Legacy", "scheduled_for": "2026-09-13", "start_time": "11:00"},
            {
                "title": "Restored",
                "scheduled_for": "2026-09-14",
                "reminders": [{"offset_value": 2, "offset_unit": "days", "relation": "before_start"}],
            },
        ]
    }
    response = client.post(
        "/import-data",
        data={"data_text": json.dumps(old_backup)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    legacy = db.query(PlannerItem).filter_by(title="Legacy").one()
    assert legacy.reminders == []
    restored = db.query(PlannerItem).filter_by(title="Restored").one()
    assert [(value.offset_value, value.offset_unit) for value in restored.reminders] == [(2, "days")]


def test_runtime_migration_creates_planner_reminder_table(tmp_path, monkeypatch):
    legacy_engine = create_engine(f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}")
    Base.metadata.create_all(bind=legacy_engine)
    PlannerReminder.__table__.drop(bind=legacy_engine)
    with legacy_engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO users (username, password_hash, created_at, theme, expense_period_start_day) "
            "VALUES ('legacy', 'hash', '2026-09-01 00:00:00', 'system', 1)"
        )
        connection.exec_driver_sql(
            "INSERT INTO planner_items "
            "(owner_id, title, scheduled_for, recurrence_interval, color, is_done, created_at, updated_at) "
            "VALUES (1, 'Existing', '2026-09-12', 1, '#2563eb', 0, "
            "'2026-09-01 00:00:00', '2026-09-01 00:00:00')"
        )
    monkeypatch.setattr(main_module, "engine", legacy_engine)

    assert main_module.schema_change_required() is True
    main_module.ensure_runtime_schema()

    assert "planner_reminders" in inspect(legacy_engine).get_table_names()
    with legacy_engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT title FROM planner_items").scalar_one() == "Existing"
