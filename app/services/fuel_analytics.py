"""Explainable delivery detection and robust fuel forecasting."""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..models import (
    FuelDeliveryEvent,
    FuelForecast,
    FuelObservation,
    FuelStation,
    FuelStationChatMessage,
    FuelStationMark,
)
from ..timezone import UTC, now_utc, to_msk

DETECTOR_VERSION = "appearance-rules-v2"
CLASSIFIER_VERSION = "delivery-rules-v1"
FORECAST_VERSION = "median-v2"
MAX_TRANSITION_GAP = timedelta(minutes=90)
MAX_CORRELATION_LAG = timedelta(hours=6)
MERGE_APPEARANCE_WINDOW = timedelta(minutes=60)
MULTI_FUEL_WINDOW = timedelta(minutes=15)
MIN_FORECAST_EVENTS = 3
MIN_CORRELATION_MATCHES = 3
MIN_DELIVERY_CONFIDENCE = 0.6
DELIVERY_EVENT_TYPES = ("probable_delivery", "confirmed_delivery")
DIRECT_DELIVERY_PATTERNS = (
    r"\bбензовоз\w*\s+(?:уже\s+)?(?:приехал|приехала|слива\w*|слил\w*)",
    r"\bслива(?:ет|ют|ли|лся|ется|ют)\b",
    r"\bслил(?:и|ся)?\b",
    r"\bпривез(?:ли|ла)?\b",
    r"\bзавезли\b",
    r"\bзаправк\w*\s+начал\w*\b",
    r"\bначал\w*\s+(?:заправк\w*|отпуска\w*)\b",
)
WAITING_PATTERNS = (
    r"\bжд[её]м\b",
    r"\bждут\b",
    r"\bскоро\s+(?:будет|привезут)\b",
    r"\bговорят\b.*\bпривезут\b",
    r"\bне\s+(?:приехал|привезли)\b",
    r"\bобещали\b",
    r"\bожидается\b",
    r"\bв\s+пути\b",
    r"\bстоит\s+ли\s+ждать\b",
)


def _median(values: list[float]) -> float:
    return float(statistics.median(values))


def _mad(values: list[float], center: float | None = None) -> float:
    if not values:
        return 0.0
    middle = _median(values) if center is None else center
    return _median([abs(value - middle) for value in values])


def _mentioned_fuels(text: str) -> set[str]:
    return set(re.findall(r"(?<!\d)(92|95|98|100)(?!\d)", text))


def chat_delivery_signal(
    message: FuelStationChatMessage, fuel_type: str, *, allow_station_level: bool = False
) -> float:
    text = (message.body or "").lower().replace("ё", "е")
    mentioned_fuels = _mentioned_fuels(text)
    if mentioned_fuels and fuel_type not in mentioned_fuels:
        return 0.0
    if not mentioned_fuels and not allow_station_level:
        return 0.0
    positive = any(re.search(pattern, text) for pattern in DIRECT_DELIVERY_PATTERNS)
    negative = any(re.search(pattern, text) for pattern in WAITING_PATTERNS)
    if not positive and not negative:
        return 0.0
    if negative:
        return -(0.16 + (0.03 if message.on_site else 0))
    weight = 0.34 if mentioned_fuels else 0.22
    weight += 0.06 if message.on_site else 0
    weight += 0.06 if message.author_reliable else 0
    weight += min(message.author_tier or 0, 3) * 0.015
    return weight


def _nearby_chat_evidence(
    messages: list[FuelStationChatMessage],
    fuel_type: str,
    transition_at: datetime,
    *,
    allow_station_level: bool,
) -> tuple[float, list[str], bool, list[datetime]]:
    score = 0.0
    keys: list[str] = []
    trusted_confirmation = False
    direct_times: list[datetime] = []
    positive_authors: set[str] = set()
    for message in messages:
        point = message.source_created_at
        if abs(point - transition_at) > timedelta(hours=2):
            continue
        signal = chat_delivery_signal(message, fuel_type, allow_station_level=allow_station_level)
        if signal:
            score += signal
            keys.append(message.provider_message_id)
            if signal > 0:
                direct_times.append(point)
                positive_authors.add(message.author_id or f"message:{message.provider_message_id}")
            if signal > 0 and (message.on_site or message.author_reliable):
                trusted_confirmation = True
    if len(positive_authors) > 1:
        score += min(0.1, (len(positive_authors) - 1) * 0.05)
    return max(-0.3, min(0.58, score)), keys, trusted_confirmation, direct_times


