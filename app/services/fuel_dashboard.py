"""Batch-built presentation data for the private Fuel dashboard."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import FuelDeliveryEvent, FuelObservation, FuelStationMark, FuelStationSubscription
from app.services.fuel_availability import evaluate_fuel_availability
from app.timezone import msk_day_bounds_utc, now_utc, to_msk

STATUS_LABELS = {
    "available": "ЕСТЬ",
    "candidate": "ВОЗМОЖНО",
    "unavailable": "НЕТ",
    "unknown": "НЕТ ДАННЫХ",
}
STATUS_SYMBOLS = {"available": "✓", "candidate": "?", "unavailable": "×", "unknown": "·"}


@dataclass
class FuelDashboardData:
    stations: list[dict] = field(default_factory=list)
    brands: list[str] = field(default_factory=list)
    last_updated_at: datetime | None = None
    confirmed_appearances: int = 0
    candidates: int = 0
    probable_deliveries: int = 0
    confirmed_deliveries: int = 0
    confirmed_duration_minutes: int = 0


def _summary_state(states: list[str]) -> str:
    if "available" in states:
        return "available"
    if "candidate" in states:
        return "candidate"
    if states and all(state == "unavailable" for state in states):
        return "unavailable"
    return "unknown"


def _age_label(value: datetime | None, current_at: datetime) -> str:
    if value is None:
        return "Ожидаем первый опрос"
    minutes = max(0, int((current_at - value).total_seconds() // 60))
    if minutes < 1:
        return "Обновлено только что"
    if minutes < 60:
        return f"Обновлено {minutes} мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"Обновлено {hours} ч назад"
    return f"Обновлено {hours // 24} дн назад"


def load_fuel_dashboard(
    db: Session,
    user_id: int,
    *,
    stale_after_minutes: int,
    current_at: datetime | None = None,
) -> FuelDashboardData:
    """Load all dashboard data in bounded batch queries, scoped to one user's subscriptions."""
    current_at = (current_at or now_utc()).replace(tzinfo=None)
    subscriptions = db.scalars(
        select(FuelStationSubscription)
        .options(selectinload(FuelStationSubscription.station))
        .where(
            FuelStationSubscription.user_id == user_id,
            FuelStationSubscription.enabled.is_(True),
        )
        .order_by(FuelStationSubscription.updated_at.desc())
    ).all()
    station_ids = [item.station_id for item in subscriptions]
    if not station_ids:
        return FuelDashboardData()

    stale_before = current_at - timedelta(minutes=stale_after_minutes)
    observations = db.scalars(
        select(FuelObservation).where(
            FuelObservation.station_id.in_(station_ids),
            FuelObservation.observed_at >= stale_before,
            FuelObservation.observed_at <= current_at,
        )
    ).all()
    observations_by_pair: dict[tuple[int, str], list[FuelObservation]] = {}
    for item in observations:
        observations_by_pair.setdefault((item.station_id, item.fuel_type), []).append(item)
    marks = db.scalars(
        select(FuelStationMark).where(
            FuelStationMark.station_id.in_(station_ids),
            or_(
                FuelStationMark.source_created_at >= stale_before,
                and_(
                    FuelStationMark.source_created_at.is_(None),
                    FuelStationMark.fetched_at >= stale_before,
                ),
            ),
        )
    ).all()
    marks_by_station: dict[int, list[FuelStationMark]] = {}
    for mark in marks:
        marks_by_station.setdefault(mark.station_id, []).append(mark)

    day_start, _day_end = msk_day_bounds_utc(to_msk(current_at).date())
    events = db.scalars(
        select(FuelDeliveryEvent)
        .where(
            FuelDeliveryEvent.station_id.in_(station_ids),
            FuelDeliveryEvent.estimated_at <= current_at,
            or_(
                FuelDeliveryEvent.estimated_at >= day_start,
                FuelDeliveryEvent.disappeared_at.is_(None),
                FuelDeliveryEvent.disappeared_at > current_at,
            ),
        )
        .order_by(FuelDeliveryEvent.estimated_at.desc())
    ).all()
    result = FuelDashboardData()
    visible_pairs = {
        (subscription.station_id, fuel_type)
        for subscription in subscriptions
        for fuel_type in subscription.tracked_fuel_types
    }
    today_events = [
        event for event in events
        if event.estimated_at >= day_start and (event.station_id, event.fuel_type) in visible_pairs
    ]
    result.candidates = sum(event.event_type == "candidate_appearance" for event in today_events)
    result.confirmed_appearances = sum(event.event_type != "candidate_appearance" for event in today_events)
    result.probable_deliveries = sum(event.event_type == "probable_delivery" for event in today_events)
    result.confirmed_deliveries = sum(event.event_type == "confirmed_delivery" for event in today_events)
    for event in today_events:
        if event.event_type == "candidate_appearance":
            continue
        interval_start = max(event.estimated_at, day_start)
        recorded_end = event.disappeared_at
        if recorded_end is None and event.availability_duration_minutes is not None:
            recorded_end = event.estimated_at + timedelta(minutes=event.availability_duration_minutes)
        interval_end = min(recorded_end or current_at, current_at)
        if interval_end > interval_start:
            result.confirmed_duration_minutes += round((interval_end - interval_start).total_seconds() / 60)

    updated_values: list[datetime] = []
    brand_values: set[str] = set()
    for subscription in subscriptions:
        station = subscription.station
        brand = (station.brand or "").strip()
        if brand:
            brand_values.add(brand)
        fuels = []
        station_availability_times: list[datetime] = []
        for fuel_type in subscription.tracked_fuel_types:
            availability = evaluate_fuel_availability(
                observations_by_pair.get((station.id, fuel_type), ()),
                marks_by_station.get(station.id, ()),
                fuel_type,
                current_at=current_at,
                stale_after_minutes=stale_after_minutes,
            )
            state = availability.state
            if availability.observed_at:
                updated_values.append(availability.observed_at)
                station_availability_times.append(availability.observed_at)
            fuels.append({
                "fuel_type": fuel_type,
                "state": state,
                "label": STATUS_LABELS[state],
                "symbol": STATUS_SYMBOLS[state],
                "observed_at": availability.observed_at.isoformat() if availability.observed_at else None,
                "has_queue": availability.has_queue,
                "explanation": list(availability.explanation),
            })
        states = [item["state"] for item in fuels]
        station_updated_at = max(station_availability_times, default=None)
        result.stations.append({
            "id": station.id,
            "brand": brand,
            "name": station.brand or station.name or "АЗС",
            "address": station.address or "Адрес не указан",
            "latitude": station.latitude,
            "longitude": station.longitude,
            "updated_at": station_updated_at.isoformat() if station_updated_at else None,
            "updated_text": _age_label(station_updated_at, current_at),
            "updated_epoch": int(station_updated_at.timestamp()) if station_updated_at else 0,
            "summary_state": _summary_state(states),
            "fuels": fuels,
        })
    result.brands = sorted(brand_values, key=str.casefold)
    result.last_updated_at = max(updated_values, default=None)
    return result
