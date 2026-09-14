"""Provider-neutral primitives for the first fuel-monitoring phase."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..models import FuelObservation, FuelStation, FuelStationComment
from ..timezone import now_utc

logger = logging.getLogger(__name__)
FUEL_TYPES = ("95", "98", "100")


class ProviderUnavailable(RuntimeError):
    pass


class FuelStationCandidate(BaseModel):
    provider: str
    provider_station_id: str
    name: str | None = None
    brand: str | None = None
    address: str | None = None
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    distance_meters: int | None = Field(default=None, ge=0)
    raw: dict[str, Any] = Field(default_factory=dict)


class GeocodedPlace(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    display_name: str


class FuelDataProvider(Protocol):
    async def get_stations_near(self, latitude: float, longitude: float, radius_km: float) -> list[FuelStationCandidate]: ...

    async def get_station_comments(self, provider_station_id: str, limit: int = 12) -> list[dict[str, Any]]: ...


class Geocoder(Protocol):
    async def search(self, query: str) -> list[GeocodedPlace]: ...


def _value(raw: dict[str, Any], *names: str) -> Any:
    for name in names:
        if raw.get(name) is not None:
            return raw[name]
    return None


def _stations(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("stations", "items", "results", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _distance_meters(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> int:
    radius = 6_371_000
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    delta_p, delta_l = math.radians(b_lat - a_lat), math.radians(b_lon - a_lon)
    value = math.sin(delta_p / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(delta_l / 2) ** 2
    return round(radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value)))


class GdeBenzProvider:
    """Thin, defensive adapter for the unofficial GdeBenz JSON API."""

    def __init__(self, *, timeout_seconds: int = 10, user_agent: str = "HomeOS-FuelMonitor/1.0") -> None:
        self.timeout_seconds = timeout_seconds
        self.headers = {"User-Agent": user_agent, "Accept": "application/json", "Referer": "https://gdebenz.ru/"}

    async def get_stations_near(self, latitude: float, longitude: float, radius_km: float = 3) -> list[FuelStationCandidate]:
        params = {"lat": str(latitude), "lon": str(longitude), "radius_km": str(radius_km)}
        payload = await self._get_json("/api/nearby", params)
        candidates: list[FuelStationCandidate] = []
        for raw in _stations(payload):
            station_id = _value(raw, "osm_id", "id")
            lat, lon = _value(raw, "lat", "latitude"), _value(raw, "lon", "lng", "longitude")
            try:
                if station_id is None or lat is None or lon is None:
                    continue
                station_lat, station_lon = float(lat), float(lon)
            except (TypeError, ValueError):
                continue
            candidates.append(FuelStationCandidate(
                provider="gdebenz", provider_station_id=str(station_id), name=_value(raw, "name", "title"),
                brand=_value(raw, "brand"), address=_value(raw, "address", "addr"), latitude=station_lat, longitude=station_lon,
                distance_meters=_distance_meters(latitude, longitude, station_lat, station_lon), raw=raw,
            ))
        return sorted(candidates, key=lambda item: item.distance_meters or 0)

    async def get_station_comments(self, provider_station_id: str, limit: int = 12) -> list[dict[str, Any]]:
        payload = await self._get_json(f"/api/comments/{provider_station_id}/recent", {"limit": str(limit)})
        return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []

    async def _get_json(self, path: str, params: dict[str, str]) -> Any:
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(base_url="https://gdebenz.ru", headers=self.headers, timeout=self.timeout_seconds) as client:
                    response = await client.get(path, params=params)
                if response.status_code == 429 or response.status_code >= 500:
                    raise ProviderUnavailable(f"GdeBenz HTTP {response.status_code}")
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError, ProviderUnavailable) as exc:
                if attempt == 2:
                    logger.warning("GdeBenz request failed path=%s error=%s", path, type(exc).__name__)
                    raise ProviderUnavailable("Источник GdeBenz временно недоступен") from exc
                await asyncio.sleep(0.4 * (2**attempt))
        raise AssertionError("unreachable")


class NominatimGeocoder:
    _last_request: datetime | None = None

    def __init__(self, *, timeout_seconds: int = 10, user_agent: str = "HomeOS-FuelMonitor/1.0") -> None:
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent

    async def search(self, query: str) -> list[GeocodedPlace]:
        delay = self._last_request and 1 - (datetime.now(timezone.utc) - self._last_request).total_seconds()
        if delay and delay > 0:
            await asyncio.sleep(delay)
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, headers={"User-Agent": self.user_agent}) as client:
                response = await client.get("https://nominatim.openstreetmap.org/search", params={"q": query, "format": "jsonv2", "limit": 5})
            self._last_request = datetime.now(timezone.utc)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Nominatim request failed error=%s", type(exc).__name__)
            raise ProviderUnavailable("Геокодер временно недоступен") from exc
        return [GeocodedPlace(latitude=float(item["lat"]), longitude=float(item["lon"]), display_name=str(item["display_name"])) for item in payload if isinstance(item, dict) and item.get("lat") and item.get("lon")]


def normalize_fuel_states(raw: dict[str, Any], *, stale_after_minutes: int = 120, now: datetime | None = None) -> dict[str, str]:
    """Map documented GdeBenz status words without inferring unavailable from missing data."""
    status = str(raw.get("status") or "unknown").lower()
    last_at = raw.get("last_at")
    if isinstance(last_at, str):
        try:
            parsed = datetime.fromisoformat(last_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            # Freshness is saved independently on observations in Phase 2.  Do
            # not erase a useful source state just because it has aged.
        except ValueError:
            pass
    fuels = raw.get("fuels_now")
    if status in {"unknown", "none", ""} or fuels is None or fuels == "":
        return {fuel: "unknown" for fuel in FUEL_TYPES}
    available = {str(item).replace("аи-", "").replace("AI-", "") for item in (fuels if isinstance(fuels, list) else str(fuels).split(","))}
    if status in {"no", "unavailable"}:
        return {fuel: "unavailable" for fuel in FUEL_TYPES}
    result = {fuel: ("available" if fuel in available else "unavailable") for fuel in FUEL_TYPES}
    if status in {"low", "queue"}:
        result = {fuel: ("low" if value == "available" else value) for fuel, value in result.items()}
    return result


def parse_source_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc).replace(tzinfo=None) if parsed.tzinfo is None else parsed.astimezone(timezone.utc).replace(tzinfo=None)


def source_is_stale(source_updated_at: datetime | None, stale_after_minutes: int, observed_at: datetime) -> bool:
    return source_updated_at is None or observed_at - source_updated_at > timedelta(minutes=stale_after_minutes)


class FuelCollectorHealth:
    running = False
    last_poll_started_at: datetime | None = None
    last_poll_finished_at: datetime | None = None
    last_successful_poll_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None

    def as_dict(self, *, enabled: bool) -> dict[str, Any]:
        return {
            "enabled": enabled, "running": self.running, "last_poll_started_at": self.last_poll_started_at,
            "last_poll_finished_at": self.last_poll_finished_at, "last_successful_poll_at": self.last_successful_poll_at,
            "last_error_at": self.last_error_at, "last_error": self.last_error,
        }


collector_health = FuelCollectorHealth()
fuel_poll_lock = asyncio.Lock()


def _source_confidence(raw: dict[str, Any]) -> float | None:
    value = raw.get("confidence_base")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _save_comments(db: Session, station: FuelStation, comments: list[dict[str, Any]], observed_at: datetime) -> None:
    for raw in comments:
        text = str(raw.get("detail") or raw.get("text") or "").strip() or None
        created_at = parse_source_datetime(raw.get("created_at"))
        identity = f"{station.id}|{text or ''}|{created_at.isoformat() if created_at else ''}"
        key = str(raw.get("id") or hashlib.sha256(identity.encode()).hexdigest())
        if db.scalar(select(FuelStationComment.id).where(FuelStationComment.station_id == station.id, FuelStationComment.source_key == key)):
            continue
        db.add(FuelStationComment(station_id=station.id, provider=station.provider, source_key=key, text=text,
                                 source_created_at=created_at, fetched_at=observed_at, raw_data=raw))


async def run_fuel_poll_cycle(*, session_factory: Any, provider: FuelDataProvider, stale_after_minutes: int,
                              comments_due: bool = False) -> dict[str, int]:
    """Fetch sequentially: SQLite gets one bounded writer transaction per station."""
    collector_health.running = True
    collector_health.last_poll_started_at = now_utc()
    summary = {"stations": 0, "success": 0, "failed": 0, "observations": 0}
    try:
        with session_factory() as db:
            stations = db.scalars(select(FuelStation).options(selectinload(FuelStation.fuels)).where(FuelStation.enabled.is_(True))).all()
        summary["stations"] = len(stations)
        for saved in stations:
            try:
                candidates = await provider.get_stations_near(saved.latitude, saved.longitude, 1)
                candidate = next((item for item in candidates if item.provider_station_id == saved.provider_station_id), None)
                if candidate is None:
                    raise ProviderUnavailable("Станция не найдена в ответе источника")
                observed_at = now_utc()
                raw = candidate.raw
                source_updated_at = parse_source_datetime(raw.get("last_at"))
                states = normalize_fuel_states(raw, stale_after_minutes=stale_after_minutes)
                with session_factory() as db:
                    station = db.get(FuelStation, saved.id)
                    if station is None or not station.enabled:
                        continue
                    for fuel in station.fuels:
                        if fuel.enabled:
                            db.add(FuelObservation(station_id=station.id, fuel_type=fuel.fuel_type, state=states[fuel.fuel_type],
                                observed_at=observed_at, source_updated_at=source_updated_at, source_status=str(raw.get("status") or "") or None,
                                confirmations=int(raw["confirmations"]) if str(raw.get("confirmations", "")).isdigit() else None,
                                confidence=_source_confidence(raw), is_stale=source_is_stale(source_updated_at, stale_after_minutes, observed_at), raw_data=raw))
                            summary["observations"] += 1
                    station.last_successful_poll_at = observed_at
                    if comments_due:
                        try:
                            comments = await provider.get_station_comments(station.provider_station_id)
                            _save_comments(db, station, comments, observed_at)
                        except Exception as exc:
                            # Comment availability is ancillary: it must never
                            # discard a successful state snapshot.
                            logger.warning("Fuel comments poll failed station_id=%s error=%s", station.id, type(exc).__name__)
                    db.commit()
                summary["success"] += 1
                collector_health.last_successful_poll_at = observed_at
            except Exception as exc:
                summary["failed"] += 1
                collector_health.last_error_at, collector_health.last_error = now_utc(), type(exc).__name__
                logger.warning("Fuel station poll failed station_id=%s error=%s", saved.id, type(exc).__name__)
        return summary
    finally:
        collector_health.running = False
        collector_health.last_poll_finished_at = now_utc()
