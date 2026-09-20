import asyncio
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest
from sqlalchemy import event

import app.main as main_module
import app.services.fuel as fuel_module
from app.database import SessionLocal
from app.database import engine as app_engine
from app.models import (
    FuelDeliveryEvent,
    FuelForecast,
    FuelMonitorSettings,
    FuelObservation,
    FuelStation,
    FuelStationChatMessage,
    FuelStationFuel,
    FuelStationMark,
    FuelStationSubscription,
    Recipe,
    Vehicle,
)
from app.services.fuel import (
    FuelChatFeed,
    FuelMarksFeed,
    FuelStationCandidate,
    GdeBenzProvider,
    ProviderUnavailable,
    normalize_fuel_states,
    parse_source_datetime,
    run_fuel_poll_cycle,
    source_is_stale,
)
from app.services.fuel_analytics import (
    FORECAST_VERSION,
    backfill_delivery_events,
    build_forecast,
    chat_delivery_signal,
    correlate_event_series,
    detect_delivery_events,
    mark_evidence_weight,
    refresh_forecasts,
    station_correlations,
)
from app.services.fuel_availability import evaluate_fuel_availability
from app.services.fuel_dashboard import load_fuel_dashboard
from app.services.fuel_routes import (
    distance_to_route_km,
    find_stations_near_route,
    haversine_km,
    project_onto_route,
    route_point_at_progress,
    route_sample_points,
)
from app.services.fuel_settings import FuelRuntimeSettings, get_fuel_runtime_settings, save_fuel_runtime_settings
from app.services.fuel_timeline import build_fuel_timeline
from app.services.fuel_trip import calculate_trip, select_recommended_stops
from app.services.route_engine import (
    OSRMRouteProvider,
    RouteEngine,
    RouteNotFound,
    RouteResult,
)
from app.timezone import format_msk


def add_subscription(db, user, station, fuel_types=("95", "98", "100"), *, enabled=True):
    subscription = FuelStationSubscription(
        user_id=user.id,
        station_id=station.id,
        enabled=enabled,
        track_95="95" in fuel_types,
        track_98="98" in fuel_types,
        track_100="100" in fuel_types,
    )
    db.add(subscription)
    return subscription


class StaticRouteEngine:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def route(self, start, end):
        self.calls.append((start, end))
        if self.error:
            raise self.error
        return self.result


def exact_route(start, end, *, distance_km, duration_minutes=60, geometry=None):
    return RouteResult(
        distance_km=distance_km,
        duration_minutes=duration_minutes,
        geometry=tuple(geometry or (start, end)),
        provider="osrm",
        profile="driving",
    )


def test_gdebenz_normalization_per_fuel():
    assert normalize_fuel_states({"status": "yes", "fuels_now": ["95", "100"]}) == {
        "95": "available", "98": "unavailable", "100": "available"
    }


def test_unknown_and_missing_fuels_are_not_unavailable():
    assert set(normalize_fuel_states({"status": "unknown", "fuels_now": None}).values()) == {"unknown"}


def test_queue_and_low_do_not_claim_physical_low_stock():
    assert normalize_fuel_states({"status": "low", "fuels_now": ["95"]})["95"] == "available"
    assert normalize_fuel_states({"status": "queue", "fuels_now": ["95"]})["95"] == "available"
    assert set(normalize_fuel_states({"status": "no", "fuels_now": ["95"]}).values()) == {"unavailable"}


def availability_observation(identifier, state, at, *, source_at=None, status="yes", fuel_type="95"):
    return FuelObservation(
        id=identifier,
        station_id=1,
        fuel_type=fuel_type,
        state=state,
        observed_at=at,
        source_updated_at=source_at,
        source_status=status,
        is_stale=False,
    )


def availability_mark(key, at, status, detail, *, author_id=None):
    raw = {"status": status, "detail": detail}
    if author_id is not None:
        raw["author_id"] = author_id
    return FuelStationMark(
        station_id=1,
        provider="gdebenz",
        source_key=key,
        text=detail,
        source_created_at=at,
        fetched_at=at,
        raw_data=raw,
    )


def current_availability(observations=(), marks=(), fuel_type="95"):
    return evaluate_fuel_availability(
        observations,
        marks,
        fuel_type,
        current_at=datetime(2026, 9, 15, 20),
        stale_after_minutes=120,
    )


def test_current_availability_requires_independent_positive_evidence():
    first = datetime(2026, 9, 15, 19)
    one = current_availability([
        availability_observation(1, "available", first, source_at=first),
    ])
    assert one.state == "candidate"

    confirmed = current_availability([
        availability_observation(1, "available", first, source_at=first),
        availability_observation(2, "available", first + timedelta(minutes=10), source_at=first + timedelta(minutes=10)),
    ])
    assert confirmed.state == "available"


def test_repeated_poll_snapshot_is_one_current_availability_evidence():
    source_at = datetime(2026, 9, 15, 19)
    observations = [
        availability_observation(
            index,
            "available",
            source_at + timedelta(minutes=index),
            source_at=source_at,
        )
        for index in range(10)
    ]
    result = current_availability(observations)
    assert result.state == "candidate"
    assert result.positive_count == 1

    same_author_marks = [
        availability_mark("author-1", source_at, "yes", "95", author_id="driver-1"),
        availability_mark(
            "author-2",
            source_at + timedelta(minutes=10),
            "yes",
            "95",
            author_id="driver-1",
        ),
    ]
    same_author = current_availability(marks=same_author_marks)
    assert same_author.state == "candidate"
    assert same_author.positive_count == 1


def test_newer_contradictions_replace_older_current_availability():
    start = datetime(2026, 9, 15, 19)
    observations = [
        availability_observation(index, state, start + timedelta(minutes=minute), source_at=start + timedelta(minutes=minute))
        for index, state, minute in (
            (1, "available", 0), (2, "available", 5),
            (3, "unavailable", 20), (4, "unavailable", 25),
        )
    ]
    assert current_availability(observations).state == "unavailable"

    reversed_states = [
        availability_observation(index, state, start + timedelta(minutes=minute), source_at=start + timedelta(minutes=minute))
        for index, state, minute in (
            (1, "unavailable", 0), (2, "unavailable", 5),
            (3, "available", 20), (4, "available", 25),
        )
    ]
    assert current_availability(reversed_states).state == "available"


def test_queue_is_separate_from_availability_and_never_creates_it():
    start = datetime(2026, 9, 15, 19)
    queue_marks = [
        availability_mark("queue-1", start, "queue", "95"),
        availability_mark("queue-2", start + timedelta(minutes=8), "queue", "95"),
    ]
    result = current_availability(marks=queue_marks)
    assert result.state == "available"
    assert result.has_queue is True

    no_95 = current_availability([
        availability_observation(1, "unavailable", start, source_at=start, status="queue"),
    ])
    assert no_95.state == "unavailable"
    assert no_95.has_queue is False


def test_current_availability_stale_conflict_and_aggregate_mark_deduplication():
    old = datetime(2026, 9, 15, 16)
    assert current_availability([
        availability_observation(1, "available", old, source_at=old),
    ]).state == "unknown"

    start = datetime(2026, 9, 15, 19)
    conflict = current_availability([
        availability_observation(1, "available", start, source_at=start),
        availability_observation(2, "available", start + timedelta(minutes=5), source_at=start + timedelta(minutes=5)),
        availability_observation(3, "unavailable", start + timedelta(minutes=10), source_at=start + timedelta(minutes=10)),
    ])
    assert conflict.state == "candidate"

    duplicate = current_availability(
        [availability_observation(1, "available", start, source_at=start)],
        [availability_mark("same-upstream", start, "yes", "95")],
    )
    assert duplicate.state == "candidate"
    assert duplicate.positive_count == 1


@pytest.mark.parametrize("fuel_type", ["95", "98", "100"])
def test_current_availability_rules_are_generic_for_all_fuels(fuel_type):
    start = datetime(2026, 9, 15, 19)
    result = current_availability(
        marks=[
            availability_mark(f"{fuel_type}-1", start, "yes", fuel_type),
            availability_mark(f"{fuel_type}-2", start + timedelta(minutes=5), "yes", fuel_type),
        ],
        fuel_type=fuel_type,
    )
    assert result.state == "available"


def test_old_source_mark_is_stale_without_losing_source_state():
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    assert normalize_fuel_states({"status": "yes", "fuels_now": ["95"], "last_at": old})["95"] == "available"
    assert source_is_stale(parse_source_datetime(old), 120, datetime.now(timezone.utc).replace(tzinfo=None))


def test_fuel_station_api_and_soft_disable(client, login, make_user):
    make_user("fuel-user")
    login("fuel-user")
    response = client.post("/fuel/stations", data={
        "provider": "gdebenz", "provider_station_id": "123", "latitude": "59.9", "longitude": "30.2",
        "brand": "Teboil", "name": "Teboil", "address": "Test", "fuel_types": ["95", "100"],
    }, follow_redirects=False)
    assert response.status_code == 303
    stations = client.get("/api/fuel/stations").json()
    assert stations[0]["fuel_types"] == ["95", "100"]
    assert client.post(f"/fuel/stations/{stations[0]['id']}/delete", follow_redirects=False).status_code == 303
    assert client.get("/api/fuel/stations").json()[0]["enabled"] is False
    response = client.post("/fuel/stations", data={
        "provider": "gdebenz", "provider_station_id": "123", "latitude": "59.9", "longitude": "30.2",
        "brand": "Teboil", "name": "Teboil", "address": "Test", "fuel_types": ["98"],
    }, follow_redirects=False)
    assert response.status_code == 303
    stations = client.get("/api/fuel/stations").json()
    assert len(stations) == 1
    assert stations[0]["enabled"] is True
    assert stations[0]["fuel_types"] == ["98"]


def test_route_lookup_samples_corridor_filters_and_deduplicates():
    class RouteProvider:
        def __init__(self):
            self.calls = []

        async def get_stations_near(self, latitude, longitude, radius_km):
            self.calls.append((latitude, longitude, radius_km))
            return [
                FuelStationCandidate(
                    provider="gdebenz",
                    provider_station_id="on-route",
                    brand="Teboil",
                    name="Тебойл",
                    address="У маршрута",
                    latitude=59.84,
                    longitude=30.15,
                    raw={"status": "yes", "fuels_now": "95,100", "last_at": "2026-09-15T09:30:00Z"},
                ),
                FuelStationCandidate(
                    provider="gdebenz",
                    provider_station_id="outside",
                    name="Далеко",
                    latitude=60.0,
                    longitude=30.15,
                ),
            ]

    provider = RouteProvider()
    stations = asyncio.run(find_stations_near_route(
        provider,
        start_latitude=59.84,
        start_longitude=30.10,
        end_latitude=59.84,
        end_longitude=30.20,
        radius_km=3,
    ))
    assert len(provider.calls) >= 2
    assert {call[2] for call in provider.calls} == {3}
    assert [station["provider_station_id"] for station in stations] == ["on-route"]
    assert stations[0]["distance_to_route_km"] == 0
    assert [fuel["state"] for fuel in stations[0]["fuels"]] == ["candidate", "unavailable", "candidate"]
    assert stations[0]["updated_at"] == "2026-09-15T09:30:00Z"
    assert distance_to_route_km(59.84, 30.15, 59.84, 30.10, 59.84, 30.20) < 0.01
    assert route_sample_points(59.84, 30.10, 59.84, 30.20, 3)[0] == (59.84, 30.10)


def test_osrm_provider_uses_lon_lat_and_normalizes_geometry_to_lat_lon():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "code": "Ok",
            "routes": [{
                "distance": 12_345.6,
                "duration": 987.0,
                "geometry": {"coordinates": [
                    [30.10, 59.83], [30.12, 59.84], [30.14, 59.85],
                ]},
            }],
        }, request=request)

    provider = OSRMRouteProvider(
        "https://router.test",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(provider.get_route((59.83, 30.10), (59.85, 30.14)))
    assert requests[0].url.path.endswith(
        "/route/v1/driving/30.100000,59.830000;30.140000,59.850000"
    )
    assert dict(requests[0].url.params) == {
        "overview": "full",
        "geometries": "geojson",
        "steps": "false",
        "alternatives": "false",
    }
    assert result.distance_km == pytest.approx(12.3456)
    assert result.duration_minutes == pytest.approx(16.45)
    assert result.geometry == (
        (59.83, 30.10), (59.84, 30.12), (59.85, 30.14),
    )


@pytest.mark.parametrize(
    "kind",
    ["connect_timeout", "read_timeout", "400", "429", "500", "json", "routes", "geometry"],
)
def test_osrm_technical_failures_use_approximate_fallback(kind):
    def handler(request: httpx.Request) -> httpx.Response:
        if kind == "connect_timeout":
            raise httpx.ConnectTimeout("route provider unavailable", request=request)
        if kind == "read_timeout":
            raise httpx.ReadTimeout("slow route provider", request=request)
        if kind in {"400", "429", "500"}:
            return httpx.Response(int(kind), json={"code": "Error"}, request=request)
        if kind == "json":
            return httpx.Response(200, text="not json", request=request)
        if kind == "routes":
            return httpx.Response(200, json={"code": "Ok", "routes": []}, request=request)
        return httpx.Response(200, json={
            "code": "Ok",
            "routes": [{"distance": 1000, "duration": 60, "geometry": {"coordinates": [[30, 59]]}}],
        }, request=request)

    engine = RouteEngine(OSRMRouteProvider(
        "https://router.test", transport=httpx.MockTransport(handler)
    ))
    result = asyncio.run(engine.route((59.0, 30.0), (60.0, 31.0)))
    assert result.is_approximate is True
    assert result.provider == "approximate"
    assert result.duration_minutes is None
    assert "приблизительно" in result.warnings[0]


def test_osrm_no_route_is_not_replaced_with_fake_route():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "NoRoute", "routes": []}, request=request)

    engine = RouteEngine(OSRMRouteProvider(
        "https://router.test", transport=httpx.MockTransport(handler)
    ))
    with pytest.raises(RouteNotFound, match="маршрут"):
        asyncio.run(engine.route((59.0, 30.0), (60.0, 31.0)))


def test_route_engine_cache_is_ttl_bounded_and_ignores_trip_inputs():
    class Provider:
        name = "test"
        profile = "driving"

        def __init__(self):
            self.calls = 0

        async def get_route(self, start, end):
            self.calls += 1
            return exact_route(start, end, distance_km=100 + self.calls)

    clock = [100.0]
    provider = Provider()
    engine = RouteEngine(
        provider,
        cache_seconds=30,
        cache_max_entries=2,
        clock=lambda: clock[0],
    )
    first = asyncio.run(engine.route((59.0, 30.0), (60.0, 31.0)))
    # Fuel percent, tank and consumption never enter the route cache key.
    second = asyncio.run(engine.route((59.0, 30.0), (60.0, 31.0)))
    assert first is second
    assert provider.calls == 1
    asyncio.run(engine.route((59.0, 30.0), (61.0, 31.0)))
    asyncio.run(engine.route((59.0, 30.0), (62.0, 31.0)))
    assert engine.cache_size == 2
    clock[0] += 31
    asyncio.run(engine.route((59.0, 30.0), (62.0, 31.0)))
    assert provider.calls == 4


@pytest.mark.parametrize("coordinate", [(float("nan"), 30), (float("inf"), 30), (91, 30), (59, 181)])
def test_route_engine_rejects_invalid_coordinates_without_provider_call(coordinate):
    class Provider:
        name = "test"
        profile = "driving"
        calls = 0

        async def get_route(self, start, end):
            self.calls += 1
            raise AssertionError("provider must not be called")

    provider = Provider()
    with pytest.raises(ValueError, match="Координаты"):
        asyncio.run(RouteEngine(provider).route(coordinate, (60, 31)))
    assert provider.calls == 0


