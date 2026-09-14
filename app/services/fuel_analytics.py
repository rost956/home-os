"""Explainable delivery detection and robust fuel forecasting."""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import FuelDeliveryEvent, FuelForecast, FuelObservation, FuelStation, FuelStationComment
from ..timezone import UTC, now_utc, to_msk

DETECTOR_VERSION = "rules-v1"
FORECAST_VERSION = "median-v1"
MAX_TRANSITION_GAP = timedelta(minutes=90)
MAX_CORRELATION_LAG = timedelta(hours=6)
MIN_FORECAST_EVENTS = 3
MIN_CORRELATION_MATCHES = 3
POSITIVE_WORDS = ("бензовоз", "привезли", "привез", "завезли", "сливают", "слили", "появился", "появилась")
NEGATIVE_WORDS = ("ждут", "ждем", "ждём", "не приехал", "не привезли", "обещали", "ожидается")


def _median(values: list[float]) -> float:
    return float(statistics.median(values))


def _mad(values: list[float], center: float | None = None) -> float:
    if not values:
        return 0.0
    middle = _median(values) if center is None else center
    return _median([abs(value - middle) for value in values])


def comment_signal(comment: FuelStationComment, fuel_type: str) -> float:
    text = (comment.text or "").lower().replace("ё", "е")
    mentioned_fuels = set(re.findall(r"(?<!\d)(95|98|100)(?!\d)", text))
    if mentioned_fuels and fuel_type not in mentioned_fuels:
        return 0.0
    positive = any(word.replace("ё", "е") in text for word in POSITIVE_WORDS) or f"есть {fuel_type}" in text
    negative = any(word.replace("ё", "е") in text for word in NEGATIVE_WORDS)
    if not positive and not negative:
        return 0.0
    raw = comment.raw_data or {}
    weight = 0.025 + (0.015 if raw.get("on_site") else 0) + (0.015 if raw.get("author_reliable") else 0)
    weight += min(float(raw.get("author_tier") or 0), 3) * 0.005
    return -weight if negative else weight


def _nearby_comment_evidence(
    comments: list[FuelStationComment], fuel_type: str, transition_at: datetime
) -> tuple[float, list[str]]:
    score = 0.0
    keys: list[str] = []
    for comment in comments:
        point = comment.source_created_at or comment.fetched_at
        if abs(point - transition_at) > timedelta(hours=2):
            continue
        signal = comment_signal(comment, fuel_type)
        if signal:
            score += signal
            keys.append(comment.source_key)
    return max(-0.08, min(0.08, score)), keys


def detect_delivery_events(
    observations: list[FuelObservation], comments: list[FuelStationComment] | None = None
) -> list[dict[str, Any]]:
    """Require two unavailable samples before and two positive samples after a transition."""
    ordered = sorted(observations, key=lambda item: (item.observed_at, item.id or 0))
    results: list[dict[str, Any]] = []
    comments = comments or []
    for index in range(2, len(ordered) - 1):
        before2, before, after, confirm = ordered[index - 2 : index + 2]
        if before.state != "unavailable" or before2.state != "unavailable":
            continue
        if after.state not in {"available", "low"} or confirm.state not in {"available", "low"}:
            continue
        if any(item.is_stale for item in (before2, before, after, confirm)):
            continue
        gap = after.observed_at - before.observed_at
        if gap <= timedelta(0) or gap > MAX_TRANSITION_GAP:
            continue
        source_quality = statistics.mean(value for value in (after.confidence, confirm.confidence) if value is not None) if any(
            value is not None for value in (after.confidence, confirm.confidence)
        ) else 0.5
        confirmations = max(after.confirmations or 0, confirm.confirmations or 0)
        comment_delta, comment_keys = _nearby_comment_evidence(comments, after.fuel_type, after.observed_at)
        source_timestamp_changed = bool(
            before.source_updated_at
            and after.source_updated_at
            and before.source_updated_at < after.source_updated_at <= after.observed_at
        )
        confidence = 0.55 + 0.15 + 0.15 + min(source_quality, 1) * 0.1 + min(confirmations, 10) / 200
        if source_timestamp_changed:
            confidence += 0.04
        confidence -= min(gap.total_seconds() / MAX_TRANSITION_GAP.total_seconds(), 1) * 0.1
        confidence = max(0.0, min(0.99, confidence + comment_delta))
        results.append({
            "station_id": after.station_id,
            "fuel_type": after.fuel_type,
            "window_start": before.observed_at,
            "window_end": after.observed_at,
            "estimated_at": before.observed_at + gap / 2,
            "confidence": confidence,
            "before_observation_id": before.id,
            "after_observation_id": after.id,
            "detection_reason": "Подтверждён переход «нет → есть»",
            "evidence_json": {
                "previous_unavailable_count": 2,
                "following_available_count": 2,
                "gap_minutes": round(gap.total_seconds() / 60, 1),
                "source_confidence": round(source_quality, 3),
                "confirmations": confirmations,
                "source_timestamp_changed": source_timestamp_changed,
                "source_before_at": before.source_updated_at.isoformat() if before.source_updated_at else None,
                "source_after_at": after.source_updated_at.isoformat() if after.source_updated_at else None,
                "comment_keys": comment_keys,
                "comment_score": round(comment_delta, 3),
            },
        })
    return results


