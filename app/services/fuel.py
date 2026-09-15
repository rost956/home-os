"""Provider-neutral primitives for the first fuel-monitoring phase."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, selectinload

from ..models import (
    FuelObservation,
    FuelStation,
    FuelStationChatMessage,
    FuelStationMark,
    FuelStationSubscription,
)
from ..timezone import now_utc

logger = logging.getLogger(__name__)
FUEL_TYPES = ("95", "98", "100")


class ProviderUnavailable(RuntimeError):
    pass


class StationNotFound(RuntimeError):
    """The provider answered, but did not return the saved stable station ID."""

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

    async def get_station_marks(self, provider_station_id: str, limit: int = 12) -> "FuelMarksFeed": ...

    async def get_station_chat(
        self, provider_station_id: str, limit: int = 20, cursor: str | int | None = None
    ) -> "FuelChatFeed": ...


class Geocoder(Protocol):
    async def search(self, query: str) -> list[GeocodedPlace]: ...


class FuelMarksFeed(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)
    latest_source_at: datetime | None = None
    freshness_degraded: bool = False


class FuelChatFeed(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)
    latest_source_at: datetime | None = None
    badges_degraded: bool = False
    next_cursor: str | int | None = None


def _value(raw: dict[str, Any], *names: str) -> Any:
    for name in names:
        if raw.get(name) is not None:
            return raw[name]
    return None


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


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

    def __init__(
        self,
        *,
        timeout_seconds: int = 10,
        user_agent: str = "HomeOS-FuelMonitor/1.0",
        transport: httpx.AsyncBaseTransport | None = None,
        client_id_path: Path | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.client_id_path = client_id_path
        self._cached_client_id: str | None = None
        self._client_id_degraded = False
        self.headers = {"User-Agent": user_agent, "Accept": "application/json", "Referer": "https://gdebenz.ru/"}

    def _client_id(self) -> str:
        """Mirror the public frontend's persistent opaque ``gas_cid`` value."""
        if self._cached_client_id:
            return self._cached_client_id
        value = ""
        if self.client_id_path is not None:
            try:
                value = self.client_id_path.read_text(encoding="ascii").strip().lower()
            except FileNotFoundError:
                pass
            except OSError:
                self._client_id_degraded = True
        if not re.fullmatch(r"[a-f0-9]{16,64}", value):
            value = secrets.token_hex(16)
            if self.client_id_path is not None:
                try:
                    self.client_id_path.parent.mkdir(parents=True, exist_ok=True)
                    self.client_id_path.write_text(value, encoding="ascii")
                except OSError:
                    self._client_id_degraded = True
                    logger.warning("GdeBenz client id is not persistent; marks freshness is degraded")
            else:
                self._client_id_degraded = True
        self._cached_client_id = value
        return value

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

    async def get_station_marks(self, provider_station_id: str, limit: int = 12) -> FuelMarksFeed:
        client_id = self._client_id()
        degraded = self._client_id_degraded
        cvt = ""
        try:
            station_payload = await self._get_json(
                f"/api/comments/{provider_station_id}", {"fp": client_id}
            )
            if isinstance(station_payload, dict):
                cvt = str(station_payload.get("cvt") or "")
        except ProviderUnavailable:
            degraded = True
            logger.warning(
                "GdeBenz marks token unavailable station=%s freshness_degraded=true",
                provider_station_id,
            )
        payload = await self._get_json(
            f"/api/comments/{provider_station_id}/recent",
            {
                "limit": str(limit),
                "fp": client_id,
                "cvt": cvt,
                "_": str(round(datetime.now(timezone.utc).timestamp() * 1000)),
            },
        )
        items = [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []
        latest = max(
            (point for point in (parse_source_datetime(item.get("created_at")) for item in items) if point),
            default=None,
        )
        return FuelMarksFeed(items=items, latest_source_at=latest, freshness_degraded=degraded)

    async def get_station_chat(
        self, provider_station_id: str, limit: int = 20, cursor: str | int | None = None
    ) -> FuelChatFeed:
        params = {"limit": str(limit)}
        if cursor is not None:
            params["cursor"] = str(cursor)
        payload = await self._get_json(
            f"/api/chats/{provider_station_id}", params, base_url="https://api.gdebenz.ru"
        )
        items = payload.get("comments", []) if isinstance(payload, dict) else []
        items = [dict(item) for item in items if isinstance(item, dict)]
        badges_degraded = False
        badge_items = [
            [item.get("id"), item.get("author_id")]
            for item in items
            if item.get("id") is not None and item.get("author_id") is not None
        ]
        if badge_items:
            try:
                badges = await self._get_json(
                    "/api/comments-badges",
                    {},
                    method="POST",
                    json_body={"items": badge_items},
                )
                if isinstance(badges, dict):
                    reliable = {str(value) for value in badges.get("reliable", [])}
                    onsite = {str(value) for value in badges.get("onsite", [])}
                    tiers = {str(key): value for key, value in (badges.get("tiers") or {}).items()}
                    for item in items:
                        author_id = str(item.get("author_id") or "")
                        message_id = str(item.get("id") or "")
                        item["author_reliable"] = author_id in reliable
                        try:
                            item["author_tier"] = int(tiers.get(author_id) or 0)
                        except (TypeError, ValueError):
                            item["author_tier"] = 0
                        item["on_site"] = bool(item.get("on_site") or message_id in onsite)
            except ProviderUnavailable:
                badges_degraded = True
                logger.warning("GdeBenz chat badges unavailable station=%s", provider_station_id)
        latest = max(
            (point for point in (parse_source_datetime(item.get("created_at")) for item in items) if point),
            default=None,
        )
        return FuelChatFeed(
            items=items,
            latest_source_at=latest,
            badges_degraded=badges_degraded,
            next_cursor=payload.get("next_cursor") if isinstance(payload, dict) else None,
        )

    async def _get_json(
        self,
        path: str,
        params: dict[str, str],
        *,
        base_url: str = "https://gdebenz.ru",
        method: str = "GET",
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(
                    base_url=base_url,
                    headers=self.headers,
                    timeout=self.timeout_seconds,
                    follow_redirects=True,
                    transport=self.transport,
                ) as client:
                    response = await client.request(method, path, params=params, json=json_body)
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError, ProviderUnavailable) as exc:
                if attempt == 2:
                    if isinstance(exc, httpx.HTTPStatusError):
                        failed_response = exc.response
                        logger.warning(
                            "GdeBenz request failed path=%s status=%s url=%s body=%s",
                            path,
                            failed_response.status_code,
                            failed_response.request.url,
                            failed_response.text[:500],
                        )
                    else:
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
    last_at = _value(raw, "last_at", "updated")
    if isinstance(last_at, str):
        try:
            parsed = datetime.fromisoformat(last_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            # Freshness is saved independently on observations in Phase 2.  Do
            # not erase a useful source state just because it has aged.
        except ValueError:
            pass
    fuels = _value(raw, "fuels_now", "fuelsNow")
    if status in {"unknown", "none", ""} or fuels is None or fuels == "":
        return {fuel: "unknown" for fuel in FUEL_TYPES}
    available = {
        str(item).strip().lower().replace("аи-", "").replace("ai-", "")
        for item in (fuels if isinstance(fuels, list) else str(fuels).split(","))
    }
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
    next_poll_at: datetime | None = None
    last_station_count = 0
    last_success_count = 0
    last_failed_count = 0
    last_observation_count = 0
    last_marks_received = 0
    last_marks_new = 0
    last_marks_duplicates = 0
    last_marks_source_at: datetime | None = None
    last_marks_feed_stale = False
    last_chat_received = 0
    last_chat_new = 0
    last_chat_source_at: datetime | None = None
    last_manual_poll_started_at: datetime | None = None

    def as_dict(self, *, enabled: bool) -> dict[str, Any]:
        return {
            "enabled": enabled, "running": self.running, "last_poll_started_at": self.last_poll_started_at,
            "last_poll_finished_at": self.last_poll_finished_at, "last_successful_poll_at": self.last_successful_poll_at,
            "last_error_at": self.last_error_at, "last_error": self.last_error,
            "next_poll_at": self.next_poll_at,
            "last_station_count": self.last_station_count,
            "last_success_count": self.last_success_count,
            "last_failed_count": self.last_failed_count,
            "last_observation_count": self.last_observation_count,
            "last_marks_received": self.last_marks_received,
            "last_marks_new": self.last_marks_new,
            "last_marks_duplicates": self.last_marks_duplicates,
            "last_marks_source_at": self.last_marks_source_at,
            "last_marks_feed_stale": self.last_marks_feed_stale,
            "last_chat_received": self.last_chat_received,
            "last_chat_new": self.last_chat_new,
            "last_chat_source_at": self.last_chat_source_at,
        }


collector_health = FuelCollectorHealth()
fuel_poll_lock = asyncio.Lock()


def _source_confidence(raw: dict[str, Any]) -> float | None:
    value = _value(raw, "confidence_base", "confidenceBase")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _mark_source_key(raw: dict[str, Any]) -> str:
    created_at = parse_source_datetime(raw.get("created_at"))
    identity = {
        "created_at": created_at.isoformat(timespec="microseconds") if created_at else str(raw.get("created_at") or ""),
        "status": str(raw.get("status") or "").strip().lower(),
        "detail": " ".join(str(raw.get("detail") or raw.get("text") or "").split()),
        "on_site": bool(raw.get("on_site")),
    }
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _save_marks(
    db: Session, station: FuelStation, marks: list[dict[str, Any]], observed_at: datetime
) -> tuple[int, int]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in marks:
        text = str(raw.get("detail") or raw.get("text") or "").strip() or None
        created_at = parse_source_datetime(raw.get("created_at"))
        key = _mark_source_key(raw)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "station_id": station.id,
            "provider": station.provider,
            "source_key": key,
            "text": text,
            "source_created_at": created_at,
            "fetched_at": observed_at,
            "raw_data": raw,
        })
    if not rows:
        return 0, len(marks)
    result = db.execute(
        sqlite_insert(FuelStationMark)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["station_id", "source_key"])
    )
    created = max(0, int(result.rowcount or 0))
    return created, len(marks) - created


