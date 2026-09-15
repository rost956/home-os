"""Provider-backed station lookup along a route segment."""

from __future__ import annotations

import math

from .fuel import FuelDataProvider, FuelStationCandidate

EARTH_RADIUS_KM = 6_371.0
MAX_ROUTE_LENGTH_KM = 250.0
MAX_ROUTE_REQUESTS = 50


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
    if route_length > MAX_ROUTE_LENGTH_KM:
        raise ValueError(f"Длина маршрута не должна превышать {MAX_ROUTE_LENGTH_KM:.0f} км")
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


async def find_stations_near_route(
    provider: FuelDataProvider,
    *,
    start_latitude: float,
    start_longitude: float,
    end_latitude: float,
    end_longitude: float,
    radius_km: float,
) -> list[dict[str, object]]:
    """Fetch and deduplicate provider stations within a corridor around a route."""
    points = route_sample_points(
        start_latitude,
        start_longitude,
        end_latitude,
        end_longitude,
        radius_km,
    )
    candidates: dict[tuple[str, str], tuple[FuelStationCandidate, float]] = {}
    for latitude, longitude in points:
        for station in await provider.get_stations_near(latitude, longitude, radius_km):
            route_distance = distance_to_route_km(
                station.latitude,
                station.longitude,
                start_latitude,
                start_longitude,
                end_latitude,
                end_longitude,
            )
            if route_distance > radius_km:
                continue
            key = (station.provider, station.provider_station_id)
            current = candidates.get(key)
            if current is None or route_distance < current[1]:
                candidates[key] = (station, route_distance)

    result = []
    for station, route_distance in candidates.values():
        result.append(
            {
                "provider": station.provider,
                "provider_station_id": station.provider_station_id,
                "name": station.name,
                "brand": station.brand,
                "address": station.address,
                "latitude": station.latitude,
                "longitude": station.longitude,
                "distance_to_route_km": round(route_distance, 2),
                "distance_from_start_km": round(
                    haversine_km(
                        start_latitude,
                        start_longitude,
                        station.latitude,
                        station.longitude,
                    ),
                    2,
                ),
            }
        )
    return sorted(
        result,
        key=lambda item: (
            float(item["distance_from_start_km"]),
            str(item["provider_station_id"]),
        ),
    )