def backfill_delivery_events(db: Session, station_id: int | None = None) -> int:
    query = select(FuelObservation)
    if station_id is not None:
        query = query.where(FuelObservation.station_id == station_id)
    observations = db.scalars(query.order_by(FuelObservation.station_id, FuelObservation.fuel_type, FuelObservation.observed_at)).all()
    grouped: dict[tuple[int, str], list[FuelObservation]] = {}
    for item in observations:
        grouped.setdefault((item.station_id, item.fuel_type), []).append(item)
    created = 0
    for (current_station_id, _fuel), items in grouped.items():
        comments = db.scalars(select(FuelStationComment).where(FuelStationComment.station_id == current_station_id)).all()
        for candidate in detect_delivery_events(items, comments):
            if db.scalar(select(FuelDeliveryEvent.id).where(
                FuelDeliveryEvent.after_observation_id == candidate["after_observation_id"]
            )):
                continue
            db.add(FuelDeliveryEvent(**candidate, detector_version=DETECTOR_VERSION))
            created += 1
    db.flush()
    return created


@dataclass(frozen=True)
class Correlation:
    source_station_id: int
    target_station_id: int
    fuel_type: str
    matches: int
    median_lag_minutes: float
    mad_minutes: float
    order_consistency: float


def correlate_event_series(
    source_station_id: int,
    target_station_id: int,
    fuel_type: str,
    source_events: list[datetime],
    target_events: list[datetime],
) -> Correlation | None:
    lags: list[float] = []
    used_targets: set[datetime] = set()
    for source in sorted(source_events):
        following = [
            target
            for target in target_events
            if target not in used_targets and timedelta(0) < target - source <= MAX_CORRELATION_LAG
        ]
        if following:
            matched = min(following)
            used_targets.add(matched)
            lags.append((matched - source).total_seconds() / 60)
    if len(lags) < MIN_CORRELATION_MATCHES:
        return None
    median_lag = _median(lags)
    return Correlation(source_station_id, target_station_id, fuel_type, len(lags), median_lag, _mad(lags, median_lag), 1.0)


def station_correlations(db: Session, target: FuelStation, fuel_type: str) -> list[Correlation]:
    peers = db.scalars(select(FuelStation).where(
        FuelStation.owner_id == target.owner_id, FuelStation.brand == target.brand,
        FuelStation.id != target.id, FuelStation.enabled.is_(True)
    )).all()
    target_events = list(db.scalars(select(FuelDeliveryEvent.estimated_at).where(
        FuelDeliveryEvent.station_id == target.id, FuelDeliveryEvent.fuel_type == fuel_type
    )).all())
    results: list[Correlation] = []
    for peer in peers:
        source_events = list(db.scalars(select(FuelDeliveryEvent.estimated_at).where(
            FuelDeliveryEvent.station_id == peer.id, FuelDeliveryEvent.fuel_type == fuel_type
        )).all())
        relation = correlate_event_series(peer.id, target.id, fuel_type, source_events, target_events)
        if relation:
            results.append(relation)
    return results


def _robust_intervals(events: list[FuelDeliveryEvent]) -> tuple[list[float], float, float]:
    intervals = [(right.estimated_at - left.estimated_at).total_seconds() / 60 for left, right in zip(events, events[1:])]
    median_interval = _median(intervals)
    mad_interval = _mad(intervals, median_interval)
    if mad_interval:
        filtered = [value for value in intervals if abs(value - median_interval) <= 3 * mad_interval]
        if filtered:
            median_interval, mad_interval = _median(filtered), _mad(filtered)
            intervals = filtered
    return intervals, median_interval, mad_interval