def _save_chat_messages(
    db: Session, station: FuelStation, messages: list[dict[str, Any]], observed_at: datetime
) -> tuple[int, int]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in messages:
        message_id = str(raw.get("id") or "").strip()
        body = str(raw.get("body") or "").strip()
        created_at = parse_source_datetime(raw.get("created_at"))
        if not message_id or not body or created_at is None or message_id in seen:
            continue
        seen.add(message_id)
        rows.append({
            "station_id": station.id,
            "provider": station.provider,
            "provider_message_id": message_id,
            "author_id": str(raw.get("author_id")) if raw.get("author_id") is not None else None,
            "author_name": str(raw.get("author_name") or "").strip() or None,
            "body": body,
            "source_created_at": created_at,
            "reactions_json": raw.get("reactions") if isinstance(raw.get("reactions"), dict) else {},
            "reply_to_id": str(raw.get("reply_to_id")) if raw.get("reply_to_id") is not None else None,
            "reply_to_name": str(raw.get("reply_to_name") or "").strip() or None,
            "reply_to_excerpt": str(raw.get("reply_to_excerpt") or "").strip() or None,
            "author_reliable": bool(raw.get("author_reliable")),
            "author_tier": _safe_int(raw.get("author_tier")),
            "on_site": bool(raw.get("on_site")),
            "ingested_at": observed_at,
        })
    if not rows:
        return 0, len(messages)
    result = db.execute(
        sqlite_insert(FuelStationChatMessage)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["station_id", "provider_message_id"])
    )
    created = max(0, int(result.rowcount or 0))
    return created, len(messages) - created


