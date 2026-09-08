from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

from starlette.requests import Request

from app.config import load_settings
from app.web import templates


def test_home_ai_is_frozen_by_default(monkeypatch):
    monkeypatch.delenv("HOME_AI_ENABLED", raising=False)

    assert load_settings().home_ai_enabled is False


def test_frozen_ai_is_not_registered_and_needs_no_runtime():
    environment = os.environ.copy()
    environment.update(
        {
            "APP_ENV": "test",
            "SECRET_KEY": "test-secret-key-with-at-least-thirty-two-characters",
            "HOME_AI_ENABLED": "false",
            "AI_ENABLED": "false",
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.main import app; print(any(getattr(route, 'path', '').startswith('/ai') for route in app.routes))",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.stdout.strip() == "False"


def test_navigation_groups_existing_pages_and_hides_frozen_ai(client, make_user, login):
    user = make_user("navigation-user")
    login(user.username)

    response = client.get("/today")

    assert response.status_code == 200
    for label in ("Главное", "Еда", "Финансы", "Дом", "Сервис"):
        assert label in response.text
    assert 'href="/ai/settings"' in response.text

    frozen_nav = templates.get_template("base.html").render(
        request=Request({"type": "http", "headers": [], "query_string": b""}),
        user=SimpleNamespace(username="navigation-user", theme="light"),
        home_ai_enabled=False,
    )
    assert 'href="/ai/settings"' not in frozen_nav
