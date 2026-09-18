from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import inspect, select

import app.main as main_module
from app.database import engine
from app.models import (
    FuelStationSubscription,
    Vehicle,
    VehicleFuelEntry,
    VehicleTrip,
    VehicleTripFuelEntry,
    VehicleTripPlannedStop,
)
from app.services.vehicle_trips import (
    TripValidationError,
    complete_trip,
    create_trip_snapshot,
    decode_polyline,
    encode_polyline,
    link_fuel_entry,
    start_trip,
    trip_metrics,
    unlink_fuel_entry,
)


def vehicle(owner_id: int, name: str = "Focus") -> Vehicle:
    return Vehicle(
        owner_id=owner_id,
        display_name=name,
        make="Ford",
        model="Focus",
        year=2020,
        current_odometer=100_000,
    )


def plan(*, distance: float = 1000, state: str = "available") -> dict:
    return {
        "route": {
            "start": {"label": "Санкт-Петербург", "latitude": 59.93, "longitude": 30.31},
            "end": {"label": "Мурманск", "latitude": 68.97, "longitude": 33.07},
            "distance_km": distance,
            "duration_minutes": 960,
            "geometry": [[59.93, 30.31], [62.0, 31.0], [68.97, 33.07]],
            "provider": "osrm",
            "profile": "driving",
            "is_approximate": False,
        },
        "vehicle": {"id": 1, "title": "Focus"},
        "fuel_type": "95",
        "calculation": {
            "can_plan": True,
            "distance_km": distance,
            "tank_liters": 50,
            "consumption_l_per_100km": 8,
            "fuel_level_percent": 50,
            "required_liters": distance * 0.08,
            "current_fuel_liters": 25,
            "current_range_km": 312.5,
        },
        "recommended_stops": [
            {
                "provider": "gdebenz",
                "provider_station_id": "automatic-station",
                "brand": "Teboil",
                "name": "Тебойл",
                "address": "Трасса, 1",
                "latitude": 61.0,
                "longitude": 31.0,
                "route_progress_km": 250,
                "distance_from_start_km": 250,
                "distance_to_route_km": 0.8,
                "updated_at": "2026-09-18T10:00:00Z",
                "station_id": None,
                "selected_fuel": {"state": state, "label": "ЕСТЬ"},
                "after_refuel_range_km": 625,
            }
        ],
        "warnings": ["Историческое предупреждение"],
    }


def fuel_entry(vehicle_id: int, ident_day: int, odometer: int, liters: str, cost: str) -> VehicleFuelEntry:
    liters_value = Decimal(liters)
    cost_value = Decimal(cost)
    return VehicleFuelEntry(
        vehicle_id=vehicle_id,
        occurred_on=date(2026, 9, ident_day),
        odometer=odometer,
        liters=liters_value,
        total_cost=cost_value,
        price_per_liter=(cost_value / liters_value).quantize(Decimal("0.001")),
        full_tank=False,
    )


def test_polyline_round_trip_is_compact_and_precise():
    points = [[59.93428, 30.33510], [60.00001, 31.00002], [68.97068, 33.07497]]
    encoded = encode_polyline(points)
    assert len(encoded) < len(str(points))
    assert decode_polyline(encoded) == points


def test_trip_tables_and_runtime_migration_are_idempotent():
    main_module.ensure_runtime_schema()
    main_module.ensure_runtime_schema()
    tables = set(inspect(engine).get_table_names())
    assert {"vehicle_trips", "vehicle_trip_planned_stops", "vehicle_trip_fuel_entries"} <= tables


