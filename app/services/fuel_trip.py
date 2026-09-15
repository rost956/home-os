"""Pure calculations and stop selection for the Fuel trip planner."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .fuel_routes import EARTH_RADIUS_KM

SAFE_RESERVE_PERCENT = 15.0
ROAD_DISTANCE_FACTOR = 1.18


def route_point_at_fraction(
    start_latitude: float,
    start_longitude: float,
    end_latitude: float,
    end_longitude: float,
    fraction: float,
) -> tuple[float, float]:
    longitude_delta = (end_longitude - start_longitude + 180) % 360 - 180
    return (
        start_latitude + (end_latitude - start_latitude) * fraction,
        ((start_longitude + longitude_delta * fraction + 180) % 360) - 180,
    )


def route_progress_fraction(
    latitude: float,
    longitude: float,
    start_latitude: float,
    start_longitude: float,
    end_latitude: float,
    end_longitude: float,
) -> float:
    """Project a nearby station onto the route and return clamped progress."""
    reference_latitude = math.radians((start_latitude + end_latitude) / 2)
    def longitude_delta(value: float) -> float:
        return (value - start_longitude + 180) % 360 - 180
    end_x = EARTH_RADIUS_KM * math.radians(longitude_delta(end_longitude)) * math.cos(reference_latitude)
    end_y = EARTH_RADIUS_KM * math.radians(end_latitude - start_latitude)
    point_x = EARTH_RADIUS_KM * math.radians(longitude_delta(longitude)) * math.cos(reference_latitude)
    point_y = EARTH_RADIUS_KM * math.radians(latitude - start_latitude)
    length_squared = end_x**2 + end_y**2
    if length_squared == 0:
        return 0.0
    return max(0.0, min(1.0, (point_x * end_x + point_y * end_y) / length_squared))


class FuelTripPlanRequest(BaseModel):
    vehicle_id: int = Field(gt=0)
    start: str = Field(min_length=2, max_length=300)
    end: str = Field(min_length=2, max_length=300)
    fuel_type: Literal["95", "98", "100"]
    fuel_level_percent: float = Field(ge=0, le=100)
    tank_liters: float | None = Field(default=None, gt=0, le=300)
    consumption_l_per_100km: float | None = Field(default=None, gt=0, le=100)

    @field_validator("start", "end")
    @classmethod
    def clean_place(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if len(cleaned) < 2:
            raise ValueError("Укажите точку маршрута")
        return cleaned


def calculate_trip(
    distance_km: float,
    *,
    tank_liters: float | None,
    consumption_l_per_100km: float | None,
    fuel_level_percent: float,
    reserve_percent: float = SAFE_RESERVE_PERCENT,
) -> dict[str, Any]:
    """Calculate fuel needs without treating an empty tank as usable range."""
    warnings: list[str] = []
    if tank_liters is None:
        warnings.append("У автомобиля не указан объём бака — введите его вручную.")
    if consumption_l_per_100km is None:
        warnings.append("Недостаточно данных о расходе — введите расход вручную.")
    if tank_liters is None or consumption_l_per_100km is None:
        return {
            "can_plan": False,
            "distance_km": round(distance_km, 1),
            "tank_liters": tank_liters,
            "consumption_l_per_100km": consumption_l_per_100km,
            "fuel_level_percent": fuel_level_percent,
            "reserve_percent": reserve_percent,
            "warnings": warnings,
        }

    required_liters = distance_km * consumption_l_per_100km / 100
    current_liters = tank_liters * fuel_level_percent / 100
    reserve_liters = tank_liters * reserve_percent / 100
    current_range = current_liters / consumption_l_per_100km * 100
    safe_current_range = max(0.0, current_liters - reserve_liters) / consumption_l_per_100km * 100
    full_range = tank_liters / consumption_l_per_100km * 100
    safe_full_range = (tank_liters - reserve_liters) / consumption_l_per_100km * 100
    next_from = safe_current_range * 0.85
    next_to = safe_current_range
    if safe_current_range >= distance_km:
        next_from = next_to = None
    return {
        "can_plan": True,
        "distance_km": round(distance_km, 1),
        "tank_liters": round(tank_liters, 1),
        "consumption_l_per_100km": round(consumption_l_per_100km, 2),
        "fuel_level_percent": round(fuel_level_percent, 1),
        "required_liters": round(required_liters, 1),
        "current_fuel_liters": round(current_liters, 1),
        "current_range_km": round(current_range, 1),
        "safe_current_range_km": round(safe_current_range, 1),
        "full_range_km": round(full_range, 1),
        "safe_full_range_km": round(safe_full_range, 1),
        "next_refuel_from_km": round(next_from, 1) if next_from is not None else None,
        "next_refuel_to_km": round(next_to, 1) if next_to is not None else None,
        "reserve_percent": reserve_percent,
        "warnings": warnings,
    }


def planned_search_distances(calculation: dict[str, Any]) -> list[float]:
    """Return useful points around which provider candidates should be requested."""
    if not calculation.get("can_plan"):
        return []
    route_distance = float(calculation["distance_km"])
    first_range = float(calculation["safe_current_range_km"])
    full_range = float(calculation["safe_full_range_km"])
    if first_range >= route_distance:
        return []
    if first_range <= 0:
        return []
    targets = [max(0.0, first_range * 0.92)]
    while targets[-1] + full_range < route_distance:
        targets.append(targets[-1] + full_range * 0.92)
    return targets[:20]


def _freshness_rank(value: Any, now: datetime) -> int:
    if not value:
        return 2
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return 2
    age_hours = (now - parsed.astimezone(timezone.utc)).total_seconds() / 3600
    return 0 if age_hours <= 2 else 1


def select_recommended_stops(
    candidates: list[dict[str, Any]],
    *,
    fuel_type: str,
    calculation: dict[str, Any],
    max_deviation_km: float = 10.0,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Choose reachable useful stops, ordered in the direction of travel."""
    if not calculation.get("can_plan"):
        return [], list(calculation.get("warnings", []))
    now = now or datetime.now(timezone.utc)
    route_distance = float(calculation["distance_km"])
    position = 0.0
    usable_range = float(calculation["safe_current_range_km"])
    full_usable_range = float(calculation["safe_full_range_km"])
    full_range = float(calculation["full_range_km"])
    chosen: list[dict[str, Any]] = []
    warnings: list[str] = []
    remaining = sorted(candidates, key=lambda item: float(item["distance_from_start_km"]))
    state_rank = {"available": 0, "low": 1, "candidate": 2, "unknown": 3}

    while position + usable_range < route_distance:
        minimum_progress = position + max(10.0, usable_range * 0.45)
        reachable = []
        for station in remaining:
            station_distance = float(station["distance_from_start_km"])
            deviation = float(station["distance_to_route_km"])
            fuel = next(
                (item for item in station.get("fuels", []) if item.get("fuel_type") == fuel_type),
                {"state": "unknown", "label": "НЕТ ДАННЫХ", "symbol": "·"},
            )
            if (
                minimum_progress <= station_distance <= position + usable_range
                and deviation <= max_deviation_km
                and fuel.get("state") != "unavailable"
            ):
                reachable.append((station, fuel))
        if not reachable:
            warnings.append(
                f"Не найдена подходящая АЗС до отметки {position + usable_range:.0f} км. "
                "Проверьте запас топлива или увеличьте его перед поездкой."
            )
            break
        target = position + usable_range * 0.9
        station, selected_fuel = min(
            reachable,
            key=lambda pair: (
                state_rank.get(str(pair[1].get("state")), 4),
                float(pair[0]["distance_to_route_km"]),
                _freshness_rank(pair[0].get("updated_at"), now),
                0 if pair[0].get("has_confirmed_event") else 1,
                abs(float(pair[0]["distance_from_start_km"]) - target),
            ),
        )
        stop = dict(station)
        stop["selected_fuel"] = selected_fuel
        stop["after_refuel_range_km"] = round(full_range, 1)
        chosen.append(stop)
        position = float(station["distance_from_start_km"])
        usable_range = full_usable_range
        remaining = [
            item for item in remaining if float(item["distance_from_start_km"]) > position
        ]
        if len(chosen) >= 20:
            warnings.append("Количество остановок ограничено двадцатью.")
            break
    return chosen, warnings
