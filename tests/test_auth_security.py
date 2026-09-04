from __future__ import annotations

import pytest

from app.config import load_settings
from app.main import same_origin
from app.models import User


def test_health_and_private_route_redirect(client):
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert health.headers["x-content-type-options"] == "nosniff"

    response = client.get("/expenses", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_registration_and_login(client, db):
    response = client.post(
        "/register",
        data={"username": "new.user", "password": "long-password", "password_confirm": "long-password"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert db.query(User).filter(User.username == "new.user").count() == 1

    client.post("/logout")
    denied = client.post(
        "/login",
        data={"username": "new.user", "password": "wrong-password"},
    )
    assert denied.status_code == 200
    assert "Неверный ник или пароль" in denied.text

    accepted = client.post(
        "/login",
        data={"username": "new.user", "password": "long-password"},
        follow_redirects=False,
    )
    assert accepted.status_code == 303


def test_login_rejects_oversized_password_without_hashing(client, make_user):
    make_user("alice")
    response = client.post("/login", data={"username": "alice", "password": "x" * 257})
    assert response.status_code == 200
    assert "Неверный ник или пароль" in response.text


def test_production_rejects_default_secret(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "change-me-in-production")
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        load_settings()


def test_same_origin_requires_exact_host():
    assert same_origin("https://home.example.test/path", "home.example.test")
    assert not same_origin("https://evil.example.test", "home.example.test")
    assert not same_origin("javascript:alert(1)", "home.example.test")
