"""Explainable current Fuel availability derived from recent upstream evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Literal

from ..models import FuelObservation, FuelStationMark

EVIDENCE_DUPLICATE_TOLERANCE = timedelta(minutes=2)
AvailabilityState = Literal["available", "candidate", "unavailable", "unknown"]


@dataclass(frozen=True)
class AvailabilityEvidence:
    key: str
    polarity: Literal["positive", "negative"]
    happened_at: datetime
    source: Literal["snapshot", "mark"]
    queue: bool = False


@dataclass(frozen=True)
class FuelAvailability:
    state: AvailabilityState
    observed_at: datetime | None
    has_queue: bool
    positive_count: int
    negative_count: int
    explanation: tuple[str, ...]


def _mentioned_fuels(value: str) -> set[str]:
    normalized = value.replace("АИ-", "").upper()
    return set(
        re.findall(
            r"(?<!\d)(?:92|95|98|100)(?!\d)|(?<!\w)(?:ДТ|DT)(?!\w)",
            normalized,
        )
    )


def _mark_evidence(mark: FuelStationMark, fuel_type: str) -> AvailabilityEvidence | None:
    raw = mark.raw_data or {}
    status = str(raw.get("status") or "").strip().lower()
    detail = str(raw.get("detail") or raw.get("text") or mark.text or "")
    fuels = _mentioned_fuels(detail)
    happened_at = mark.source_created_at or mark.fetched_at
    if status in {"no", "unavailable"}:
        polarity: Literal["positive", "negative"] = "negative"
    elif fuel_type in fuels and status in {"yes", "queue", "low", "available"}:
        polarity = "positive"
    elif fuels and fuel_type not in fuels and status in {"yes", "queue", "low", "available"}:
        polarity = "negative"
    else:
        return None
    author_id = str(raw.get("author_id") or "").strip()
    identity = f"author:{author_id}:{polarity}" if author_id else f"mark:{mark.source_key}"
    return AvailabilityEvidence(
        key=identity,
        polarity=polarity,
        happened_at=happened_at,
        source="mark",
        queue=status == "queue" and polarity == "positive",
    )


def _observation_evidence(observation: FuelObservation) -> AvailabilityEvidence | None:
    if observation.is_stale or observation.state == "unknown":
        return None
    polarity: Literal["positive", "negative"] = (
        "negative" if observation.state == "unavailable" else "positive"
    )
    happened_at = observation.source_updated_at or observation.observed_at
    if observation.source_updated_at is not None:
        identity = f"snapshot:{observation.source_updated_at.isoformat()}:{polarity}"
    else:
        # Legacy rows without an upstream timestamp remain useful, but repeated
        # HomeOS polls of the same state are one conservative evidence item.
        identity = f"legacy-snapshot:{polarity}"
    status = str(observation.source_status or "").lower()
    return AvailabilityEvidence(
        key=identity,
        polarity=polarity,
        happened_at=happened_at,
        source="snapshot",
        queue=status == "queue" and polarity == "positive",
    )


def _deduplicate(items: Iterable[AvailabilityEvidence]) -> list[AvailabilityEvidence]:
    by_key: dict[str, AvailabilityEvidence] = {}
    for item in items:
        current = by_key.get(item.key)
        if current is None or item.happened_at > current.happened_at:
            by_key[item.key] = item
    result = list(by_key.values())
    marks = [item for item in result if item.source == "mark"]
    # The station aggregate is commonly derived from the same public mark. If
    # both change together, keep the mark and do not manufacture a second vote.
    return sorted(
        (
            item
            for item in result
            if item.source != "snapshot"
            or not any(
                mark.polarity == item.polarity
                and abs(mark.happened_at - item.happened_at) <= EVIDENCE_DUPLICATE_TOLERANCE
                for mark in marks
            )
        ),
        key=lambda item: (item.happened_at, item.key),
    )


def collect_fuel_availability_evidence(
    observations: Iterable[FuelObservation],
    marks: Iterable[FuelStationMark],
    fuel_type: str,
) -> tuple[AvailabilityEvidence, ...]:
    """Return raw evidence used by both current and historical state evaluation."""
    return tuple(
        item
        for item in (
            *(_observation_evidence(observation) for observation in observations),
            *(_mark_evidence(mark, fuel_type) for mark in marks),
        )
        if item is not None
    )


def evaluate_fuel_availability_evidence(
    evidence: Iterable[AvailabilityEvidence],
    *,
    current_at: datetime,
    stale_after_minutes: int,
) -> FuelAvailability:
    """Resolve availability at a cutoff without using evidence from its future."""
    cutoff = current_at - timedelta(minutes=stale_after_minutes)
    evidence = _deduplicate(
        item for item in evidence if cutoff < item.happened_at <= current_at
    )
    if not evidence:
        return FuelAvailability(
            state="unknown",
            observed_at=None,
            has_queue=False,
            positive_count=0,
            negative_count=0,
            explanation=("Нет достаточно свежих данных.",),
        )

    positives = [item for item in evidence if item.polarity == "positive"]
    negatives = [item for item in evidence if item.polarity == "negative"]
    latest = evidence[-1]
    latest_opposite_at = max(
        (item.happened_at for item in evidence if item.polarity != latest.polarity),
        default=None,
    )
    current_run = [
        item
        for item in evidence
        if item.polarity == latest.polarity
        and (latest_opposite_at is None or item.happened_at > latest_opposite_at)
    ]

    if latest.polarity == "positive":
        state: AvailabilityState = "available" if len(current_run) >= 2 else "candidate"
        explanation = [f"Свежих независимых подтверждений: {len(current_run)}."]
        if negatives:
            explanation.append(
                "Более старые отрицательные отметки вытеснены новыми данными."
            )
    else:
        # One fresh negative is decisive when there is no competing positive.
        # After confirmed availability, one contradiction makes the state
        # uncertain; two independent fresh negatives establish unavailability.
        state = "unavailable" if not positives or len(current_run) >= 2 else "candidate"
        explanation = [
            f"Свежих независимых отрицательных свидетельств: {len(current_run)}."
        ]
        if positives:
            explanation.append(
                "Есть более старые положительные данные; учтено противоречие."
            )

    has_queue = state != "unavailable" and any(item.queue for item in current_run)
    return FuelAvailability(
        state=state,
        observed_at=latest.happened_at,
        has_queue=has_queue,
        positive_count=len(positives),
        negative_count=len(negatives),
        explanation=tuple(explanation),
    )


def evaluate_fuel_availability(
    observations: Iterable[FuelObservation],
    marks: Iterable[FuelStationMark],
    fuel_type: str,
    *,
    current_at: datetime,
    stale_after_minutes: int,
) -> FuelAvailability:
    """Resolve current state from independent recent evidence, newest first."""
    return evaluate_fuel_availability_evidence(
        collect_fuel_availability_evidence(observations, marks, fuel_type),
        current_at=current_at,
        stale_after_minutes=stale_after_minutes,
    )
