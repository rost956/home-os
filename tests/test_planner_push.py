import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import app.main as main_module
from app.database import Base
from app.models import PlannerItem, PlannerReminder, PlannerReminderDelivery, PushSubscription
from app.services.planner_push import (
    PushSendError,
    _claim_delivery,
    materialize_due_deliveries,
    run_delivery_cycle,
)

NOW = datetime(2026, 9, 12, 7, 0, tzinfo=UTC)  # 10:00 Europe/Moscow


class FakeSender:
    def __init__(self, failures=None):
        self.failures = {key: list(value) for key, value in (failures or {}).items()}
        self.calls = []

    def send(self, subscription, payload):
        self.calls.append((subscription.endpoint, payload))
        outcomes = self.failures.get(subscription.endpoint, [])
        if outcomes:
            status_code = outcomes.pop(0)
            if status_code is not None:
                raise PushSendError("temporary" if status_code >= 500 else "gone", status_code)


def push_settings(monkeypatch, *, enabled=True, public_key=None, private_key="private-key"):
    settings = replace(
        main_module.settings,
        push_enabled=enabled,
        vapid_public_key=public_key if public_key is not None else "B" * 87,
        vapid_private_key=private_key,
        vapid_subject="mailto:push@example.com",
    )
    monkeypatch.setattr(main_module, "settings", settings)
    monkeypatch.setattr(main_module, "webpush", lambda **kwargs: None)
    return settings


def add_subscription(db, user, suffix="one"):
    subscription = PushSubscription(
        user_id=user.id,
        endpoint=f"https://fcm.googleapis.com/push/{suffix}",
        p256dh="key",
        auth="auth",
    )
    db.add(subscription)
    db.flush()
    return subscription


def add_event(db, user, *, title="Event", day=date(2026, 9, 12), start_time="10:00", recurrence=None, until=None):
    item = PlannerItem(
        owner_id=user.id,
        title=title,
        scheduled_for=day,
        start_time=start_time,
        recurrence_frequency=recurrence,
        recurrence_until=until,
    )
    db.add(item)
    db.flush()
    return item


def add_reminder(item, value=0, unit="minutes"):
    reminder = PlannerReminder(offset_value=value, offset_unit=unit)
    item.reminders.append(reminder)
    return reminder


def cycle(db, sender, now=NOW, catchup=timedelta(minutes=60)):
    return run_delivery_cycle(db, sender, catchup=catchup, clock=lambda: now)


def test_due_delivery_is_persistent_private_and_idempotent(db, make_user):
    user = make_user("alice")
    item = add_event(db, user)
    reminder = add_reminder(item)
    subscription = add_subscription(db, user)
    db.commit()
    sender = FakeSender()

    first = cycle(db, sender)
    second = cycle(db, sender)

    assert (first.materialized, first.sent) == (1, 1)
    assert (second.materialized, second.sent) == (0, 0)
    delivery = db.scalar(select(PlannerReminderDelivery))
    assert delivery.status == "sent"
    assert delivery.attempts == 1
    assert delivery.sent_at == NOW.replace(tzinfo=None)
    assert (delivery.planner_reminder_id, delivery.push_subscription_id) == (reminder.id, subscription.id)
    assert len(sender.calls) == 1
    payload = sender.calls[0][1]
    assert payload["planner_item_id"] == item.id
    assert payload["occurrence_key"] == f"{item.id}@2026-09-12"
    assert payload["url"] == "/planner?month=2026-09&day=2026-09-12"
    assert "description" not in payload and "owner_id" not in payload


def test_future_reminder_is_not_materialized_or_sent(db, make_user):
    user = make_user("alice")
    item = add_event(db, user, start_time="10:01")
    add_reminder(item)
    add_subscription(db, user)
    db.commit()
    sender = FakeSender()

    result = cycle(db, sender)

    assert result.materialized == result.sent == 0
    assert sender.calls == []


def test_multiple_reminders_and_devices_deliver_independently(db, make_user):
    user = make_user("alice")
    item = add_event(db, user)
    add_reminder(item, 0)
    add_reminder(item, 1, "hours")
    first = add_subscription(db, user, "phone")
    add_subscription(db, user, "laptop")
    db.commit()
    sender = FakeSender({first.endpoint: [500, 500]})

    result = cycle(db, sender)

    assert result.materialized == 4
    assert result.sent == 2
    assert result.retried == 2
    assert db.query(PlannerReminderDelivery).count() == 4
    assert {delivery.status for delivery in db.query(PlannerReminderDelivery).all()} == {"sent", "retry"}


