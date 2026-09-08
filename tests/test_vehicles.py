from __future__ import annotations

from app.models import Vehicle


def vehicle_data(**overrides: str) -> dict[str, str]:
    values = {
        "display_name": "Фокус",
        "make": "Ford",
        "model": "Focus",
        "year": "2018",
        "license_plate": "А123ВС 77",
        "vin": "wf0abc12345",
        "current_odometer": "104300",
        "notes": "Зимняя резина в гараже",
    }
    values.update(overrides)
    return values


def test_vehicle_empty_state_and_create_detail_with_normalized_vin(client, db, make_user, login):
    user = make_user("vehicle-owner")
    login(user.username)
    assert "Автомобилей пока нет" in client.get("/vehicles").text

    response = client.post("/vehicles", data=vehicle_data(), follow_redirects=False)
    assert response.status_code == 303
    vehicle = db.query(Vehicle).one()
    assert vehicle.owner_id == user.id
    assert vehicle.vin == "WF0ABC12345"
    assert vehicle.current_odometer == 104300

    detail = client.get(f"/vehicles/{vehicle.id}")
    assert detail.status_code == 200
    assert "104 300 км" in detail.text
    assert "WF0ABC12345" in detail.text
    assert 'href="/vehicles"' in detail.text


def test_vehicle_validation_preserves_form_values(client, db, make_user, login):
    user = make_user("vehicle-validation")
    login(user.username)
    cases = (
        (vehicle_data(make=""), "марку и модель"),
        (vehicle_data(year="1700"), "Год выпуска"),
        (vehicle_data(current_odometer="-1"), "Пробег"),
        (vehicle_data(vin="abc"), "VIN"),
    )
    for payload, message in cases:
        response = client.post("/vehicles", data=payload)
        assert response.status_code == 200
        assert message in response.text
    assert db.query(Vehicle).count() == 0


def test_vehicle_edit_delete_and_optional_fields(client, db, make_user, login):
    user = make_user("vehicle-crud")
    vehicle = Vehicle(owner_id=user.id, make="Lada", model="Vesta", year=2020, current_odometer=0)
    db.add(vehicle)
    db.commit()
    login(user.username)
    updated = vehicle_data(display_name="", make="Lada", model="Vesta SW", year="2021", license_plate="", vin="", current_odometer="250", notes="")
    response = client.post(f"/vehicles/{vehicle.id}/edit", data=updated, follow_redirects=False)
    assert response.status_code == 303
    db.refresh(vehicle)
    assert (vehicle.display_name, vehicle.model, vehicle.license_plate, vehicle.vin, vehicle.notes) == (None, "Vesta SW", None, None, None)
    assert vehicle.current_odometer == 250
    vehicle_id = vehicle.id
    deleted = client.post(f"/vehicles/{vehicle_id}/delete", follow_redirects=False)
    assert deleted.status_code == 303
    db.expire_all()
    assert db.get(Vehicle, vehicle_id) is None


def test_multiple_vehicles_are_listed_and_other_users_cannot_access_or_mutate(client, db, make_user, login):
    owner = make_user("vehicle-multi-owner")
    other = make_user("vehicle-other")
    first = Vehicle(owner_id=owner.id, make="Ford", model="Focus", year=2018, current_odometer=10)
    second = Vehicle(owner_id=owner.id, display_name="Вторая", make="Kia", model="Rio", year=2022, current_odometer=20)
    foreign = Vehicle(owner_id=other.id, make="VW", model="Polo", year=2019, current_odometer=30)
    db.add_all([first, second, foreign])
    db.commit()
    login(owner.username)
    listing = client.get("/vehicles")
    assert listing.status_code == 200
    assert "Focus" in listing.text and "Вторая" in listing.text and "Polo" not in listing.text
    assert client.get(f"/vehicles/{foreign.id}").status_code == 404
    assert client.get(f"/vehicles/{foreign.id}/edit").status_code == 404
    assert client.post(f"/vehicles/{foreign.id}/edit", data=vehicle_data()).status_code == 404
    assert client.post(f"/vehicles/{foreign.id}/delete").status_code == 404
    assert 'href="/vehicles"' in listing.text
