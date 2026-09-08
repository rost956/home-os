# ruff: noqa: E701, E702
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.models import Vehicle, VehicleLogEntry, VehicleMaintenanceItem
from app.services.vehicle_maintenance import add_months, calculate_maintenance


@pytest.mark.parametrize(
    ("current", "today", "last_km", "last_date", "km", "months", "status"),
    [
        (10500, date(2026, 1, 1), 10000, None, 10000, None, "ok"),
        (19500, date(2026, 1, 1), 10000, None, 10000, None, "soon"),
        (20000, date(2026, 1, 1), 10000, None, 10000, None, "overdue"),
        (0, date(2026, 1, 1), None, date(2025, 1, 15), None, 24, "ok"),
        (0, date(2026, 12, 20), None, date(2025, 1, 15), None, 24, "soon"),
        (0, date(2027, 1, 15), None, date(2025, 1, 15), None, 24, "overdue"),
        (20000, date(2026, 1, 1), 10000, date(2025, 6, 1), 10000, 12, "overdue"),
        (10000, date(2026, 6, 1), 10000, date(2025, 6, 1), 10000, 12, "overdue"),
    ],
)
def test_calculation_statuses(current, today, last_km, last_date, km, months, status):
    assert calculate_maintenance(current_odometer=current, today=today, last_odometer=last_km, last_date=last_date, interval_km=km, interval_months=months).status == status


def test_calendar_month_end_and_leap_year():
    assert add_months(date(2025, 1, 31), 1) == date(2025, 2, 28)
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)


def vehicle(db, user_id):
    result = Vehicle(owner_id=user_id, make="Ford", model="Focus", year=2020, current_odometer=100000)
    db.add(result); db.commit(); return result


def form(**extra):
    result = {"name": "Engine oil", "category": "engine", "last_service_date": "2026-01-01", "last_service_odometer": "95000", "interval_km": "10000", "interval_months": "12", "notes": ""}
    result.update(extra); return result


def test_maintenance_crud_service_and_logbook(client, db, make_user, login):
    user = make_user("maintenance-user"); car = vehicle(db, user.id); login(user.username)
    assert client.post(f"/vehicles/{car.id}/maintenance", data=form(), follow_redirects=False).status_code == 303
    item = db.query(VehicleMaintenanceItem).one()
    page = client.get(f"/vehicles/{car.id}/maintenance")
    assert "Engine oil" in page.text and "maintenance-card" in page.text
    service = client.post(f"/vehicles/{car.id}/maintenance/{item.id}/service", data={"occurred_on": "2026-05-01", "odometer": "110000", "cost": "2500.50", "service_location": "Shop", "notes": "done", "add_to_log": "on"}, follow_redirects=False)
    assert service.status_code == 303
    db.refresh(item); db.refresh(car)
    assert item.last_service_odometer == 110000 and car.current_odometer == 110000
    log = db.query(VehicleLogEntry).one()
    assert log.title == "Engine oil" and log.cost == Decimal("2500.50") and log.service_location == "Shop"
    assert client.post(f"/vehicles/{car.id}/maintenance/{item.id}/delete", follow_redirects=False).status_code == 303


@pytest.mark.parametrize("payload", [form(interval_km="", interval_months=""), form(interval_km="0"), form(category="bad"), form(last_service_odometer="", interval_km="10"), form(last_service_date="", interval_months="12")])
def test_maintenance_validation(client, db, make_user, login, payload):
    user = make_user(f"maintenance-validation-{len(payload)}-{payload.get('category')}"); car = vehicle(db, user.id); login(user.username)
    assert client.post(f"/vehicles/{car.id}/maintenance", data=payload).status_code == 200
    assert db.query(VehicleMaintenanceItem).count() == 0


def test_maintenance_ownership_and_vehicle_isolation(client, db, make_user, login):
    owner = make_user("maintenance-owner"); other = make_user("maintenance-other"); car = vehicle(db, owner.id); second = vehicle(db, owner.id)
    item = VehicleMaintenanceItem(vehicle_id=car.id, name="Private", category="engine", last_service_odometer=1, interval_km=1)
    db.add(item); db.commit(); login(other.username)
    for method, url in [("get", f"/vehicles/{car.id}/maintenance"), ("post", f"/vehicles/{car.id}/maintenance/{item.id}/delete"), ("post", f"/vehicles/{car.id}/maintenance/{item.id}/service")]:
        response = getattr(client, method)(url, data={} if method == "post" else None) if method == "post" else getattr(client, method)(url)
        assert response.status_code == 404
    login(owner.username)
    assert "Private" not in client.get(f"/vehicles/{second.id}/maintenance").text