def mark_evidence_weight(mark: FuelStationMark, fuel_type: str, transition_at: datetime) -> float:
    point = mark.source_created_at or mark.fetched_at
    age_minutes = abs((point - transition_at).total_seconds()) / 60
    if age_minutes > 60:
        return 0.0
    freshness = max(0.1, 1 - age_minutes / 70)
    raw = mark.raw_data or {}
    trust = 1.0
    trust += 0.35 if raw.get("on_site") else 0
    trust += 0.35 if raw.get("author_reliable") else 0
    trust += min(int(raw.get("author_tier") or 0), 3) * 0.08
    trust += 0.1 if raw.get("acct_ok") else -0.05
    text = (mark.text or "").lower().replace("аи-", "")
    fuels = _mentioned_fuels(text)
    status = str(raw.get("status") or "").lower()
    base = 0.035 * freshness * trust
    if fuel_type in fuels and status not in {"no", "unavailable"}:
        return base
    if status in {"no", "unavailable"} or (fuels and fuel_type not in fuels):
        return -base
    return 0.0


def _nearby_mark_evidence(
    marks: list[FuelStationMark], fuel_type: str, transition_at: datetime
) -> tuple[float, list[str], int, int, bool]:
    score = 0.0
    keys: list[str] = []
    supports = 0
    conflicts = 0
    stale_feed = False
    for mark in marks:
        raw = mark.raw_data or {}
        stale_feed = stale_feed or bool(raw.get("_feed_stale"))
        signal = mark_evidence_weight(mark, fuel_type, transition_at)
        if signal:
            score += signal
            keys.append(mark.source_key)
            if signal > 0:
                supports += 1
            else:
                conflicts += 1
    if supports > 1:
        score += min(0.06, (supports - 1) * 0.02)
    if supports and conflicts:
        score -= 0.05
    if stale_feed:
        score -= 0.05
    return max(-0.2, min(0.18, score)), keys, supports, conflicts, stale_feed


def _availability_run(
    ordered: list[FuelObservation], start_index: int
) -> tuple[float, datetime | None, int, int]:
    after = ordered[start_index]
    last_positive_at = after.observed_at
    disappeared_at = None
    positive_count = 0
    toggles = 0
    previous_group = "positive"
    episode_end = after.observed_at + MERGE_APPEARANCE_WINDOW
    for item in ordered[start_index:]:
        if item.state in {"available", "low"}:
            positive_count += 1
            last_positive_at = item.observed_at
            current_group = "positive"
        elif item.state == "unavailable":
            current_group = "unavailable"
            if disappeared_at is None:
                disappeared_at = item.observed_at
        else:
            continue
        if item.observed_at <= episode_end and current_group != previous_group:
            toggles += 1
        previous_group = current_group
        if disappeared_at is not None and item.observed_at > episode_end:
            break
    duration_end = disappeared_at or last_positive_at
    duration = max(0.0, (duration_end - after.observed_at).total_seconds() / 60)
    return duration, disappeared_at, positive_count, toggles


