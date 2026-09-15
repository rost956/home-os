"""Persistent, private Web Push notifications and daily Fuel digests."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

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
from app.services.planner_push import PushSender, PushSendError
from app.timezone import MSK, msk_day_bounds_utc, now_utc, to_msk

logger = logging.getLogger("home_service.fuel_push")
MAX_DELIVERY_ATTEMPTS = 3
EVENT_MERGE_WINDOW = timedelta(minutes=30)


@dataclass
class FuelPushCycleResult:
    materialized: int = 0
    digests: int = 0
    sent: int = 0
    retried: int = 0
    failed: int = 0
    skipped: int = 0


@dataclass
class FuelDigest:
    day: date
    stations: list[dict[str, Any]] = field(default_factory=list)
    confirmed_appearances: int = 0
    candidates: int = 0
    probable_deliveries: int = 0
    confirmed_deliveries: int = 0
    confirmed_duration_minutes: int = 0

    @property
    def short_body(self) -> str:
        return (
            f"Появлений: {self.confirmed_appearances}; поставок: "
            f"{self.confirmed_deliveries + self.probable_deliveries}; "
            f"подтверждено {self.confirmed_duration_minutes} мин"
        )


def get_or_create_settings(db: Session, user_id: int) -> FuelNotificationSettings:
    value = db.scalar(select(FuelNotificationSettings).where(FuelNotificationSettings.user_id == user_id))
    if value is None:
        value = FuelNotificationSettings(user_id=user_id)
        db.add(value)
        db.flush()
    return value


def _parse_hhmm(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def _quiet_until(settings: FuelNotificationSettings, now: datetime) -> datetime | None:
    if not settings.quiet_hours_enabled:
        return None
    local = (now.replace(tzinfo=UTC) if now.tzinfo is None else now).astimezone(MSK)
    start, end = _parse_hhmm(settings.quiet_hours_start), _parse_hhmm(settings.quiet_hours_end)
    current = local.time().replace(tzinfo=None)
    quiet = start <= current < end if start < end else current >= start or current < end
    if not quiet:
        return None
    end_day = local.date() + timedelta(days=1) if start >= end and current >= start else local.date()
    return datetime.combine(end_day, end, tzinfo=MSK).astimezone(UTC).replace(tzinfo=None)


def _fuel_enabled(subscription: FuelStationSubscription, fuel: str) -> bool:
    return bool(
        subscription.enabled
        and getattr(subscription, f"track_{fuel}", False)
        and getattr(subscription, f"notify_{fuel}", False)
    )


def _event_kind(event: FuelDeliveryEvent, settings: FuelNotificationSettings) -> str | None:
    if event.event_type == "candidate_appearance":
        return "candidate" if settings.notification_level == "candidate_and_confirmed" else None
    if event.event_type == "confirmed_availability":
        return "confirmed"
    if event.event_type in {"probable_delivery", "confirmed_delivery"}:
        return "delivery" if settings.notify_probable_delivery else "confirmed"
    return None


def _event_copy(event: FuelDeliveryEvent, station: FuelStation, kind: str) -> tuple[str, str]:
    station_name = station.address or station.brand or station.name or "АЗС"
    fuel = f"АИ-{event.fuel_type}"
    if kind == "candidate":
        return f"Возможно появился {fuel}", f"{station_name}. Сигнал предварительный — проверьте перед поездкой."
    if kind == "delivery":
        certainty = "Подтверждена" if event.event_type == "confirmed_delivery" else "Вероятна"
        return f"{certainty} поставка {fuel}", station_name
    return f"Появился {fuel}", f"{station_name}. Наличие подтверждено."


def _add_unique(db: Session, notification: FuelNotification) -> bool:
    try:
        with db.begin_nested():
            db.add(notification)
            db.flush()
        return True
    except IntegrityError:
        return False


def materialize_event_notifications(db: Session, now: datetime) -> int:
    now = now.replace(tzinfo=None)
    rows = db.execute(
        select(FuelDeliveryEvent, FuelStationSubscription, FuelNotificationSettings, FuelStation)
        .join(FuelStationSubscription, FuelStationSubscription.station_id == FuelDeliveryEvent.station_id)
        .join(FuelNotificationSettings, FuelNotificationSettings.user_id == FuelStationSubscription.user_id)
        .join(FuelStation, FuelStation.id == FuelDeliveryEvent.station_id)
        .where(
            FuelNotificationSettings.notifications_enabled.is_(True),
            FuelStationSubscription.enabled.is_(True),
            FuelDeliveryEvent.estimated_at <= now,
        )
    ).all()
    created = 0
    for event, subscription, preferences, station in rows:
        watermark = max(
            (value for value in (preferences.notifications_enabled_at, subscription.notifications_enabled_at) if value),
            default=None,
        )
        if watermark is None or event.estimated_at < watermark or not _fuel_enabled(subscription, event.fuel_type):
            continue
        kind = _event_kind(event, preferences)
        if kind is None:
            continue
        identity = f"fuel-event:{subscription.user_id}:{event.id}:{kind}"
        title, body = _event_copy(event, station, kind)
        quiet_until = _quiet_until(preferences, now)
        status = "deferred" if quiet_until else "pending"
        skip_reason = None
        # A delivery classification shortly after the same appearance updates the browser notification.
        if kind == "delivery":
            prior = db.scalar(select(FuelNotification).where(
                FuelNotification.user_id == subscription.user_id,
                FuelNotification.event_id == event.id,
                FuelNotification.notification_type.in_(("candidate", "confirmed")),
                FuelNotification.created_at >= now - EVENT_MERGE_WINDOW,
            ))
            if prior:
                status, skip_reason = "skipped", "merged_with_recent_appearance"
        notification = FuelNotification(
            identity_key=identity,
            user_id=subscription.user_id,
            station_id=event.station_id,
            fuel_type=event.fuel_type,
            event_id=event.id,
            notification_type=kind,
            status=status,
            available_after=quiet_until or now,
            tag=f"fuel-{event.station_id}-{event.fuel_type}",
            title=title,
            body=body,
            internal_url=f"/fuel/{event.station_id}",
            skip_reason=skip_reason,
        )
        created += int(_add_unique(db, notification))
    db.commit()
    return created


def build_daily_fuel_digest(
    db: Session,
    user_id: int,
    day: date,
    *,
    now: datetime | None = None,
    stale_after_minutes: int = 120,
) -> FuelDigest:
    now = (now or now_utc()).replace(tzinfo=None)
    start, end = msk_day_bounds_utc(day)
    digest = FuelDigest(day=day)
    subscriptions = db.scalars(select(FuelStationSubscription).where(
        FuelStationSubscription.user_id == user_id,
        FuelStationSubscription.enabled.is_(True),
    )).all()
    for subscription in subscriptions:
        station = db.get(FuelStation, subscription.station_id)
        row = {"station_id": subscription.station_id, "name": station.address or station.brand or "АЗС", "fuels": []}
        for fuel in subscription.tracked_fuel_types:
            events = db.scalars(select(FuelDeliveryEvent).where(
                FuelDeliveryEvent.station_id == subscription.station_id,
                FuelDeliveryEvent.fuel_type == fuel,
                FuelDeliveryEvent.estimated_at >= start,
                FuelDeliveryEvent.estimated_at <= end,
            )).all()
            digest.candidates += sum(item.event_type == "candidate_appearance" for item in events)
            digest.confirmed_appearances += sum(item.event_type != "candidate_appearance" for item in events)
            digest.probable_deliveries += sum(item.event_type == "probable_delivery" for item in events)
            digest.confirmed_deliveries += sum(item.event_type == "confirmed_delivery" for item in events)
            for item in events:
                if item.event_type == "candidate_appearance":
                    continue
                interval_start = max(item.estimated_at, start)
                recorded_end = item.disappeared_at
                if recorded_end is None and item.availability_duration_minutes is not None:
                    recorded_end = item.estimated_at + timedelta(minutes=item.availability_duration_minutes)
                interval_end = min(recorded_end or now, end, now)
                if interval_end > interval_start:
                    digest.confirmed_duration_minutes += round(
                        (interval_end - interval_start).total_seconds() / 60
                    )
            latest = db.scalar(select(FuelObservation).where(
                FuelObservation.station_id == subscription.station_id,
                FuelObservation.fuel_type == fuel,
                FuelObservation.observed_at <= now,
            ).order_by(FuelObservation.observed_at.desc()).limit(1))
            if latest is None or latest.is_stale or latest.observed_at < now - timedelta(minutes=stale_after_minutes):
                state = "insufficient_data"
            else:
                state = latest.state
            row["fuels"].append({"fuel_type": fuel, "state": state})
        digest.stations.append(row)
    return digest


def materialize_due_digests(db: Session, now: datetime, stale_after_minutes: int) -> int:
    now = now.replace(tzinfo=None)
    local = to_msk(now)
    created = 0
    settings_rows = db.scalars(select(FuelNotificationSettings).where(
        FuelNotificationSettings.notifications_enabled.is_(True),
        FuelNotificationSettings.daily_digest_enabled.is_(True),
    )).all()
    for preferences in settings_rows:
        if local.time().replace(tzinfo=None) < _parse_hhmm(preferences.daily_digest_time):
            continue
        digest = build_daily_fuel_digest(db, preferences.user_id, local.date(), now=now, stale_after_minutes=stale_after_minutes)
        created += int(_add_unique(db, FuelNotification(
            identity_key=f"fuel-digest:{preferences.user_id}:{local.date().isoformat()}",
            user_id=preferences.user_id,
            notification_type="daily_digest",
            local_date=local.date(),
            status="pending",
            available_after=now,
            tag=f"fuel-digest-{preferences.user_id}",
            title="Бензин · итоги дня",
            body=digest.short_body,
            internal_url="/fuel",
        )))
    db.commit()
    return created


def _event_still_valid(db: Session, notification: FuelNotification, now: datetime, stale_after_minutes: int) -> bool:
    preferences = db.scalar(select(FuelNotificationSettings).where(
        FuelNotificationSettings.user_id == notification.user_id,
        FuelNotificationSettings.notifications_enabled.is_(True),
    ))
    if notification.notification_type == "daily_digest":
        return bool(preferences and preferences.daily_digest_enabled and notification.local_date == to_msk(now).date())
    subscription = db.scalar(select(FuelStationSubscription).where(
        FuelStationSubscription.user_id == notification.user_id,
        FuelStationSubscription.station_id == notification.station_id,
    ))
    event = db.get(FuelDeliveryEvent, notification.event_id)
    if not preferences or not subscription or not event or event.disappeared_at is not None:
        return False
    if not _fuel_enabled(subscription, notification.fuel_type or ""):
        return False
    latest = db.scalar(select(FuelObservation).where(
        FuelObservation.station_id == notification.station_id,
        FuelObservation.fuel_type == notification.fuel_type,
    ).order_by(FuelObservation.observed_at.desc()).limit(1))
    return bool(
        latest and latest.state in {"available", "low"} and not latest.is_stale
        and latest.observed_at >= now - timedelta(minutes=stale_after_minutes)
    )


def deliver_pending(
    db: Session, sender: PushSender, now: datetime, *, stale_after_minutes: int
) -> FuelPushCycleResult:
    now = now.replace(tzinfo=None)
    result = FuelPushCycleResult()
    notifications = db.scalars(select(FuelNotification).where(
        FuelNotification.status.in_(("pending", "deferred")),
        FuelNotification.available_after <= now,
    ).order_by(FuelNotification.available_after, FuelNotification.id)).all()
    for notification in notifications:
        if not _event_still_valid(db, notification, now, stale_after_minutes):
            notification.status, notification.skip_reason = "skipped", "event_no_longer_active"
            result.skipped += 1
            db.commit()
            continue
        devices = db.scalars(select(PushSubscription).where(
            PushSubscription.user_id == notification.user_id,
            PushSubscription.disabled_at.is_(None),
        )).all()
        if not devices:
            notification.status, notification.skip_reason = "skipped", "no_active_devices"
            result.skipped += 1
            db.commit()
            continue
        for device in devices:
            delivery = db.scalar(select(FuelNotificationDelivery).where(
                FuelNotificationDelivery.notification_id == notification.id,
                FuelNotificationDelivery.push_subscription_id == device.id,
            ))
            if delivery is None:
                delivery = FuelNotificationDelivery(notification_id=notification.id, push_subscription_id=device.id)
                db.add(delivery)
                db.flush()
            if delivery.status not in {"pending", "retry"} or delivery.attempts >= MAX_DELIVERY_ATTEMPTS:
                continue
            delivery.attempts += 1
            delivery.last_attempt_at = now
            payload = {
                "title": notification.title, "body": notification.body,
                "url": notification.internal_url, "tag": notification.tag,
                "timestamp": int(now.replace(tzinfo=UTC).timestamp() * 1000),
                "icon": "/static/icon-192.png", "badge": "/static/icon-192.png",
            }
            try:
                sender.send(device, payload)
                delivery.status, delivery.sent_at, delivery.last_error = "sent", now, None
                device.last_success_at, device.failure_count = now, 0
                result.sent += 1
            except PushSendError as exc:
                delivery.last_error = f"HTTP {exc.status_code}" if exc.status_code else str(exc)[:500]
                device.last_failure_at, device.failure_count = now, device.failure_count + 1
                if exc.status_code in {404, 410}:
                    device.disabled_at, delivery.status = now, "failed"
                    result.failed += 1
                elif delivery.attempts >= MAX_DELIVERY_ATTEMPTS:
                    delivery.status = "failed"
                    result.failed += 1
                else:
                    delivery.status = "retry"
                    result.retried += 1
                logger.warning("Fuel push failed notification_id=%s device_id=%s status=%s", notification.id, device.id, exc.status_code)
            db.commit()
        deliveries = db.scalars(select(FuelNotificationDelivery).where(
            FuelNotificationDelivery.notification_id == notification.id
        )).all()
        if any(item.status in {"pending", "retry"} for item in deliveries):
            notification.status = "pending"
        elif any(item.status == "sent" for item in deliveries):
            notification.status, notification.sent_at = "sent", now
        elif deliveries and all(item.status == "failed" for item in deliveries):
            notification.status, notification.skip_reason = "skipped", "all_devices_failed"
        db.commit()
    return result


def run_fuel_notification_cycle(
    db: Session,
    sender: PushSender,
    *,
    stale_after_minutes: int = 120,
    clock: Callable[[], datetime] = now_utc,
) -> FuelPushCycleResult:
    now = clock().replace(tzinfo=None)
    result = FuelPushCycleResult()
    result.materialized = materialize_event_notifications(db, now)
    result.digests = materialize_due_digests(db, now, stale_after_minutes)
    sent = deliver_pending(db, sender, now, stale_after_minutes=stale_after_minutes)
    result.sent, result.retried, result.failed, result.skipped = sent.sent, sent.retried, sent.failed, sent.skipped
    return result