async def run_fuel_poll_cycle(*, session_factory: Any, provider: FuelDataProvider, stale_after_minutes: int,
                              nearby_radius_km: int, marks_chat_due: bool = False) -> dict[str, int]:
    """Fetch sequentially: SQLite gets one bounded writer transaction per station."""
    collector_health.running = True
    collector_health.last_poll_started_at = now_utc()
    summary = {"stations": 0, "success": 0, "failed": 0, "observations": 0}
    successful_station_ids: list[int] = []
    marks_totals = {"received": 0, "new": 0, "duplicates": 0, "latest": None, "stale": False}
    chat_totals = {"received": 0, "new": 0, "latest": None}
    try:
        with session_factory() as db:
            stations = db.scalars(
                select(FuelStation)
                .join(FuelStationSubscription)
                .options(selectinload(FuelStation.subscriptions))
                .where(FuelStationSubscription.enabled.is_(True))
                .distinct()
            ).all()
        summary["stations"] = len(stations)
        for saved in stations:
            try:
                required_fuels = {
                    fuel_type
                    for subscription in saved.subscriptions
                    if subscription.enabled
                    for fuel_type in subscription.tracked_fuel_types
                }
                if not required_fuels:
                    continue
                candidates = await provider.get_stations_near(saved.latitude, saved.longitude, nearby_radius_km)
                candidate = next((item for item in candidates if item.provider_station_id == saved.provider_station_id), None)
                if candidate is None:
                    candidate_ids = [item.provider_station_id for item in candidates]
                    logger.warning(
                        "Fuel station not found saved_station_id=%s provider_station_id=%s lat=%s lon=%s radius_km=%s candidates=%s",
                        saved.id, saved.provider_station_id, saved.latitude, saved.longitude, nearby_radius_km, candidate_ids,
                    )
                    raise StationNotFound("Станция не найдена в ответе источника")
                observed_at = now_utc()
                raw = candidate.raw
                source_updated_at = parse_source_datetime(_value(raw, "last_at", "updated"))
                states = normalize_fuel_states(raw, stale_after_minutes=stale_after_minutes)
                with session_factory() as db:
                    station = db.get(FuelStation, saved.id)
                    if station is None:
                        continue
                    for fuel_type in sorted(required_fuels):
                        db.add(FuelObservation(station_id=station.id, fuel_type=fuel_type, state=states[fuel_type],
                            observed_at=observed_at, source_updated_at=source_updated_at, source_status=str(raw.get("status") or "") or None,
                            confirmations=int(raw["confirmations"]) if str(raw.get("confirmations", "")).isdigit() else None,
                            confidence=_source_confidence(raw), is_stale=source_is_stale(source_updated_at, stale_after_minutes, observed_at), raw_data=raw))
                        summary["observations"] += 1
                    station.last_successful_poll_at = observed_at
                    if marks_chat_due:
                        try:
                            marks_feed = await provider.get_station_marks(station.provider_station_id)
                            marks_stale = marks_feed.freshness_degraded or bool(
                                source_updated_at
                                and (
                                    marks_feed.latest_source_at is None
                                    or source_updated_at - marks_feed.latest_source_at > timedelta(minutes=30)
                                )
                            )
                            marks_for_storage = [
                                {**item, "_feed_stale": marks_stale} for item in marks_feed.items
                            ]
                            with db.begin_nested():
                                marks_new, marks_duplicates = _save_marks(
                                    db, station, marks_for_storage, observed_at
                                )
                            marks_totals["received"] += len(marks_feed.items)
                            marks_totals["new"] += marks_new
                            marks_totals["duplicates"] += marks_duplicates
                            marks_totals["stale"] = marks_totals["stale"] or marks_stale
                            if marks_feed.latest_source_at and (
                                marks_totals["latest"] is None
                                or marks_feed.latest_source_at > marks_totals["latest"]
                            ):
                                marks_totals["latest"] = marks_feed.latest_source_at
                            logger.info(
                                "Fuel marks station=%s received=%s new=%s duplicates=%s latest=%s stale=%s",
                                station.id,
                                len(marks_feed.items),
                                marks_new,
                                marks_duplicates,
                                marks_feed.latest_source_at.isoformat() if marks_feed.latest_source_at else None,
                                str(marks_stale).lower(),
                            )
                        except Exception as exc:
                            marks_totals["stale"] = True
                            logger.warning("Fuel marks poll failed station_id=%s error=%s", station.id, type(exc).__name__)
                        try:
                            has_chat_history = db.scalar(select(FuelStationChatMessage.id).where(
                                FuelStationChatMessage.station_id == station.id
                            ).limit(1)) is not None
                            chat_feed = await provider.get_station_chat(station.provider_station_id)
                            chat_items = list(chat_feed.items)
                            cursor = chat_feed.next_cursor
                            pages = 1
                            while not has_chat_history and cursor is not None and pages < 3:
                                older_feed = await provider.get_station_chat(
                                    station.provider_station_id, cursor=cursor
                                )
                                chat_items.extend(older_feed.items)
                                cursor = older_feed.next_cursor
                                pages += 1
                            with db.begin_nested():
                                chat_new, _chat_duplicates = _save_chat_messages(
                                    db, station, chat_items, observed_at
                                )
                            chat_totals["received"] += len(chat_items)
                            chat_totals["new"] += chat_new
                            if chat_feed.latest_source_at and (
                                chat_totals["latest"] is None
                                or chat_feed.latest_source_at > chat_totals["latest"]
                            ):
                                chat_totals["latest"] = chat_feed.latest_source_at
                            logger.info(
                                "Fuel chat station=%s received=%s new=%s latest=%s",
                                station.id,
                                len(chat_items),
                                chat_new,
                                chat_feed.latest_source_at.isoformat() if chat_feed.latest_source_at else None,
                            )
                        except Exception as exc:
                            logger.warning("Fuel chat poll failed station_id=%s error=%s", station.id, type(exc).__name__)
                    db.commit()
                summary["success"] += 1
                successful_station_ids.append(saved.id)
                collector_health.last_successful_poll_at = observed_at
            except Exception as exc:
                summary["failed"] += 1
                message = str(exc).strip() or type(exc).__name__
                collector_health.last_error_at, collector_health.last_error = now_utc(), message[:200]
                logger.warning("Fuel station poll failed station_id=%s error=%s", saved.id, type(exc).__name__)
        from .fuel_analytics import process_fuel_history

        for station_id in successful_station_ids:
            with session_factory() as db:
                events, forecasts = process_fuel_history(db, station_id)
                if events or forecasts:
                    logger.info(
                        "Fuel analytics updated station_id=%s delivery_events=%s forecasts=%s",
                        station_id,
                        events,
                        forecasts,
                    )
        collector_health.last_station_count = summary["stations"]
        collector_health.last_success_count = summary["success"]
        collector_health.last_failed_count = summary["failed"]
        collector_health.last_observation_count = summary["observations"]
        if marks_chat_due:
            collector_health.last_marks_received = int(marks_totals["received"])
            collector_health.last_marks_new = int(marks_totals["new"])
            collector_health.last_marks_duplicates = int(marks_totals["duplicates"])
            collector_health.last_marks_source_at = marks_totals["latest"]
            collector_health.last_marks_feed_stale = bool(marks_totals["stale"])
            collector_health.last_chat_received = int(chat_totals["received"])
            collector_health.last_chat_new = int(chat_totals["new"])
            collector_health.last_chat_source_at = chat_totals["latest"]
        if summary["failed"] == 0:
            collector_health.last_error = None
        return summary
    finally:
        collector_health.running = False
        collector_health.last_poll_finished_at = now_utc()
