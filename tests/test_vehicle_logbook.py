from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.models import Vehicle, VehicleLogEntry


def payload(**overrides: str) -> dict[str, str]:
    data = {
        "occurred_on": "2026-05-18", "odometer": "120500", "entry_type": "maintenance",
        "title": "Oil change", "description": "Engine oil and filter", "cost": "3500.50",
        "service_location": "Garage 7", "notes": "Use 5W-30",
    }
    data.update(overrides)
    return data


def create_vehicle(db, owner_id: int, model: str = "Focus") -> Vehicle:
    vehicle = Vehicle(owner_id=owner_id, make="Ford", model=model, year=2020, current_odometer=100_000)
    db.add(vehicle)
    db.commit()
    return vehicle


def test_logbook_crud_and_vehicle_overview_summary(client, db, make_user, login):
    owner = make_user("log-owner")
    vehicle = create_vehicle(db, owner.id)
    login(owner.username)

    created = client.post(f"/vehicles/{vehicle.id}/log", data=payload(), follow_redirects=False)
    assert created.status_code == 303
    entry = db.query(VehicleLogEntry).one()
    assert entry.vehicle_id == vehicle.id
    assert entry.cost == Decimal("3500.50")
    assert entry.odometer == 120_500
    assert vehicle.current_odometer == 100_000

    listing = client.get(f"/vehicles/{vehicle.id}/log")
    assert listing.status_code == 200
    assert "Oil change" in listing.text and "3500,5" in listing.text
    detail = client.get(f"/vehicles/{vehicle.id}/log/{entry.id}")
    assert detail.status_code == 200 and "Garage 7" in detail.text
    overview = client.get(f"/vehicles/{vehicle.id}")
    assert "Oil change" in overview.text and "3500,5" in overview.text

    changed = client.post(f"/vehicles/{vehicle.id}/log/{entry.id}/edit", data=payload(title="Brake service", cost="0", odometer=""), follow_redirects=False)
    assert changed.status_code == 303
    db.refresh(entry)
    assert entry.title == "Brake service" and entry.cost == Decimal("0.00") and entry.odometer is None
    entry_id = entry.id
    deleted = client.post(f"/vehicles/{vehicle.id}/log/{entry_id}/delete", follow_redirects=False)
    assert deleted.status_code == 303
    db.expire_all()
    assert db.get(VehicleLogEntry, entry_id) is None


@pytest.mark.parametrize("field,value", [("entry_type", "fuel"), ("odometer", "-1"), ("cost", "-0.01"), ("occurred_on", "bad"), ("title", "")])
def test_logbook_validation_preserves_form(client, db, make_user, login, field, value):
    owner = make_user(f"log-validation-{field}")
    vehicle = create_vehicle(db, owner.id)
    login(owner.username)
    response = client.post(f"/vehicles/{vehicle.id}/log", data=payload(**{field: value}))
    assert response.status_code == 200
    assert db.query(VehicleLogEntry).count() == 0


def test_logbook_filters_sorts_print_and_vehicle_boundaries(client, db, make_user, login):
    owner = make_user("log-filters")
    vehicle = create_vehicle(db, owner.id)
    other_vehicle = create_vehicle(db, owner.id, "Fiesta")
    db.add_all([
        VehicleLogEntry(vehicle_id=vehicle.id, occurred_on=date(2026, 1, 1), entry_type="repair", title="Old repair", odometer=900, cost=Decimal("200")),
        VehicleLogEntry(vehicle_id=vehicle.id, occurred_on=date(2026, 3, 1), entry_type="maintenance", title="New service", odometer=1100, cost=Decimal("50")),
        VehicleLogEntry(vehicle_id=vehicle.id, occurred_on=date(2026, 2, 1), entry_type="note", title="Middle note"),
        VehicleLogEntry(vehicle_id=other_vehicle.id, occurred_on=date(2026, 5, 1), entry_type="repair", title="Other vehicle"),
    ])
    db.commit()
    login(owner.username)

    filtered = client.get(f"/vehicles/{vehicle.id}/log?entry_type=repair&date_from=2026-01-01&date_to=2026-01-31")
    assert "Old repair" in filtered.text and "New service" not in filtered.text and "Other vehicle" not in filtered.text
    sorted_page = client.get(f"/vehicles/{vehicle.id}/log?sort=cost_asc")
    assert sorted_page.text.index("New service") < sorted_page.text.index("Old repair")
    searched = client.get(f"/vehicles/{vehicle.id}/log?q=Middle")
    assert "Middle note" in searched.text and "Old repair" not in searched.text
    printed = client.get(f"/vehicles/{vehicle.id}/log/print?entry_type=maintenance")
    assert printed.status_code == 200 and "New service" in printed.text and "Old repair" not in printed.text


def test_logbook_strict_owner_access_for_all_nested_routes(client, db, make_user, login):
    owner = make_user("log-private-owner")
    outsider = make_user("log-private-outsider")
    vehicle = create_vehicle(db, owner.id)
    entry = VehicleLogEntry(vehicle_id=vehicle.id, occurred_on=date(2026, 1, 1), entry_type="note", title="Private")
    db.add(entry)
    db.commit()
    login(outsider.username)
    urls = [
        ("get", f"/vehicles/{vehicle.id}/log"), ("get", f"/vehicles/{vehicle.id}/log/new"),
        ("post", f"/vehicles/{vehicle.id}/log"), ("get", f"/vehicles/{vehicle.id}/log/{entry.id}"),
        ("get", f"/vehicles/{vehicle.id}/log/{entry.id}/edit"), ("post", f"/vehicles/{vehicle.id}/log/{entry.id}/edit"),
        ("post", f"/vehicles/{vehicle.id}/log/{entry.id}/delete"), ("get", f"/vehicles/{vehicle.id}/log/print"),
    ]
    for method, url in urls:
        response = getattr(client, method)(url, data=payload()) if method == "post" else getattr(client, method)(url)
        assert response.status_code == 404