def build_forecast(db: Session, station: FuelStation, fuel_type: str, generated_at: datetime | None = None) -> dict[str, Any] | None:
    events = db.scalars(select(FuelDeliveryEvent).where(
        FuelDeliveryEvent.station_id == station.id, FuelDeliveryEvent.fuel_type == fuel_type,
        FuelDeliveryEvent.confidence >= 0.45,
    ).order_by(FuelDeliveryEvent.estimated_at)).all()
    if len(events) < MIN_FORECAST_EVENTS:
        return None
    generated_at = generated_at or now_utc()
    _intervals, median_interval, interval_mad = _robust_intervals(events)
    baseline_expected = events[-1].estimated_at + timedelta(minutes=median_interval)
    local_minutes = [to_msk(item.estimated_at).hour * 60 + to_msk(item.estimated_at).minute for item in events]
    typical_minute = _median(local_minutes)
    time_mad = _mad(local_minutes, typical_minute)
    candidate_local = to_msk(baseline_expected).replace(hour=int(typical_minute // 60) % 24, minute=int(typical_minute % 60), second=0, microsecond=0)
    baseline_expected = candidate_local.astimezone(UTC).replace(tzinfo=None)
    spread = max(45.0, min(360.0, max(interval_mad, time_mad) * 1.5))
    confidence = min(0.82, 0.28 + min(len(events), 10) * 0.045 + statistics.mean(item.confidence for item in events) * 0.2)
    confidence -= min(spread / 720, 0.25)
    reason: dict[str, Any] = {
        "event_count": len(events),
        "median_interval_minutes": round(median_interval),
        "interval_mad_minutes": round(interval_mad),
        "typical_time": f"{int(typical_minute // 60) % 24:02d}:{int(typical_minute % 60):02d}",
        "typical_time_spread_minutes": round(time_mad),
        "baseline_confidence": round(max(0.1, confidence), 3),
    }
    correlations = station_correlations(db, station, fuel_type)
    usable = []
    for relation in correlations:
        recent_source = db.scalar(select(FuelDeliveryEvent).where(
            FuelDeliveryEvent.station_id == relation.source_station_id,
            FuelDeliveryEvent.fuel_type == fuel_type,
            FuelDeliveryEvent.estimated_at >= generated_at - MAX_CORRELATION_LAG,
        ).order_by(FuelDeliveryEvent.estimated_at.desc()).limit(1))
        if recent_source:
            usable.append((relation, recent_source))
    expected_at = baseline_expected
    if usable:
        relation, source_event = max(usable, key=lambda row: row[1].estimated_at)
        cross_expected = source_event.estimated_at + timedelta(minutes=relation.median_lag_minutes)
        weight = min(0.35, 0.15 + (relation.matches - 3) * 0.05)
        expected_at = baseline_expected + (cross_expected - baseline_expected) * weight
        spread = max(30.0, spread * (1 - weight) + max(15.0, relation.mad_minutes * 2) * weight)
        confidence = min(0.9, confidence + weight * 0.25)
        reason["cross_station_signal"] = {
            "source_station_id": relation.source_station_id,
            "event_at": source_event.estimated_at.isoformat(),
            "median_lag_minutes": round(relation.median_lag_minutes),
            "mad_minutes": round(relation.mad_minutes),
            "matches": relation.matches,
            "weight": round(weight, 2),
        }
    return {
        "station_id": station.id, "fuel_type": fuel_type, "generated_at": generated_at,
        "expected_at": expected_at, "range_from": expected_at - timedelta(minutes=spread),
        "range_to": expected_at + timedelta(minutes=spread), "confidence": max(0.1, min(0.9, confidence)),
        "model_version": FORECAST_VERSION, "reason_json": reason,
    }


def refresh_forecasts(db: Session, station_id: int | None = None) -> int:
    query = select(FuelStation).where(FuelStation.enabled.is_(True))
    if station_id is not None:
        query = query.where(FuelStation.id == station_id)
    created = 0
    for station in db.scalars(query).all():
        for fuel_type in ("95", "98", "100"):
            values = build_forecast(db, station, fuel_type)
            if values is None:
                continue
            latest = db.scalar(select(FuelForecast).where(
                FuelForecast.station_id == station.id, FuelForecast.fuel_type == fuel_type
            ).order_by(FuelForecast.generated_at.desc()).limit(1))
            signature = (values["expected_at"], values["range_from"], values["range_to"], round(values["confidence"], 3), values["reason_json"])
            if latest and signature == (latest.expected_at, latest.range_from, latest.range_to, round(latest.confidence, 3), latest.reason_json):
                continue
            db.add(FuelForecast(**values))
            created += 1
    db.flush()
    return created


def evaluate_forecasts(db: Session, station_id: int | None = None) -> int:
    query = select(FuelForecast).where(FuelForecast.evaluated_at.is_(None))
    if station_id is not None:
        query = query.where(FuelForecast.station_id == station_id)
    updated = 0
    for forecast in db.scalars(query).all():
        actual = db.scalar(select(FuelDeliveryEvent).where(
            FuelDeliveryEvent.station_id == forecast.station_id,
            FuelDeliveryEvent.fuel_type == forecast.fuel_type,
            FuelDeliveryEvent.estimated_at > forecast.generated_at,
        ).order_by(FuelDeliveryEvent.estimated_at).limit(1))
        if actual is None:
            continue
        forecast.evaluated_at = now_utc()
        forecast.actual_delivery_event_id = actual.id
        forecast.absolute_error_minutes = abs((actual.estimated_at - forecast.expected_at).total_seconds()) / 60
        forecast.was_within_range = forecast.range_from <= actual.estimated_at <= forecast.range_to
        updated += 1
    return updated


def process_fuel_history(db: Session, station_id: int | None = None) -> tuple[int, int]:
    events = backfill_delivery_events(db, station_id)
    evaluate_forecasts(db, station_id)
    forecasts = refresh_forecasts(db, station_id)
    if events and station_id is not None:
        source = db.get(FuelStation, station_id)
        if source and source.brand:
            peer_ids = db.scalars(select(FuelStation.id).where(
                FuelStation.owner_id == source.owner_id,
                FuelStation.brand == source.brand,
                FuelStation.id != source.id,
                FuelStation.enabled.is_(True),
            )).all()
            for peer_id in peer_ids:
                forecasts += refresh_forecasts(db, peer_id)
    db.commit()
    return events, forecasts