def _classify_delivery(
    candidate: dict[str, Any],
    same_station_fuels: list[str],
    marks: list[FuelStationMark],
    chat_messages: list[FuelStationChatMessage],
) -> None:
    evidence = candidate["evidence_json"]
    duration = candidate["availability_duration_minutes"] or 0.0
    disappeared_at = candidate["disappeared_at"]
    appearance_confidence = candidate["appearance_confidence"]
    confirmations = int(evidence["confirmations"])
    source_quality = float(evidence["source_confidence"])
    toggles = int(evidence["instability_toggle_count"])
    allow_station_level = len(set(same_station_fuels)) == 1
    chat_score, chat_keys, trusted_chat, direct_times = _nearby_chat_evidence(
        chat_messages,
        candidate["fuel_type"],
        candidate["window_end"],
        allow_station_level=allow_station_level,
    )
    mark_score, mark_keys, mark_supports, mark_conflicts, marks_feed_stale = _nearby_mark_evidence(
        marks, candidate["fuel_type"], candidate["window_end"]
    )

    score = 0.10 + appearance_confidence * 0.15
    positive_reasons = ["Топливо появилось после двух отметок об отсутствии"]
    caveats: list[str] = []
    if duration >= 45:
        score += 0.33
        positive_reasons.append(f"Доступность сохранялась {round(duration)} мин")
    elif duration >= 20:
        score += 0.16
        positive_reasons.append(f"Доступность наблюдалась {round(duration)} мин")
    elif duration >= 10:
        score += 0.03
        caveats.append(f"Топливо было доступно только около {round(duration)} мин")
    elif disappeared_at is not None:
        score -= 0.15
        caveats.append(f"Топливо исчезло примерно через {round(duration)} мин")
    else:
        caveats.append("Длительность наличия пока не подтверждена")
    if disappeared_at is not None and duration < 20:
        score -= 0.12
    extra_fuels = sorted(set(same_station_fuels) - {candidate["fuel_type"]})
    score += min(len(extra_fuels), 2) * 0.15
    if extra_fuels:
        positive_reasons.append("Одновременно появились: " + ", ".join(f"АИ-{fuel}" for fuel in extra_fuels))
    else:
        caveats.append("Другие виды топлива одновременно не появились")
    score += chat_score
    if chat_score > 0:
        positive_reasons.append("В чате есть прямое сообщение о поставке")
    elif chat_score < 0:
        caveats.append("В чате говорят только об ожидании поставки")
    else:
        caveats.append("В чате нет прямого подтверждения поставки")
    score += mark_score
    if mark_supports:
        positive_reasons.append(f"Свежих подтверждающих отметок: {mark_supports}")
    if mark_conflicts:
        caveats.append(f"Отметок, расходящихся с появлением: {mark_conflicts}")
    if marks_feed_stale:
        caveats.append("Лента отметок могла быть устаревшей")
    score += min(confirmations, 20) * 0.003
    score += min(max(source_quality, 0.0), 1.0) * 0.04
    if toggles >= 2:
        score -= min(0.3, toggles * 0.08)
        caveats.append("После появления статус был нестабилен")
    score = round(max(0.0, min(0.99, score)), 3)
    direct_chat = bool(direct_times)
    if direct_chat and ((trusted_chat and score >= 0.65) or score >= 0.72):
        event_type = "confirmed_delivery"
        reason = "Поставка подтверждена прямым сообщением в чате"
    elif score >= MIN_DELIVERY_CONFIDENCE:
        event_type = "probable_delivery"
        reason = "По совокупности признаков это вероятная поставка"
    else:
        event_type = "availability_appearance"
        reason = "Зафиксировано появление топлива; поставка не подтверждена"
    candidate["event_type"] = event_type
    candidate["delivery_confidence"] = score
    candidate["detection_reason"] = reason
    candidate["classifier_version"] = CLASSIFIER_VERSION
    evidence["same_station_fuels_appeared"] = sorted(same_station_fuels)
    evidence["chat_message_ids"] = chat_keys
    evidence["chat_score"] = round(chat_score, 3)
    evidence["trusted_delivery_chat"] = trusted_chat
    evidence["delivery_window_start"] = min(direct_times).isoformat() if direct_times else None
    evidence["delivery_window_end"] = max(direct_times).isoformat() if direct_times else None
    evidence["availability_started_at"] = candidate["window_end"].isoformat()
    evidence["appearance_estimated_at"] = candidate["estimated_at"].isoformat()
    if direct_times:
        first_direct = min(direct_times)
        last_direct = max(direct_times)
        candidate["estimated_at"] = first_direct + (last_direct - first_direct) / 2
    evidence["mark_keys"] = mark_keys
    evidence["mark_score"] = round(mark_score, 3)
    evidence["mark_support_count"] = mark_supports
    evidence["mark_conflict_count"] = mark_conflicts
    evidence["marks_feed_stale"] = marks_feed_stale
    evidence["delivery_score"] = score
    evidence["delivery_evidence"] = positive_reasons
    evidence["delivery_caveats"] = caveats