def test_route_projection_uses_curved_geometry_and_authoritative_distance():
    u_route = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    on_route = project_onto_route(0.5, 0.01, u_route, 360.0)
    near_chord = project_onto_route(0.5, 0.5, u_route, 360.0)
    assert on_route.distance_to_route_km < 2
    assert near_chord.distance_to_route_km > 50

    scaled = project_onto_route(0.0, 0.5, ((0.0, 0.0), (0.0, 1.0)), 120.0)
    assert scaled.route_progress_km == pytest.approx(60, abs=0.1)
    assert route_point_at_progress(((0.0, 0.0), (0.0, 1.0)), 120.0, 60.0) == pytest.approx((0.0, 0.5))


def test_real_route_filters_station_near_old_straight_line():
    class Provider:
        async def get_stations_near(self, latitude, longitude, radius_km):
            return [
                FuelStationCandidate(
                    provider="gdebenz", provider_station_id="road", name="На дороге",
                    latitude=0.5, longitude=0.01,
                ),
                FuelStationCandidate(
                    provider="gdebenz", provider_station_id="chord", name="У прямой",
                    latitude=0.5, longitude=0.5,
                ),
            ]

    stations = asyncio.run(find_stations_near_route(
        Provider(),
        start_latitude=0,
        start_longitude=0,
        end_latitude=0,
        end_longitude=1,
        radius_km=3,
        route_geometry=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
        route_distance_km=360,
        sample_progress_km=(60,),
    ))
    assert [item["provider_station_id"] for item in stations] == ["road"]
    assert stations[0]["route_progress_km"] < 100


def test_station_order_follows_route_progress_not_direct_distance():
    class Provider:
        async def get_stations_near(self, latitude, longitude, radius_km):
            return [
                FuelStationCandidate(
                    provider="gdebenz", provider_station_id="late", name="Позже",
                    latitude=0.2, longitude=1.0,
                ),
                FuelStationCandidate(
                    provider="gdebenz", provider_station_id="middle", name="Раньше",
                    latitude=1.0, longitude=0.5,
                ),
            ]

    stations = asyncio.run(find_stations_near_route(
        Provider(),
        start_latitude=0,
        start_longitude=0,
        end_latitude=0,
        end_longitude=1,
        radius_km=3,
        route_geometry=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
        route_distance_km=360,
        sample_progress_km=(180,),
    ))
    assert [item["provider_station_id"] for item in stations] == ["middle", "late"]
    assert [item["route_progress_km"] for item in stations] == sorted(
        item["route_progress_km"] for item in stations
    )
    assert haversine_km(0, 0, 0.2, 1.0) < haversine_km(0, 0, 1.0, 0.5)


def test_route_stations_api_uses_provider_and_requires_auth(client, login, make_user, monkeypatch, db):
    class RouteProvider:
        async def get_stations_near(self, latitude, longitude, radius_km):
            return [FuelStationCandidate(
                provider="gdebenz",
                provider_station_id="route-api",
                brand="Teboil",
                name="Тебойл",
                address="пр-кт Ветеранов, 188/1",
                latitude=59.8348,
                longitude=30.1211,
                raw={"status": "queue", "fuels_now": "95,98", "updated": "2026-09-15T10:00:00Z"},
            )]

    url = "/api/fuel/route-stations?start_lat=59.83&start_lon=30.10&end_lat=59.84&end_lon=30.14&radius_km=3"
    assert client.get(url, follow_redirects=False).status_code in {302, 303, 307}
    user = make_user("fuel-route-api")
    saved = FuelStation(
        owner_id=user.id,
        provider="gdebenz",
        provider_station_id="route-api",
        brand="Teboil",
        latitude=59.8348,
        longitude=30.1211,
    )
    db.add(saved)
    db.flush()
    add_subscription(db, user, saved)
    db.commit()
    login(user.username)
    monkeypatch.setattr(main_module, "make_gdebenz_provider", RouteProvider)
    routing = StaticRouteEngine(exact_route(
        (59.83, 30.10),
        (59.84, 30.14),
        distance_km=3.0,
        geometry=((59.83, 30.10), (59.835, 30.121), (59.84, 30.14)),
    ))
    monkeypatch.setattr(main_module, "make_route_engine", lambda: routing)
    response = client.get(url)
    assert response.status_code == 200
    payload = response.json()
    assert payload["radius_km"] == 3
    assert payload["route"]["is_approximate"] is False
    assert len(payload["route"]["geometry"]) == 3
    assert payload["stations"][0]["provider_station_id"] == "route-api"
    assert payload["stations"][0]["fuels"][0]["state"] == "candidate"
    assert payload["stations"][0]["fuels"][0]["has_queue"] is True
    assert payload["stations"][0]["station_id"] == saved.id
    assert "raw" not in payload["stations"][0]
    invalid = client.get(
        "/api/fuel/route-stations?start_lat=91&start_lon=30&end_lat=59&end_lon=30"
    )
    assert invalid.status_code == 422
    approximate = RouteResult(
        distance_km=3.1,
        duration_minutes=None,
        geometry=((59.83, 30.10), (59.84, 30.14)),
        provider="approximate",
        profile="driving",
        is_approximate=True,
        warnings=("Расстояние рассчитано приблизительно.",),
    )
    monkeypatch.setattr(
        main_module, "make_route_engine", lambda: StaticRouteEngine(approximate)
    )
    fallback = client.get(url)
    assert fallback.status_code == 200
    assert fallback.json()["route"]["is_approximate"] is True
    assert fallback.json()["route"]["warnings"]
    monkeypatch.setattr(
        main_module,
        "make_route_engine",
        lambda: StaticRouteEngine(error=RouteNotFound(
            "Автомобильный маршрут между указанными точками не найден."
        )),
    )
    no_route = client.get(url)
    assert no_route.status_code == 422
    assert "маршрут" in no_route.json()["detail"]


def test_trip_calculation_keeps_safe_reserve_and_fuel_need():
    result = calculate_trip(
        1350,
        tank_liters=60,
        consumption_l_per_100km=8.5,
        fuel_level_percent=50,
    )
    assert result["required_liters"] == 114.8
    assert result["current_range_km"] == 352.9
    assert result["safe_current_range_km"] == 247.1
    assert result["safe_full_range_km"] == 600
    assert result["next_refuel_from_km"] < result["next_refuel_to_km"]


def test_trip_calculation_missing_vehicle_data_returns_warnings():
    result = calculate_trip(
        500,
        tank_liters=None,
        consumption_l_per_100km=None,
        fuel_level_percent=50,
    )
    assert result["can_plan"] is False
    assert len(result["warnings"]) == 2


def test_trip_stop_selection_prioritizes_state_and_excludes_bad_candidates():
    calculation = calculate_trip(
        900,
        tank_liters=60,
        consumption_l_per_100km=10,
        fuel_level_percent=50,
    )

    def candidate(identifier, distance, deviation, state, *, updated_at="2026-09-15T10:00:00Z"):
        return {
            "provider_station_id": identifier,
            "name": identifier,
            "distance_from_start_km": distance,
            "distance_to_route_km": deviation,
            "updated_at": updated_at,
            "fuels": [{"fuel_type": "95", "state": state, "label": state, "symbol": "?"}],
        }

    candidates = [
        candidate("outside", 200, 15, "available"),
        candidate("no-fuel", 210, 0.1, "unavailable"),
        candidate("candidate", 205, 0.1, "candidate"),
        candidate("available", 195, 0.8, "available"),
        candidate("second", 650, 0.3, "available"),
    ]
    stops, warnings = select_recommended_stops(
        candidates,
        fuel_type="95",
        calculation=calculation,
        now=datetime(2026, 9, 15, 11, tzinfo=timezone.utc),
    )
    assert [item["provider_station_id"] for item in stops] == ["available", "second"]
    assert warnings == []


def test_trip_stop_selection_uses_cumulative_progress_and_warns_when_unsafe():
    calculation = calculate_trip(
        1_200,
        tank_liters=50,
        consumption_l_per_100km=10,
        fuel_level_percent=70,
    )

    def station(identifier, progress):
        return {
            "provider_station_id": identifier,
            "name": identifier,
            "distance_from_start_km": progress,
            "route_progress_km": progress,
            "distance_to_route_km": 0.5,
            "fuels": [{"fuel_type": "95", "state": "available"}],
        }

    stops, warnings = select_recommended_stops(
        [station("one", 250), station("two", 520), station("three", 790), station("four", 1_050)],
        fuel_type="95",
        calculation=calculation,
    )
    assert [item["distance_from_start_km"] for item in stops] == [250, 520, 790]
    assert warnings == []

    unsafe_calculation = calculate_trip(
        500,
        tank_liters=40,
        consumption_l_per_100km=10,
        fuel_level_percent=77.5,
    )
    unsafe, warnings = select_recommended_stops(
        [station("too-far", 330)],
        fuel_type="95",
        calculation=unsafe_calculation,
    )
    assert unsafe == []
    assert "Не найдена подходящая АЗС" in warnings[0]