def test_save_trip_uses_server_plan_is_idempotent_and_does_not_subscribe(
    client, db, make_user, login, monkeypatch
):
    owner = make_user("trip-save")
    car = vehicle(owner.id)
    db.add(car)
    db.commit()

    authoritative = plan(distance=1337)

    async def fake_plan(request_data, user, session):
        assert request_data.vehicle_id == car.id
        assert user.id == owner.id
        assert session is not None
        return authoritative

    monkeypatch.setattr(main_module, "_build_fuel_trip_plan", fake_plan)
    login(owner.username)
    payload = {
        "vehicle_id": car.id,
        "start": "СПб",
        "end": "Мурманск",
        "fuel_type": "95",
        "fuel_level_percent": 50,
        "tank_liters": 50,
        "consumption_l_per_100km": 8,
        "client_request_id": "save_request_123",
        "distance_km": 1,
    }
    first = client.post("/api/fuel/trips", json=payload)
    second = client.post("/api/fuel/trips", json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json()["created"] is True and second.json()["created"] is False
    assert first.json()["id"] == second.json()["id"]

    saved = db.scalar(select(VehicleTrip))
    stop = db.scalar(select(VehicleTripPlannedStop))
    assert saved.planned_distance_km == Decimal("1337.00")
    assert saved.route_provider == "osrm"
    assert decode_polyline(saved.route_geometry_polyline) == authoritative["route"]["geometry"]
    assert stop.provider_station_id == "automatic-station"
    assert stop.station_id is None and stop.availability_state == "available"
    assert db.query(FuelStationSubscription).count() == 0


def test_lifecycle_one_active_trip_per_vehicle_and_different_vehicles_allowed(db, make_user):
    owner = make_user("trip-life")
    first_car, second_car = vehicle(owner.id, "One"), vehicle(owner.id, "Two")
    db.add_all([first_car, second_car])
    db.commit()
    first, _ = create_trip_snapshot(db, owner_id=owner.id, vehicle=first_car, client_request_id="life_first", plan=plan())
    second, _ = create_trip_snapshot(db, owner_id=owner.id, vehicle=first_car, client_request_id="life_second", plan=plan())
    other, _ = create_trip_snapshot(db, owner_id=owner.id, vehicle=second_car, client_request_id="life_other", plan=plan())

    start_trip(db, first, odometer_km=100_000, fuel_level=40, fuel_unit="liters")
    try:
        start_trip(db, second, odometer_km=100_000, fuel_level=50, fuel_unit="percent")
    except TripValidationError as exc:
        assert "уже есть активная" in str(exc)
    else:  # pragma: no cover - protects the lifecycle invariant
        raise AssertionError("second trip unexpectedly became active")
    start_trip(db, other, odometer_km=100_000, fuel_level=None, fuel_unit="percent")
    assert first.status == other.status == "active"

    complete_trip(db, first, odometer_km=101_000, fuel_level=15, fuel_unit="liters", notes="Готово")
    assert first.status == "completed"
    assert first.started_at and first.completed_at
    try:
        start_trip(db, first, odometer_km=101_000, fuel_level=None, fuel_unit="percent")
    except TripValidationError:
        pass
    else:  # pragma: no cover
        raise AssertionError("completed trip restarted")


def test_metrics_distinguish_purchased_consumed_actual_and_estimated(db, make_user):
    owner = make_user("trip-metrics")
    car = vehicle(owner.id)
    db.add(car)
    db.commit()
    trip, _ = create_trip_snapshot(db, owner_id=owner.id, vehicle=car, client_request_id="metrics_trip", plan=plan())
    start_trip(db, trip, odometer_km=100_000, fuel_level=40, fuel_unit="liters")
    entries = [
        fuel_entry(car.id, 18, 100_400, "30", "1800"),
        fuel_entry(car.id, 19, 100_800, "25", "1500"),
    ]
    db.add_all(entries)
    db.commit()
    for entry in entries:
        link_fuel_entry(db, trip, entry)
    complete_trip(db, trip, odometer_km=101_000, fuel_level=15, fuel_unit="liters", notes=None)

    metrics = trip_metrics(trip)
    assert metrics["distance"] == {"value": Decimal("1000"), "source": "odometer", "quality": "actual"}
    assert metrics["purchased_liters"] == Decimal("55.000")
    assert metrics["fuel_consumed_liters"] == Decimal("80.00")
    assert metrics["consumption_l_per_100km"] == Decimal("8.00")
    assert metrics["consumption_quality"] == "actual"
    assert metrics["cost"] == Decimal("3300.00")

    estimated, _ = create_trip_snapshot(db, owner_id=owner.id, vehicle=car, client_request_id="metrics_estimated", plan=plan(distance=750))
    start_trip(db, estimated, odometer_km=102_000, fuel_level=50, fuel_unit="percent")
    estimate_entry = fuel_entry(car.id, 20, 102_500, "50", "3000")
    db.add(estimate_entry)
    db.commit()
    link_fuel_entry(db, estimated, estimate_entry)
    complete_trip(db, estimated, odometer_km=102_750, fuel_level=25, fuel_unit="percent", notes=None)
    estimated_metrics = trip_metrics(estimated)
    assert estimated_metrics["purchased_liters"] == Decimal("50.000")
    assert estimated_metrics["fuel_consumed_liters"] == Decimal("62.50")
    assert estimated_metrics["consumption_quality"] == "estimated"


def test_link_unlink_delete_preserves_existing_fuel_entry(db, make_user):
    owner = make_user("trip-link")
    first_car, second_car = vehicle(owner.id, "One"), vehicle(owner.id, "Two")
    db.add_all([first_car, second_car])
    db.commit()
    trip, _ = create_trip_snapshot(db, owner_id=owner.id, vehicle=first_car, client_request_id="link_trip", plan=plan())
    start_trip(db, trip, odometer_km=100_000, fuel_level=None, fuel_unit="percent")
    entry = fuel_entry(first_car.id, 18, 100_100, "20", "1200")
    wrong = fuel_entry(second_car.id, 18, 100_100, "20", "1200")
    db.add_all([entry, wrong])
    db.commit()
    assert link_fuel_entry(db, trip, entry) is True
    assert link_fuel_entry(db, trip, entry) is False
    try:
        link_fuel_entry(db, trip, wrong)
    except TripValidationError as exc:
        assert "другому автомобилю" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("wrong vehicle entry linked")
    unlink_fuel_entry(db, trip, entry.id)
    assert db.get(VehicleFuelEntry, entry.id) is not None
    link_fuel_entry(db, trip, entry)
    trip_id, entry_id, car_id = trip.id, entry.id, first_car.id
    db.delete(trip)
    db.commit()
    assert db.get(VehicleTrip, trip_id) is None
    assert db.get(VehicleFuelEntry, entry_id) is not None
    assert db.get(Vehicle, car_id) is not None
    assert db.query(VehicleTripFuelEntry).count() == 0


def test_trip_privacy_history_snapshot_and_offline_detail(client, db, make_user, login, monkeypatch):
    owner = make_user("trip-owner")
    stranger = make_user("trip-stranger")
    car = vehicle(owner.id)
    db.add(car)
    db.commit()
    trip, _ = create_trip_snapshot(db, owner_id=owner.id, vehicle=car, client_request_id="private_trip", plan=plan(state="available"))

    def external_call_forbidden(*args, **kwargs):
        raise AssertionError("history must not call a live provider")

    monkeypatch.setattr(main_module, "make_route_engine", external_call_forbidden)
    monkeypatch.setattr(main_module, "make_gdebenz_provider", external_call_forbidden)
    login(stranger.username)
    assert client.get(f"/vehicles/trips/{trip.id}").status_code == 404
    assert client.get(f"/api/fuel/trips/{trip.id}").status_code == 404

    client.post("/logout")
    login(owner.username)
    detail = client.get(f"/vehicles/trips/{trip.id}")
    api = client.get(f"/api/fuel/trips/{trip.id}")
    assert detail.status_code == api.status_code == 200
    assert "на момент планирования" in detail.text
    assert "ЕСТЬ" in detail.text
    assert api.json()["planned_stops"][0]["availability_state"] == "available"


def test_active_trip_is_offered_and_new_refuel_links_explicitly(client, db, make_user, login):
    owner = make_user("trip-refuel-form")
    car = vehicle(owner.id)
    db.add(car)
    db.commit()
    trip, _ = create_trip_snapshot(db, owner_id=owner.id, vehicle=car, client_request_id="form_trip", plan=plan())
    start_trip(db, trip, odometer_km=100_000, fuel_level=None, fuel_unit="percent")
    login(owner.username)

    form = client.get(f"/vehicles/{car.id}/fuel/new")
    assert form.status_code == 200
    assert f'value="{trip.id}"' in form.text
    response = client.post(
        f"/vehicles/{car.id}/fuel",
        data={
            "occurred_on": "2026-09-18",
            "odometer": "100100",
            "liters": "20",
            "total_cost": "1200",
            "trip_id": str(trip.id),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    entry = db.scalar(select(VehicleFuelEntry))
    assert db.scalar(select(VehicleTripFuelEntry).where(VehicleTripFuelEntry.fuel_entry_id == entry.id)).trip_id == trip.id


def test_negative_consumption_is_inconsistent_not_a_number(db, make_user):
    owner = make_user("trip-inconsistent")
    car = vehicle(owner.id)
    db.add(car)
    db.commit()
    trip, _ = create_trip_snapshot(db, owner_id=owner.id, vehicle=car, client_request_id="bad_fuel", plan=plan())
    start_trip(db, trip, odometer_km=100_000, fuel_level=5, fuel_unit="liters")
    complete_trip(db, trip, odometer_km=100_500, fuel_level=45, fuel_unit="liters", notes=None)
    metrics = trip_metrics(trip)
    assert metrics["fuel_consumed_liters"] is None
    assert metrics["consumption_l_per_100km"] is None
    assert metrics["consumption_quality"] == "inconsistent"
    assert metrics["warning"]


def test_suggested_refuels_are_limited_to_trip_dates_and_unlinked(client, db, make_user, login):
    owner = make_user("trip-suggest")
    car = vehicle(owner.id)
    db.add(car)
    db.commit()
    trip, _ = create_trip_snapshot(db, owner_id=owner.id, vehicle=car, client_request_id="suggest_trip", plan=plan())
    start_trip(db, trip, odometer_km=100_000, fuel_level=None, fuel_unit="percent")
    trip.started_at = datetime(2026, 9, 18, 10)
    trip.completed_at = datetime(2026, 9, 20, 20)
    trip.status = "completed"
    entries = [
        fuel_entry(car.id, 17, 100_010, "10", "600"),
        fuel_entry(car.id, 18, 100_100, "20", "1200"),
        fuel_entry(car.id, 20, 100_500, "30", "1800"),
        fuel_entry(car.id, 21, 100_700, "10", "600"),
    ]
    db.add_all(entries)
    db.commit()
    link_fuel_entry(db, trip, entries[1])
    login(owner.username)
    page = client.get(f"/vehicles/trips/{trip.id}")
    assert page.status_code == 200
    assert "1800" in page.text
    assert "600" not in page.text


def test_trip_html_lifecycle_validates_odometer_and_ownership(client, db, make_user, login):
    owner = make_user("trip-html-life")
    stranger = make_user("trip-html-other")
    car = vehicle(owner.id)
    db.add(car)
    db.commit()
    trip, _ = create_trip_snapshot(db, owner_id=owner.id, vehicle=car, client_request_id="html_life", plan=plan())

    login(stranger.username)
    assert client.post(f"/vehicles/trips/{trip.id}/start", data={}).status_code == 404
    client.post("/logout")
    login(owner.username)
    started = client.post(
        f"/vehicles/trips/{trip.id}/start",
        data={"odometer_km": "100000", "fuel_level": "40", "fuel_unit": "liters"},
        follow_redirects=False,
    )
    assert started.status_code == 303
    db.refresh(trip)
    assert trip.status == "active"

    invalid = client.post(
        f"/vehicles/trips/{trip.id}/complete",
        data={"odometer_km": "99000", "fuel_level": "20", "fuel_unit": "liters"},
        follow_redirects=False,
    )
    assert invalid.status_code == 303
    db.refresh(trip)
    assert trip.status == "active"
    completed = client.post(
        f"/vehicles/trips/{trip.id}/complete",
        data={"odometer_km": "101000", "fuel_level": "20", "fuel_unit": "liters"},
        follow_redirects=False,
    )
    assert completed.status_code == 303
    db.refresh(trip)
    assert trip.status == "completed"


def test_trip_history_and_detail_have_no_overflow_at_mobile_or_desktop(
    client, db, make_user, login
):
    sync_api = pytest.importorskip("playwright.sync_api")
    owner = make_user("trip-browser")
    car = vehicle(owner.id)
    db.add(car)
    db.commit()
    trip, _ = create_trip_snapshot(
        db,
        owner_id=owner.id,
        vehicle=car,
        client_request_id="browser_trip",
        plan=plan(),
    )
    login(owner.username)
    pages = [client.get("/vehicles/trips").text, client.get(f"/vehicles/trips/{trip.id}").text]
    stylesheet = client.get("/static/style.css?v=76").text
    pages = [
        re.sub(
            r'<link rel="stylesheet" href="/static/style\.css\?v=76">',
            f"<style>{stylesheet}</style>",
            re.sub(r'<(?:link|script)[^>]+(?:leaflet|unpkg)[^>]*>(?:</script>)?', "", html),
        )
        for html in pages
    ]
    errors: list[str] = []
    try:
        with sync_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            for width in (390, 1440):
                for html in pages:
                    page = browser.new_page(viewport={"width": width, "height": 900})
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.set_content(html, wait_until="load")
                    assert page.evaluate(
                        "Math.max(document.body.scrollWidth, document.documentElement.scrollWidth) <= innerWidth"
                    )
                    assert page.locator(".vehicle-trip-status").count() >= 1
                    page.close()
            browser.close()
    except sync_api.Error as exc:  # pragma: no cover - depends on local browser assets
        pytest.skip(f"Playwright Chromium unavailable: {exc}")
    assert errors == []
