"""Provider-neutral automotive routing with a small in-process cache."""

from __future__ import annotations

import logging
import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)

ROAD_DISTANCE_FACTOR = 1.18
FALLBACK_WARNING = (
    "Сервис дорожных маршрутов временно недоступен. "
    "Расстояние рассчитано приблизительно."
)


class RouteError(Exception):
    """Base class for expected routing failures."""


class RouteUnavailable(RouteError):
    """The routing provider failed technically and approximation is allowed."""


class RouteNotFound(RouteError):
    """The provider successfully established that no road route exists."""


@dataclass(frozen=True)
class RouteResult:
    distance_km: float
    duration_minutes: float | None
    geometry: tuple[tuple[float, float], ...]
    provider: str
    profile: str
    is_approximate: bool = False
    warnings: tuple[str, ...] = ()


class RouteProvider(Protocol):
    name: str
    profile: str

    async def get_route(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> RouteResult: ...


def validate_coordinate(latitude: float, longitude: float) -> tuple[float, float]:
    """Validate and normalize a WGS84 coordinate without accepting NaN/Infinity."""
    latitude = float(latitude)
    longitude = float(longitude)
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        raise ValueError("Координаты должны быть конечными числами")
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("Координаты находятся вне допустимого диапазона")
    return latitude, longitude


class OSRMRouteProvider:
    """Normalize the OSRM Route API into HomeOS ``lat, lon`` geometry."""

    name = "osrm"

    def __init__(
        self,
        base_url: str,
        *,
        profile: str = "driving",
        timeout_seconds: float = 8,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.profile = profile
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def get_route(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> RouteResult:
        start_lat, start_lon = validate_coordinate(*start)
        end_lat, end_lon = validate_coordinate(*end)
        coordinates = f"{start_lon:.6f},{start_lat:.6f};{end_lon:.6f},{end_lat:.6f}"
        url = f"{self.base_url}/route/v1/{self.profile}/{coordinates}"
        params = {
            "overview": "full",
            "geometries": "geojson",
            "steps": "false",
            "alternatives": "false",
        }
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.transport,
                follow_redirects=True,
                headers={"Accept": "application/json"},
            ) as client:
                response = await client.get(url, params=params)
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            logger.warning("Route provider failed provider=%s error=%s", self.name, type(exc).__name__)
            raise RouteUnavailable("Routing provider request failed") from exc

        elapsed_ms = round((time.monotonic() - started) * 1000)
        try:
            payload = response.json()
        except ValueError as exc:
            logger.warning(
                "Route provider returned invalid JSON provider=%s status=%s duration_ms=%s",
                self.name,
                response.status_code,
                elapsed_ms,
            )
            raise RouteUnavailable("Routing provider returned invalid JSON") from exc

        if isinstance(payload, dict) and payload.get("code") == "NoRoute":
            raise RouteNotFound("Автомобильный маршрут между указанными точками не найден.")
        if response.status_code >= 400:
            logger.warning(
                "Route provider HTTP failure provider=%s status=%s duration_ms=%s",
                self.name,
                response.status_code,
                elapsed_ms,
            )
            raise RouteUnavailable(f"Routing provider HTTP {response.status_code}")
        try:
            if payload.get("code") != "Ok":
                raise ValueError("unexpected provider code")
            route = payload["routes"][0]
            distance_km = float(route["distance"]) / 1000
            duration_minutes = float(route["duration"]) / 60
            coordinates = route["geometry"]["coordinates"]
            geometry = tuple((float(point[1]), float(point[0])) for point in coordinates)
            if (
                not math.isfinite(distance_km)
                or not math.isfinite(duration_minutes)
                or distance_km <= 0
                or duration_minutes < 0
                or len(geometry) < 2
            ):
                raise ValueError("invalid route values")
            for point in geometry:
                validate_coordinate(*point)
        except (AttributeError, KeyError, IndexError, TypeError, ValueError) as exc:
            logger.warning(
                "Route provider response malformed provider=%s status=%s duration_ms=%s",
                self.name,
                response.status_code,
                elapsed_ms,
            )
            raise RouteUnavailable("Routing provider response is malformed") from exc
        logger.info(
            "Route provider succeeded provider=%s duration_ms=%s distance_km=%s",
            self.name,
            elapsed_ms,
            round(distance_km),
        )
        return RouteResult(
            distance_km=distance_km,
            duration_minutes=duration_minutes,
            geometry=geometry,
            provider=self.name,
            profile=self.profile,
        )


def approximate_route(
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    profile: str,
) -> RouteResult:
    """Preserve the Phase 10 straight-line estimate as a technical fallback."""
    from .fuel_routes import haversine_km

    start = validate_coordinate(*start)
    end = validate_coordinate(*end)
    return RouteResult(
        distance_km=haversine_km(*start, *end) * ROAD_DISTANCE_FACTOR,
        duration_minutes=None,
        geometry=(start, end),
        provider="approximate",
        profile=profile,
        is_approximate=True,
        warnings=(FALLBACK_WARNING,),
    )


class RouteEngine:
    """Cache successful provider routes and fall back only on technical failures."""

    def __init__(
        self,
        provider: RouteProvider,
        *,
        cache_seconds: int = 1_800,
        cache_max_entries: int = 128,
        clock=time.monotonic,
    ) -> None:
        self.provider = provider
        self.cache_seconds = cache_seconds
        self.cache_max_entries = cache_max_entries
        self.clock = clock
        self._cache: OrderedDict[tuple[object, ...], tuple[float, RouteResult]] = OrderedDict()

    def _key(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> tuple[object, ...]:
        return (
            self.provider.name,
            self.provider.profile,
            *(round(value, 6) for value in (*start, *end)),
        )

    async def route(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> RouteResult:
        start = validate_coordinate(*start)
        end = validate_coordinate(*end)
        key = self._key(start, end)
        now = self.clock()
        cached = self._cache.get(key)
        if cached and now - cached[0] < self.cache_seconds:
            self._cache.move_to_end(key)
            logger.info("Route cache hit provider=%s", self.provider.name)
            return cached[1]
        if cached:
            del self._cache[key]
        logger.info("Route cache miss provider=%s", self.provider.name)
        try:
            result = await self.provider.get_route(start, end)
        except RouteUnavailable as exc:
            logger.warning("Route fallback used provider=%s error=%s", self.provider.name, type(exc).__name__)
            return approximate_route(start, end, profile=self.provider.profile)
        self._cache[key] = (now, result)
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_max_entries:
            self._cache.popitem(last=False)
        return result

    @property
    def cache_size(self) -> int:
        return len(self._cache)