def detect_delivery_events(
    observations: list[FuelObservation],
    marks: list[FuelStationMark] | None = None,
    chat_messages: list[FuelStationChatMessage] | None = None,
) -> list[dict[str, Any]]:
    """Detect objective appearances, then classify whether each resembles a delivery."""
    grouped: dict[tuple[int, str], list[FuelObservation]] = {}
    for item in observations:
        grouped.setdefault((item.station_id, item.fuel_type), []).append(item)
    results: list[dict[str, Any]] = []
    marks = marks or []
    chat_messages = chat_messages or []
    for items in grouped.values():
        ordered = sorted(items, key=lambda item: (item.observed_at, item.id or 0))
        last_appearance_at: datetime | None = None
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
            estimated_at = before.observed_at + gap / 2
            if last_appearance_at and estimated_at - last_appearance_at <= MERGE_APPEARANCE_WINDOW:
                continue
            last_appearance_at = estimated_at
            quality_values = [value for value in (after.confidence, confirm.confidence) if value is not None]
            source_quality = statistics.mean(quality_values) if quality_values else 0.5
            confirmations = max(after.confirmations or 0, confirm.confirmations or 0)
            source_timestamp_changed = bool(
                before.source_updated_at
                and after.source_updated_at
                and before.source_updated_at < after.source_updated_at <= after.observed_at
            )
            appearance_confidence = 0.85 + min(source_quality, 1) * 0.1 + min(confirmations, 10) / 200
            if source_timestamp_changed:
                appearance_confidence += 0.04
            appearance_confidence -= min(gap.total_seconds() / MAX_TRANSITION_GAP.total_seconds(), 1) * 0.1
            appearance_confidence = round(max(0.0, min(0.99, appearance_confidence)), 3)
            duration, disappeared_at, positive_count, toggles = _availability_run(ordered, index)
            results.append({
                "station_id": after.station_id,
                "fuel_type": after.fuel_type,
                "window_start": before.observed_at,
                "window_end": after.observed_at,
                "estimated_at": estimated_at,
                "event_type": "availability_appearance",
                "confidence": appearance_confidence,
                "appearance_confidence": appearance_confidence,
                "delivery_confidence": 0.0,
                "availability_duration_minutes": round(duration, 1),
                "disappeared_at": disappeared_at,
                "before_observation_id": before.id,
                "after_observation_id": after.id,
                "detection_reason": "Зафиксировано появление топлива",
                "classifier_version": CLASSIFIER_VERSION,
                "evidence_json": {
                    "previous_unavailable_count": 2,
                    "following_available_count": positive_count,
                    "gap_minutes": round(gap.total_seconds() / 60, 1),
                    "source_confidence": round(source_quality, 3),
                    "confirmations": confirmations,
                    "source_timestamp_changed": source_timestamp_changed,
                    "source_before_at": before.source_updated_at.isoformat() if before.source_updated_at else None,
                    "source_after_at": after.source_updated_at.isoformat() if after.source_updated_at else None,
                    "instability_toggle_count": toggles,
                },
            })
    simultaneous_groups = [
        [
            item["fuel_type"]
            for item in results
            if item["station_id"] == candidate["station_id"]
            and abs(item["estimated_at"] - candidate["estimated_at"]) <= MULTI_FUEL_WINDOW
        ]
        for candidate in results
    ]
    for candidate, simultaneous_fuels in zip(results, simultaneous_groups, strict=True):
        _classify_delivery(candidate, simultaneous_fuels, marks, chat_messages)
    return results


