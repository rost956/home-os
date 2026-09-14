from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..config import settings
from ..models import FuelMonitorSettings

POLL_INTERVAL_MIN_SECONDS = 60
POLL_INTERVAL_MAX_SECONDS = 86_400
COMMENTS_INTERVAL_MIN_SECONDS = 300
COMMENTS_INTERVAL_MAX_SECONDS = 604_800
STALE_AFTER_MIN_MINUTES = 1
STALE_AFTER_MAX_MINUTES = 10_080
NEARBY_RADIUS_MIN_KM = 1
NEARBY_RADIUS_MAX_KM = 20


@dataclass(frozen=True)
class FuelRuntimeSettings:
    monitor_enabled: bool
    poll_interval_seconds: int
    comments_poll_interval_seconds: int
    stale_after_minutes: int
    nearby_radius_km: int
    source: str


def env_fuel_settings() -> FuelRuntimeSettings:
    return FuelRuntimeSettings(
        monitor_enabled=settings.fuel_monitor_enabled,
        poll_interval_seconds=settings.fuel_poll_interval_seconds,
        comments_poll_interval_seconds=settings.fuel_comments_poll_interval_seconds,
        stale_after_minutes=settings.fuel_data_stale_after_minutes,
        nearby_radius_km=settings.fuel_nearby_radius_km,
        source="env",
    )


def get_fuel_runtime_settings(db: Session) -> FuelRuntimeSettings:
    saved = db.get(FuelMonitorSettings, 1)
    if saved is None:
        return env_fuel_settings()
    return FuelRuntimeSettings(
        monitor_enabled=saved.monitor_enabled,
        poll_interval_seconds=saved.poll_interval_seconds,
        comments_poll_interval_seconds=saved.comments_poll_interval_seconds,
        stale_after_minutes=saved.stale_after_minutes,
        nearby_radius_km=saved.nearby_radius_km,
        source="database",
    )


def validate_fuel_settings(
    *,
    poll_interval_seconds: int,
    comments_poll_interval_seconds: int,
    stale_after_minutes: int,
    nearby_radius_km: int,
) -> None:
    ranges = (
        (poll_interval_seconds, POLL_INTERVAL_MIN_SECONDS, POLL_INTERVAL_MAX_SECONDS, "Интервал проверки"),
        (
            comments_poll_interval_seconds,
            COMMENTS_INTERVAL_MIN_SECONDS,
            COMMENTS_INTERVAL_MAX_SECONDS,
            "Интервал комментариев",
        ),
        (stale_after_minutes, STALE_AFTER_MIN_MINUTES, STALE_AFTER_MAX_MINUTES, "Порог устаревания"),
        (nearby_radius_km, NEARBY_RADIUS_MIN_KM, NEARBY_RADIUS_MAX_KM, "Радиус поиска"),
    )
    for value, minimum, maximum, label in ranges:
        if not minimum <= value <= maximum:
            raise ValueError(f"{label}: допустимо от {minimum} до {maximum}")


def save_fuel_runtime_settings(
    db: Session,
    *,
    monitor_enabled: bool,
    poll_interval_seconds: int,
    comments_poll_interval_seconds: int,
    stale_after_minutes: int,
    nearby_radius_km: int,
) -> FuelRuntimeSettings:
    validate_fuel_settings(
        poll_interval_seconds=poll_interval_seconds,
        comments_poll_interval_seconds=comments_poll_interval_seconds,
        stale_after_minutes=stale_after_minutes,
        nearby_radius_km=nearby_radius_km,
    )
    saved = db.get(FuelMonitorSettings, 1)
    if saved is None:
        saved = FuelMonitorSettings(id=1)
        db.add(saved)
    saved.monitor_enabled = monitor_enabled
    saved.poll_interval_seconds = poll_interval_seconds
    saved.comments_poll_interval_seconds = comments_poll_interval_seconds
    saved.stale_after_minutes = stale_after_minutes
    saved.nearby_radius_km = nearby_radius_km
    db.commit()
    return get_fuel_runtime_settings(db)
