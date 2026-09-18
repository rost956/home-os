"""Persistence, lifecycle and honest metrics for saved vehicle trips."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Vehicle,
    VehicleFuelEntry,
    VehicleTrip,
    VehicleTripFuelEntry,
    VehicleTripPlannedStop,
)

POLYLINE_PRECISION = 100_000
VALID_TRANSITIONS = {
    "planned": {"active", "cancelled"},
    "active": {"completed", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}


class TripValidationError(ValueError):
    """A user-correctable trip lifecycle or data validation error."""


def _encode_value(value: int) -> str:
    value = ~(value << 1) if value < 0 else value << 1
    chunks: list[str] = []
    while value >= 0x20:
        chunks.append(chr((0x20 | (value & 0x1F)) + 63))
        value >>= 5
    chunks.append(chr(value + 63))
    return "".join(chunks)


def encode_polyline(points: list[list[float]] | list[tuple[float, float]]) -> str:
    """Encode normalized ``lat, lon`` route geometry without external dependencies."""
    previous_latitude = previous_longitude = 0
    encoded: list[str] = []
    for latitude, longitude in points:
        current_latitude = round(float(latitude) * POLYLINE_PRECISION)
        current_longitude = round(float(longitude) * POLYLINE_PRECISION)
        encoded.append(_encode_value(current_latitude - previous_latitude))
        encoded.append(_encode_value(current_longitude - previous_longitude))
        previous_latitude, previous_longitude = current_latitude, current_longitude
    return "".join(encoded)


def decode_polyline(value: str) -> list[list[float]]:
    """Decode a stored route snapshot to Leaflet-compatible ``lat, lon`` pairs."""
    coordinates: list[list[float]] = []
    index = latitude = longitude = 0

    def decode_number() -> int:
        nonlocal index
        result = shift = 0
        while index < len(value):
            byte = ord(value[index]) - 63
            index += 1
            result |= (byte & 0x1F) << shift
            shift += 5
            if byte < 0x20:
                return ~(result >> 1) if result & 1 else result >> 1
        raise TripValidationError("Сохранённая геометрия маршрута повреждена")

    while index < len(value):
        latitude += decode_number()
        longitude += decode_number()
        coordinates.append(
            [latitude / POLYLINE_PRECISION, longitude / POLYLINE_PRECISION]
        )
    return coordinates


def _decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _snapshot_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def create_trip_snapshot(
    db: Session,
    *,
    owner_id: int,
    vehicle: Vehicle,
    client_request_id: str,
    plan: dict[str, Any],
    title: str | None = None,
) -> tuple[VehicleTrip, bool]:
    """Atomically persist the authoritative server plan and its recommended stops."""
    existing = db.scalar(
        select(VehicleTrip).where(
            VehicleTrip.owner_id == owner_id,
            VehicleTrip.client_request_id == client_request_id,
        )
    )
    if existing is not None:
        return existing, False
    route = plan["route"]
    calculation = plan["calculation"]
    geometry = route.get("geometry") or []
    if len(geometry) < 2:
        raise TripValidationError("Маршрут не содержит геометрию")
    clean_title = " ".join((title or "").split()) or None
    if clean_title and len(clean_title) > 180:
        raise TripValidationError("Название поездки должно быть не длиннее 180 символов")
    trip = VehicleTrip(
        owner_id=owner_id,
        vehicle_id=vehicle.id,
        client_request_id=client_request_id,
        title=clean_title,
        start_label=str(route["start"]["label"])[:300],
        end_label=str(route["end"]["label"])[:300],
        start_latitude=float(route["start"]["latitude"]),
        start_longitude=float(route["start"]["longitude"]),
        end_latitude=float(route["end"]["latitude"]),
        end_longitude=float(route["end"]["longitude"]),
        fuel_type=str(plan["fuel_type"]),
        planned_distance_km=_decimal(route["distance_km"]),
        planned_duration_minutes=_decimal(route.get("duration_minutes")),
        route_provider=str(route["provider"]),
        route_profile=str(route.get("profile") or "driving"),
        route_is_approximate=bool(route.get("is_approximate")),
        route_geometry_polyline=encode_polyline(geometry),
        planned_consumption_l_per_100km=_decimal(
            calculation.get("consumption_l_per_100km")
        ),
        planned_tank_liters=_decimal(calculation.get("tank_liters")),
        planned_start_fuel_percent=_decimal(calculation.get("fuel_level_percent")),
        planned_start_fuel_liters=_decimal(calculation.get("current_fuel_liters")),
        planned_range_km=_decimal(calculation.get("current_range_km")),
        planned_fuel_needed_liters=_decimal(calculation.get("required_liters")),
        warnings_json=list(plan.get("warnings") or []),
    )
    db.add(trip)
    db.flush()
    for sequence, station in enumerate(plan.get("recommended_stops") or [], start=1):
        selected_fuel = station.get("selected_fuel") or {}
        db.add(
            VehicleTripPlannedStop(
                trip_id=trip.id,
                station_id=station.get("station_id"),
                sequence=sequence,
                provider=str(station.get("provider") or "gdebenz")[:32],
                provider_station_id=str(station["provider_station_id"])[:128],
                station_name=str(
                    station.get("brand") or station.get("name") or "АЗС"
                )[:180],
                address=(str(station["address"])[:300] if station.get("address") else None),
                latitude=float(station["latitude"]),
                longitude=float(station["longitude"]),
                route_progress_km=_decimal(
                    station.get("route_progress_km", station["distance_from_start_km"])
                ),
                distance_to_route_km=_decimal(station["distance_to_route_km"]),
                fuel_type=str(plan["fuel_type"]),
                availability_state=str(selected_fuel.get("state") or "unknown")[:20],
                availability_label=str(selected_fuel.get("label") or "НЕТ ДАННЫХ")[:40],
                availability_updated_at=_snapshot_datetime(station.get("updated_at")),
                after_refuel_range_km=_decimal(station.get("after_refuel_range_km")),
            )
        )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(VehicleTrip).where(
                VehicleTrip.owner_id == owner_id,
                VehicleTrip.client_request_id == client_request_id,
            )
        )
        if existing is not None:
            return existing, False
        raise
    db.refresh(trip)
    return trip, True


def transition_trip(trip: VehicleTrip, target: str) -> None:
    if target not in VALID_TRANSITIONS.get(trip.status, set()):
        raise TripValidationError("Недопустимый переход статуса поездки")


def fuel_level_liters(
    value: float | None,
    unit: str,
    tank_liters: Decimal | None,
) -> tuple[Decimal | None, bool]:
    if value is None:
        return None, False
    numeric = Decimal(str(value))
    if unit == "percent":
        if numeric < 0 or numeric > 100:
            raise TripValidationError("Уровень топлива должен быть от 0 до 100%")
        if tank_liters is None:
            raise TripValidationError("Для уровня в процентах нужен объём бака")
        return (tank_liters * numeric / Decimal("100")).quantize(Decimal("0.01")), True
    if unit == "liters":
        if numeric < 0 or (tank_liters is not None and numeric > tank_liters):
            raise TripValidationError("Количество топлива не может превышать объём бака")
        return numeric.quantize(Decimal("0.01")), False
    raise TripValidationError("Выберите единицу уровня топлива")


def start_trip(
    db: Session,
    trip: VehicleTrip,
    *,
    odometer_km: int | None,
    fuel_level: float | None,
    fuel_unit: str,
) -> None:
    transition_trip(trip, "active")
    active = db.scalar(
        select(VehicleTrip.id).where(
            VehicleTrip.vehicle_id == trip.vehicle_id,
            VehicleTrip.status == "active",
            VehicleTrip.id != trip.id,
        )
    )
    if active is not None:
        raise TripValidationError("Для этого автомобиля уже есть активная поездка.")
    if odometer_km is not None and not 0 <= odometer_km <= 10_000_000:
        raise TripValidationError("Пробег должен быть от 0 до 10 000 000 км")
    liters, estimated = fuel_level_liters(
        fuel_level, fuel_unit, trip.planned_tank_liters
    )
    trip.status = "active"
    trip.started_at = datetime.now(timezone.utc).replace(tzinfo=None)
    trip.start_odometer_km = odometer_km
    trip.start_fuel_liters = liters
    trip.start_fuel_estimated = estimated
    if odometer_km is not None:
        trip.vehicle.current_odometer = max(trip.vehicle.current_odometer, odometer_km)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise TripValidationError("Для этого автомобиля уже есть активная поездка.") from exc


def complete_trip(
    db: Session,
    trip: VehicleTrip,
    *,
    odometer_km: int | None,
    fuel_level: float | None,
    fuel_unit: str,
    notes: str | None,
) -> None:
    transition_trip(trip, "completed")
    if odometer_km is not None and not 0 <= odometer_km <= 10_000_000:
        raise TripValidationError("Пробег должен быть от 0 до 10 000 000 км")
    if (
        odometer_km is not None
        and trip.start_odometer_km is not None
        and odometer_km < trip.start_odometer_km
    ):
        raise TripValidationError("Конечный пробег не может быть меньше начального")
    clean_notes = (notes or "").strip()
    if len(clean_notes) > 4000:
        raise TripValidationError("Заметка должна быть не длиннее 4000 символов")
    liters, estimated = fuel_level_liters(
        fuel_level, fuel_unit, trip.planned_tank_liters
    )
    trip.status = "completed"
    trip.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    trip.end_odometer_km = odometer_km
    trip.end_fuel_liters = liters
    trip.end_fuel_estimated = estimated
    trip.notes = clean_notes or None
    if odometer_km is not None:
        trip.vehicle.current_odometer = max(trip.vehicle.current_odometer, odometer_km)
    db.commit()


def cancel_trip(db: Session, trip: VehicleTrip) -> None:
    transition_trip(trip, "cancelled")
    trip.status = "cancelled"
    trip.cancelled_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()


def link_fuel_entry(
    db: Session,
    trip: VehicleTrip,
    entry: VehicleFuelEntry,
    *,
    planned_stop_id: int | None = None,
) -> bool:
    if trip.status not in {"active", "completed"}:
        raise TripValidationError("Заправки можно добавлять только в начатую поездку")
    if entry.vehicle_id != trip.vehicle_id:
        raise TripValidationError("Заправка относится к другому автомобилю")
    existing = db.scalar(
        select(VehicleTripFuelEntry).where(
            VehicleTripFuelEntry.fuel_entry_id == entry.id
        )
    )
    if existing is not None:
        if existing.trip_id == trip.id:
            return False
        raise TripValidationError("Заправка уже связана с другой поездкой")
    if planned_stop_id is not None:
        stop = db.scalar(
            select(VehicleTripPlannedStop).where(
                VehicleTripPlannedStop.id == planned_stop_id,
                VehicleTripPlannedStop.trip_id == trip.id,
            )
        )
        if stop is None:
            raise TripValidationError("Рекомендованная остановка не найдена")
    db.add(
        VehicleTripFuelEntry(
            trip_id=trip.id,
            fuel_entry_id=entry.id,
            planned_stop_id=planned_stop_id,
        )
    )
    db.commit()
    return True


def unlink_fuel_entry(db: Session, trip: VehicleTrip, entry_id: int) -> None:
    link = db.scalar(
        select(VehicleTripFuelEntry).where(
            VehicleTripFuelEntry.trip_id == trip.id,
            VehicleTripFuelEntry.fuel_entry_id == entry_id,
        )
    )
    if link is None:
        raise TripValidationError("Заправка не связана с этой поездкой")
    db.delete(link)
    db.commit()


def trip_metrics(trip: VehicleTrip) -> dict[str, Any]:
    entries = [link.fuel_entry for link in trip.fuel_links]
    purchased = sum((entry.liters for entry in entries), Decimal("0"))
    known_costs = [entry.total_cost for entry in entries if entry.total_cost is not None]
    missing_cost_count = len(entries) - len(known_costs)
    cost = sum(known_costs, Decimal("0"))
    if trip.start_odometer_km is not None and trip.end_odometer_km is not None:
        distance_value: Decimal | None = Decimal(
            trip.end_odometer_km - trip.start_odometer_km
        )
        distance_source = "odometer"
        distance_quality = "actual"
    else:
        distance_value = trip.planned_distance_km
        distance_source = "planned_route"
        distance_quality = "estimated"

    consumed: Decimal | None = None
    consumption: Decimal | None = None
    fuel_quality = "unavailable"
    warning = None
    if trip.start_fuel_liters is not None and trip.end_fuel_liters is not None:
        candidate = trip.start_fuel_liters + purchased - trip.end_fuel_liters
        if candidate < Decimal("-0.5"):
            warning = "Данные об уровне топлива противоречат объёму заправок."
            fuel_quality = "inconsistent"
        else:
            consumed = max(candidate, Decimal("0"))
            fuel_quality = (
                "estimated"
                if trip.start_fuel_estimated or trip.end_fuel_estimated
                else "actual"
            )
            if distance_value is not None and distance_value > 0:
                consumption = consumed * Decimal("100") / distance_value
                if distance_quality != "actual":
                    fuel_quality = "estimated"

    return {
        "distance": {
            "value": distance_value,
            "source": distance_source,
            "quality": distance_quality,
        },
        "refuel_count": len(entries),
        "purchased_liters": purchased,
        "fuel_consumed_liters": consumed,
        "consumption_l_per_100km": consumption,
        "consumption_quality": fuel_quality,
        "cost": cost,
        "cost_complete": missing_cost_count == 0,
        "missing_cost_count": missing_cost_count,
        "warning": warning,
    }


def suggested_fuel_entries(db: Session, trip: VehicleTrip) -> list[VehicleFuelEntry]:
    if trip.started_at is None:
        return []
    start_date = trip.started_at.date()
    end_date = (trip.completed_at or datetime.now(timezone.utc).replace(tzinfo=None)).date()
    return db.scalars(
        select(VehicleFuelEntry)
        .outerjoin(VehicleTripFuelEntry)
        .where(
            VehicleFuelEntry.vehicle_id == trip.vehicle_id,
            VehicleFuelEntry.occurred_on >= start_date,
            VehicleFuelEntry.occurred_on <= end_date,
            VehicleTripFuelEntry.id.is_(None),
        )
        .order_by(VehicleFuelEntry.occurred_on, VehicleFuelEntry.odometer)
    ).all()


def load_trip(db: Session, trip_id: int, owner_id: int) -> VehicleTrip | None:
    return db.scalar(
        select(VehicleTrip)
        .options(
            selectinload(VehicleTrip.vehicle),
            selectinload(VehicleTrip.planned_stops),
            selectinload(VehicleTrip.fuel_links).selectinload(
                VehicleTripFuelEntry.fuel_entry
            ),
        )
        .where(VehicleTrip.id == trip_id, VehicleTrip.owner_id == owner_id)
    )


def trip_date_bounds(trip: VehicleTrip) -> tuple[date | None, date | None]:
    return (
        trip.started_at.date() if trip.started_at else None,
        trip.completed_at.date() if trip.completed_at else None,
    )
