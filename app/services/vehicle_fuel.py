from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class FuelSegment:
    start: object
    end: object
    distance_km: int
    liters: Decimal
    cost: Decimal
    consumption: Decimal
    cost_per_km: Decimal


def fuel_segments(entries: list[object]) -> list[FuelSegment]:
    ordered = sorted(entries, key=lambda entry: (entry.occurred_on, entry.odometer, entry.id))
    segments, start_index = [], None
    for index, entry in enumerate(ordered):
        if not entry.full_tank:
            continue
        if start_index is not None:
            start = ordered[start_index]
            distance = entry.odometer - start.odometer
            if distance > 0:
                fills = ordered[start_index + 1:index + 1]
                liters = sum((fill.liters for fill in fills), Decimal("0"))
                cost = sum((fill.total_cost for fill in fills), Decimal("0"))
                segments.append(FuelSegment(start, entry, distance, liters, cost, liters * 100 / distance, cost / distance))
        start_index = index
    return segments


def fuel_summary(entries: list[object], segments: list[FuelSegment] | None = None) -> dict[str, Decimal | int | None]:
    liters = sum((entry.liters for entry in entries), Decimal("0"))
    cost = sum((entry.total_cost for entry in entries), Decimal("0"))
    segments = segments if segments is not None else fuel_segments(entries)
    distance = sum((segment.distance_km for segment in segments), 0)
    segment_liters = sum((segment.liters for segment in segments), Decimal("0"))
    segment_cost = sum((segment.cost for segment in segments), Decimal("0"))
    return {"count": len(entries), "liters": liters, "cost": cost, "price": cost / liters if liters else None, "observed_distance": max((entry.odometer for entry in entries), default=0) - min((entry.odometer for entry in entries), default=0) if len(entries) > 1 else None, "segment_distance": distance or None, "consumption": segment_liters * 100 / distance if distance else None, "cost_per_km": segment_cost / distance if distance else None}
