import asyncio
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models import FuelObservation, FuelStation, FuelStationComment, FuelStationFuel
from app.services.fuel import (
    FuelStationCandidate,
    normalize_fuel_states,
    parse_source_datetime,
    run_fuel_poll_cycle,
    source_is_stale,
)


def test_gdebenz_normalization_per_fuel():
    assert normalize_fuel_states({"status": "yes", "fuels_now": ["95", "100"]}) == {
        "95": "available", "98": "unavailable", "100": "available"
    }


def test_unknown_and_missing_fuels_are_not_unavailable():
    assert set(normalize_fuel_states({"status": "unknown", "fuels_now": None}).values()) == {"unknown"}


def test_low_queue_and_no_are_explicit():
    assert normalize_fuel_states({"status": "low", "fuels_now": ["95"]})["95"] == "low"
    assert normalize_fuel_states({"status": "queue", "fuels_now": ["95"]})["95"] == "low"
    assert set(normalize_fuel_states({"status": "no", "fuels_now": ["95"]}).values()) == {"unavailable"}


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


def test_fuel_page_renders_with_registered_moscow_datetime_filter(client, login, make_user):
    make_user("fuel-page")
    login("fuel-page")
    response = client.get("/fuel")
    assert response.status_code == 200
    assert "АЗС пока не выбраны" in response.text


class FakeProvider:
    def __init__(self):
        self.calls = []

    async def get_stations_near(self, latitude, longitude, radius_km):
        self.calls.append((latitude, longitude))
        return [FuelStationCandidate(provider="gdebenz", provider_station_id="station", latitude=latitude, longitude=longitude,
                                     raw={"status": "yes", "fuels_now": "95,100", "last_at": "2026-09-14 12:00:00", "confirmations": 4, "confidence_base": 0.7})]

    async def get_station_comments(self, provider_station_id, limit=12):
        return [{"status": "yes", "detail": "95", "created_at": "2026-09-14 12:00:00"}]


def test_collector_persists_only_selected_fuels_and_skips_disabled(make_user):
    user = make_user("poller")
    with SessionLocal() as db:
        enabled = FuelStation(owner_id=user.id, provider="gdebenz", provider_station_id="station", latitude=59.9, longitude=30.2)
        disabled = FuelStation(owner_id=user.id, provider="gdebenz", provider_station_id="disabled", latitude=60, longitude=30.2, enabled=False)
        db.add_all([enabled, disabled])
        db.flush()
        db.add_all([FuelStationFuel(station_id=enabled.id, fuel_type="95", enabled=True), FuelStationFuel(station_id=enabled.id, fuel_type="98", enabled=False), FuelStationFuel(station_id=enabled.id, fuel_type="100", enabled=True)])
        db.commit()
        station_id = enabled.id
    provider = FakeProvider()
    summary = asyncio.run(run_fuel_poll_cycle(session_factory=SessionLocal, provider=provider, stale_after_minutes=120, comments_due=True))
    with SessionLocal() as db:
        observations = db.query(FuelObservation).filter_by(station_id=station_id).all()
        comments = db.query(FuelStationComment).filter_by(station_id=station_id).all()
        station = db.get(FuelStation, station_id)
    assert summary == {"stations": 1, "success": 1, "failed": 0, "observations": 2}
    assert {item.fuel_type for item in observations} == {"95", "100"}
    assert station.last_successful_poll_at is not None
    assert len(comments) == 1
    assert len(provider.calls) == 1
