from __future__ import annotations

from datetime import date, timedelta

from app.services.finance import financial_period_bounds


def test_settings_page_persists_server_side_preferences(client, db, make_user, login):
    user = make_user("settings-owner")
    other = make_user("settings-other")
    login(user.username)

    response = client.post(
        "/settings",
        data={"appearance": "dark", "financial_period_start_day": "25"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    db.expire_all()
    assert db.get(type(user), user.id).theme == "dark"
    assert db.get(type(user), user.id).expense_period_start_day == 25
    assert db.get(type(user), other.id).theme == "system"
    assert db.get(type(user), other.id).expense_period_start_day == 1
    assert 'data-theme="dark"' in client.get("/settings").text


def test_settings_validation_does_not_overwrite_existing_values(client, db, make_user, login):
    user = make_user("settings-validation")
    user.theme = "light"
    user.expense_period_start_day = 12
    db.commit()
    login(user.username)

    response = client.post("/settings", data={"appearance": "system", "financial_period_start_day": "32"})

    assert response.status_code == 200
    assert "от 1 до 31" in response.text
    db.expire_all()
    saved = db.get(type(user), user.id)
    assert (saved.theme, saved.expense_period_start_day) == ("light", 12)


def test_financial_period_bounds_cover_clamped_months_and_year_boundary():
    assert financial_period_bounds(date(2026, 4, 10), 1) == (date(2026, 4, 1), date(2026, 4, 30))
    assert financial_period_bounds(date(2026, 9, 24), 25) == (date(2026, 8, 25), date(2026, 9, 24))
    assert financial_period_bounds(date(2026, 9, 25), 25) == (date(2026, 9, 25), date(2026, 10, 24))
    assert financial_period_bounds(date(2026, 1, 31), 31) == (date(2026, 1, 31), date(2026, 2, 27))
    assert financial_period_bounds(date(2026, 2, 28), 31) == (date(2026, 2, 28), date(2026, 3, 30))
    assert financial_period_bounds(date(2024, 2, 29), 31) == (date(2024, 2, 29), date(2024, 3, 30))
    assert financial_period_bounds(date(2026, 4, 30), 31) == (date(2026, 4, 30), date(2026, 5, 30))
    assert financial_period_bounds(date(2026, 1, 1), 25) == (date(2025, 12, 25), date(2026, 1, 24))


def test_consecutive_financial_periods_are_contiguous():
    first_start, first_end = financial_period_bounds(date(2026, 2, 28), 31)
    second_start, second_end = financial_period_bounds(first_end + timedelta(days=1), 31)

    assert second_start == first_end + timedelta(days=1)
    assert second_end >= second_start
