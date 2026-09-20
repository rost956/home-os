"""Build compact availability intervals from shared fuel source history."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ..models import FuelObservation, FuelStationMark
from ..timezone import now_utc
from .fuel_availability import (
    FuelAvailability,
    collect_fuel_availability_evidence,
    evaluate_fuel_availability_evidence,
)


def _timeline_state(availability: FuelAvailability) -> str:
    if availability.state == "available":
        return "confirmed_available"
    return availability.state


def _append_interval(
    intervals: list[dict[str, Any]],
    start: datetime,
    end: datetime,
    availability: FuelAvailability,
) -> None:
    if end <= start:
        return
    state = _timeline_state(availability)
    duration_minutes = round((end - start).total_seconds() / 60)
    hours, minutes = divmod(duration_minutes, 60)
    duration_label = f"{hours} ч {minutes:02d} мин" if hours else f"{minutes} мин"
    value = {
        "start": start,
        "end": end,
        "state": state,
        "low": False,
        "confidence": None,
        "supporting_marks": availability.positive_count,
        "contradicting_marks": availability.negative_count,
        "trusted_marks": 0,
        "duration_minutes": duration_minutes,
        "duration_label": duration_label,
        "event_id": None,
    }
    if intervals and intervals[-1]["end"] == start and intervals[-1]["state"] == state:
        previous = intervals[-1]
        previous["end"] = end
        previous["supporting_marks"] = availability.positive_count
        previous["contradicting_marks"] = availability.negative_count
        previous["duration_minutes"] = round(
            (end - previous["start"]).total_seconds() / 60
        )
        hours, minutes = divmod(previous["duration_minutes"], 60)
        previous["duration_label"] = (
            f"{hours} ч {minutes:02d} мин" if hours else f"{minutes} мин"
        )
        return
    intervals.append(value)


def build_fuel_timeline(
    observations: list[FuelObservation],
    marks: list[FuelStationMark],
    fuel_types: tuple[str, ...] | list[str],
    *,
    current_at: datetime | None = None,
    hours: int = 24,
    stale_after_minutes: int = 120,
) -> dict[str, Any]:
    """Reconstruct states at each evidence cutoff using the current-state engine."""
    current_at = current_at or now_utc()
    window_start = current_at - timedelta(hours=hours)
    stale_after = timedelta(minutes=stale_after_minutes)
    total_seconds = (current_at - window_start).total_seconds()
    state_labels = {
        "unavailable": "Нет топлива",
        "candidate": "Возможное появление",
        "confirmed_available": "Наличие подтверждено",
        "unknown": "Нет достоверных данных",
    }
    fuels: dict[str, list[dict[str, Any]]] = {}
    for fuel_type in fuel_types:
        fuel_observations = [
            item for item in observations if item.fuel_type == fuel_type
        ]
        evidence = collect_fuel_availability_evidence(
            fuel_observations, marks, fuel_type
        )
        boundaries = {window_start, current_at}
        for item in evidence:
            if window_start < item.happened_at < current_at:
                boundaries.add(item.happened_at)
            expires_at = item.happened_at + stale_after
            if window_start < expires_at < current_at:
                boundaries.add(expires_at)

        intervals: list[dict[str, Any]] = []
        ordered_boundaries = sorted(boundaries)
        for start, end in zip(ordered_boundaries, ordered_boundaries[1:]):
            availability = evaluate_fuel_availability_evidence(
                evidence,
                current_at=start,
                stale_after_minutes=stale_after_minutes,
            )
            _append_interval(intervals, start, end, availability)

        for interval in intervals:
            interval["left_percent"] = round(
                (interval["start"] - window_start).total_seconds()
                / total_seconds
                * 100,
                4,
            )
            interval["width_percent"] = round(
                (interval["end"] - interval["start"]).total_seconds()
                / total_seconds
                * 100,
                4,
            )
            interval["label"] = state_labels[interval["state"]]
        fuels[fuel_type] = intervals
    return {"from": window_start, "to": current_at, "hours": hours, "fuels": fuels}


def load_fuel_timeline(
    db: Session,
    station_id: int,
    fuel_types: tuple[str, ...],
    *,
    current_at: datetime | None = None,
    hours: int = 24,
    stale_after_minutes: int = 120,
) -> dict[str, Any]:
    current_at = current_at or now_utc()
    load_from = current_at - timedelta(hours=hours, minutes=stale_after_minutes)
    observations = db.scalars(
        select(FuelObservation)
        .where(
            FuelObservation.station_id == station_id,
            FuelObservation.fuel_type.in_(fuel_types),
            FuelObservation.observed_at >= load_from,
            FuelObservation.observed_at <= current_at,
        )
        .order_by(FuelObservation.observed_at)
    ).all()
    marks = db.scalars(
        select(FuelStationMark)
        .where(
            FuelStationMark.station_id == station_id,
            or_(
                and_(
                    FuelStationMark.source_created_at.is_not(None),
                    FuelStationMark.source_created_at >= load_from,
                    FuelStationMark.source_created_at <= current_at,
                ),
                and_(
                    FuelStationMark.source_created_at.is_(None),
                    FuelStationMark.fetched_at >= load_from,
                    FuelStationMark.fetched_at <= current_at,
                ),
            ),
        )
        .order_by(FuelStationMark.source_created_at, FuelStationMark.fetched_at)
    ).all()
    return build_fuel_timeline(
        list(observations),
        list(marks),
        fuel_types,
        current_at=current_at,
        hours=hours,
        stale_after_minutes=stale_after_minutes,
    )
