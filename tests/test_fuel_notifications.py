from datetime import datetime, timedelta

from app.models import (
    FuelDeliveryEvent,
    FuelNotification,
    FuelNotificationDelivery,
    FuelNotificationSettings,
    FuelObservation,
    FuelStation,
    FuelStationSubscription,
    PushSubscription,
)
from app.services.fuel_notifications import (
    build_daily_fuel_digest,
    materialize_due_digests,
    materialize_event_notifications,
    run_fuel_notification_cycle,
)
from app.services.planner_push import PushSendError


class RecordingSender:
    def __init__(self, failure_status=None):
        self.payloads = []
        self.failure_status = failure_status

    def send(self, subscription, payload):
        if self.failure_status:
            raise PushSendError("failed", self.failure_status)
        self.payloads.append((subscription.id, payload))


def setup_event(db, user, *, event_type="confirmed_availability", fuel="95", now=None, notify=True):
    now = now or datetime(2026, 9, 15, 12)
    station = FuelStation(
        owner_id=user.id, provider="gdebenz", provider_station_id=f"station-{user.id}",
        brand="Teboil", address="пр-кт Ветеранов, 188/1", latitude=59.8, longitude=30.1,
    )
    db.add(station)
    db.flush()
    subscription = FuelStationSubscription(
        user_id=user.id, station_id=station.id, track_95=True, track_98=False, track_100=False,
        notify_95=notify, notifications_enabled_at=now - timedelta(minutes=1),
    )
    preferences = FuelNotificationSettings(
        user_id=user.id, notifications_enabled=True, notifications_enabled_at=now - timedelta(minutes=1)
    )
    before = FuelObservation(station_id=station.id, fuel_type=fuel, state="unavailable", observed_at=now - timedelta(minutes=2))
    after = FuelObservation(station_id=station.id, fuel_type=fuel, state="available", observed_at=now)
    db.add_all([subscription, preferences, before, after])
    db.flush()
    event = FuelDeliveryEvent(
        station_id=station.id, fuel_type=fuel, window_start=before.observed_at,
        window_end=after.observed_at, estimated_at=now, event_type=event_type,
        confidence=.9, appearance_confidence=.9, before_observation_id=before.id,
        after_observation_id=after.id, detection_reason="test", evidence_json={}, detector_version="test",
    )
    db.add(event)
    db.commit()
    return station, subscription, preferences, event


def add_device(db, user, suffix="phone"):
    device = PushSubscription(
        user_id=user.id, endpoint=f"https://fcm.googleapis.com/push/{suffix}", p256dh="key", auth="auth"
    )
    db.add(device)
    db.commit()
    return device


def test_confirmed_event_is_persistent_deduplicated_and_sent(db, make_user):
    user = make_user("fuel-push")
    _station, _subscription, _preferences, event = setup_event(db, user)
    add_device(db, user)
    sender = RecordingSender()
    def clock():
        return datetime(2026, 9, 15, 12, 1)
    first = run_fuel_notification_cycle(db, sender, clock=clock)
    second = run_fuel_notification_cycle(db, sender, clock=clock)
    assert first.materialized == 1
    assert first.sent == 1
    assert second.materialized == second.sent == 0
    notification = db.query(FuelNotification).one()
    assert notification.event_id == event.id
    assert notification.status == "sent"
    assert db.query(FuelNotificationDelivery).count() == 1
    assert sender.payloads[0][1]["url"].startswith("/fuel/")


def test_candidate_requires_opt_in_and_preferences_are_private(db, make_user):
    first = make_user("fuel-candidate-a")
    second = make_user("fuel-candidate-b")
    station, subscription, preferences, event = setup_event(db, first, event_type="candidate_appearance")
    db.add(FuelStationSubscription(
        user_id=second.id, station_id=station.id, track_95=True, track_98=False, track_100=False,
        notify_95=True, notifications_enabled_at=event.estimated_at - timedelta(minutes=1),
    ))
    db.add(FuelNotificationSettings(
        user_id=second.id, notifications_enabled=True, notification_level="candidate_and_confirmed",
        notifications_enabled_at=event.estimated_at - timedelta(minutes=1),
    ))
    db.commit()
    assert materialize_event_notifications(db, event.estimated_at) == 1
    assert db.query(FuelNotification).one().user_id == second.id
    preferences.notification_level = "candidate_and_confirmed"
    db.commit()
    assert materialize_event_notifications(db, event.estimated_at) == 1
    assert {row.user_id for row in db.query(FuelNotification)} == {first.id, second.id}


def test_activation_watermark_prevents_historic_push(db, make_user):
    user = make_user("fuel-watermark")
    _station, _subscription, preferences, event = setup_event(db, user)
    preferences.notifications_enabled_at = event.estimated_at + timedelta(seconds=1)
    db.commit()
    assert materialize_event_notifications(db, event.estimated_at + timedelta(minutes=5)) == 0


def test_quiet_notification_is_revalidated_and_skipped(db, make_user):
    user = make_user("fuel-quiet")
    _station, _subscription, preferences, event = setup_event(db, user, now=datetime(2026, 9, 15, 21))
    preferences.quiet_hours_enabled = True
    preferences.quiet_hours_start = "23:00"
    preferences.quiet_hours_end = "07:00"
    add_device(db, user)
    db.commit()
    # 21:00 UTC is midnight in Moscow and therefore quiet.
    assert materialize_event_notifications(db, event.estimated_at) == 1
    notification = db.query(FuelNotification).one()
    assert notification.status == "deferred"
    event.disappeared_at = datetime(2026, 9, 16, 1)
    db.commit()
    result = run_fuel_notification_cycle(db, RecordingSender(), clock=lambda: datetime(2026, 9, 16, 4, 1))
    assert result.skipped == 1
    assert notification.status == "skipped"