def test_trip_plan_api_uses_owned_vehicle_and_provider_candidates(
    client, login, make_user, monkeypatch, db
):
    user = make_user("fuel-trip-api")
    vehicle = Vehicle(
        owner_id=user.id,
        display_name="Фокус",
        make="Ford",
        model="Focus 3",
        year=2014,
        current_odometer=120000,
    )
    db.add(vehicle)
    db.commit()

    class TripProvider:
        calls = 0

        async def get_stations_near(self, latitude, longitude, radius_km):
            self.calls += 1
            return [FuelStationCandidate(
                provider="gdebenz",
                provider_station_id=f"trip-{self.calls}",
                brand="Teboil",
                latitude=latitude,
                longitude=longitude,
                raw={"status": "yes", "fuels_now": "95", "updated": "2026-09-15T10:00:00Z"},
            )]

    monkeypatch.setattr(main_module, "make_gdebenz_provider", TripProvider)
    routing = StaticRouteEngine(exact_route(
        (59.0, 30.0),
        (64.0, 30.0),
        distance_km=700,
        duration_minutes=510,
        geometry=((59.0, 30.0), (60.5, 30.4), (62.5, 30.2), (64.0, 30.0)),
    ))
    monkeypatch.setattr(main_module, "make_route_engine", lambda: routing)
    login(user.username)
    response = client.post("/api/fuel/trip-plan", json={
        "vehicle_id": vehicle.id,
        "start": "59.0,30.0",
        "end": "64.0,30.0",
        "fuel_type": "95",
        "fuel_level_percent": 50,
        "tank_liters": 50,
        "consumption_l_per_100km": 10,
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload["vehicle"]["title"] == "Фокус"
    assert payload["calculation"]["required_liters"] > 60
    assert len(payload["recommended_stops"]) >= 1
    assert all(
        item["selected_fuel"]["fuel_type"] == "95"
        for item in payload["recommended_stops"]
    )
    assert payload["route"]["duration_minutes"] == 510
    assert payload["route"]["is_approximate"] is False
    assert payload["route"]["geometry"][1] == [60.5, 30.4]
    assert payload["warnings"] == []
    missing_data = client.post("/api/fuel/trip-plan", json={
        "vehicle_id": vehicle.id,
        "start": "59.0,30.0",
        "end": "60.0,30.0",
        "fuel_type": "95",
        "fuel_level_percent": 50,
    })
    assert missing_data.status_code == 200
    assert missing_data.json()["calculation"]["can_plan"] is False
    assert len(missing_data.json()["warnings"]) >= 2
    forbidden_vehicle = client.post("/api/fuel/trip-plan", json={
        "vehicle_id": vehicle.id + 999,
        "start": "59.0,30.0",
        "end": "60.0,30.0",
        "fuel_type": "95",
        "fuel_level_percent": 50,
        "tank_liters": 50,
        "consumption_l_per_100km": 10,
    })
    assert forbidden_vehicle.status_code == 404


def test_trip_plan_api_reports_fallback_and_rejects_no_route(
    client, login, make_user, monkeypatch, db
):
    user = make_user("fuel-trip-routing-errors")
    vehicle = Vehicle(
        owner_id=user.id,
        display_name="Тестовый автомобиль",
        make="Test",
        model="Route",
        year=2020,
        current_odometer=100,
    )
    db.add(vehicle)
    db.commit()
    login(user.username)

    fallback = RouteResult(
        distance_km=118,
        duration_minutes=None,
        geometry=((59.0, 30.0), (60.0, 30.0)),
        provider="approximate",
        profile="driving",
        is_approximate=True,
        warnings=("Сервис дорожных маршрутов временно недоступен. Расстояние рассчитано приблизительно.",),
    )
    monkeypatch.setattr(main_module, "make_route_engine", lambda: StaticRouteEngine(fallback))
    invalid = client.post("/api/fuel/trip-plan", json={
        "vehicle_id": vehicle.id,
        "start": "nan,30.0",
        "end": "60.0,30.0",
        "fuel_type": "95",
        "fuel_level_percent": 100,
        "tank_liters": 50,
        "consumption_l_per_100km": 10,
    })
    assert invalid.status_code == 400
    assert "Координаты" in invalid.json()["detail"]
    response = client.post("/api/fuel/trip-plan", json={
        "vehicle_id": vehicle.id,
        "start": "59.0,30.0",
        "end": "60.0,30.0",
        "fuel_type": "95",
        "fuel_level_percent": 100,
        "tank_liters": 50,
        "consumption_l_per_100km": 10,
    })
    assert response.status_code == 200
    assert response.json()["route"]["is_approximate"] is True
    assert "приблизительно" in response.json()["warnings"][0]

    monkeypatch.setattr(
        main_module,
        "make_route_engine",
        lambda: StaticRouteEngine(error=RouteNotFound(
            "Автомобильный маршрут между указанными точками не найден."
        )),
    )
    response = client.post("/api/fuel/trip-plan", json={
        "vehicle_id": vehicle.id,
        "start": "59.0,30.0",
        "end": "60.0,30.0",
        "fuel_type": "95",
        "fuel_level_percent": 100,
        "tank_liters": 50,
        "consumption_l_per_100km": 10,
    })
    assert response.status_code == 422
    assert "маршрут" in response.json()["detail"]


def test_fuel_page_renders_with_registered_moscow_datetime_filter(client, login, make_user):
    make_user("fuel-page")
    login("fuel-page")
    response = client.get("/fuel")
    assert response.status_code == 200
    assert "Пока ничего не отслеживается" in response.text
    assert "Поездка" in response.text
    assert '/static/style.css?v=76' in response.text
    assert response.headers["cache-control"] == "no-store"


def test_fuel_dashboard_renders_forecast_stale_and_collector_error(client, login, make_user, db):
    user = make_user("fuel-dashboard-states")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    station = FuelStation(
        owner_id=user.id,
        provider="gdebenz",
        provider_station_id="dashboard-station",
        brand="Teboil",
        address="Ветеранов, 188/1",
        latitude=59.9,
        longitude=30.2,
        last_successful_poll_at=now - timedelta(minutes=5),
    )
    db.add(station)
    db.flush()
    add_subscription(db, user, station, ("95",))
    db.add(
        FuelObservation(
            station_id=station.id,
            fuel_type="95",
            state="unavailable",
            observed_at=now - timedelta(minutes=5),
            is_stale=True,
        )
    )
    db.add(
        FuelForecast(
            station_id=station.id,
            fuel_type="95",
            generated_at=now,
            expected_at=now + timedelta(hours=2),
            range_from=now + timedelta(hours=1),
            range_to=now + timedelta(hours=3),
            confidence=0.72,
            model_version=FORECAST_VERSION,
            reason_json={},
        )
    )
    db.commit()
    login("fuel-dashboard-states")
    previous_error = main_module.collector_health.last_error
    main_module.collector_health.last_error = "GdeBenz HTTP 503"
    try:
        response = client.get("/fuel")
    finally:
        main_module.collector_health.last_error = previous_error
    assert response.status_code == 200
    assert "Обновление задерживается" in response.text
    assert "GdeBenz HTTP 503" not in response.text
    assert "НЕТ ДАННЫХ" in response.text
    assert "72%" not in response.text


def test_fuel_dashboard_states_brands_and_privacy_are_built_in_batches(db, make_user):
    first = make_user("fuel-dashboard-a")
    second = make_user("fuel-dashboard-b")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    shared = FuelStation(
        owner_id=first.id, provider="gdebenz", provider_station_id="shared-dashboard",
        brand="Teboil", address="Ветеранов, 188/1", latitude=59.8, longitude=30.1,
    )
    private = FuelStation(
        owner_id=second.id, provider="gdebenz", provider_station_id="private-dashboard",
        brand="Татнефть", address="Чужая АЗС", latitude=59.7, longitude=30.2,
    )
    db.add_all([shared, private])
    db.flush()
    add_subscription(db, first, shared, ("95", "98"))
    add_subscription(db, second, shared, ("100",))
    add_subscription(db, second, private, ("95",))
    before = FuelObservation(
        station_id=shared.id, fuel_type="95", state="unavailable", observed_at=now - timedelta(minutes=8),
        source_updated_at=now - timedelta(minutes=8),
    )
    available = FuelObservation(
        station_id=shared.id, fuel_type="95", state="available", observed_at=now - timedelta(minutes=3),
        source_updated_at=now - timedelta(minutes=3),
    )
    confirmed = FuelObservation(
        station_id=shared.id, fuel_type="95", state="available", observed_at=now - timedelta(minutes=1),
        source_updated_at=now - timedelta(minutes=1),
    )
    candidate = FuelObservation(
        station_id=shared.id, fuel_type="98", state="available", observed_at=now - timedelta(minutes=2),
        source_updated_at=now - timedelta(minutes=2),
    )
    db.add_all([before, available, confirmed, candidate])
    db.flush()
    db.add(FuelDeliveryEvent(
        station_id=shared.id, fuel_type="98", window_start=before.observed_at,
        window_end=candidate.observed_at, estimated_at=candidate.observed_at,
        event_type="candidate_appearance", confidence=.4, appearance_confidence=.4,
        before_observation_id=before.id, after_observation_id=candidate.id,
        detection_reason="dashboard candidate", evidence_json={}, detector_version="test",
    ))
    db.commit()
    first_id = first.id

    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(app_engine, "before_cursor_execute", record)
    try:
        dashboard = load_fuel_dashboard(db, first_id, stale_after_minutes=120, current_at=now)
    finally:
        event.remove(app_engine, "before_cursor_execute", record)

    assert len(statements) == 5
    assert [station["id"] for station in dashboard.stations] == [shared.id]
    assert dashboard.brands == ["Teboil"]
    assert {fuel["fuel_type"]: fuel["state"] for fuel in dashboard.stations[0]["fuels"]} == {
        "95": "available", "98": "candidate"
    }
    assert dashboard.stations[0]["summary_state"] == "available"


def test_delivery_event_does_not_keep_current_availability_alive(db, make_user):
    user = make_user("fuel-dashboard-expired-event")
    current_at = datetime(2026, 9, 15, 20)
    station = FuelStation(
        owner_id=user.id,
        provider="gdebenz",
        provider_station_id="expired-event",
        brand="Teboil",
        latitude=59.8,
        longitude=30.1,
    )
    db.add(station)
    db.flush()
    add_subscription(db, user, station, ("95",))
    before = FuelObservation(
        station_id=station.id,
        fuel_type="95",
        state="unavailable",
        observed_at=current_at - timedelta(hours=5),
        source_updated_at=current_at - timedelta(hours=5),
    )
    after = FuelObservation(
        station_id=station.id,
        fuel_type="95",
        state="available",
        observed_at=current_at - timedelta(hours=4),
        source_updated_at=current_at - timedelta(hours=4),
    )
    db.add_all([before, after])
    db.flush()
    db.add(FuelDeliveryEvent(
        station_id=station.id,
        fuel_type="95",
        window_start=before.observed_at,
        window_end=after.observed_at,
        estimated_at=after.observed_at,
        event_type="confirmed_delivery",
        confidence=.9,
        appearance_confidence=.9,
        delivery_confidence=.9,
        before_observation_id=before.id,
        after_observation_id=after.id,
        detection_reason="historical delivery",
        evidence_json={},
        detector_version="test",
    ))
    db.commit()

    dashboard = load_fuel_dashboard(
        db,
        user.id,
        stale_after_minutes=120,
        current_at=current_at,
    )

    assert dashboard.stations[0]["fuels"][0]["state"] == "unknown"


def test_fuel_settings_default_to_env_and_database_override_persists(db):
    default = get_fuel_runtime_settings(db)
    assert default.source == "env"
    assert default.poll_interval_seconds == main_module.settings.fuel_poll_interval_seconds
    assert default.comments_poll_interval_seconds == main_module.settings.fuel_comments_poll_interval_seconds

    save_fuel_runtime_settings(
        db,
        monitor_enabled=False,
        poll_interval_seconds=120,
        comments_poll_interval_seconds=600,
        stale_after_minutes=90,
        nearby_radius_km=4,
    )
    with SessionLocal() as recreated_session:
        saved = get_fuel_runtime_settings(recreated_session)
    assert saved.source == "database"
    assert saved.monitor_enabled is False
    assert saved.poll_interval_seconds == 120
    assert saved.comments_poll_interval_seconds == 600
    assert saved.stale_after_minutes == 90
    assert saved.nearby_radius_km == 4


@pytest.mark.parametrize(
    ("poll_interval", "comments_interval"),
    [(59, 300), (60, 299), (86_401, 300), (60, 604_801)],
)
def test_fuel_settings_reject_invalid_intervals(db, poll_interval, comments_interval):
    with pytest.raises(ValueError):
        save_fuel_runtime_settings(
            db,
            monitor_enabled=True,
            poll_interval_seconds=poll_interval,
            comments_poll_interval_seconds=comments_interval,
            stale_after_minutes=120,
            nearby_radius_km=3,
        )
    assert db.get(FuelMonitorSettings, 1) is None


def test_fuel_settings_routes_render_save_and_toggle(client, login, make_user):
    make_user("fuel-settings")
    login("fuel-settings")
    response = client.get("/fuel/settings")
    assert response.status_code == 200
    assert "Настройки бензина" in response.text
    assert "Проверить сейчас" in response.text

    response = client.post(
        "/fuel/settings",
        data={
            "poll_interval_seconds": "120",
            "comments_poll_interval_seconds": "600",
            "stale_after_minutes": "90",
            "nearby_radius_km": "4",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with SessionLocal() as db:
        saved = get_fuel_runtime_settings(db)
    assert saved.monitor_enabled is False
    assert saved.poll_interval_seconds == 120

    invalid = client.post(
        "/fuel/settings",
        data={
            "monitor_enabled": "on",
            "poll_interval_seconds": "59",
            "comments_poll_interval_seconds": "600",
            "stale_after_minutes": "90",
            "nearby_radius_km": "4",
        },
    )
    assert invalid.status_code == 400
    assert "допустимо от 60" in invalid.text


def test_repeated_fuel_settings_save_only_wakes_scheduler(client, login, make_user, monkeypatch):
    make_user("fuel-settings-wakeup")
    login("fuel-settings-wakeup")

    class WakeupSpy:
        calls = 0

        def set(self):
            self.calls += 1

    wakeup = WakeupSpy()
    monkeypatch.setattr(main_module, "fuel_scheduler_wakeup", wakeup)
    data = {
        "monitor_enabled": "on",
        "poll_interval_seconds": "300",
        "comments_poll_interval_seconds": "900",
        "stale_after_minutes": "120",
        "nearby_radius_km": "3",
    }
    assert client.post("/fuel/settings", data=data, follow_redirects=False).status_code == 303
    assert client.post("/fuel/settings", data=data, follow_redirects=False).status_code == 303
    assert wakeup.calls == 2


def test_station_settings_update_fuels_and_enabled_state(client, login, make_user, db):
    user = make_user("fuel-station-settings")
    station = FuelStation(
        owner_id=user.id,
        provider="gdebenz",
        provider_station_id="settings-station",
        brand="Teboil",
        latitude=59.9,
        longitude=30.2,
    )
    db.add(station)
    db.flush()
    subscription = add_subscription(db, user, station)
    db.commit()
    station_id = station.id
    login("fuel-station-settings")

    assert client.get(f"/fuel/{station_id}/settings").status_code == 200
    response = client.post(
        f"/fuel/{station_id}/settings",
        data={"fuel_types": ["95", "100"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with SessionLocal() as check_db:
        updated = check_db.get(FuelStationSubscription, subscription.id)
    assert updated.enabled is False
    assert set(updated.tracked_fuel_types) == {"95", "100"}


def test_scheduler_rereads_runtime_interval(monkeypatch):
    runtimes = iter(
        (
            FuelRuntimeSettings(True, 60, 300, 120, 3, "database"),
            FuelRuntimeSettings(True, 120, 600, 120, 3, "database"),
        )
    )
    waits = []

    def fake_settings(_db):
        return next(runtimes)

    async def fake_cycle(**_kwargs):
        return {"stations": 0, "success": 0, "failed": 0, "observations": 0}

    async def fake_wait(awaitable, timeout):
        awaitable.close()
        waits.append(timeout)
        if len(waits) == 2:
            raise asyncio.CancelledError
        raise TimeoutError

    monkeypatch.setattr(main_module, "get_fuel_runtime_settings", fake_settings)
    monkeypatch.setattr(main_module, "run_fuel_poll_cycle", fake_cycle)
    monkeypatch.setattr(main_module.asyncio, "wait_for", fake_wait)
    main_module.fuel_scheduler_wakeup.clear()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main_module.fuel_poll_scheduler())
    assert waits == [60, 120]


def test_poll_now_does_not_start_when_cycle_is_locked(client, login, make_user, monkeypatch):
    make_user("fuel-manual-lock")
    login("fuel-manual-lock")

    class BusyLock:
        def locked(self):
            return True

    monkeypatch.setattr(main_module, "fuel_poll_lock", BusyLock())
    response = client.post("/fuel/poll-now", follow_redirects=False)
    assert response.status_code == 303
    assert "Проверка уже выполняется" in unquote(response.headers["location"])


def test_poll_now_is_debounced(client, login, make_user, monkeypatch):
    make_user("fuel-manual-debounce")
    login("fuel-manual-debounce")
    calls = []

    async def fake_cycle(**_kwargs):
        calls.append(1)
        return {"stations": 0, "success": 0, "failed": 0, "observations": 0}

    monkeypatch.setattr(main_module, "run_fuel_poll_cycle", fake_cycle)
    main_module.collector_health.last_manual_poll_started_at = None
    first = client.post("/fuel/poll-now", follow_redirects=False)
    second = client.post("/fuel/poll-now", follow_redirects=False)
    assert first.status_code == 303
    assert second.status_code == 303
    assert "Повторная проверка" in unquote(second.headers["location"])
    assert len(calls) == 1


def test_gdebenz_provider_follows_redirects_sends_headers_and_parses_stations():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/nearby":
            return httpx.Response(301, headers={"Location": "/api/nearby-final"}, request=request)
        return httpx.Response(200, json={"stations": [{"osm_id": "usr_-yN7-ZKW2RA", "brand": "Teboil", "name": "Тебойл", "addr": "пр-кт Ветеранов, 188/1", "lat": 59.83486, "lon": 30.120975}]}, request=request)

    provider = GdeBenzProvider(user_agent="test-agent", transport=httpx.MockTransport(handler))
    stations = asyncio.run(provider.get_stations_near(59.83486, 30.120975, 3))
    assert stations[0].provider_station_id == "usr_-yN7-ZKW2RA"
    assert len(requests) == 2
    assert requests[0].headers["user-agent"] == "test-agent"
    assert requests[0].headers["accept"] == "application/json"
    assert requests[0].headers["referer"] == "https://gdebenz.ru/"


def test_gdebenz_marks_use_persistent_frontend_client_id_and_fresh_request(tmp_path):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/recent"):
            created_at = "2026-09-14 21:17:35" if request.url.params.get("fp") else "2026-09-14 20:23:33"
            return httpx.Response(200, json=[{"status": "queue", "detail": "92, 95, ДТ", "created_at": created_at}], request=request)
        return httpx.Response(200, json={"cvt": "station-token"}, request=request)

    client_id_path = tmp_path / "gdebenz_client_id"
    provider = GdeBenzProvider(transport=httpx.MockTransport(handler), client_id_path=client_id_path)
    feed = asyncio.run(provider.get_station_marks("1947395383"))
    first_client_id = client_id_path.read_text(encoding="ascii")
    recreated = GdeBenzProvider(transport=httpx.MockTransport(handler), client_id_path=client_id_path)
    asyncio.run(recreated.get_station_marks("1947395383"))

    recent = next(request for request in requests if request.url.path.endswith("/recent"))
    assert feed.latest_source_at == datetime(2026, 9, 14, 21, 17, 35)
    assert len(first_client_id) == 32
    assert recent.url.params["fp"] == first_client_id
    assert recent.url.params["cvt"] == "station-token"
    assert recent.url.params["_"].isdigit()
    assert all(request.url.params.get("fp") == first_client_id for request in requests if request.url.path.endswith("/recent"))


def test_gdebenz_marks_flag_degraded_when_client_id_cannot_be_persisted(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/recent"):
            return httpx.Response(200, json=[], request=request)
        return httpx.Response(200, json={"cvt": ""}, request=request)

    provider = GdeBenzProvider(
        transport=httpx.MockTransport(handler),
        client_id_path=tmp_path,
    )
    feed = asyncio.run(provider.get_station_marks("1947395383"))
    assert feed.freshness_degraded is True


def test_gdebenz_chat_parses_messages_and_merges_badges():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/comments-badges":
            return httpx.Response(200, json={"reliable": [540191], "onsite": [534095], "tiers": {"540191": 2}}, request=request)
        return httpx.Response(200, json={"comments": [{"id": 534095, "author_id": 540191, "author_name": "Владимир В.",
            "body": "бензовоз слился, привез 95 и 92", "created_at": "2026-09-14T14:42:15.297916Z", "reactions": {}}], "next_cursor": None}, request=request)

    provider = GdeBenzProvider(transport=httpx.MockTransport(handler))
    feed = asyncio.run(provider.get_station_chat("1947395383"))
    assert feed.items[0]["author_reliable"] is True
    assert feed.items[0]["author_tier"] == 2
    assert feed.items[0]["on_site"] is True
    assert requests[0].url.host == "api.gdebenz.ru"
    assert requests[1].method == "POST"
    assert requests[1].url.path == "/api/comments-badges"


def test_gdebenz_http_status_error_logs_response_context(monkeypatch, caplog):
    async def no_sleep(_delay):
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="blocked by test provider", request=request)

    monkeypatch.setattr(fuel_module.asyncio, "sleep", no_sleep)
    provider = GdeBenzProvider(transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderUnavailable), caplog.at_level("WARNING"):
        asyncio.run(provider.get_stations_near(59.83486, 30.120975, 3))
    assert "status=403" in caplog.text
    assert "https://gdebenz.ru/api/nearby" in caplog.text
    assert "blocked by test provider" in caplog.text


class FakeProvider:
    def __init__(self):
        self.calls = []

    async def get_stations_near(self, latitude, longitude, radius_km):
        self.calls.append((latitude, longitude))
        return [FuelStationCandidate(provider="gdebenz", provider_station_id="station", latitude=latitude, longitude=longitude,
                                     raw={"status": "yes", "fuels_now": "95,100", "last_at": "2026-09-14 12:00:00", "confirmations": 4, "confidence_base": 0.7})]

    async def get_station_marks(self, provider_station_id, limit=12):
        item = {"status": "yes", "detail": "95", "created_at": "2026-09-14 12:00:00"}
        return FuelMarksFeed(items=[item], latest_source_at=datetime(2026, 9, 14, 12))

    async def get_station_chat(self, provider_station_id, limit=20):
        return FuelChatFeed()


def test_collector_persists_only_selected_fuels_and_skips_disabled(make_user):
    user = make_user("poller")
    with SessionLocal() as db:
        enabled = FuelStation(owner_id=user.id, provider="gdebenz", provider_station_id="station", latitude=59.9, longitude=30.2)
        disabled = FuelStation(owner_id=user.id, provider="gdebenz", provider_station_id="disabled", latitude=60, longitude=30.2, enabled=False)
        db.add_all([enabled, disabled])
        db.flush()
        add_subscription(db, user, enabled, ("95", "100"))
        add_subscription(db, user, disabled, ("95",), enabled=False)
        db.commit()
        station_id = enabled.id
    provider = FakeProvider()
    summary = asyncio.run(run_fuel_poll_cycle(session_factory=SessionLocal, provider=provider, stale_after_minutes=120, nearby_radius_km=3, marks_chat_due=True))
    with SessionLocal() as db:
        observations = db.query(FuelObservation).filter_by(station_id=station_id).all()
        marks = db.query(FuelStationMark).filter_by(station_id=station_id).all()
        station = db.get(FuelStation, station_id)
    assert summary == {"stations": 1, "success": 1, "failed": 0, "observations": 2}
    assert {item.fuel_type for item in observations} == {"95", "100"}
    assert station.last_successful_poll_at is not None
    assert len(marks) == 1
    assert len(provider.calls) == 1
    assert provider.calls[0] == (59.9, 30.2)


def _create_polled_station(make_user, username):
    user = make_user(username)
    with SessionLocal() as db:
        station = FuelStation(owner_id=user.id, provider="gdebenz", provider_station_id=username,
            latitude=59.9, longitude=30.2)
        db.add(station)
        db.flush()
        add_subscription(db, user, station, ("95",))
        db.commit()
        return station.id


def test_mark_ingestion_is_idempotent_and_keeps_identical_content_at_different_times(make_user):
    station_id = _create_polled_station(make_user, "mark-dedupe")

    class MarkProvider:
        poll = 0

        async def get_stations_near(self, latitude, longitude, radius_km):
            return [FuelStationCandidate(provider="gdebenz", provider_station_id="mark-dedupe",
                latitude=latitude, longitude=longitude,
                raw={"status": "queue", "fuelsNow": "95", "updated": "2026-09-14 21:20:00"})]

        async def get_station_marks(self, provider_station_id, limit=12):
            self.poll += 1
            first = {"status": "queue", "detail": "92,95,ДТ", "created_at": "2026-09-14 21:15:00"}
            marks = [first, dict(first)]
            if self.poll > 1:
                marks += [
                    {"status": "queue", "detail": "92,95,ДТ", "created_at": "2026-09-14 21:17:00"},
                    {"status": "queue", "detail": "92,95,ДТ", "created_at": "2026-09-14 21:20:00"},
                ]
            return FuelMarksFeed(items=marks, latest_source_at=parse_source_datetime(marks[-1]["created_at"]))

        async def get_station_chat(self, provider_station_id, limit=20):
            return FuelChatFeed()

    provider = MarkProvider()
    first = asyncio.run(run_fuel_poll_cycle(session_factory=SessionLocal, provider=provider,
        stale_after_minutes=120, nearby_radius_km=3, marks_chat_due=True))
    second = asyncio.run(run_fuel_poll_cycle(session_factory=SessionLocal, provider=provider,
        stale_after_minutes=120, nearby_radius_km=3, marks_chat_due=True))
    with SessionLocal() as db:
        marks = db.query(FuelStationMark).filter_by(station_id=station_id).order_by(FuelStationMark.source_created_at).all()
        observations = db.query(FuelObservation).filter_by(station_id=station_id).all()
    assert first["failed"] == second["failed"] == 0
    assert [mark.source_created_at.minute for mark in marks] == [15, 17, 20]
    assert len(observations) == 2


def test_chat_ingestion_deduplicates_stable_provider_message_id(make_user):
    station_id = _create_polled_station(make_user, "chat-dedupe")

    class ChatProvider:
        async def get_stations_near(self, latitude, longitude, radius_km):
            return [FuelStationCandidate(provider="gdebenz", provider_station_id="chat-dedupe",
                latitude=latitude, longitude=longitude,
                raw={"status": "yes", "fuelsNow": "95", "updated": "2026-09-14 14:42:15"})]

        async def get_station_marks(self, provider_station_id, limit=12):
            return FuelMarksFeed()

        async def get_station_chat(self, provider_station_id, limit=20):
            item = {"id": 534095, "author_id": 540191, "author_name": "Владимир В.",
                "body": "бензовоз слился, привез 95 и 92", "created_at": "2026-09-14T14:42:15Z",
                "reactions": {}, "author_reliable": True, "on_site": True}
            return FuelChatFeed(items=[item], latest_source_at=parse_source_datetime(item["created_at"]))

    provider = ChatProvider()
    for _ in range(2):
        summary = asyncio.run(run_fuel_poll_cycle(session_factory=SessionLocal, provider=provider,
            stale_after_minutes=120, nearby_radius_km=3, marks_chat_due=True))
        assert summary["failed"] == 0
    with SessionLocal() as db:
        messages = db.query(FuelStationChatMessage).filter_by(station_id=station_id).all()
    assert len(messages) == 1
    assert messages[0].provider_message_id == "534095"


def test_chat_initial_backfill_is_bounded_and_later_polls_only_latest_page(make_user):
    station_id = _create_polled_station(make_user, "chat-pages")

    class PagedChatProvider:
        cursors = []

        async def get_stations_near(self, latitude, longitude, radius_km):
            return [FuelStationCandidate(provider="gdebenz", provider_station_id="chat-pages",
                latitude=latitude, longitude=longitude,
                raw={"status": "yes", "fuelsNow": "95", "updated": "2026-09-14 15:00:00"})]

        async def get_station_marks(self, provider_station_id, limit=12):
            return FuelMarksFeed()

        async def get_station_chat(self, provider_station_id, limit=20, cursor=None):
            self.cursors.append(cursor)
            page = 0 if cursor is None else int(cursor)
            item = {"id": page + 1, "author_id": page + 10, "body": f"message {page}",
                "created_at": f"2026-09-14T14:{40 + page:02d}:00Z"}
            return FuelChatFeed(items=[item], next_cursor=page + 1)

    provider = PagedChatProvider()
    asyncio.run(run_fuel_poll_cycle(session_factory=SessionLocal, provider=provider,
        stale_after_minutes=120, nearby_radius_km=3, marks_chat_due=True))
    asyncio.run(run_fuel_poll_cycle(session_factory=SessionLocal, provider=provider,
        stale_after_minutes=120, nearby_radius_km=3, marks_chat_due=True))
    with SessionLocal() as db:
        assert db.query(FuelStationChatMessage).filter_by(station_id=station_id).count() == 3
    assert provider.cursors == [None, 1, 2, None]


def test_marks_and_chat_timestamps_are_utc_and_render_once_in_moscow():
    chat_at = parse_source_datetime("2026-09-14T14:42:15Z")
    mark_at = parse_source_datetime("2026-09-14 21:15:13")
    assert chat_at == datetime(2026, 9, 14, 14, 42, 15)
    assert mark_at == datetime(2026, 9, 14, 21, 15, 13)
    assert format_msk(chat_at, "%d.%m %H:%M") == "14.09 17:42"
    assert format_msk(mark_at, "%d.%m %H:%M") == "15.09 00:15"


def test_stale_marks_feed_does_not_turn_available_aggregate_into_unavailable(make_user):
    station_id = _create_polled_station(make_user, "stale-marks")

    class StaleMarksProvider:
        async def get_stations_near(self, latitude, longitude, radius_km):
            return [FuelStationCandidate(provider="gdebenz", provider_station_id="stale-marks",
                latitude=latitude, longitude=longitude,
                raw={"status": "yes", "fuelsNow": "95", "updated": "2026-09-14 21:15:00"})]

        async def get_station_marks(self, provider_station_id, limit=12):
            item = {"status": "yes", "detail": "92,95,ДТ", "created_at": "2026-09-14 20:23:00"}
            return FuelMarksFeed(items=[item], latest_source_at=parse_source_datetime(item["created_at"]))

        async def get_station_chat(self, provider_station_id, limit=20):
            return FuelChatFeed()

    summary = asyncio.run(run_fuel_poll_cycle(session_factory=SessionLocal, provider=StaleMarksProvider(),
        stale_after_minutes=120, nearby_radius_km=3, marks_chat_due=True))
    with SessionLocal() as db:
        observed = db.query(FuelObservation).filter_by(station_id=station_id).one()
    assert summary["failed"] == 0
    assert observed.state == "available"
    assert main_module.collector_health.last_marks_feed_stale is True


def test_collector_uses_configured_radius_and_matches_station_id(make_user):
    user = make_user("radius-poller")
    with SessionLocal() as db:
        station = FuelStation(owner_id=user.id, provider="gdebenz", provider_station_id="usr_-yN7-ZKW2RA", latitude=59.834818865764575, longitude=30.12108201831411)
        db.add(station)
        db.flush()
        add_subscription(db, user, station, ("95",))
        db.commit()
        station_id = station.id

    class RadiusProvider:
        radius = None

        async def get_stations_near(self, latitude, longitude, radius_km):
            self.radius = radius_km
            if radius_km < 3:
                return []
            return [FuelStationCandidate(provider="gdebenz", provider_station_id="usr_-yN7-ZKW2RA", latitude=latitude, longitude=longitude, distance_meters=2100,
                                         raw={"status": "queue", "fuels_now": "95", "last_at": "2026-09-14 12:00:00"})]

        async def get_station_marks(self, provider_station_id, limit=12):
            return FuelMarksFeed()

        async def get_station_chat(self, provider_station_id, limit=20):
            return FuelChatFeed()

    provider = RadiusProvider()
    summary = asyncio.run(run_fuel_poll_cycle(session_factory=SessionLocal, provider=provider, stale_after_minutes=120, nearby_radius_km=3))
    assert provider.radius == 3
    assert summary["success"] == 1
    with SessionLocal() as db:
        observation = db.query(FuelObservation).one()
    assert observation.station_id == station_id
    assert observation.state == "available"
    assert observation.source_status == "queue"


def observation(identifier, state, minute, *, fuel_type="95", stale=False, confidence=0.8, confirmations=5,
                source_minute=None):
    return FuelObservation(id=identifier, station_id=1, fuel_type=fuel_type, state=state,
        observed_at=datetime(2026, 9, 14, 15) + timedelta(minutes=minute), is_stale=stale,
        source_updated_at=(datetime(2026, 9, 14, 15) + timedelta(minutes=source_minute)
                           if source_minute is not None else None),
        confidence=confidence, confirmations=confirmations)


def test_repeated_polls_of_same_source_snapshot_stay_candidate():
    events = detect_delivery_events([
        observation(1, "unavailable", 10, source_minute=10),
        observation(2, "unavailable", 15, source_minute=15),
        observation(3, "available", 20, source_minute=20),
        observation(4, "available", 25, source_minute=20),
    ])
    assert len(events) == 1
    assert events[0]["window_start"] == datetime(2026, 9, 14, 15, 15)
    assert events[0]["window_end"] == datetime(2026, 9, 14, 15, 20)
    assert events[0]["event_type"] == "candidate_appearance"
    assert events[0]["confidence"] < 0.7
    assert events[0]["evidence_json"]["following_available_count"] == 2
    assert events[0]["evidence_json"]["distinct_positive_source_snapshots"] == 1


def test_delivery_detector_rejects_noise_unknown_large_gap_and_stale():
    noise = detect_delivery_events([
        observation(1, "available", 10), observation(2, "unavailable", 15),
        observation(3, "available", 20),
    ])
    assert len(noise) == 1
    assert noise[0]["event_type"] == "candidate_appearance"
    assert detect_delivery_events([observation(1, "unknown", 10), observation(2, "unknown", 15), observation(3, "available", 20), observation(4, "available", 25)]) == []
    large_gap = [observation(1, "unavailable", 0), observation(2, "unavailable", 1),
                 observation(3, "available", 2), observation(4, "available", 3)]
    large_gap[2].observed_at += timedelta(hours=8)
    large_gap[3].observed_at += timedelta(hours=8)
    assert detect_delivery_events(large_gap) == []
    assert detect_delivery_events([observation(1, "unavailable", 10), observation(2, "unavailable", 15, stale=True),
                                   observation(3, "available", 20), observation(4, "available", 25)]) == []


def chat_message(message_id, body, *, minute=20, on_site=False, reliable=False, tier=0):
    return FuelStationChatMessage(station_id=1, provider="gdebenz", provider_message_id=str(message_id),
        author_id=str(message_id), body=body, source_created_at=datetime(2026, 9, 14, 15) + timedelta(minutes=minute),
        on_site=on_site, author_reliable=reliable, author_tier=tier)


def test_chat_rules_are_fuel_specific_and_metadata_weighted():
    positive = chat_message(1, "Бензовоз слился, привез 95 и 92", on_site=True, reliable=True, tier=3)
    waiting = chat_message(2, "Говорят, скоро привезут бензин")
    assert chat_delivery_signal(positive, "95") > 0.4
    assert chat_delivery_signal(positive, "100") == 0
    assert chat_delivery_signal(waiting, "95", allow_station_level=True) < 0


def test_short_appearance_is_not_a_delivery_or_forecast_training_event():
    events = detect_delivery_events([
        observation(1, "unavailable", 0), observation(2, "unavailable", 5),
        observation(3, "low", 10), observation(4, "low", 15), observation(5, "unavailable", 20),
    ])
    assert len(events) == 1
    assert events[0]["event_type"] == "candidate_appearance"
    assert events[0]["appearance_confidence"] < 0.5
    assert events[0]["delivery_confidence"] < 0.6
    assert events[0]["availability_duration_minutes"] == 10


def source_mark(key, minute, status, detail, *, on_site=False, reliable=False):
    point = datetime(2026, 9, 14, 15) + timedelta(minutes=minute)
    return FuelStationMark(
        station_id=1,
        provider="gdebenz",
        source_key=key,
        text=detail,
        source_created_at=point,
        fetched_at=point,
        raw_data={
            "status": status,
            "detail": detail,
            "on_site": on_site,
            "author_reliable": reliable,
            "acct_ok": reliable,
        },
    )


def test_suspicious_98_production_case_stays_low_confidence_candidate():
    observations = [
        observation(1, "unavailable", 18, fuel_type="98", source_minute=18),
        observation(2, "available", 20, fuel_type="98", source_minute=20),
        observation(3, "available", 22, fuel_type="98", source_minute=20),
    ]
    marks = [source_mark("yes", 20, "yes", "98")]
    marks.extend(source_mark(f"no-{index}", 19 + index, "no", "92, 95, ДТ") for index in range(3))
    event = detect_delivery_events(observations, marks=marks)[0]

    assert event["event_type"] == "candidate_appearance"
    assert event["appearance_confidence"] < 0.5
    assert event["delivery_confidence"] < 0.6
    assert event["availability_duration_minutes"] == 2
    assert event["evidence_json"]["mark_support_count"] == 1
    assert event["evidence_json"]["mark_conflict_count"] == 3


def test_fresh_source_updates_and_supporting_marks_confirm_availability():
    observations = [
        observation(1, "unavailable", 0, source_minute=0),
        observation(2, "available", 5, source_minute=5),
        observation(3, "available", 15, source_minute=15),
        observation(4, "available", 30, source_minute=30),
    ]
    marks = [source_mark(str(index), minute, "yes", "92, 95, ДТ") for index, minute in enumerate((5, 12, 24))]
    event = detect_delivery_events(observations, marks=marks)[0]

    assert event["event_type"] == "confirmed_availability"
    assert event["appearance_confidence"] >= 0.7


def test_reliable_on_site_mark_can_confirm_availability():
    observations = [
        observation(1, "unavailable", 0, source_minute=0),
        observation(2, "available", 5, source_minute=5),
    ]
    mark = source_mark("trusted", 5, "yes", "95", on_site=True, reliable=True)
    event = detect_delivery_events(observations, marks=[mark])[0]

    assert event["event_type"] == "confirmed_availability"
    assert event["evidence_json"]["trusted_on_site_mark"] is True


def test_direct_delivery_chat_confirms_only_matching_fuel():
    observations = []
    identifier = 1
    for fuel in ("95", "98"):
        for state, minute in (("unavailable", 0), ("unavailable", 5), ("available", 10), ("available", 15)):
            item = observation(identifier, state, minute, fuel_type=fuel)
            item.fuel_type = fuel
            observations.append(item)
            identifier += 1
    message = chat_message(20, "бензовоз слился, привез 95 и 92", minute=8, on_site=True, reliable=True)
    events = {event["fuel_type"]: event for event in detect_delivery_events(observations, chat_messages=[message])}
    assert events["95"]["event_type"] == "confirmed_delivery"
    assert events["98"]["event_type"] != "confirmed_delivery"
    assert events["95"]["estimated_at"] == message.source_created_at
    assert events["95"]["evidence_json"]["availability_started_at"] != events["95"]["evidence_json"]["delivery_window_start"]


def test_waiting_chat_never_confirms_delivery():
    observations = [
        observation(1, "unavailable", 0), observation(2, "unavailable", 5),
        observation(3, "available", 10), observation(4, "available", 15),
    ]
    message = chat_message(21, "говорят скоро привезут бензин", minute=8)
    event = detect_delivery_events(observations, chat_messages=[message])[0]
    assert event["event_type"] != "confirmed_delivery"
    assert event["evidence_json"]["chat_score"] < 0

    waiting_for_truck = chat_message(22, "ждём бензовоз", minute=8)
    event = detect_delivery_events(observations, chat_messages=[waiting_for_truck])[0]
    assert event["event_type"] == "candidate_appearance"
    assert event["evidence_json"]["chat_score"] < 0


def test_durable_and_multi_fuel_appearances_raise_delivery_confidence():
    short = detect_delivery_events([
        observation(1, "unavailable", 0), observation(2, "unavailable", 5),
        observation(3, "available", 10), observation(4, "available", 15), observation(5, "unavailable", 20),
    ])[0]
    durable_observations = []
    identifier = 10
    for fuel in ("95", "98", "100"):
        for state, minute in (("unavailable", 0), ("unavailable", 5), ("available", 10),
                              ("available", 20), ("available", 35), ("available", 60)):
            item = observation(identifier, state, minute, fuel_type=fuel, source_minute=minute)
            item.fuel_type = fuel
            durable_observations.append(item)
            identifier += 1
    durable = detect_delivery_events(durable_observations)
    assert all(event["delivery_confidence"] > short["delivery_confidence"] for event in durable)
    assert all(event["event_type"] == "probable_delivery" for event in durable)


def test_strong_multi_fuel_delivery_with_trusted_comment_is_confirmed():
    observations = []
    identifier = 100
    for fuel in ("95", "98", "100"):
        for state, minute in (("unavailable", 0), ("available", 5), ("available", 20), ("available", 50)):
            observations.append(observation(
                identifier, state, minute, fuel_type=fuel, source_minute=minute
            ))
            identifier += 1
    message = chat_message(
        101, "бензовоз приехал, привезли 95, 98 и 100", minute=6,
        on_site=True, reliable=True,
    )
    events = detect_delivery_events(observations, chat_messages=[message])

    assert all(event["event_type"] == "confirmed_delivery" for event in events)
    assert all(event["delivery_confidence"] >= 0.6 for event in events)


def test_fresh_trusted_mark_has_more_weight_than_old_untrusted_mark():
    transition = datetime(2026, 9, 14, 15, 20)
    fresh = FuelStationMark(station_id=1, provider="gdebenz", source_key="fresh", text="92, 95, ДТ",
        source_created_at=transition - timedelta(minutes=2), fetched_at=transition,
        raw_data={"status": "queue", "on_site": True, "author_reliable": True, "author_tier": 2, "acct_ok": True})
    old = FuelStationMark(station_id=1, provider="gdebenz", source_key="old", text="92, 95, ДТ",
        source_created_at=transition - timedelta(minutes=45), fetched_at=transition,
        raw_data={"status": "queue", "on_site": False, "author_reliable": False, "author_tier": 0, "acct_ok": False})
    assert mark_evidence_weight(fresh, "95", transition) > mark_evidence_weight(old, "95", transition)


def test_delivery_backfill_is_idempotent(db, make_user):
    user = make_user("backfill")
    station = FuelStation(owner_id=user.id, provider="gdebenz", provider_station_id="backfill", latitude=59.9, longitude=30.2)
    db.add(station)
    db.flush()
    for item in [observation(None, "unavailable", 10), observation(None, "unavailable", 15),
                 observation(None, "available", 20), observation(None, "available", 25)]:
        item.station_id = station.id
        db.add(item)
    db.commit()
    assert backfill_delivery_events(db, station.id) == 1
    db.commit()
    assert backfill_delivery_events(db, station.id) == 0
    assert db.query(FuelDeliveryEvent).count() == 1


def test_backfill_upgrades_existing_appearance_when_direct_chat_arrives(db, make_user):
    user = make_user("backfill-chat-upgrade")
    station = FuelStation(owner_id=user.id, provider="gdebenz", provider_station_id="backfill-chat",
        latitude=59.9, longitude=30.2)
    db.add(station)
    db.flush()
    for item in [observation(None, "unavailable", 0), observation(None, "unavailable", 5),
                 observation(None, "available", 10), observation(None, "available", 15)]:
        item.station_id = station.id
        db.add(item)
    db.commit()
    assert backfill_delivery_events(db, station.id) == 1
    db.commit()
    event = db.query(FuelDeliveryEvent).one()
    assert event.event_type == "candidate_appearance"
    db.add(FuelStationChatMessage(station_id=station.id, provider="gdebenz", provider_message_id="upgrade",
        author_id="trusted", body="бензовоз слился, привез 95 и 92",
        source_created_at=datetime(2026, 9, 14, 15, 8), on_site=True, author_reliable=True, author_tier=1))
    db.commit()
    assert backfill_delivery_events(db, station.id) == 1
    db.commit()
    assert db.query(FuelDeliveryEvent).one().event_type == "confirmed_delivery"
    assert backfill_delivery_events(db, station.id) == 0


def add_delivery(db, station_id, when, confidence=0.9, fuel_type="95"):
    before = FuelObservation(station_id=station_id, fuel_type=fuel_type, state="unavailable", observed_at=when - timedelta(minutes=5), is_stale=False)
    after = FuelObservation(station_id=station_id, fuel_type=fuel_type, state="available", observed_at=when, is_stale=False)
    db.add_all([before, after])
    db.flush()
    event = FuelDeliveryEvent(station_id=station_id, fuel_type=fuel_type, window_start=before.observed_at,
        window_end=after.observed_at, estimated_at=when, confidence=confidence, before_observation_id=before.id,
        after_observation_id=after.id, event_type="confirmed_delivery", appearance_confidence=confidence,
        delivery_confidence=confidence, detection_reason="test", evidence_json={}, detector_version="test",
        classifier_version="test")
    db.add(event)
    return event


def test_forecast_needs_three_events_and_resists_night_outlier(db, make_user):
    user = make_user("forecast")
    station = FuelStation(owner_id=user.id, provider="gdebenz", provider_station_id="forecast", latitude=59.9, longitude=30.2)
    db.add(station)
    db.flush()
    add_delivery(db, station.id, datetime(2026, 9, 10, 14, 0))
    add_delivery(db, station.id, datetime(2026, 9, 11, 14, 10))
    assert build_forecast(db, station, "95") is None
    add_delivery(db, station.id, datetime(2026, 9, 12, 14, 5))
    add_delivery(db, station.id, datetime(2026, 9, 13, 0, 10))
    add_delivery(db, station.id, datetime(2026, 9, 14, 14, 0))
    db.flush()
    forecast = build_forecast(db, station, "95", datetime(2026, 9, 14, 16))
    assert forecast is not None
    assert 13 <= forecast["expected_at"].hour <= 16
    assert forecast["range_from"] < forecast["expected_at"] < forecast["range_to"]
    assert forecast["reason_json"]["event_count"] == 5


def test_forecast_excludes_high_confidence_appearance_only_events(db, make_user):
    user = make_user("appearance-only-forecast")
    station = FuelStation(owner_id=user.id, provider="gdebenz", provider_station_id="appearance-only",
        latitude=59.9, longitude=30.2)
    db.add(station)
    db.flush()
    add_subscription(db, user, station, ("95",))
    for day in range(10, 13):
        event = add_delivery(db, station.id, datetime(2026, 9, day, 12), confidence=0.99)
        event.event_type = "candidate_appearance"
        event.delivery_confidence = 0.2
    db.flush()
    assert build_forecast(db, station, "95") is None

    db.add(FuelForecast(
        station_id=station.id,
        fuel_type="95",
        generated_at=datetime(2026, 9, 13, 12),
        expected_at=datetime(2026, 9, 14, 12),
        range_from=datetime(2026, 9, 14, 11),
        range_to=datetime(2026, 9, 14, 13),
        confidence=0.9,
        model_version="median-v1",
        reason_json={"event_count": 3},
    ))
    db.flush()
    refresh_forecasts(db, station.id)
    assert db.query(FuelForecast).filter_by(station_id=station.id, fuel_type="95").count() == 0


def test_cross_station_lag_requires_three_matching_pairs():
    base = datetime(2026, 9, 10, 12)
    source = [base + timedelta(days=index) for index in range(4)]
    target = [point + timedelta(minutes=lag) for point, lag in zip(source, [40, 41, 39, 41])]
    assert correlate_event_series(1, 2, "95", source[:2], target[:2]) is None
    relation = correlate_event_series(1, 2, "95", source, target)
    assert relation is not None
    assert 39 <= relation.median_lag_minutes <= 41
    assert relation.matches == 4


def test_cross_station_ignores_different_brands_and_adjusts_same_brand_forecast(db, make_user):
    user = make_user("cross-station")
    source = FuelStation(owner_id=user.id, provider="gdebenz", provider_station_id="source", brand="Teboil", latitude=59.8, longitude=30.1)
    target = FuelStation(owner_id=user.id, provider="gdebenz", provider_station_id="target", brand="Teboil", latitude=59.9, longitude=30.2)
    other = FuelStation(owner_id=user.id, provider="gdebenz", provider_station_id="other", brand="Other", latitude=60, longitude=30.3)
    db.add_all([source, target, other])
    db.flush()
    add_subscription(db, user, source, ("95",))
    add_subscription(db, user, target, ("95",))
    add_subscription(db, user, other, ("95",))
    for day in range(10, 13):
        add_delivery(db, source.id, datetime(2026, 9, day, 12))
        add_delivery(db, target.id, datetime(2026, 9, day, 12, 40))
        add_delivery(db, other.id, datetime(2026, 9, day, 12, 20))
    add_delivery(db, source.id, datetime(2026, 9, 13, 15))
    db.flush()
    relations = station_correlations(db, target, "95")
    assert len(relations) == 1
    assert relations[0].source_station_id == source.id
    forecast = build_forecast(db, target, "95", datetime(2026, 9, 13, 15, 10))
    assert forecast is not None
    assert forecast["expected_at"] > datetime(2026, 9, 13, 12, 40)
    assert forecast["reason_json"]["cross_station_signal"]["matches"] == 3


def test_cross_station_training_excludes_candidate_events(db, make_user):
    user = make_user("cross-station-candidates")
    source = FuelStation(owner_id=user.id, provider="gdebenz", provider_station_id="candidate-source",
        brand="Teboil", latitude=59.8, longitude=30.1)
    target = FuelStation(owner_id=user.id, provider="gdebenz", provider_station_id="candidate-target",
        brand="Teboil", latitude=59.9, longitude=30.2)
    db.add_all([source, target])
    db.flush()
    add_subscription(db, user, source, ("95",))
    add_subscription(db, user, target, ("95",))
    for day in range(10, 14):
        source_event = add_delivery(db, source.id, datetime(2026, 9, day, 12))
        target_event = add_delivery(db, target.id, datetime(2026, 9, day, 12, 40))
        source_event.event_type = target_event.event_type = "candidate_appearance"
    db.flush()

    assert station_correlations(db, target, "95") == []


def test_fuel_detail_renders_all_availability_and_delivery_states(client, login, make_user, db):
    user = make_user("fuel-event-states")
    station = FuelStation(owner_id=user.id, provider="gdebenz", provider_station_id="event-states",
        brand="Teboil", latitude=59.9, longitude=30.2)
    db.add(station)
    db.flush()
    add_subscription(db, user, station)
    event_types = (
        "candidate_appearance", "confirmed_availability", "probable_delivery", "confirmed_delivery"
    )
    for index, event_type in enumerate(event_types):
        event = add_delivery(db, station.id, datetime(2026, 9, 10 + index, 12))
        event.event_type = event_type
        event.evidence_json = {"appearance_evidence": ["Тестовый сигнал"], "appearance_caveats": []}
        if event_type == "candidate_appearance":
            event.appearance_confidence = event.confidence = 0.34
            event.delivery_confidence = 0.2
    db.commit()
    login("fuel-event-states")

    response = client.get(f"/fuel/{station.id}")

    assert response.status_code == 200
    assert "Возможное появление · ожидаем подтверждения" in response.text
    assert "Наличие подтверждено" in response.text
    assert "Вероятная поставка" in response.text
    assert "Поставка подтверждена" in response.text


def test_fuel_detail_renders_without_events(client, login, make_user, db):
    user = make_user("fuel-no-events")
    station = FuelStation(owner_id=user.id, provider="gdebenz", provider_station_id="no-events",
        brand="Teboil", latitude=59.9, longitude=30.2)
    db.add(station)
    db.flush()
    add_subscription(db, user, station)
    db.commit()
    login("fuel-no-events")

    response = client.get(f"/fuel/{station.id}")

    assert response.status_code == 200
    assert "Появлений топлива пока не обнаружено" in response.text
    assert "Недостаточно подтверждённых поставок" in response.text


def test_fuel_detail_displays_queue_separately_from_availability(client, login, make_user, db):
    user = make_user("fuel-detail-queue")
    station = FuelStation(
        owner_id=user.id,
        provider="gdebenz",
        provider_station_id="detail-queue",
        brand="Teboil",
        latitude=59.9,
        longitude=30.2,
    )
    db.add(station)
    db.flush()
    add_subscription(db, user, station, ("95",))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for minutes in (10, 5):
        point = now - timedelta(minutes=minutes)
        db.add(FuelObservation(
            station_id=station.id,
            fuel_type="95",
            state="available",
            observed_at=point,
            source_updated_at=point,
            source_status="queue",
            is_stale=False,
        ))
    db.commit()
    login(user.username)

    response = client.get(f"/fuel/{station.id}")

    assert response.status_code == 200
    assert "Ситуация: очередь" in response.text
    assert "<strong>Есть</strong>" in response.text
    assert "<strong>Мало</strong>" not in response.text
    assert "Почему такой статус?" in response.text


def test_fuel_detail_renders_delivery_forecast_and_technical_history(client, login, make_user, db):
    user = make_user("fuel-detail")
    station = FuelStation(owner_id=user.id, provider="gdebenz", provider_station_id="detail", brand="Teboil",
        address="Ветеранов, 188/1", latitude=59.9, longitude=30.2)
    db.add(station)
    db.flush()
    add_subscription(db, user, station, ("95",))
    event = add_delivery(db, station.id, datetime(2026, 9, 14, 15))
    db.flush()
    db.add(FuelForecast(station_id=station.id, fuel_type="95", generated_at=datetime(2026, 9, 14, 16),
        expected_at=datetime(2026, 9, 15, 15), range_from=datetime(2026, 9, 15, 14), range_to=datetime(2026, 9, 15, 16),
        confidence=0.74, model_version=FORECAST_VERSION, reason_json={"event_count": 3, "typical_time": "18:00", "median_interval_minutes": 1440}))
    db.add(FuelForecast(station_id=station.id, fuel_type="95", generated_at=datetime(2026, 9, 14, 17),
        expected_at=datetime(2026, 9, 16, 0, 33), range_from=datetime(2026, 9, 16, 0, 3),
        range_to=datetime(2026, 9, 16, 1, 3), confidence=0.13, model_version="median-v1", reason_json={}))
    db.add(FuelStationMark(station_id=station.id, provider="gdebenz", source_key="detail-mark", text="92, 95, ДТ",
        source_created_at=datetime(2026, 9, 14, 21, 15), fetched_at=datetime(2026, 9, 14, 21, 16),
        raw_data={"on_site": True, "author_reliable": True}))
    db.add(FuelStationChatMessage(station_id=station.id, provider="gdebenz", provider_message_id="detail-chat",
        author_id="42", author_name="Владимир В.", body="бензовоз слился, привез 95 и 92",
        source_created_at=datetime(2026, 9, 14, 14, 42), on_site=True, author_reliable=True, author_tier=2))
    db.commit()
    login("fuel-detail")
    response = client.get(f"/fuel/{station.id}")
    assert response.status_code == 200
    assert "История появления топлива" in response.text
    assert "Почему такой прогноз?" in response.text
    assert "Техническая история" in response.text
    assert "Последние отметки GdeBenz" in response.text
    assert "Чат GdeBenz" in response.text
    assert "Владимир В." in response.text
    assert "00:33" not in response.text
    assert str(round(event.confidence * 100)) in response.text


def test_shared_physical_station_is_private_per_user_and_polled_once(client, login, make_user, db):
    make_user("fuel-shared-a")
    make_user("fuel-shared-b")
    station_form = {
        "provider": "gdebenz",
        "provider_station_id": "shared-teboil",
        "latitude": "59.8348",
        "longitude": "30.1210",
        "brand": "Teboil",
        "address": "Ветеранов, 188/1",
    }
    login("fuel-shared-a")
    assert client.post(
        "/fuel/stations", data={**station_form, "fuel_types": ["95"]},
        follow_redirects=False,
    ).status_code == 303
    db.expire_all()
    station = db.query(FuelStation).filter_by(provider_station_id="shared-teboil").one()
    station_id = station.id

    login("fuel-shared-b")
    assert "Ветеранов, 188/1" not in client.get("/fuel").text
    assert client.get(f"/fuel/{station_id}").status_code == 404
    assert client.post(
        "/fuel/stations", data={**station_form, "fuel_types": ["100"]},
        follow_redirects=False,
    ).status_code == 303
    db.expire_all()
    assert db.query(FuelStation).filter_by(provider_station_id="shared-teboil").count() == 1
    assert db.query(FuelStationSubscription).filter_by(station_id=station_id).count() == 2

    class SharedProvider:
        calls = 0

        async def get_stations_near(self, latitude, longitude, radius_km):
            self.calls += 1
            return [FuelStationCandidate(
                provider="gdebenz", provider_station_id="shared-teboil",
                latitude=latitude, longitude=longitude,
                raw={"status": "yes", "fuels_now": "95,100"},
            )]

        async def get_station_marks(self, provider_station_id, limit=12):
            return FuelMarksFeed()

        async def get_station_chat(self, provider_station_id, limit=20, cursor=None):
            return FuelChatFeed()

    provider = SharedProvider()
    summary = asyncio.run(run_fuel_poll_cycle(
        session_factory=SessionLocal, provider=provider,
        stale_after_minutes=120, nearby_radius_km=3,
    ))
    assert summary["stations"] == summary["success"] == provider.calls == 1
    assert summary["observations"] == 2
    db.expire_all()
    assert {item.fuel_type for item in db.query(FuelObservation).filter_by(station_id=station_id)} == {"95", "100"}

    login("fuel-shared-a")
    detail_a = client.get(f"/fuel/{station_id}")
    assert detail_a.status_code == 200
    assert "АИ-95" in detail_a.text
    assert "АИ-100" not in detail_a.text
    assert {item["fuel_type"] for item in client.get(
        f"/api/fuel/stations/{station_id}/observations"
    ).json()} == {"95"}
    assert set(client.get(f"/api/fuel/stations/{station_id}/timeline").json()["fuels"]) == {"95"}
    login("fuel-shared-b")
    detail_b = client.get(f"/fuel/{station_id}")
    assert detail_b.status_code == 200
    assert "АИ-100" in detail_b.text
    assert "АИ-95" not in detail_b.text
    assert {item["fuel_type"] for item in client.get(
        f"/api/fuel/stations/{station_id}/observations"
    ).json()} == {"100"}
    assert set(client.get(f"/api/fuel/stations/{station_id}/timeline").json()["fuels"]) == {"100"}

    login("fuel-shared-a")
    assert client.post(f"/fuel/stations/{station_id}/delete", follow_redirects=False).status_code == 303
    disabled_detail = client.get(f"/fuel/{station_id}")
    assert "Мониторинг этой АЗС выключен" in disabled_detail.text
    assert "Отключить</button>" not in disabled_detail.text
    provider.calls = 0
    assert asyncio.run(run_fuel_poll_cycle(
        session_factory=SessionLocal, provider=provider,
        stale_after_minutes=120, nearby_radius_km=3,
    ))["stations"] == 1
    assert provider.calls == 1
    login("fuel-shared-b")
    assert client.post(f"/fuel/stations/{station_id}/delete", follow_redirects=False).status_code == 303
    provider.calls = 0
    assert asyncio.run(run_fuel_poll_cycle(
        session_factory=SessionLocal, provider=provider,
        stale_after_minutes=120, nearby_radius_km=3,
    ))["stations"] == 0
    assert provider.calls == 0
    db.expire_all()
    assert db.get(FuelStation, station_id) is not None
    assert db.query(FuelObservation).filter_by(station_id=station_id).count() == 3
    assert client.post(
        f"/fuel/{station_id}/settings",
        data={"enabled": "on", "fuel_types": ["100"]},
        follow_redirects=False,
    ).status_code == 303
    provider.calls = 0
    assert asyncio.run(run_fuel_poll_cycle(
        session_factory=SessionLocal, provider=provider,
        stale_after_minutes=120, nearby_radius_km=3,
    ))["stations"] == 1
    assert provider.calls == 1


def test_foreign_station_routes_and_api_return_404(client, login, make_user, db):
    user_a = make_user("fuel-private-a")
    user_b = make_user("fuel-private-b")
    station = FuelStation(
        owner_id=user_b.id, provider="gdebenz", provider_station_id="private-b",
        brand="Скрытая АЗС", latitude=59.9, longitude=30.2,
    )
    db.add(station)
    db.flush()
    subscription = add_subscription(db, user_b, station, ("100",))
    db.commit()
    original = (subscription.enabled, subscription.track_95, subscription.track_100)
    login("fuel-private-a")

    assert "Скрытая АЗС" not in client.get("/fuel").text
    assert all(client.get(path).status_code == 404 for path in (
        f"/fuel/{station.id}",
        f"/fuel/{station.id}/settings",
        f"/api/fuel/stations/{station.id}/observations",
        f"/api/fuel/stations/{station.id}/deliveries",
        f"/api/fuel/stations/{station.id}/forecast",
        f"/api/fuel/stations/{station.id}/timeline",
    ))
    response = client.post(
        f"/fuel/{station.id}/settings",
        data={"enabled": "on", "fuel_types": ["95"], "user_id": str(user_b.id)},
    )
    assert response.status_code == 404
    db.expire_all()
    unchanged = db.get(FuelStationSubscription, subscription.id)
    assert (unchanged.enabled, unchanged.track_95, unchanged.track_100) == original
    assert user_a.id != user_b.id


def timeline_observation(identifier, fuel_type, state, when, *, stale=False):
    return FuelObservation(
        id=identifier, station_id=1, fuel_type=fuel_type, state=state,
        observed_at=when, source_updated_at=when, is_stale=stale,
    )


def test_timeline_continuous_unavailable_and_current_interval():
    now = datetime(2026, 9, 15, 12)
    observations = [
        timeline_observation(index, "95", "unavailable", now - timedelta(hours=24 - index * 2))
        for index in range(13)
    ]
    result = build_fuel_timeline(
        observations, [], ["95"], current_at=now, stale_after_minutes=120
    )
    assert [(item["state"], item["start"], item["end"]) for item in result["fuels"]["95"]] == [
        ("unavailable", now - timedelta(hours=24), now)
    ]
    assert result["fuels"]["95"][0]["duration_label"] == "24 ч 00 мин"

    start = now - timedelta(minutes=30)
    current = build_fuel_timeline(
        [timeline_observation(20, "95", "unavailable", now - timedelta(hours=1)),
         timeline_observation(21, "95", "available", start),
         timeline_observation(22, "95", "available", start + timedelta(minutes=5))],
        [],
        ["95"], current_at=now, stale_after_minutes=120,
    )["fuels"]["95"][-1]
    assert current["state"] == "confirmed_available"
    assert current["end"] == now


def test_timeline_candidate_confirmed_stale_gap_and_window_clipping():
    now = datetime(2026, 9, 15, 12)
    candidate_start = now - timedelta(hours=3)
    confirmed_start = now - timedelta(hours=1)
    observations = [
        timeline_observation(1, "95", "available", candidate_start),
        timeline_observation(2, "95", "unavailable", candidate_start + timedelta(minutes=10)),
        timeline_observation(3, "95", "available", confirmed_start),
    ]
    observations.append(
        timeline_observation(4, "95", "available", confirmed_start + timedelta(minutes=5))
    )
    intervals = build_fuel_timeline(
        observations, [], ["95"], current_at=now, stale_after_minutes=30
    )["fuels"]["95"]
    assert any(item["state"] == "candidate" for item in intervals)
    assert any(item["state"] == "confirmed_available" for item in intervals)
    assert any(item["state"] == "unknown" for item in intervals)

    old_start = now - timedelta(hours=30)
    old_end = now - timedelta(hours=20)
    clipped = build_fuel_timeline(
        [timeline_observation(10, "100", "available", old_start),
         timeline_observation(11, "100", "unavailable", old_end)],
        [],
        ["100"], current_at=now, stale_after_minutes=1000,
    )["fuels"]["100"]
    assert clipped[0]["start"] == now - timedelta(hours=24)
    assert clipped[0]["state"] == "candidate"
    assert clipped[-1]["state"] == "unknown"


def _availability_at(marks, fuel_type, at):
    return evaluate_fuel_availability(
        [], marks, fuel_type, current_at=at, stale_after_minutes=120
    ).state


def _timeline_state_at(intervals, at):
    return next(
        item["state"]
        for item in intervals
        if item["start"] <= at < item["end"]
    )


def test_real_mark_sequence_reconstructs_95_and_98_without_future_evidence():
    day = datetime(2026, 9, 15)
    reports = [
        ("15:17", "92,95,ДТ"),
        ("15:31", "92,95,ДТ"),
        ("15:52", "92,95,ДТ"),
        ("15:55", "92,95,ДТ"),
        ("16:09", "92,95,ДТ"),
        ("16:25", "92,95,ДТ"),
        ("16:35", "92,95,98,ДТ"),
        ("16:39", "92,95,ДТ"),
        ("16:49", "92,95,ДТ"),
        ("17:08", "92,95,ДТ"),
        ("17:19", "95"),
        ("17:24", "92,95,ДТ"),
        ("17:38", "92,95,ДТ"),
        ("17:39", "92,95,ДТ"),
        ("17:58", "92,95,ДТ"),
        ("18:32", "92,95,ДТ"),
        ("19:08", "92,95,ДТ"),
        ("19:20", "92,95,ДТ"),
        ("19:32", "92,95,ДТ"),
        ("19:52", "92,95,ДТ"),
        ("20:09", "92,95,ДТ"),
        ("20:34", "92,95,ДТ"),
        ("20:38", "92,95,ДТ"),
        ("21:03", "92,95,ДТ"),
        ("21:09", "92,95,ДТ"),
        ("21:23", "92,95,ДТ"),
        ("21:29", "92,95,ДТ"),
        ("21:30", "92,95,ДТ"),
        ("21:35", ""),
        ("21:40", "92"),
    ]
    marks = []
    for index, (clock, detail) in enumerate(reports):
        hour, minute = (int(part) for part in clock.split(":"))
        marks.append(
            availability_mark(
                f"real-{index}", day.replace(hour=hour, minute=minute), "yes", detail
            )
        )

    cutoffs = [
        day.replace(hour=15, minute=20),
        day.replace(hour=15, minute=40),
        day.replace(hour=16, minute=40),
        day.replace(hour=18),
        day.replace(hour=21, minute=31),
        day.replace(hour=21, minute=41),
    ]
    assert [_availability_at(marks, "95", at) for at in cutoffs] == [
        "candidate", "available", "available", "available", "available", "candidate"
    ]
    assert [_availability_at(marks, "98", at) for at in cutoffs] == [
        "unavailable", "unavailable", "candidate", "unavailable", "unavailable", "unavailable"
    ]
    assert _availability_at(marks, "95", day.replace(hour=21, minute=36)) == "available"
    assert _availability_at(marks, "98", day.replace(hour=21, minute=36)) == "unavailable"

    current_at = day.replace(hour=21, minute=41)
    timeline = build_fuel_timeline(
        [], marks, ["95", "98"], current_at=current_at, stale_after_minutes=120
    )
    expected_95 = [
        "candidate", "confirmed_available", "confirmed_available",
        "confirmed_available", "confirmed_available", "candidate",
    ]
    expected_98 = [
        "unavailable", "unavailable", "candidate",
        "unavailable", "unavailable", "unavailable",
    ]
    assert [_timeline_state_at(timeline["fuels"]["95"], at) for at in cutoffs[:-1]] + [
        timeline["fuels"]["95"][-1]["state"]
    ] == expected_95
    assert [_timeline_state_at(timeline["fuels"]["98"], at) for at in cutoffs[:-1]] + [
        timeline["fuels"]["98"][-1]["state"]
    ] == expected_98
    assert not any(
        item["state"] == "confirmed_available" for item in timeline["fuels"]["98"]
    )
    for fuel_type in ("95", "98"):
        current_state = _availability_at(marks, fuel_type, current_at)
        expected_timeline_state = (
            "confirmed_available" if current_state == "available" else current_state
        )
        assert timeline["fuels"][fuel_type][-1]["state"] == expected_timeline_state


def test_legacy_station_migration_creates_subscription_idempotently(make_user):
    user = make_user("fuel-legacy-owner")
    with SessionLocal() as db:
        station = FuelStation(
            owner_id=user.id, provider="gdebenz", provider_station_id="legacy-station",
            latitude=59.9, longitude=30.2, enabled=True,
        )
        db.add(station)
        db.flush()
        db.add_all([
            FuelStationFuel(station_id=station.id, fuel_type="95", enabled=True),
            FuelStationFuel(station_id=station.id, fuel_type="98", enabled=False),
            FuelStationFuel(station_id=station.id, fuel_type="100", enabled=True),
        ])
        event = add_delivery(db, station.id, datetime(2026, 9, 14, 12))
        db.flush()
        db.add_all([
            FuelStationMark(
                station_id=station.id, provider="gdebenz", source_key="legacy-mark",
                text="есть 95", fetched_at=datetime(2026, 9, 14, 12),
            ),
            FuelForecast(
                station_id=station.id, fuel_type="95", generated_at=datetime(2026, 9, 14, 13),
                expected_at=datetime(2026, 9, 15, 12), range_from=datetime(2026, 9, 15, 11),
                range_to=datetime(2026, 9, 15, 13), confidence=0.8,
                model_version=FORECAST_VERSION, reason_json={"event_count": 3},
                actual_delivery_event_id=event.id,
            ),
        ])
        db.commit()
        station_id = station.id
    with app_engine.begin() as connection:
        main_module._migrate_fuel_station_subscriptions(connection)
    with SessionLocal() as db:
        subscription = db.query(FuelStationSubscription).filter_by(
            user_id=user.id, station_id=station_id
        ).one()
        assert subscription.enabled is True
        assert subscription.tracked_fuel_types == ("95", "100")
        assert db.get(FuelStation, station_id) is not None
        assert db.query(FuelObservation).filter_by(station_id=station_id).count() == 2
        assert db.query(FuelStationMark).filter_by(station_id=station_id).count() == 1
        assert db.query(FuelDeliveryEvent).filter_by(station_id=station_id).count() == 1
        assert db.query(FuelForecast).filter_by(station_id=station_id).count() == 1
        subscription.enabled = False
        db.commit()

    with app_engine.begin() as connection:
        main_module._migrate_fuel_station_subscriptions(connection)
    with SessionLocal() as db:
        subscription = db.query(FuelStationSubscription).filter_by(
            user_id=user.id, station_id=station_id
        ).one()
        assert subscription.enabled is False
        assert subscription.tracked_fuel_types == ("95", "100")


def test_mobile_timeline_stress_layout_and_single_popover(client, login, make_user, db):
    playwright = pytest.importorskip("playwright.sync_api")
    user = make_user("fuel-mobile-timeline")
    station = FuelStation(
        owner_id=user.id, provider="gdebenz", provider_station_id="mobile-stress",
        brand="Teboil", latitude=59.9, longitude=30.2,
    )
    db.add(station)
    db.flush()
    add_subscription(db, user, station)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for fuel_type in ("95", "98", "100"):
        for cycle in range(8):
            cycle_start = now - timedelta(hours=24) + timedelta(hours=cycle * 3)
            before = FuelObservation(
                station_id=station.id, fuel_type=fuel_type, state="unavailable",
                observed_at=cycle_start, is_stale=False,
            )
            appeared = FuelObservation(
                station_id=station.id, fuel_type=fuel_type, state="available",
                observed_at=cycle_start + timedelta(hours=1), is_stale=False,
            )
            disappeared = FuelObservation(
                station_id=station.id, fuel_type=fuel_type, state="unavailable",
                observed_at=cycle_start + timedelta(hours=1, minutes=2), is_stale=False,
            )
            unknown = FuelObservation(
                station_id=station.id, fuel_type=fuel_type, state="unknown",
                observed_at=cycle_start + timedelta(hours=2), is_stale=False,
            )
            db.add_all([before, appeared, disappeared, unknown])
            db.flush()
            confidence = 0.85 if cycle % 2 == 0 else 0.25
            db.add(FuelDeliveryEvent(
                station_id=station.id, fuel_type=fuel_type,
                window_start=before.observed_at, window_end=appeared.observed_at,
                estimated_at=appeared.observed_at, disappeared_at=disappeared.observed_at,
                before_observation_id=before.id, after_observation_id=appeared.id,
                event_type="confirmed_availability" if cycle % 2 == 0 else "candidate_appearance",
                confidence=confidence, appearance_confidence=confidence,
                delivery_confidence=0.1, availability_duration_minutes=2,
                detection_reason="mobile stress", evidence_json={"mark_support_count": cycle % 3},
                detector_version="test", classifier_version="test",
            ))
    db.commit()
    login("fuel-mobile-timeline")
    response = client.get(f"/fuel/{station.id}")
    assert response.status_code == 200
    settings_response = client.get("/fuel/settings")
    assert settings_response.status_code == 200

    browser_paths = [
        shutil.which(name) for name in ("msedge", "google-chrome", "chromium", "chromium-browser")
    ] + [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    executable = next((path for path in browser_paths if path and Path(path).exists()), None)
    with playwright.sync_playwright() as manager:
        try:
            browser = manager.chromium.launch(
                headless=True, executable_path=executable
            ) if executable else manager.chromium.launch(headless=True)
        except playwright.Error as exc:
            pytest.skip(f"Chromium browser is unavailable: {exc}")
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.set_content(response.text, wait_until="load")
            page.add_style_tag(content=Path("app/static/style.css").read_text(encoding="utf-8"))

            assert page.locator(".fuel-timeline-row").count() == 3
            desktop_row = page.locator(".fuel-timeline-row").first
            assert desktop_row.evaluate(
                "element => getComputedStyle(element).gridTemplateColumns.startsWith('52px')"
            )
            segments = page.locator(".fuel-timeline-segment")
            assert all((text or "").strip() == "" for text in segments.all_text_contents())
            assert page.locator(".fuel-timeline-segment .fuel-timeline-popover").count() == 0
            assert page.locator("[data-fuel-timeline-popover]").count() == 1
            assert page.locator("[data-fuel-timeline-popover]").evaluate(
                "element => element.closest('.fuel-timeline-segment') === null"
            )
            segments.nth(0).hover()
            assert page.locator("[data-fuel-timeline-popover]").is_visible()
            assert "АИ-95" in page.locator("[data-fuel-timeline-popover]").text_content()
            page.locator(".fuel-timeline-card h2").hover()
            assert not page.locator("[data-fuel-timeline-popover]").is_visible()
            segments.nth(0).focus()
            assert page.locator("[data-fuel-timeline-popover]").is_visible()
            segments.nth(0).blur()
            assert not page.locator("[data-fuel-timeline-popover]").is_visible()

            page.set_viewport_size({"width": 390, "height": 844})
            assert segments.count() > 20
            assert page.evaluate(
                "() => Math.max(document.body.scrollWidth, document.documentElement.scrollWidth) === innerWidth"
            )
            assert all((text or "").strip() == "" for text in segments.all_text_contents())
            assert page.evaluate("""() => {
                const style = getComputedStyle(document.querySelector('.fuel-timeline-segment'));
                return style.overflow === 'hidden' && style.minWidth === '0px'
                    && style.boxSizing === 'border-box' && style.fontSize === '0px'
                    && style.lineHeight === '0px' && style.whiteSpace === 'nowrap';
            }""")
            overlaps = page.evaluate("""() => [...document.querySelectorAll('.fuel-timeline-track')]
                .flatMap((track, row) => {
                    const segments = [...track.querySelectorAll('.fuel-timeline-segment')]
                        .map(item => item.getBoundingClientRect()).sort((a, b) => a.left - b.left);
                    return segments.slice(1).map((item, index) => ({
                        row, overlap: segments[index].right - item.left
                    })).filter(item => item.overlap > 0.5);
                })""")
            assert overlaps == []
            assert page.locator(".fuel-timeline-axis time:visible").count() <= 5

            segments.nth(0).click()
            assert page.locator(".fuel-timeline-segment.open").count() == 1
            assert page.locator("[data-fuel-timeline-popover]").is_visible()
            segments.nth(4).click()
            assert page.locator(".fuel-timeline-segment.open").count() == 1
            assert page.locator("[data-fuel-timeline-popover]").is_visible()
            page.locator(".fuel-timeline-card h2").click()
            assert page.locator(".fuel-timeline-segment.open").count() == 0
            assert not page.locator("[data-fuel-timeline-popover]").is_visible()

            page.set_content(settings_response.text, wait_until="load")
            page.add_style_tag(content=Path("app/static/style.css").read_text(encoding="utf-8"))
            assert page.locator(".fuel-notification-settings").is_visible()
            assert page.evaluate(
                "() => Math.max(document.body.scrollWidth, document.documentElement.scrollWidth) === innerWidth"
            )
        finally:
            browser.close()


def test_fuel_dashboard_filters_map_privacy_and_mobile_layout(client, login, make_user, db):
    playwright = pytest.importorskip("playwright.sync_api")
    user = make_user("fuel-dashboard-browser")
    other = make_user("fuel-dashboard-private")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stations = [
        FuelStation(owner_id=user.id, provider="gdebenz", provider_station_id="ui-a", brand="Teboil", address="Ветеранов, 188/1", latitude=59.835, longitude=30.121),
        FuelStation(owner_id=user.id, provider="gdebenz", provider_station_id="ui-b", brand="Газпромнефть", address="Ленинский, 90", latitude=59.85, longitude=30.15),
        FuelStation(owner_id=user.id, provider="gdebenz", provider_station_id="ui-c", brand="Татнефть", address="Маршала Жукова, 10", latitude=59.82, longitude=30.17),
        FuelStation(owner_id=other.id, provider="gdebenz", provider_station_id="ui-private", brand="Скрытая сеть", address="Чужая АЗС", latitude=60.0, longitude=30.0),
    ]
    db.add_all(stations)
    db.flush()
    add_subscription(db, user, stations[0], ("95", "98"))
    add_subscription(db, user, stations[1], ("95",))
    add_subscription(db, user, stations[2], ("100",))
    add_subscription(db, other, stations[0], ("100",))
    add_subscription(db, other, stations[3], ("95",))
    before = FuelObservation(station_id=stations[0].id, fuel_type="98", state="unavailable", observed_at=now - timedelta(minutes=9), source_updated_at=now - timedelta(minutes=9))
    available_95 = FuelObservation(station_id=stations[0].id, fuel_type="95", state="available", observed_at=now - timedelta(minutes=4), source_updated_at=now - timedelta(minutes=4))
    available_95_confirmed = FuelObservation(station_id=stations[0].id, fuel_type="95", state="available", observed_at=now - timedelta(minutes=2), source_updated_at=now - timedelta(minutes=2))
    candidate_98 = FuelObservation(station_id=stations[0].id, fuel_type="98", state="available", observed_at=now - timedelta(minutes=1), source_updated_at=now - timedelta(minutes=1))
    unavailable_95 = FuelObservation(station_id=stations[1].id, fuel_type="95", state="unavailable", observed_at=now - timedelta(minutes=3), source_updated_at=now - timedelta(minutes=3))
    db.add_all([before, available_95, available_95_confirmed, candidate_98, unavailable_95])
    db.flush()
    db.add(FuelDeliveryEvent(
        station_id=stations[0].id, fuel_type="98", window_start=before.observed_at,
        window_end=candidate_98.observed_at, estimated_at=candidate_98.observed_at,
        event_type="candidate_appearance", confidence=.35, appearance_confidence=.35,
        before_observation_id=before.id, after_observation_id=candidate_98.id,
        detection_reason="browser candidate", evidence_json={}, detector_version="test",
    ))
    vehicle = Vehicle(
        owner_id=user.id,
        display_name="Ford Focus 3",
        make="Ford",
        model="Focus",
        year=2014,
        current_odometer=120000,
    )
    db.add(vehicle)
    db.commit()
    login(user.username)
    response = client.get("/fuel")
    assert response.status_code == 200
    assert "https://unpkg.com" in response.headers["content-security-policy"]
    assert "https://tile.openstreetmap.org" in response.headers["content-security-policy"]
    assert "geolocation=(self)" in response.headers["permissions-policy"]
    assert "Скрытая сеть" not in response.text
    assert "Чужая АЗС" not in response.text

    leaflet_stub = r"""
    window.fuelGeoCalls = 0;
    window.fuelRouteLines = 0;
    window.fuelRouteRemovals = 0;
    window.fuelRouteGeometries = [];
    Object.defineProperty(navigator, 'geolocation', {configurable: true, value: {
      getCurrentPosition(success){ window.fuelGeoCalls += 1; success({coords: {latitude: 59.84, longitude: 30.12}}); }
    }});
    window.L = {
      map: id => ({invalidateSize(){}, setView(){}, fitBounds(){}, closePopup(){ document.querySelectorAll('.leaflet-popup').forEach(item => item.remove()); }}),
      tileLayer: () => ({addTo(){ return this; }}),
      polyline: points => ({addTo(){ window.fuelRouteLines += 1; window.fuelRouteGeometries.push(points); return this; }, remove(){ window.fuelRouteRemovals += 1; }}),
      layerGroup: () => ({
        elements: [], addTo(){ return this; },
        clearLayers(){ this.elements.forEach(item => item.remove()); this.elements = []; document.querySelectorAll('.leaflet-popup').forEach(item => item.remove()); }
      }),
      divIcon: options => options,
      marker: (point, options) => {
        const handlers = {};
        const element = document.createElement('button');
        element.type = 'button'; element.className = options.icon.className; element.innerHTML = options.icon.html; element.title = options.title;
        const marker = {
          addTo(layer){ document.getElementById('fuelMap').append(element); layer.elements.push(element); return marker; },
          getElement(){ return element; },
          on(name, callback){ handlers[name] = callback; element.addEventListener(name, callback); return marker; },
          bindPopup(content){ element.addEventListener('click', () => { document.querySelectorAll('.leaflet-popup').forEach(item => item.remove()); const popup = document.createElement('div'); popup.className = 'leaflet-popup'; popup.append(content); document.getElementById('fuelMap').append(popup); }); return marker; }
        };
        return marker;
      }
    };
    """
    browser_paths = [
        shutil.which(name) for name in ("msedge", "google-chrome", "chromium", "chromium-browser")
    ] + [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]
    executable = next((path for path in browser_paths if path and Path(path).exists()), None)
    trip_payload = {
        "route": {
            "start": {"label": "Санкт-Петербург", "latitude": 59.93, "longitude": 30.31},
            "end": {"label": "Мурманск", "latitude": 68.97, "longitude": 33.08},
            "distance_km": 1350.0,
            "duration_minutes": 1040.0,
            "is_approximate": False,
            "provider": "osrm",
            "profile": "driving",
            "warnings": [],
            "geometry": [[59.93, 30.31], [61.2, 31.1], [65.1, 32.4], [68.97, 33.08]],
        },
        "vehicle": {"id": vehicle.id, "title": "Ford Focus 3", "make": "Ford", "model": "Focus", "consumption_source": "manual"},
        "fuel_type": "95",
        "calculation": {
            "can_plan": True, "distance_km": 1350.0, "tank_liters": 55.0,
            "consumption_l_per_100km": 8.5, "fuel_level_percent": 50.0,
            "required_liters": 114.8, "current_fuel_liters": 27.5,
            "current_range_km": 323.5, "safe_current_range_km": 226.5,
            "full_range_km": 647.1, "safe_full_range_km": 550.0,
            "next_refuel_from_km": 192.5, "next_refuel_to_km": 226.5,
            "reserve_percent": 15.0,
        },
        "recommended_stops": [
            {
                "provider": "gdebenz", "provider_station_id": "trip-first",
                "station_id": stations[0].id, "brand": "Teboil", "name": "Тебойл",
                "address": "Ветеранов, 188/1", "latitude": 59.835, "longitude": 30.121,
                "distance_from_start_km": 240.0, "distance_to_route_km": 0.8,
                "updated_at": "2026-09-15T09:30:00Z",
                "fuels": [
                    {"fuel_type": "95", "state": "available", "label": "ЕСТЬ", "symbol": "✓"},
                    {"fuel_type": "98", "state": "low", "label": "МАЛО / ОЧЕРЕДЬ", "symbol": "!"},
                    {"fuel_type": "100", "state": "unavailable", "label": "НЕТ", "symbol": "×"},
                ],
                "selected_fuel": {"fuel_type": "95", "state": "available", "label": "ЕСТЬ", "symbol": "✓"},
                "after_refuel_range_km": 647.1,
            },
            {
                "provider": "gdebenz", "provider_station_id": "trip-second",
                "station_id": None, "brand": "Газпромнефть", "name": "АЗС",
                "address": "У маршрута", "latitude": 59.88, "longitude": 30.25,
                "distance_from_start_km": 720.0, "distance_to_route_km": 0.5,
                "updated_at": None,
                "fuels": [
                    {"fuel_type": fuel, "state": "unknown", "label": "НЕТ ДАННЫХ", "symbol": "·"}
                    for fuel in ("95", "98", "100")
                ],
                "selected_fuel": {"fuel_type": "95", "state": "candidate", "label": "ВОЗМОЖНО", "symbol": "?"},
                "after_refuel_range_km": 647.1,
            },
        ],
        "warnings": ["Расстояние приблизительное."],
    }
    trip_payload_2 = json.loads(json.dumps(trip_payload))
    trip_payload_2["route"]["end"] = {
        "label": "Петрозаводск", "latitude": 61.78, "longitude": 34.35,
    }
    trip_payload_2["route"]["distance_km"] = 435.0
    trip_payload_2["route"]["duration_minutes"] = 360.0
    trip_payload_2["route"]["geometry"] = [
        [59.93, 30.31], [60.2, 31.5], [61.0, 33.1], [61.78, 34.35],
    ]
    trip_payload_unsafe = json.loads(json.dumps(trip_payload))
    trip_payload_unsafe["route"]["end"]["label"] = "Опасный маршрут"
    trip_payload_unsafe["recommended_stops"] = []
    trip_payload_unsafe["warnings"] = [
        "Не найдена подходящая АЗС до минимального остатка топлива."
    ]
    with playwright.sync_playwright() as manager:
        try:
            browser = manager.chromium.launch(headless=True, executable_path=executable) if executable else manager.chromium.launch(headless=True)
        except playwright.Error as exc:
            pytest.skip(f"Chromium browser is unavailable: {exc}")
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page_errors = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.add_init_script(leaflet_stub)
            page.route("https://unpkg.com/leaflet@1.9.4/dist/leaflet.css", lambda route: route.fulfill(content_type="text/css", body=""))
            page.route("https://unpkg.com/leaflet@1.9.4/dist/leaflet.js", lambda route: route.abort())

            def serve_homeos(route):
                path = route.request.url.split("?", 1)[0]
                if path == "http://homeos.test/fuel":
                    route.fulfill(
                        status=200,
                        headers={
                            "Content-Type": "text/html; charset=utf-8",
                            "Content-Security-Policy": response.headers["content-security-policy"],
                            "Permissions-Policy": response.headers["permissions-policy"],
                        },
                        body=response.text,
                    )
                elif path == "http://homeos.test/static/style.css":
                    route.fulfill(
                        content_type="text/css",
                        body=Path("app/static/style.css").read_text(encoding="utf-8"),
                    )
                elif path == "http://homeos.test/api/fuel/trip-plan":
                    submitted = json.loads(route.request.post_data or "{}")
                    if submitted.get("end") == "Петрозаводск":
                        payload = trip_payload_2
                    elif submitted.get("end") == "Опасный маршрут":
                        payload = trip_payload_unsafe
                    else:
                        payload = trip_payload
                    route.fulfill(content_type="application/json", body=json.dumps(payload))
                elif path.endswith("/api/notifications/unread"):
                    route.fulfill(content_type="application/json", body='{"unread_total":0,"threads":[]}')
                else:
                    route.fulfill(status=404, body="")

            page.route("http://homeos.test/**", serve_homeos)
            page.goto("http://homeos.test/fuel", wait_until="load")

            assert page.locator("[data-station-id]:visible").count() == 3
            assert page.evaluate("window.fuelGeoCalls") == 0
            page.locator('[data-fuel="95"]').click()
            assert page.locator("[data-station-id]:visible").count() == 2
            page.locator('[data-fuel="98"]').click()
            assert page.locator("[data-station-id]:visible").count() == 1
            page.locator('[data-fuel="100"]').click()
            assert page.locator("[data-station-id]:visible").count() == 1
            page.locator('[data-fuel="95"]').click()
            page.locator("#fuelOnlyAvailable").check()
            assert page.locator("[data-station-id]:visible").count() == 1
            page.locator("#fuelOnlyAvailable").uncheck()
            page.locator("#fuelStatusFilter").select_option("available")
            assert page.locator("[data-station-id]:visible").count() == 1
            assert "Teboil" in page.locator("[data-station-id]:visible").text_content()
            page.locator('[data-fuel="98"]').click()
            page.locator("#fuelStatusFilter").select_option("candidate")
            assert page.locator("[data-station-id]:visible").count() == 1
            assert "ВОЗМОЖНО" in page.locator("[data-station-id]:visible").text_content()
            page.locator("#fuelBrandFilter").select_option("Газпромнефть")
            assert page.locator(".fuel-filter-empty").is_visible()
            page.locator(".fuel-filter-empty [data-filter-reset]").click()
            page.locator("#fuelSort").select_option("distance")
            assert page.evaluate("window.fuelGeoCalls") == 1

            page.locator('[data-view="map"]').click()
            assert page.locator(".fuel-map-marker").count() == 3
            assert page.locator(".fuel-map-marker").first.bounding_box()["width"] >= 44
            assert page.locator(".fuel-map-marker").first.get_attribute("aria-label")
            assert page.locator(".fuel-map-marker").first.text_content().strip() in {"✓", "?", "×", "·"}
            page.locator(".fuel-map-marker").first.click()
            assert page.locator(".leaflet-popup").is_visible()
            assert page.locator(".leaflet-popup .btn").get_attribute("href").startswith("/fuel/")

            page.locator('[data-view="trip"]').click()
            assert page.locator("[data-trip-panel]").is_visible()
            page.locator('[name="vehicle_id"]').select_option(str(vehicle.id))
            page.locator('[name="start"]').fill("Санкт-Петербург")
            page.locator('[name="end"]').fill("Мурманск")
            page.locator('[name="tank_liters"]').fill("55")
            page.locator('[name="consumption_l_per_100km"]').fill("8.5")
            page.locator("[data-trip-form] [type=submit]").click()
            page.locator(".fuel-trip-stop").first.wait_for()
            assert page.locator(".fuel-trip-stop").count() == 2
            assert "Через 240 км" in page.locator(".fuel-trip-stop").first.text_content()
            assert "до маршрута 0.8 км" in page.locator(".fuel-trip-stop").first.text_content()
            assert "1350 км" in page.locator("[data-trip-summary]").text_content()
            assert page.locator(".fuel-trip-save").is_visible()
            assert page.locator(".fuel-map-marker").count() == 2
            assert page.locator(".fuel-map-marker").nth(1).text_content().strip() == "?"
            assert page.locator(".fuel-trip-endpoint").count() == 2
            assert page.evaluate("window.fuelRouteLines") == 1
            assert page.evaluate("window.fuelRouteGeometries.at(-1).length") == 4
            assert page.evaluate("window.fuelRouteGeometries.at(-1)[1]") == [61.2, 31.1]
            page.locator(".fuel-map-marker").first.click()
            assert "240.0 км от начала" in page.locator(".leaflet-popup").text_content()
            page.locator('[name="end"]').fill("Петрозаводск")
            page.locator("[data-trip-form] [type=submit]").click()
            page.locator("[data-trip-summary]", has_text="Петрозаводск").wait_for()
            assert page.evaluate("window.fuelRouteRemovals") >= 1
            assert page.evaluate("window.fuelRouteGeometries.at(-1)[1]") == [60.2, 31.5]
            assert "Петрозаводск" in page.locator("[data-trip-summary]").text_content()
            assert page.locator(".fuel-map-marker").count() == 2

            page.locator('[name="end"]').fill("Опасный маршрут")
            page.locator("[data-trip-form] [type=submit]").click()
            page.get_by_text(
                "Безопасная остановка не найдена — проверьте предупреждения."
            ).wait_for()
            assert "Дополнительная остановка по расчёту не требуется" not in page.locator(
                "[data-trip-status]"
            ).text_content()
            assert "Не найдена подходящая АЗС" in page.locator(
                "[data-trip-summary]"
            ).text_content()

            page.locator('[name="end"]').fill("Мурманск")
            page.locator("[data-trip-form] [type=submit]").click()
            page.locator(".fuel-trip-stop").first.wait_for()

            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("() => Math.max(document.body.scrollWidth, document.documentElement.scrollWidth) === innerWidth")
            page.locator('[data-view="trip"]').click()
            assert page.locator("[data-trip-panel]").is_visible()
            assert page.locator(".fuel-trip-stop").count() == 2
            assert page.locator(".fuel-trip-save").is_visible()
            assert page.locator("#fuelMap").is_visible()
            page.locator(".fuel-map-marker").first.scroll_into_view_if_needed()
            trip_scroll_before = page.evaluate("scrollY")
            page.locator(".fuel-map-marker").first.click()
            assert page.locator("[data-map-sheet]").is_visible()
            assert "240.0 км от начала" in page.locator("[data-map-sheet]").text_content()
            assert "95" in page.locator("[data-map-sheet]").text_content()
            assert page.locator(".leaflet-popup").count() == 0
            assert page.evaluate("scrollY") == trip_scroll_before
            assert page.locator('[data-map-sheet]:not([hidden])').count() == 1
            page.evaluate("window.fuelSheetReference = document.querySelector('[data-map-sheet]')")
            page.locator(".fuel-map-marker").nth(1).click()
            assert "720.0 км от начала" in page.locator("[data-map-sheet]").text_content()
            assert page.evaluate("document.querySelector('[data-map-sheet]') === window.fuelSheetReference")
            assert page.locator('[data-map-sheet]:not([hidden])').count() == 1
            page.locator("[data-map-sheet-close]").click()
            assert page.evaluate("() => Math.max(document.body.scrollWidth, document.documentElement.scrollWidth) === innerWidth")
            page.locator('[data-view="list"]').click()
            assert page.locator('[data-fuel-view="map"]').evaluate("element => element.hidden && getComputedStyle(element).display === 'none'")
            assert page.locator('[data-fuel-view="trip"]').evaluate("element => element.hidden && getComputedStyle(element).display === 'none'")
            page.locator('[data-fuel="98"]').click()
            page.locator(".fuel-filter-open").click()
            assert page.locator(".fuel-filter-panel").is_visible()
            page.locator("#fuelStatusFilter").select_option("candidate")
            page.locator("[data-filter-apply]").click()
            assert not page.locator(".fuel-filter-panel").is_visible()
            page.evaluate("""() => {
                const list = document.querySelector('[data-fuel-list]');
                const source = list.querySelector('[data-station-id]');
                for (let index = 0; index < 24; index += 1) {
                    const clone = source.cloneNode(true);
                    clone.removeAttribute('data-station-id');
                    clone.hidden = false;
                    list.append(clone);
                }
            }""")
            assert page.locator('[data-fuel-view="list"]').bounding_box()["height"] > 1000
            controls_bottom = page.locator(".fuel-dashboard-controls").bounding_box()["y"] + page.locator(".fuel-dashboard-controls").bounding_box()["height"]
            page.locator('[data-view="map"]').click()
            assert page.locator('[data-fuel-view="list"]').evaluate("element => element.hidden && getComputedStyle(element).display === 'none' && element.getBoundingClientRect().height === 0")
            assert page.locator('[data-fuel-view="map"]').is_visible()
            assert page.locator("#fuelMap").evaluate("element => { const box = element.getBoundingClientRect(); return box.top < innerHeight && box.bottom > 0; }")
            assert page.locator("#fuelMap").bounding_box()["y"] - controls_bottom < 40
            assert page.locator(".fuel-map-marker").count() == 1
            page.locator(".fuel-map-marker").scroll_into_view_if_needed()
            scroll_before_marker = page.evaluate("scrollY")
            page.locator(".fuel-map-marker").click()
            assert page.locator("[data-map-sheet]").is_visible()
            assert page.locator(".leaflet-popup").count() == 0
            assert page.evaluate("scrollY") == scroll_before_marker
            assert "ВОЗМОЖНО" in page.locator("[data-map-sheet]").text_content()
            assert page.locator("[data-map-sheet] dt").all_text_contents() == ["95", "98", "100"]
            assert "Расстояние:" in page.locator("[data-map-distance]").text_content()
            assert page.locator("[data-map-sheet]").evaluate("element => element.parentElement === document.body")
            assert page.locator("[data-map-sheet]").evaluate("element => getComputedStyle(element).position === 'fixed' && Number(getComputedStyle(element).zIndex) > 700")
            assert page.locator("[data-map-sheet]").evaluate("element => { const box = element.getBoundingClientRect(); return box.top >= 0 && box.bottom <= innerHeight; }")
            assert page.locator("#fuelMap").is_visible()
            assert page.locator("#fuelMap").bounding_box()["y"] < page.locator("[data-map-sheet]").bounding_box()["y"]
            assert page.locator('[data-map-sheet]:not([hidden])').count() == 1
            page.locator("[data-map-sheet-close]").click()
            assert not page.locator("[data-map-sheet]").is_visible()
            assert page.evaluate("() => Math.max(document.body.scrollWidth, document.documentElement.scrollWidth) === innerWidth")
            assert page_errors == []

            fallback_page = browser.new_page(viewport={"width": 1440, "height": 900})
            fallback_page.route(
                "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
                lambda route: route.fulfill(content_type="text/css", body=""),
            )
            fallback_page.route("https://unpkg.com/leaflet@1.9.4/dist/leaflet.js", lambda route: route.abort())
            fallback_page.route("http://homeos.test/**", serve_homeos)
            fallback_page.goto("http://homeos.test/fuel", wait_until="load")
            fallback_page.locator('[data-fuel="95"]').click()
            assert fallback_page.locator("[data-station-id]:visible").count() == 2
            fallback_page.locator('[data-view="map"]').click()
            assert fallback_page.locator(".fuel-map-empty").is_visible()
            assert "Список АЗС продолжает работать" in fallback_page.locator(".fuel-map-empty").text_content()
            fallback_page.locator('[data-view="list"]').click()
            assert fallback_page.locator("[data-station-id]:visible").count() == 2
        finally:
            browser.close()


def test_recipes_mobile_actions_stay_inside_viewport(client, login, make_user, db):
    playwright = pytest.importorskip("playwright.sync_api")
    user = make_user("recipes-mobile-browser")
    db.add(Recipe(
        owner_id=user.id,
        title="Очень длинное название рецепта для проверки мобильной вёрстки",
        ingredients="Ингредиенты",
        steps='[{"text":"Приготовить"}]',
        tags="быстро, семейный ужин, повседневное",
        cook_time_minutes=120,
    ))
    db.commit()
    login(user.username)
    response = client.get("/recipes")
    assert response.status_code == 200
    assert "https://unpkg.com" not in response.headers["content-security-policy"]
    assert "geolocation=()" in response.headers["permissions-policy"]

    browser_paths = [
        shutil.which(name) for name in ("msedge", "google-chrome", "chromium", "chromium-browser")
    ] + [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    executable = next((path for path in browser_paths if path and Path(path).exists()), None)
    with playwright.sync_playwright() as manager:
        try:
            browser = manager.chromium.launch(
                headless=True, executable_path=executable
            ) if executable else manager.chromium.launch(headless=True)
        except playwright.Error as exc:
            pytest.skip(f"Chromium browser is unavailable: {exc}")
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.set_content(response.text, wait_until="load")
            page.add_style_tag(content=Path("app/static/style.css").read_text(encoding="utf-8"))
            assert page.evaluate(
                "() => Math.max(document.body.scrollWidth, document.documentElement.scrollWidth) === innerWidth"
            )
            assert page.evaluate(
                "() => [...document.querySelectorAll('.recipe-page-head .btn')].every(button => {"
                " const box = button.getBoundingClientRect(); return box.left >= 0 && box.right <= innerWidth; })"
            )
        finally:
            browser.close()
