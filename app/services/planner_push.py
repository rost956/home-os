"""Persistent, retryable Web Push delivery for Planner reminders."""

from __future__ import annotations

import json
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Callable, Protocol

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models import PlannerReminder, PlannerReminderDelivery, PushSubscription
from app.services.planner_reminders import due_planner_reminders, reminder_label
from app.timezone import now_utc

logger = logging.getLogger("home_service.planner_push")
MAX_DELIVERY_ATTEMPTS = 3
CLAIM_TIMEOUT = timedelta(minutes=5)


class PushSender(Protocol):
    def send(self, subscription: PushSubscription, payload: dict[str, Any]) -> None: ...


class PushSendError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class PyWebPushSender:
    def __init__(self, webpush_func, private_key: str, subject: str):
        self.webpush_func = webpush_func
        self.private_key = private_key
        self.subject = subject

    def send(self, subscription: PushSubscription, payload: dict[str, Any]) -> None:
        try:
            self.webpush_func(
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
                },
                data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=self.private_key,
                vapid_claims={"sub": self.subject},
                ttl=24 * 60 * 60,
                timeout=12,
            )
        except Exception as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            raise PushSendError(type(exc).__name__, status_code=status_code) from exc


@dataclass
class DeliveryCycleResult:
    materialized: int = 0
    sent: int = 0
    retried: int = 0
    failed: int = 0
    stale_subscriptions: int = 0


def utc_aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def utc_naive(value: datetime) -> datetime:
    return utc_aware(value).replace(tzinfo=None)


def planner_notification_payload(delivery: PlannerReminderDelivery) -> dict[str, Any]:
    reminder = delivery.planner_reminder
    item = reminder.planner_item
    occurrence_day = date.fromisoformat(delivery.occurrence_key.rsplit("@", 1)[-1])
    due_at = utc_aware(delivery.due_at)
    return {
        "title": item.title,
        "body": "Событие начинается сейчас" if reminder.offset_value == 0 else reminder_label(reminder),
        "url": f"/planner?month={occurrence_day:%Y-%m}&day={occurrence_day.isoformat()}",
        "planner_item_id": item.id,
        "occurrence_key": delivery.occurrence_key,
        "tag": f"planner-reminder-{reminder.id}-{delivery.occurrence_key}",
        "timestamp": int(due_at.timestamp() * 1000),
        "icon": "/static/icon-192.png",
        "badge": "/static/icon-192.png",
    }


def materialize_due_deliveries(
    db: Session,
    window_start: datetime,
    window_end: datetime,
) -> int:
    subscriptions = db.scalars(
        select(PushSubscription).where(PushSubscription.disabled_at.is_(None))
    ).all()
    by_owner: dict[int, list[PushSubscription]] = defaultdict(list)
    for subscription in subscriptions:
        by_owner[subscription.user_id].append(subscription)
    created = 0
    for owner_id, owner_subscriptions in by_owner.items():
        for due in due_planner_reminders(db, owner_id, window_start, window_end):
            for subscription in owner_subscriptions:
                try:
                    with db.begin_nested():
                        db.add(
                            PlannerReminderDelivery(
                                planner_reminder_id=due.reminder_id,
                                occurrence_key=due.occurrence_key,
                                push_subscription_id=subscription.id,
                                due_at=utc_naive(due.due_at),
                            )
                        )
                        db.flush()
                    created += 1
                except IntegrityError:
                    pass
    db.commit()
    return created