def test_delivery_is_owner_scoped_without_cross_device_fanout(db, make_user):
    alice = make_user("alice")
    bob = make_user("bob")
    alice_item = add_event(db, alice, title="Alice event")
    bob_item = add_event(db, bob, title="Bob event")
    add_reminder(alice_item)
    add_reminder(bob_item)
    alice_device = add_subscription(db, alice, "alice-phone")
    bob_device = add_subscription(db, bob, "bob-phone")
    db.commit()
    sender = FakeSender()

    result = cycle(db, sender)

    assert (result.materialized, result.sent) == (2, 2)
    delivered = {(endpoint, payload["title"]) for endpoint, payload in sender.calls}
    assert delivered == {
        (alice_device.endpoint, "Alice event"),
        (bob_device.endpoint, "Bob event"),
    }


@pytest.mark.parametrize("status_code", [404, 410])
def test_stale_subscription_is_disabled_without_deleting_reminders(db, make_user, status_code):
    user = make_user("alice")
    item = add_event(db, user)
    reminder = add_reminder(item)
    subscription = add_subscription(db, user)
    db.commit()
    sender = FakeSender({subscription.endpoint: [status_code]})

    result = cycle(db, sender)

    db.refresh(subscription)
    assert result.stale_subscriptions == 1
    assert subscription.disabled_at == NOW.replace(tzinfo=None)
    assert db.get(PlannerReminder, reminder.id) is not None
    assert db.scalar(select(PlannerReminderDelivery)).status == "failed"


def test_transient_failures_retry_on_later_cycles_and_are_bounded(db, make_user):
    user = make_user("alice")
    item = add_event(db, user)
    add_reminder(item)
    subscription = add_subscription(db, user)
    db.commit()
    sender = FakeSender({subscription.endpoint: [500, 500, 500, None]})

    assert cycle(db, sender).retried == 1
    assert cycle(db, sender, NOW + timedelta(minutes=1)).retried == 1
    assert cycle(db, sender, NOW + timedelta(minutes=2)).failed == 1
    assert cycle(db, sender, NOW + timedelta(minutes=3)).sent == 0

    delivery = db.scalar(select(PlannerReminderDelivery))
    assert delivery.status == "failed"
    assert delivery.attempts == 3
    assert len(sender.calls) == 3


def test_recent_catchup_sends_but_old_reminder_is_not_materialized(db, make_user):
    user = make_user("alice")
    recent = add_event(db, user, title="Recent", start_time="09:30")
    add_reminder(recent)
    old = add_event(db, user, title="Old", start_time="08:59")
    add_reminder(old)
    add_subscription(db, user)
    db.commit()
    sender = FakeSender()

    result = cycle(db, sender)

    assert (result.materialized, result.sent) == (1, 1)
    assert sender.calls[0][1]["title"] == "Recent"


def test_monthly_recurring_occurrences_get_distinct_delivery_identity(db, make_user):
    user = make_user("alice")
    item = add_event(db, user, day=date(2026, 1, 31), start_time="10:00", recurrence="monthly")
    add_reminder(item)
    add_subscription(db, user)
    db.commit()
    sender = FakeSender()

    february = datetime(2026, 2, 28, 7, tzinfo=UTC)
    march = datetime(2026, 3, 31, 7, tzinfo=UTC)
    assert cycle(db, sender, february).sent == 1
    assert cycle(db, sender, march).sent == 1

    keys = {delivery.occurrence_key for delivery in db.query(PlannerReminderDelivery).all()}
    assert keys == {f"{item.id}@2026-02-28", f"{item.id}@2026-03-31"}


def test_recurrence_until_prevents_later_delivery(db, make_user):
    user = make_user("alice")
    item = add_event(
        db,
        user,
        day=date(2026, 1, 31),
        start_time="10:00",
        recurrence="monthly",
        until=date(2026, 2, 28),
    )
    add_reminder(item)
    add_subscription(db, user)
    db.commit()
    sender = FakeSender()

    assert cycle(db, sender, datetime(2026, 3, 31, 7, tzinfo=UTC)).sent == 0


