from datetime import date
from decimal import Decimal

from app.services.vehicle_fuel import fuel_segments, fuel_summary


class Entry:
    def __init__(self, ident, odo, liters, cost, full):
        self.id, self.odometer, self.liters, self.total_cost, self.full_tank = ident, odo, Decimal(str(liters)), Decimal(str(cost)), full
        self.occurred_on = date(2026, 1, ident)


def test_full_to_full_includes_partials_and_excludes_start():
    entries = [Entry(1, 100000, 40, 2000, True), Entry(2, 100300, 20, 1000, False), Entry(3, 100650, 18, 900, False), Entry(4, 101000, 22, 1100, True)]
    segments = fuel_segments(entries)
    assert len(segments) == 1
    assert segments[0].distance_km == 1000 and segments[0].liters == Decimal("60") and segments[0].consumption == Decimal("6")
    assert fuel_summary(entries, segments)["cost_per_km"] == Decimal("3")


def test_first_full_is_only_baseline_and_invalid_distance_skips():
    assert not fuel_segments([Entry(1, 1, 10, 100, True)])
    assert not fuel_segments([Entry(1, 100, 10, 100, True), Entry(2, 100, 10, 100, True)])