def backfill_delivery_events(db: Session, station_id: int | None = None) -> int:
    query = select(FuelObservation)
    if station_id is not None:
        query = query.where(FuelObservation.station_id == station_id)
    observations = db.scalars(query.order_by(FuelObservation.station_id, FuelObservation.fuel_type, FuelObservation.observed_at)).all()
    grouped: dict[int, list[FuelObservation]] = {}
    for item in observations:
        grouped.setdefault(item.station_id, []).append(item)
    changed = 0
    for current_station_id, items in grouped.items():
        marks = db.scalars(select(FuelStationMark).where(FuelStationMark.station_id == current_station_id)).all()
        chat_messages = db.scalars(select(FuelStationChatMessage).where(
            FuelStationChatMessage.station_id == current_station_id
        )).all()
        for candidate in detect_delivery_events(items, marks, chat_messages):
            existing = db.scalar(select(FuelDeliveryEvent).where(
                FuelDeliveryEvent.after_observation_id == candidate["after_observation_id"]
            ))
            if existing is None:
                db.add(FuelDeliveryEvent(**candidate, detector_version=DETECTOR_VERSION))
                changed += 1
                continue
            previous_evidence = existing.evidence_json or {}
            event_changed = existing.detector_version != DETECTOR_VERSION
            for field, value in candidate.items():
                if field == "evidence_json":
                    value = {**previous_evidence, **value}
                if getattr(existing, field) != value:
                    event_changed = True
                setattr(existing, field, value)
            existing.detector_version = DETECTOR_VERSION
            if event_changed:
                changed += 1
    db.flush()
    return changed


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
        FuelDeliveryEvent.station_id == target.id,
        FuelDeliveryEvent.fuel_type == fuel_type,
        FuelDeliveryEvent.event_type.in_(DELIVERY_EVENT_TYPES),
        FuelDeliveryEvent.delivery_confidence >= MIN_DELIVERY_CONFIDENCE,
    )).all())
    results: list[Correlation] = []
    for peer in peers:
        source_events = list(db.scalars(select(FuelDeliveryEvent.estimated_at).where(
            FuelDeliveryEvent.station_id == peer.id,
            FuelDeliveryEvent.fuel_type == fuel_type,
            FuelDeliveryEvent.event_type.in_(DELIVERY_EVENT_TYPES),
            FuelDeliveryEvent.delivery_confidence >= MIN_DELIVERY_CONFIDENCE,
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
        FuelDeliveryEvent.event_type.in_(DELIVERY_EVENT_TYPES),
        FuelDeliveryEvent.delivery_confidence >= MIN_DELIVERY_CONFIDENCE,
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
    confidence = min(
        0.82,
        0.28
        + min(len(events), 10) * 0.045
        + statistics.mean(item.delivery_confidence or 0 for item in events) * 0.2,
    )
    confidence -= min(spread / 720, 0.25)
    reason: dict[str, Any] = {
        "event_count": len(events),
        "appearance_event_count": db.scalar(
            select(func.count(FuelDeliveryEvent.id)).where(
                FuelDeliveryEvent.station_id == station.id,
                FuelDeliveryEvent.fuel_type == fuel_type,
            )
        ) or 0,
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
            FuelDeliveryEvent.event_type.in_(DELIVERY_EVENT_TYPES),
            FuelDeliveryEvent.delivery_confidence >= MIN_DELIVERY_CONFIDENCE,
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
                db.execute(delete(FuelForecast).where(
                    FuelForecast.station_id == station.id,
                    FuelForecast.fuel_type == fuel_type,
                    FuelForecast.model_version == FORECAST_VERSION,
                ))
                continue
            latest = db.scalar(select(FuelForecast).where(
                FuelForecast.station_id == station.id,
                FuelForecast.fuel_type == fuel_type,
                FuelForecast.model_version == FORECAST_VERSION,
            ).order_by(FuelForecast.generated_at.desc()).limit(1))
            signature = (values["expected_at"], values["range_from"], values["range_to"], round(values["confidence"], 3), values["reason_json"])
            if latest and signature == (latest.expected_at, latest.range_from, latest.range_to, round(latest.confidence, 3), latest.reason_json):
                continue
            db.add(FuelForecast(**values))
            created += 1
    db.flush()
    return created


def evaluate_forecasts(db: Session, station_id: int | None = None) -> int:
    query = select(FuelForecast).where(
        FuelForecast.evaluated_at.is_(None), FuelForecast.model_version == FORECAST_VERSION
    )
    if station_id is not None:
        query = query.where(FuelForecast.station_id == station_id)
    updated = 0
    for forecast in db.scalars(query).all():
        actual = db.scalar(select(FuelDeliveryEvent).where(
            FuelDeliveryEvent.station_id == forecast.station_id,
            FuelDeliveryEvent.fuel_type == forecast.fuel_type,
            FuelDeliveryEvent.event_type.in_(DELIVERY_EVENT_TYPES),
            FuelDeliveryEvent.delivery_confidence >= MIN_DELIVERY_CONFIDENCE,
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