def test_materialization_unique_constraint_is_safe(db, make_user):
    user = make_user("alice")
    item = add_event(db, user)
    reminder = add_reminder(item)
    subscription = add_subscription(db, user)
    db.commit()

    assert materialize_due_deliveries(db, NOW - timedelta(minutes=1), NOW + timedelta(microseconds=1)) == 1
    assert materialize_due_deliveries(db, NOW - timedelta(minutes=1), NOW + timedelta(microseconds=1)) == 0
    duplicate = PlannerReminderDelivery(
        planner_reminder_id=reminder.id,
        occurrence_key=f"{item.id}@2026-09-12",
        push_subscription_id=subscription.id,
        due_at=NOW.replace(tzinfo=None),
    )
    db.add(duplicate)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_delivery_claim_is_atomic_across_sessions(db, make_user):
    user = make_user("alice")
    item = add_event(db, user)
    add_reminder(item)
    add_subscription(db, user)
    db.commit()
    materialize_due_deliveries(db, NOW - timedelta(minutes=1), NOW + timedelta(microseconds=1))
    delivery_id = db.scalar(select(PlannerReminderDelivery.id))
    factory = sessionmaker(bind=db.get_bind())

    with factory() as first_worker, factory() as second_worker:
        assert _claim_delivery(first_worker, delivery_id, NOW) is not None
        assert _claim_delivery(second_worker, delivery_id, NOW) is None

    delivery = db.get(PlannerReminderDelivery, delivery_id)
    db.refresh(delivery)
    assert delivery.attempts == 1


def test_reminder_and_series_delete_remove_pending_deliveries(db, make_user):
    user = make_user("alice")
    single_item = add_event(db, user, title="Single reminder")
    reminder = add_reminder(single_item)
    series = add_event(db, user, title="Series", recurrence="weekly")
    add_reminder(series)
    add_subscription(db, user)
    db.commit()
    materialize_due_deliveries(db, NOW - timedelta(minutes=1), NOW + timedelta(microseconds=1))
    assert db.query(PlannerReminderDelivery).count() == 2

    db.delete(reminder)
    db.commit()
    assert db.query(PlannerReminderDelivery).count() == 1
    assert db.get(PlannerItem, single_item.id) is not None

    db.delete(series)
    db.commit()
    assert db.query(PlannerReminderDelivery).count() == 0


def test_user_json_export_keeps_reminders_but_excludes_push_runtime_state(
    client, db, make_user, login
):
    user = make_user("alice")
    item = add_event(db, user)
    add_reminder(item, 15)
    subscription = add_subscription(db, user, "private-device-endpoint")
    subscription.p256dh = "private-p256dh-value"
    subscription.auth = "private-auth-value"
    db.commit()
    materialize_due_deliveries(
        db,
        NOW - timedelta(minutes=20),
        NOW + timedelta(microseconds=1),
    )
    login("alice")

    response = client.get("/export/data.json")

    assert response.status_code == 200
    payload = response.json()
    assert payload["planner"][0]["reminders"] == [
        {"offset_value": 15, "offset_unit": "minutes", "relation": "before_start"}
    ]
    assert "private-device-endpoint" not in response.text
    assert "private-p256dh-value" not in response.text
    assert "private-auth-value" not in response.text
    assert "push_subscriptions" not in response.text
    assert "planner_reminder_deliveries" not in response.text


def test_subscription_api_auth_upsert_multiple_devices_and_soft_disable(
    client, db, make_user, login, monkeypatch
):
    push_settings(monkeypatch)
    alice = make_user("alice")
    make_user("bob")
    payload = {
        "endpoint": "https://fcm.googleapis.com/push/phone",
        "keys": {"p256dh": "first_key", "auth": "first_auth"},
    }
    assert client.post("/api/push/subscriptions", json=payload, follow_redirects=False).status_code == 303

    login("alice")
    assert client.post("/api/push/subscriptions", json=payload).status_code == 200
    payload["keys"] = {"p256dh": "updated_key", "auth": "updated_auth"}
    assert client.post("/api/push/subscriptions", json=payload).status_code == 200
    second = {
        "endpoint": "https://fcm.googleapis.com/push/laptop",
        "keys": {"p256dh": "laptop_key", "auth": "laptop_auth"},
    }
    assert client.post("/api/push/subscriptions", json=second).status_code == 200
    assert db.query(PushSubscription).count() == 2
    phone = db.query(PushSubscription).filter_by(endpoint=payload["endpoint"]).one()
    db.refresh(phone)
    assert phone.user_id == alice.id
    assert phone.p256dh == "updated_key"

    login("bob")
    assert client.post("/api/push/subscriptions/unsubscribe", json={"endpoint": payload["endpoint"]}).status_code == 200
    db.refresh(phone)
    assert phone.disabled_at is None

    login("alice")
    assert client.post("/api/push/subscriptions/unsubscribe", json={"endpoint": payload["endpoint"]}).status_code == 200
    db.refresh(phone)
    assert phone.disabled_at is not None


