"""Provider-backed station lookup along a route segment."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timezone
from typing import Any

from .fuel import (
    FUEL_TYPES,
    FuelDataProvider,
    FuelStationCandidate,
    normalize_fuel_states,
    parse_source_datetime,
)

EARTH_RADIUS_KM = 6_371.0
MAX_ROUTE_REQUESTS = 50
ROUTE_STATUS_LABELS = {
    "available": "ЕСТЬ",
    "candidate": "ВОЗМОЖНО",
    "low": "ЕСТЬ",
    "unavailable": "НЕТ",
    "unknown": "НЕТ ДАННЫХ",
}
ROUTE_STATUS_SYMBOLS = {
    "available": "✓",
    "candidate": "?",
    "low": "!",
    "unavailable": "×",
    "unknown": "·",
}


def _route_fuel_statuses(raw: dict[str, Any]) -> list[dict[str, Any]]:
    states = normalize_fuel_states(raw)
    has_queue = str(raw.get("status") or "").lower() == "queue"
    return [
        {
            "fuel_type": fuel_type,
            "state": "candidate" if states[fuel_type] == "available" else states[fuel_type],
            "label": ROUTE_STATUS_LABELS[
                "candidate" if states[fuel_type] == "available" else states[fuel_type]
            ],
            "symbol": ROUTE_STATUS_SYMBOLS[
                "candidate" if states[fuel_type] == "available" else states[fuel_type]
            ],
            "has_queue": has_queue and states[fuel_type] == "available",
        }
        for fuel_type in FUEL_TYPES
    ]


def _route_updated_at(raw: dict[str, Any]) -> str | None:
    value = parse_source_datetime(raw.get("last_at") or raw.get("updated"))
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def haversine_km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    """Return great-circle distance between two WGS84 points."""
    latitude_delta = math.radians(b_lat - a_lat)
    longitude_delta = math.radians(b_lon - a_lon)
    a_latitude = math.radians(a_lat)
    b_latitude = math.radians(b_lat)
    value = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(a_latitude)
        * math.cos(b_latitude)
        * math.sin(longitude_delta / 2) ** 2
    )
    value = min(1.0, max(0.0, value))
    return EARTH_RADIUS_KM * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def distance_to_route_km(
    latitude: float,
    longitude: float,
    start_latitude: float,
    start_longitude: float,
    end_latitude: float,
    end_longitude: float,
) -> float:
    """Approximate distance to a local route segment using an equirectangular plane."""
    reference_latitude = math.radians((start_latitude + end_latitude + latitude) / 3)

    def longitude_delta(point_longitude: float) -> float:
        return (point_longitude - start_longitude + 180) % 360 - 180

    def project(point_latitude: float, point_longitude: float) -> tuple[float, float]:
        return (
            EARTH_RADIUS_KM
            * math.radians(longitude_delta(point_longitude))
            * math.cos(reference_latitude),
            EARTH_RADIUS_KM * math.radians(point_latitude - start_latitude),
        )

    start_x, start_y = project(start_latitude, start_longitude)
    end_x, end_y = project(end_latitude, end_longitude)
    point_x, point_y = project(latitude, longitude)
    segment_x, segment_y = end_x - start_x, end_y - start_y
    segment_length_squared = segment_x**2 + segment_y**2
    if segment_length_squared == 0:
        return haversine_km(latitude, longitude, start_latitude, start_longitude)
    position = max(
        0.0,
        min(
            1.0,
            ((point_x - start_x) * segment_x + (point_y - start_y) * segment_y)
            / segment_length_squared,
        ),
    )
    nearest_x = start_x + position * segment_x
    nearest_y = start_y + position * segment_y
    return math.hypot(point_x - nearest_x, point_y - nearest_y)


@dataclass(frozen=True)
class RouteProjection:
    route_progress_km: float
    distance_to_route_km: float
    segment_index: int
    segment_fraction: float


def project_onto_route(
    latitude: float,
    longitude: float,
    geometry: list[tuple[float, float]] | tuple[tuple[float, float], ...],
    route_distance_km: float,
) -> RouteProjection:
    """Project a point onto a polyline and scale progress to provider distance."""
    if len(geometry) < 2:
        raise ValueError("Маршрут должен содержать минимум две точки")
    lengths = [
        haversine_km(*geometry[index], *geometry[index + 1])
        for index in range(len(geometry) - 1)
    ]
    total_geometry_km = sum(lengths)
    best_distance = math.inf
    best_progress = 0.0
    best_index = 0
    best_fraction = 0.0
    cumulative = 0.0
    for index, ((start_lat, start_lon), (end_lat, end_lon)) in enumerate(
        zip(geometry, geometry[1:])
    ):
        reference_latitude = math.radians((start_lat + end_lat + latitude) / 3)

        def local_xy(point_lat: float, point_lon: float) -> tuple[float, float]:
            longitude_delta = (point_lon - start_lon + 180) % 360 - 180
            return (
                EARTH_RADIUS_KM * math.radians(longitude_delta) * math.cos(reference_latitude),
                EARTH_RADIUS_KM * math.radians(point_lat - start_lat),
            )

        end_x, end_y = local_xy(end_lat, end_lon)
        point_x, point_y = local_xy(latitude, longitude)
        segment_length_squared = end_x**2 + end_y**2
        fraction = 0.0 if segment_length_squared == 0 else max(
            0.0,
            min(1.0, (point_x * end_x + point_y * end_y) / segment_length_squared),
        )
        distance = math.hypot(point_x - end_x * fraction, point_y - end_y * fraction)
        if distance < best_distance:
            best_distance = distance
            best_progress = cumulative + lengths[index] * fraction
            best_index = index
            best_fraction = fraction
        cumulative += lengths[index]
    scaled_progress = (
        best_progress / total_geometry_km * route_distance_km
        if total_geometry_km > 0
        else 0.0
    )
    return RouteProjection(
        route_progress_km=scaled_progress,
        distance_to_route_km=best_distance,
        segment_index=best_index,
        segment_fraction=best_fraction,
    )


def route_point_at_progress(
    geometry: list[tuple[float, float]] | tuple[tuple[float, float], ...],
    route_distance_km: float,
    progress_km: float,
) -> tuple[float, float]:
    """Interpolate a geometry coordinate at authoritative route progress."""
    if len(geometry) < 2:
        raise ValueError("Маршрут должен содержать минимум две точки")
    lengths = [
        haversine_km(*geometry[index], *geometry[index + 1])
        for index in range(len(geometry) - 1)
    ]
    total_geometry_km = sum(lengths)
    if total_geometry_km == 0 or route_distance_km <= 0:
        return geometry[0]
    target = max(0.0, min(route_distance_km, progress_km)) / route_distance_km * total_geometry_km
    cumulative = 0.0
    for index, segment_length in enumerate(lengths):
        if cumulative + segment_length >= target or index == len(lengths) - 1:
            fraction = 0.0 if segment_length == 0 else (target - cumulative) / segment_length
            start_lat, start_lon = geometry[index]
            end_lat, end_lon = geometry[index + 1]
            longitude_delta = (end_lon - start_lon + 180) % 360 - 180
            return (
                start_lat + (end_lat - start_lat) * fraction,
                ((start_lon + longitude_delta * fraction + 180) % 360) - 180,
            )
        cumulative += segment_length
    return geometry[-1]


def route_sample_points(
    start_latitude: float,
    start_longitude: float,
    end_latitude: float,
    end_longitude: float,
    radius_km: float,
) -> list[tuple[float, float]]:
    """Cover a straight route with overlapping provider nearby searches."""
    route_length = haversine_km(
        start_latitude, start_longitude, end_latitude, end_longitude
    )
    if route_length == 0:
        return [(start_latitude, start_longitude)]
    # Adjacent circles overlap, so narrow corridors do not leave blind gaps.
    intervals = max(1, math.ceil(route_length / (radius_km * 1.5)))
    if intervals + 1 > MAX_ROUTE_REQUESTS:
        raise ValueError("Маршрут требует слишком много запросов; увеличьте радиус")
    longitude_delta = (end_longitude - start_longitude + 180) % 360 - 180
    points = [
        (
            start_latitude + (end_latitude - start_latitude) * index / intervals,
            ((start_longitude + longitude_delta * index / intervals + 180) % 360) - 180,
        )
        for index in range(intervals + 1)
    ]
    points[0] = (start_latitude, start_longitude)
    points[-1] = (end_latitude, end_longitude)
    return points


def route_geometry_sample_points(
    geometry: list[tuple[float, float]] | tuple[tuple[float, float], ...],
    route_distance_km: float,
    radius_km: float,
) -> list[tuple[float, float]]:
    """Cover a road geometry with bounded overlapping nearby searches."""
    if route_distance_km <= 0:
        return [geometry[0]]
    intervals = max(1, math.ceil(route_distance_km / (radius_km * 1.5)))
    if intervals + 1 > MAX_ROUTE_REQUESTS:
        raise ValueError("Маршрут требует слишком много запросов; увеличьте радиус")
    return [
        route_point_at_progress(geometry, route_distance_km, route_distance_km * index / intervals)
        for index in range(intervals + 1)
    ]


async def find_stations_near_route(
    provider: FuelDataProvider,
    *,
    start_latitude: float,
    start_longitude: float,
    end_latitude: float,
    end_longitude: float,
    radius_km: float,
    route_geometry: list[tuple[float, float]] | tuple[tuple[float, float], ...] | None = None,
    route_distance_km: float | None = None,
    sample_progress_km: list[float] | tuple[float, ...] | None = None,
) -> list[dict[str, object]]:
    """Fetch and deduplicate provider stations within a corridor around a route."""
    geometry = route_geometry or (
        (start_latitude, start_longitude),
        (end_latitude, end_longitude),
    )
    authoritative_distance = route_distance_km
    if authoritative_distance is None:
        authoritative_distance = haversine_km(
            start_latitude, start_longitude, end_latitude, end_longitude
        )
    if sample_progress_km is None:
        points = route_geometry_sample_points(geometry, authoritative_distance, radius_km)
    else:
        points = [
            route_point_at_progress(geometry, authoritative_distance, progress)
            for progress in sample_progress_km
        ]
    candidates: dict[tuple[str, str], tuple[FuelStationCandidate, float]] = {}
    for latitude, longitude in points:
        for station in await provider.get_stations_near(latitude, longitude, radius_km):
            projection = project_onto_route(
                station.latitude, station.longitude, geometry, authoritative_distance
            )
            if projection.distance_to_route_km > radius_km:
                continue
            key = (station.provider, station.provider_station_id)
            current = candidates.get(key)
            if current is None or projection.distance_to_route_km < current[1]:
                candidates[key] = (station, projection.distance_to_route_km)

    result = []
    for station, route_distance in candidates.values():
        projection = project_onto_route(
            station.latitude, station.longitude, geometry, authoritative_distance
        )
        result.append(
            {
                "provider": station.provider,
                "provider_station_id": station.provider_station_id,
                "name": station.name,
                "brand": station.brand,
                "address": station.address,
                "latitude": station.latitude,
                "longitude": station.longitude,
                "fuels": _route_fuel_statuses(station.raw),
                "updated_at": _route_updated_at(station.raw),
                "distance_to_route_km": round(route_distance, 2),
                "route_progress_km": round(projection.route_progress_km, 2),
                # Backward-compatible Phase 9/10 field.
                "distance_from_start_km": round(projection.route_progress_km, 2),
            }
        )
    return sorted(
        result,
        key=lambda item: (
            float(item["distance_from_start_km"]),
            str(item["provider_station_id"]),
        ),
    )
