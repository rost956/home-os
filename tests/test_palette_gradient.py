from __future__ import annotations

import json
from decimal import Decimal

from app.models import ExpenseLimit, User


def test_gradient_and_palette_are_rendered_globally(client, db, make_user, login):
    user = make_user("gradient-owner")
    login(user.username)
    response = client.post("/settings", data={"appearance": "light", "financial_period_start_day": "1", "color_primary": "#123456", "gradient_enabled": "on", "gradient_start_color": "#123456", "gradient_end_color": "#654321", "gradient_angle": "42"}, follow_redirects=False)
    assert response.status_code == 303
    db.expire_all()
    assert "gradient_enabled" in db.get(User, user.id).ui_palette_json
    shared = client.get("/").text
    assert 'style="--primary:#123456' in shared
    assert "linear-gradient(42deg,#123456,#654321)" in shared


def test_gradient_validation_and_reset(client, db, make_user, login):
    user = make_user("gradient-validation")
    login(user.username)
    bad = client.post("/settings", data={"appearance": "light", "financial_period_start_day": "1", "gradient_enabled": "on", "gradient_start_color": "#000000", "gradient_end_color": "#ffffff", "gradient_angle": "361"})
    assert bad.status_code == 200
    saved = client.post("/settings", data={"appearance": "light", "financial_period_start_day": "1", "gradient_enabled": "on", "gradient_start_color": "#123456", "gradient_end_color": "#654321", "gradient_angle": "90"}, follow_redirects=False)
    assert saved.status_code == 303
    reset = client.post("/settings", data={"appearance": "light", "financial_period_start_day": "1", "reset_palette": "1"}, follow_redirects=False)
    assert reset.status_code == 303
    db.expire_all()
    assert db.get(User, user.id).ui_palette_json is None


def test_palette_survives_post_redirect_reload_and_is_effective_globally(client, db, make_user, login):
    user = make_user("palette-reload")
    login(user.username)
    for theme in ("light", "dark", "system"):
        saved = client.post(
            "/settings",
            data={"appearance": theme, "financial_period_start_day": "1", "color_primary": "#3ecfb9"},
            follow_redirects=False,
        )
        assert saved.status_code == 303

        db.expire_all()
        stored = json.loads(db.get(User, user.id).ui_palette_json)
        assert stored["primary"] == "#3ecfb9"

        for path in ("/settings", "/", "/vehicles"):
            page = client.get(path)
            assert page.status_code == 200
            assert 'style="--primary:#3ecfb9' in page.text
            assert '<style>html { --primary:#3ecfb9' not in page.text

        logged_out = client.post("/logout", follow_redirects=False)
        assert logged_out.status_code == 303
        logged_in = login(user.username)
        assert logged_in.status_code == 303
        after_login = client.get("/vehicles")
        assert after_login.status_code == 200
        assert 'style="--primary:#3ecfb9' in after_login.text


def test_palette_override_uses_element_inline_style_to_beat_theme_selectors(client, make_user, login):
    user = make_user("palette-cascade")
    login(user.username)
    response = client.post(
        "/settings",
        data={"appearance": "system", "financial_period_start_day": "1", "color_primary": "#3ecfb9"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    html = client.get("/settings").text
    opening_tag = html.split("<html", 1)[1].split(">", 1)[0]
    assert 'data-theme="system"' in opening_tag
    assert 'style="--primary:#3ecfb9;--primary-foreground:#111827"' in opening_tag

    stylesheet = client.get("/static/style.css?v=59").text
    assert ":root {" in stylesheet
    assert "--primary: #2563eb;" in stylesheet
    assert 'html[data-theme="dark"] {' in stylesheet


def test_palette_form_has_one_submitted_field_per_color_and_syncs_picker_commit(client, make_user, login):
    user = make_user("palette-fields")
    login(user.username)

    html = client.get("/settings").text
    for key in ("primary", "secondary", "bg", "surface", "text", "muted", "border", "success", "warning", "danger"):
        assert html.count(f'name="color_{key}"') == 1
        assert html.count(f'data-color-picker="color_{key}"') == 1
        assert html.count(f'data-color-hex="color_{key}"') == 1
    assert "picker.addEventListener('input', syncPicker);" in html
    assert "picker.addEventListener('change', syncPicker);" in html


def test_limit_progress_uses_gradient_accent_and_falls_back_to_primary(client, db, make_user, login):
    user = make_user("limit-gradient")
    db.add(ExpenseLimit(owner_id=user.id, category_name="Food", monthly_limit=Decimal("1000")))
    db.commit()
    login(user.username)

    enabled = client.post(
        "/settings",
        data={
            "appearance": "light",
            "financial_period_start_day": "1",
            "gradient_enabled": "on",
            "gradient_start_color": "#123456",
            "gradient_end_color": "#654321",
            "gradient_angle": "42",
        },
        follow_redirects=False,
    )
    assert enabled.status_code == 303
    gradient_page = client.get("/expenses/analytics").text
    assert 'class="bar-track limit-track total-limit-track"' in gradient_page
    assert 'style="--accent-background:linear-gradient(42deg,#123456,#654321)' in gradient_page

    disabled = client.post(
        "/settings",
        data={"appearance": "light", "financial_period_start_day": "1"},
        follow_redirects=False,
    )
    assert disabled.status_code == 303
    disabled_tag = client.get("/expenses/analytics").text.split("<html", 1)[1].split(">", 1)[0]
    assert "--accent-background" not in disabled_tag

    stylesheet = client.get("/static/style.css?v=59").text
    assert ".limit-track div { background: var(--accent-background, var(--primary)); }" in stylesheet
    assert 'html[data-theme="dark"] .limit-track div { background: var(--accent-background, var(--primary)); }' in stylesheet
    assert ".total-limit-track div" not in stylesheet
    assert "linear-gradient(90deg, #16a34a, #f97316, #dc2626)" not in stylesheet