def test_subscription_api_rejects_invalid_payload_and_unavailable_feature(
    client, db, make_user, login, monkeypatch
):
    make_user("alice")
    login("alice")
    unavailable = client.post(
        "/api/push/subscriptions",
        json={
            "endpoint": "https://fcm.googleapis.com/push/device",
            "keys": {"p256dh": "key", "auth": "auth"},
        },
    )
    assert unavailable.status_code == 503

    push_settings(monkeypatch)
    invalid = client.post(
        "/api/push/subscriptions",
        json={"endpoint": "https://example.test/push", "keys": {"p256dh": "bad key", "auth": ""}},
    )
    assert invalid.status_code == 400
    assert db.query(PushSubscription).count() == 0


def test_missing_vapid_is_safe_and_private_key_is_never_rendered(
    client, make_user, login, monkeypatch
):
    secret = "do-not-render-private-key"
    push_settings(monkeypatch, public_key="", private_key=secret)
    make_user("alice")
    login("alice")

    status = client.get("/api/push/status")
    key = client.get("/api/push/vapid-public-key")
    planner = client.get("/planner?month=2026-09")

    assert status.status_code == key.status_code == planner.status_code == 200
    assert status.json()["available"] is False
    assert key.json()["public_key"] == ""
    assert secret not in status.text + key.text + planner.text
    assert "VAPID-конфигурация не заполнена" in planner.text


def test_planner_push_ui_and_service_worker_are_progressive(client, make_user, login):
    make_user("alice")
    login("alice")

    planner = client.get("/planner?month=2026-09")
    worker = client.get("/service-worker.js")
    base_source = Path("app/templates/base.html").read_text(encoding="utf-8")

    assert 'data-planner-push' in planner.text
    assert 'data-push-enable' in planner.text
    assert "Notification.requestPermission()" in planner.text
    assert base_source.index("btn.addEventListener('click'") < base_source.index("Notification.requestPermission()")
    assert "self.addEventListener('push'" in worker.text
    assert "self.addEventListener('notificationclick'" in worker.text
    assert "clients.openWindow(targetUrl)" in worker.text
    assert "planner_item_id" in worker.text


def test_feature_disabled_cycle_does_not_send(db, monkeypatch):
    push_settings(monkeypatch, enabled=False)
    called = False

    def forbidden_cycle():
        nonlocal called
        called = True

    monkeypatch.setattr(main_module, "run_delivery_cycle", forbidden_cycle)
    main_module.run_planner_push_cycle()
    assert called is False


def test_async_scheduler_task_is_cancellable(monkeypatch):
    cycle_started = asyncio.Event()

    async def fake_to_thread(_function):
        cycle_started.set()

    monkeypatch.setattr(main_module.asyncio, "to_thread", fake_to_thread)

    async def exercise_scheduler():
        task = asyncio.create_task(main_module.planner_push_scheduler())
        await asyncio.wait_for(cycle_started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise_scheduler())


def test_runtime_migration_adds_push_columns_and_delivery_table(tmp_path, monkeypatch):
    legacy_engine = create_engine(f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}")
    Base.metadata.create_all(bind=legacy_engine)
    PlannerReminderDelivery.__table__.drop(bind=legacy_engine)
    with legacy_engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE push_subscriptions")
        connection.exec_driver_sql(
            "CREATE TABLE push_subscriptions ("
            "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, endpoint VARCHAR(600) NOT NULL UNIQUE, "
            "p256dh VARCHAR(300) NOT NULL, auth VARCHAR(120) NOT NULL, user_agent VARCHAR(500), "
            "created_at DATETIME NOT NULL, last_used_at DATETIME NOT NULL)"
        )
    monkeypatch.setattr(main_module, "engine", legacy_engine)

    assert main_module.schema_change_required() is True
    main_module.ensure_runtime_schema()

    columns = {column["name"] for column in inspect(legacy_engine).get_columns("push_subscriptions")}
    assert {"updated_at", "last_seen_at", "disabled_at"} <= columns
    assert "planner_reminder_deliveries" in inspect(legacy_engine).get_table_names()