def test_gone_device_is_disabled_without_affecting_other_device(db, make_user):
    user = make_user("fuel-gone")
    setup_event(db, user)
    first = add_device(db, user, "first")
    second = add_device(db, user, "second")

    class SelectiveSender(RecordingSender):
        def send(self, subscription, payload):
            if subscription.id == first.id:
                raise PushSendError("gone", 410)
            super().send(subscription, payload)

    result = run_fuel_notification_cycle(db, SelectiveSender(), clock=lambda: datetime(2026, 9, 15, 12, 1))
    assert result.sent == result.failed == 1
    assert db.get(PushSubscription, first.id).disabled_at is not None
    assert db.get(PushSubscription, second.id).disabled_at is None


def test_transient_failure_retries_only_failed_device(db, make_user):
    user = make_user("fuel-retry")
    setup_event(db, user)
    first = add_device(db, user, "retry-first")
    second = add_device(db, user, "retry-second")

    class OnceSender(RecordingSender):
        failed_once = False

        def send(self, subscription, payload):
            if subscription.id == first.id and not self.failed_once:
                self.failed_once = True
                raise PushSendError("temporary", 503)
            super().send(subscription, payload)

    sender = OnceSender()
    first_cycle = run_fuel_notification_cycle(db, sender, clock=lambda: datetime(2026, 9, 15, 12, 1))
    second_cycle = run_fuel_notification_cycle(db, sender, clock=lambda: datetime(2026, 9, 15, 12, 2))
    assert first_cycle.sent == first_cycle.retried == 1
    assert second_cycle.sent == 1
    assert [device_id for device_id, _payload in sender.payloads].count(second.id) == 1
    assert db.query(FuelNotification).one().status == "sent"


def test_digest_does_not_treat_unknown_as_absence(db, make_user):
    user = make_user("fuel-digest")
    station, _subscription, _preferences, _event = setup_event(db, user)
    db.add(FuelObservation(
        station_id=station.id, fuel_type="95", state="unknown", observed_at=datetime(2026, 9, 15, 18)
    ))
    db.commit()
    digest = build_daily_fuel_digest(
        db, user.id, datetime(2026, 9, 15).date(), now=datetime(2026, 9, 15, 18, 5)
    )
    assert digest.stations[0]["fuels"][0]["state"] == "unknown"


def test_daily_digest_is_once_per_moscow_day_and_ignores_quiet_hours(db, make_user):
    user = make_user("fuel-digest-once")
    _station, _subscription, preferences, _event = setup_event(db, user)
    preferences.daily_digest_enabled = True
    preferences.daily_digest_time = "21:00"
    preferences.quiet_hours_enabled = True
    preferences.quiet_hours_start = "20:00"
    preferences.quiet_hours_end = "07:00"
    db.commit()
    # 18:05 UTC is 21:05 in Moscow.
    now = datetime(2026, 9, 15, 18, 5)
    assert materialize_due_digests(db, now, 120) == 1
    assert materialize_due_digests(db, now + timedelta(minutes=5), 120) == 0
    digest_notification = db.query(FuelNotification).filter_by(notification_type="daily_digest").one()
    assert digest_notification.status == "pending"
    assert digest_notification.available_after == now


def test_service_worker_reuses_safe_internal_navigation_and_cache_version(client):
    worker = client.get("/service-worker.js")
    base = open("app/templates/base.html", encoding="utf-8").read()
    assert "payload.internal_url || payload.url" in worker.text
    assert "requestedUrl.origin === self.location.origin" in worker.text
    assert "/service-worker.js?v=3" in base
    assert "/service-worker.js?v=2" not in base


def test_fuel_notification_settings_and_station_flags_are_owned(client, db, make_user, login):
    user = make_user("fuel-notification-ui")
    other = make_user("fuel-notification-other")
    station, subscription, _preferences, _event = setup_event(db, user, notify=False)
    login(user.username)
    response = client.post("/fuel/notification-settings", data={
        "notifications_enabled": "on", "notification_level": "candidate_and_confirmed",
        "quiet_hours_enabled": "on", "quiet_hours_start": "23:00", "quiet_hours_end": "07:00",
        "daily_digest_enabled": "on", "daily_digest_time": "20:30",
    }, follow_redirects=False)
    assert response.status_code == 303
    db.refresh(subscription)
    response = client.post(f"/fuel/{station.id}/settings", data={
        "enabled": "on", "fuel_types": ["95"], "notify_fuel_types": ["95", "100"],
    }, follow_redirects=False)
    assert response.status_code == 303
    db.refresh(subscription)
    assert subscription.notify_95 is True
    assert subscription.notify_100 is False
    assert subscription.notifications_enabled_at is not None
    assert db.query(FuelNotificationSettings).filter_by(user_id=other.id).count() == 0
    page = client.get("/fuel/settings")
    assert "Тест на всех устройствах" in page.text
    assert "Web Push не настроен на сервере" in page.text or "data-push-available=\"0\"" in page.text