def _claim_delivery(db: Session, delivery_id: int, now: datetime) -> str | None:
    token = str(uuid.uuid4())
    stale_before = utc_naive(now - CLAIM_TIMEOUT)
    result = db.execute(
        update(PlannerReminderDelivery)
        .where(
            PlannerReminderDelivery.id == delivery_id,
            PlannerReminderDelivery.status.in_(("pending", "retry")),
            PlannerReminderDelivery.sent_at.is_(None),
            PlannerReminderDelivery.attempts < MAX_DELIVERY_ATTEMPTS,
            or_(
                PlannerReminderDelivery.claim_token.is_(None),
                PlannerReminderDelivery.claimed_at < stale_before,
            ),
        )
        .values(
            claim_token=token,
            claimed_at=utc_naive(now),
            last_attempt_at=utc_naive(now),
            attempts=PlannerReminderDelivery.attempts + 1,
        )
    )
    db.commit()
    return token if result.rowcount == 1 else None


def deliver_pending(
    db: Session,
    sender: PushSender,
    now: datetime,
    catchup: timedelta,
) -> DeliveryCycleResult:
    result = DeliveryCycleResult()
    now_db = utc_naive(now)
    candidate_ids = db.scalars(
        select(PlannerReminderDelivery.id)
        .join(PlannerReminderDelivery.push_subscription)
        .where(
            PlannerReminderDelivery.status.in_(("pending", "retry")),
            PlannerReminderDelivery.sent_at.is_(None),
            PlannerReminderDelivery.attempts < MAX_DELIVERY_ATTEMPTS,
            PlannerReminderDelivery.due_at >= utc_naive(now - catchup),
            PlannerReminderDelivery.due_at <= now_db,
            PushSubscription.disabled_at.is_(None),
        )
        .order_by(PlannerReminderDelivery.due_at, PlannerReminderDelivery.id)
    ).all()
    for delivery_id in candidate_ids:
        token = _claim_delivery(db, delivery_id, now)
        if not token:
            continue
        delivery = db.scalar(
            select(PlannerReminderDelivery)
            .options(
                selectinload(PlannerReminderDelivery.push_subscription),
                selectinload(PlannerReminderDelivery.planner_reminder).selectinload(PlannerReminder.planner_item),
            )
            .where(
                PlannerReminderDelivery.id == delivery_id,
                PlannerReminderDelivery.claim_token == token,
            )
        )
        if delivery is None:
            continue
        if delivery.push_subscription.disabled_at is not None:
            delivery.status = "failed"
            delivery.last_error = "Subscription disabled"
            delivery.claim_token = None
            delivery.claimed_at = None
            result.failed += 1
            db.commit()
            continue
        try:
            sender.send(delivery.push_subscription, planner_notification_payload(delivery))
            delivery.status = "sent"
            delivery.sent_at = now_db
            delivery.last_error = None
            result.sent += 1
            logger.info("Planner reminder push sent delivery_id=%s", delivery.id)
        except PushSendError as exc:
            delivery.last_error = f"HTTP {exc.status_code}" if exc.status_code else str(exc)[:500]
            if exc.status_code in {404, 410}:
                delivery.push_subscription.disabled_at = now_db
                delivery.status = "failed"
                result.stale_subscriptions += 1
                result.failed += 1
                logger.warning("Planner push subscription disabled delivery_id=%s status=%s", delivery.id, exc.status_code)
            elif delivery.attempts >= MAX_DELIVERY_ATTEMPTS:
                delivery.status = "failed"
                result.failed += 1
                logger.warning("Planner push retries exhausted delivery_id=%s", delivery.id)
            else:
                delivery.status = "retry"
                result.retried += 1
                logger.warning("Planner push transient failure delivery_id=%s", delivery.id)
        finally:
            delivery.claim_token = None
            delivery.claimed_at = None
            db.commit()
    return result


def run_delivery_cycle(
    db: Session,
    sender: PushSender,
    catchup: timedelta,
    clock: Callable[[], datetime] = now_utc,
) -> DeliveryCycleResult:
    now = utc_aware(clock())
    scan_start = now - catchup
    scan_end = now + timedelta(microseconds=1)
    materialized = materialize_due_deliveries(db, scan_start, scan_end)
    result = deliver_pending(db, sender, now, catchup)
    result.materialized = materialized
    return result
