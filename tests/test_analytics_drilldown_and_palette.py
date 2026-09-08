from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.models import ExpenseCategory, ExpenseItem, ExpenseList
from app.services.preferences import default_palette, validate_palette
from app.timezone import msk_date_to_utc_naive


def add_category(db, user, name="Еда"):
    expense_list = ExpenseList(owner_id=user.id, title="Дом")
    db.add(expense_list)
    db.flush()
    category = ExpenseCategory(expense_list_id=expense_list.id, name=name)
    db.add(category)
    db.flush()
    return category


def add_item(db, category, title, amount, day, *, analytics=True):
    db.add(
        ExpenseItem(
            category_id=category.id,
            title=title,
            amount=Decimal(amount),
            created_at=msk_date_to_utc_naive(day),
            include_in_analytics=analytics,
        )
    )


def test_category_drilldown_filters_period_totals_sorting_and_back_link(client, db, make_user, login):
    user = make_user("drill-owner")
    category = add_category(db, user)
    add_item(db, category, "Old cheap", "10", date(2026, 8, 25))
    add_item(db, category, "New expensive", "40", date(2026, 9, 24))
    add_item(db, category, "Outside", "99", date(2026, 9, 25))
    add_item(db, category, "Excluded", "88", date(2026, 9, 10), analytics=False)
    db.commit()
    login(user.username)
    url = f"/expenses/categories/{category.id}/analytics?from_date=2026-08-25&to_date=2026-09-24"

    response = client.get(url)
    assert response.status_code == 200
    assert "50 ₽" in response.text and "Операций" in response.text
    assert "Outside" not in response.text and "Excluded" not in response.text
    assert response.text.index("New expensive") < response.text.index("Old cheap")
    assert "from_date=2026-08-25" in response.text and "to_date=2026-09-24" in response.text

    for sort, first, second in (
        ("date_asc", "Old cheap", "New expensive"),
        ("date_desc", "New expensive", "Old cheap"),
        ("amount_asc", "Old cheap", "New expensive"),
        ("amount_desc", "New expensive", "Old cheap"),
        ("unexpected", "New expensive", "Old cheap"),
    ):
        sorted_response = client.get(f"{url}&sort={sort}")
        assert sorted_response.status_code == 200
        assert sorted_response.text.index(first) < sorted_response.text.index(second)


def test_category_drilldown_does_not_disclose_other_users_data_or_empty_results(client, db, make_user, login):
    owner = make_user("drill-private-owner")
    viewer = make_user("drill-viewer")
    category = add_category(db, owner, "Private")
    add_item(db, category, "Secret", "100", date(2026, 9, 1))
    empty = add_category(db, viewer, "Empty")
    db.commit()
    login(viewer.username)

    forbidden = client.get(f"/expenses/categories/{category.id}/analytics?from_date=2026-09-01&to_date=2026-09-30")
    assert forbidden.status_code == 403
    response = client.get(f"/expenses/categories/{empty.id}/analytics?from_date=2026-09-01&to_date=2026-09-30")
    assert response.status_code == 200
    assert "За выбранный период расходов в этой категории нет." in response.text


def test_palette_is_per_user_validated_and_reset(client, db, make_user, login):
    owner = make_user("palette-owner")
    other = make_user("palette-other")
    login(owner.username)
    values = {"appearance": "light", "financial_period_start_day": "1", "color_primary": "#123456", "color_surface": "#fafafa"}
    response = client.post("/settings", data=values, follow_redirects=False)
    assert response.status_code == 303
    db.expire_all()
    assert "#123456" in db.get(type(owner), owner.id).ui_palette_json
    assert db.get(type(other), other.id).ui_palette_json is None
    assert 'style="--primary:#123456' in client.get("/settings").text

    invalid = client.post("/settings", data={**values, "color_primary": "red"})
    assert invalid.status_code == 200
    assert "формате #RRGGBB" in invalid.text
    db.expire_all()
    assert "#123456" in db.get(type(owner), owner.id).ui_palette_json

    reset = client.post("/settings", data={**values, "reset_palette": "1"}, follow_redirects=False)
    assert reset.status_code == 303
    db.expire_all()
    assert db.get(type(owner), owner.id).ui_palette_json is None


def test_default_palettes_pass_contrast_validation():
    for theme in ("light", "dark"):
        palette, error = validate_palette(default_palette(theme), theme=theme)
        assert error is None
        assert palette == default_palette(theme)


def test_primary_button_uses_its_computed_foreground_for_contrast():
    palette, error = validate_palette({"primary": "#2563eb"}, theme="light")
    assert error is None
    assert palette == {"primary": "#2563eb"}


def test_unreadable_text_on_surface_is_rejected():
    _, error = validate_palette({"text": "#fefefe", "bg": "#111827", "surface": "#ffffff"}, theme="light")
    assert error == "Основной текст плохо читается на фоне карточек."
