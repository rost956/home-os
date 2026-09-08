from __future__ import annotations

from app.models import User


def test_gradient_and_palette_are_rendered_globally(client, db, make_user, login):
    user = make_user("gradient-owner")
    login(user.username)
    response = client.post("/settings", data={"appearance": "light", "financial_period_start_day": "1", "color_primary": "#123456", "gradient_enabled": "on", "gradient_start_color": "#123456", "gradient_end_color": "#654321", "gradient_angle": "42"}, follow_redirects=False)
    assert response.status_code == 303
    db.expire_all()
    assert "gradient_enabled" in db.get(User, user.id).ui_palette_json
    shared = client.get("/").text
    assert "--primary:#123456" in shared
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
