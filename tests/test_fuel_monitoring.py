import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import app.services.fuel as fuel_module
from app.database import SessionLocal
from app.models import (
    FuelDeliveryEvent,
    FuelForecast,
    FuelObservation,
    FuelStation,
    FuelStationComment,
    FuelStationFuel,
)
from app.services.fuel import (
    FuelStationCandidate,
    GdeBenzProvider,
    ProviderUnavailable,
    normalize_fuel_states,
    parse_source_datetime,
    run_fuel_poll_cycle,
    source_is_stale,
)
from app.services.fuel_analytics import (
    backfill_delivery_events,
    build_forecast,
    comment_signal,
    correlate_event_series,
    detect_delivery_events,
    station_correlations,
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
    summary = asyncio.run(run_fuel_poll_cycle(session_factory=SessionLocal, provider=provider, stale_after_minutes=120, nearby_radius_km=3, comments_due=True))
    with SessionLocal() as db:
        observations = db.query(FuelObservation).filter_by(station_id=station_id).all()
        comments = db.query(FuelStationComment).filter_by(station_id=station_id).all()
        station = db.get(FuelStation, station_id)
    assert summary == {"stations": 1, "success": 1, "failed": 0, "observations": 2}
    assert {item.fuel_type for item in observations} == {"95", "100"}
    assert station.last_successful_poll_at is not None
    assert len(comments) == 1
    assert len(provider.calls) == 1
    assert provider.calls[0] == (59.9, 30.2)


def test_collector_uses_configured_radius_and_matches_station_id(make_user):
    user = make_user("radius-poller")
    with SessionLocal() as db:
        station = FuelStation(owner_id=user.id, provider="gdebenz", provider_station_id="usr_-yN7-ZKW2RA", latitude=59.834818865764575, longitude=30.12108201831411)
        db.add(station)
        db.flush()
        db.add(FuelStationFuel(station_id=station.id, fuel_type="95", enabled=True))
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

        async def get_station_comments(self, provider_station_id, limit=12):
            return []

    provider = RadiusProvider()
    summary = asyncio.run(run_fuel_poll_cycle(session_factory=SessionLocal, provider=provider, stale_after_minutes=120, nearby_radius_km=3))
    assert provider.radius == 3
    assert summary["success"] == 1
    with SessionLocal() as db:
        observation = db.query(FuelObservation).one()
    assert observation.station_id == station_id
    assert observation.state == "low"


def observation(identifier, state, minute, *, stale=False, confidence=0.8, confirmations=5):
    return FuelObservation(id=identifier, station_id=1, fuel_type="95", state=state,
        observed_at=datetime(2026, 9, 14, 15, minute), is_stale=stale,
        confidence=confidence, confirmations=confirmations)


def test_delivery_detector_requires_stable_transition_and_is_explainable():
    events = detect_delivery_events([
        observation(1, "unavailable", 10), observation(2, "unavailable", 15),
        observation(3, "available", 20), observation(4, "available", 25),
    ])
    assert len(events) == 1
    assert events[0]["window_start"] == datetime(2026, 9, 14, 15, 15)
    assert events[0]["window_end"] == datetime(2026, 9, 14, 15, 20)
    assert events[0]["confidence"] >= 0.8
    assert events[0]["evidence_json"]["following_available_count"] == 2


def test_delivery_detector_rejects_noise_unknown_large_gap_and_stale():
    assert detect_delivery_events([observation(1, "available", 10), observation(2, "unavailable", 15), observation(3, "available", 20)]) == []
    assert detect_delivery_events([observation(1, "unknown", 10), observation(2, "unknown", 15), observation(3, "available", 20), observation(4, "available", 25)]) == []
    large_gap = [observation(1, "unavailable", 0), observation(2, "unavailable", 1),
                 observation(3, "available", 2), observation(4, "available", 3)]
    large_gap[2].observed_at += timedelta(hours=8)
    large_gap[3].observed_at += timedelta(hours=8)
    assert detect_delivery_events(large_gap) == []
    assert detect_delivery_events([observation(1, "unavailable", 10), observation(2, "unavailable", 15, stale=True),
                                   observation(3, "available", 20), observation(4, "available", 25)]) == []


def test_comment_rules_are_fuel_specific_and_metadata_weighted():
    positive = FuelStationComment(station_id=1, provider="gdebenz", source_key="p", text="Привезли 95",
        fetched_at=datetime(2026, 9, 14, 15), raw_data={"on_site": True, "author_reliable": True, "author_tier": 3})
    waiting = FuelStationComment(station_id=1, provider="gdebenz", source_key="n", text="Ждут бензовоз, не привезли 95",
        fetched_at=datetime(2026, 9, 14, 15), raw_data={})
    assert comment_signal(positive, "95") > 0.05
    assert comment_signal(positive, "100") == 0
    assert comment_signal(waiting, "95") < 0


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


def add_delivery(db, station_id, when, confidence=0.9, fuel_type="95"):
    before = FuelObservation(station_id=station_id, fuel_type=fuel_type, state="unavailable", observed_at=when - timedelta(minutes=5), is_stale=False)
    after = FuelObservation(station_id=station_id, fuel_type=fuel_type, state="available", observed_at=when, is_stale=False)
    db.add_all([before, after])
    db.flush()
    event = FuelDeliveryEvent(station_id=station_id, fuel_type=fuel_type, window_start=before.observed_at,
        window_end=after.observed_at, estimated_at=when, confidence=confidence, before_observation_id=before.id,
        after_observation_id=after.id, detection_reason="test", evidence_json={}, detector_version="test")
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


def test_fuel_detail_renders_delivery_forecast_and_technical_history(client, login, make_user, db):
    user = make_user("fuel-detail")
    station = FuelStation(owner_id=user.id, provider="gdebenz", provider_station_id="detail", brand="Teboil",
        address="Ветеранов, 188/1", latitude=59.9, longitude=30.2)
    db.add(station)
    db.flush()
    event = add_delivery(db, station.id, datetime(2026, 9, 14, 15))
    db.flush()
    db.add(FuelForecast(station_id=station.id, fuel_type="95", generated_at=datetime(2026, 9, 14, 16),
        expected_at=datetime(2026, 9, 15, 15), range_from=datetime(2026, 9, 15, 14), range_to=datetime(2026, 9, 15, 16),
        confidence=0.74, model_version="test", reason_json={"event_count": 3, "typical_time": "18:00", "median_interval_minutes": 1440}))
    db.commit()
    login("fuel-detail")
    response = client.get(f"/fuel/{station.id}")
    assert response.status_code == 200
    assert "История поставок" in response.text
    assert "Почему такой прогноз?" in response.text
    assert "Техническая история" in response.text
    assert str(round(event.confidence * 100)) in response.text
