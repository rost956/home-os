"""Build compact availability intervals from shared fuel source history."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import FuelDeliveryEvent, FuelObservation
from ..timezone import now_utc


def _event_for(
    observed_at: datetime,
    events: list[FuelDeliveryEvent],
) -> FuelDeliveryEvent | None:
    matches = [
        event
        for event in events
        if event.window_end <= observed_at
        and (event.disappeared_at is None or observed_at < event.disappeared_at)
    ]
    return max(matches, key=lambda item: item.window_end, default=None)


def _interval_state(
    observation: FuelObservation,
    events: list[FuelDeliveryEvent],
) -> tuple[str, FuelDeliveryEvent | None]:
    if observation.is_stale or observation.state == "unknown":
        return "unknown", None
    if observation.state == "unavailable":
        return "unavailable", None
    event = _event_for(observation.observed_at, events)
    if event and event.event_type in {
        "confirmed_availability", "probable_delivery", "confirmed_delivery"
    }:
        return "confirmed_available", event
    return "candidate", event


def _append_interval(
    intervals: list[dict[str, Any]],
    start: datetime,
    end: datetime,
    state: str,
    *,
    event: FuelDeliveryEvent | None = None,
    low: bool = False,
) -> None:
    if end <= start:
        return
    evidence = event.evidence_json if event else {}
    duration_minutes = round((end - start).total_seconds() / 60)
    hours, minutes = divmod(duration_minutes, 60)
    duration_label = f"{hours} ч {minutes:02d} мин" if hours else f"{minutes} мин"
    value = {
        "start": start,
        "end": end,
        "state": state,
        "low": low,
        "confidence": event.appearance_confidence if event else None,
        "supporting_marks": int(evidence.get("mark_support_count") or 0),
        "contradicting_marks": int(evidence.get("mark_conflict_count") or 0),
        "trusted_marks": 1 if evidence.get("trusted_on_site_mark") else 0,
        "duration_minutes": duration_minutes,
        "duration_label": duration_label,
        "event_id": event.id if event else None,
    }
    if intervals:
        previous = intervals[-1]
        signature = ("state", "low", "confidence", "supporting_marks", "contradicting_marks", "trusted_marks", "event_id")
        if previous["end"] == start and all(previous[key] == value[key] for key in signature):
            previous["end"] = end
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
    events: list[FuelDeliveryEvent],
    fuel_types: tuple[str, ...] | list[str],
    *,
    current_at: datetime | None = None,
    hours: int = 24,
    stale_after_minutes: int = 120,
) -> dict[str, Any]:
    """Return clipped state intervals; unobserved and stale gaps are explicit unknowns."""
    current_at = current_at or now_utc()
    window_start = current_at - timedelta(hours=hours)
    total_seconds = (current_at - window_start).total_seconds()
    state_labels = {
        "unavailable": "Нет топлива",
        "candidate": "Возможное появление",
        "confirmed_available": "Наличие подтверждено",
        "unknown": "Нет достоверных данных",
    }
    fuels: dict[str, list[dict[str, Any]]] = {}
    for fuel_type in fuel_types:
        points = sorted(
            (
                item for item in observations
                if item.fuel_type == fuel_type and item.observed_at <= current_at
            ),
            key=lambda item: (item.observed_at, item.id or 0),
        )
        fuel_events = [item for item in events if item.fuel_type == fuel_type]
        intervals: list[dict[str, Any]] = []
        relevant = [item for item in points if item.observed_at >= window_start]
        before = [item for item in points if item.observed_at < window_start]
        if before:
            relevant.insert(0, before[-1])
        if not relevant:
            _append_interval(intervals, window_start, current_at, "unknown")
            intervals[0].update({
                "left_percent": 0.0,
                "width_percent": 100.0,
                "label": state_labels["unknown"],
            })
            fuels[fuel_type] = intervals
            continue
        if relevant[0].observed_at > window_start:
            _append_interval(intervals, window_start, relevant[0].observed_at, "unknown")
        for index, observation in enumerate(relevant):
            start = max(window_start, observation.observed_at)
            end = min(
                current_at,
                relevant[index + 1].observed_at if index + 1 < len(relevant) else current_at,
            )
            if end <= window_start or start >= current_at:
                continue
            state, event = _interval_state(observation, fuel_events)
            if state == "unknown":
                _append_interval(intervals, start, end, state)
                continue
            fresh_until = max(
                start,
                min(end, observation.observed_at + timedelta(minutes=stale_after_minutes)),
            )
            _append_interval(
                intervals, start, fresh_until, state, event=event,
                low=observation.state == "low",
            )
            _append_interval(intervals, fresh_until, end, "unknown")
        if intervals and intervals[-1]["end"] < current_at:
            _append_interval(intervals, intervals[-1]["end"], current_at, "unknown")
        for interval in intervals:
            interval["left_percent"] = round(
                (interval["start"] - window_start).total_seconds() / total_seconds * 100, 4
            )
            interval["width_percent"] = round(
                (interval["end"] - interval["start"]).total_seconds() / total_seconds * 100, 4
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
    observations = db.scalars(select(FuelObservation).where(
        FuelObservation.station_id == station_id,
        FuelObservation.fuel_type.in_(fuel_types),
        FuelObservation.observed_at >= load_from,
        FuelObservation.observed_at <= current_at,
    ).order_by(FuelObservation.observed_at)).all()
    events = db.scalars(select(FuelDeliveryEvent).where(
        FuelDeliveryEvent.station_id == station_id,
        FuelDeliveryEvent.fuel_type.in_(fuel_types),
        FuelDeliveryEvent.window_end <= current_at,
        or_(FuelDeliveryEvent.disappeared_at.is_(None), FuelDeliveryEvent.disappeared_at >= load_from),
    ).order_by(FuelDeliveryEvent.window_end)).all()
    return build_fuel_timeline(
        list(observations), list(events), fuel_types,
        current_at=current_at, hours=hours, stale_after_minutes=stale_after_minutes,
    )
