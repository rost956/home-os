from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.ai.client import FakeAIClient, LlamaCppClient
from app.ai.config import AISettings, get_ai_settings, load_ai_settings
from app.ai.dependencies import get_ai_client
from app.ai.errors import AIResponseValidationError, AITimeoutError, AIUnavailableError
from app.ai.schemas import AIAvailability, AICompletionRequest, AIJsonMessage
from app.main import app


def enabled_settings(**overrides: object) -> AISettings:
    values: dict[str, object] = {
        "enabled": True,
        "base_url": "http://llama.local:8081/v1",
        "model": "qwen-small",
        "api_key": None,
        "connect_timeout_seconds": 1,
        "read_timeout_seconds": 5,
        "max_tokens": 128,
        "context_budget": 2000,
        "max_concurrency": 1,
        "temperature": 0.2,
        "enable_thinking": False,
    }
    values.update(overrides)
    return AISettings(**values)


def request() -> AICompletionRequest:
    return AICompletionRequest(messages=[{"role": "user", "content": "Привет"}], max_tokens=64)


def test_ai_is_disabled_by_default(monkeypatch):
    for name in ("AI_ENABLED", "AI_BASE_URL", "AI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    settings = load_ai_settings()
    assert settings.enabled is False
    assert settings.base_url is None
    assert settings.model is None


def test_enabled_ai_requires_url_and_model(monkeypatch):
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.delenv("AI_BASE_URL", raising=False)
    monkeypatch.delenv("AI_MODEL", raising=False)
    with pytest.raises(RuntimeError, match="AI_BASE_URL"):
        load_ai_settings()

    monkeypatch.setenv("AI_BASE_URL", "http://127.0.0.1:8081/v1")
    with pytest.raises(RuntimeError, match="AI_MODEL"):
        load_ai_settings()


def test_llama_client_parses_completion_and_structured_json():
    def handler(http_request: httpx.Request) -> httpx.Response:
        assert http_request.url == httpx.URL("http://llama.local:8081/v1/chat/completions")
        assert http_request.read()
        assert json.loads(http_request.content)["chat_template_kwargs"] == {"enable_thinking": False}
        return httpx.Response(
            200,
            json={"model": "qwen-small", "choices": [{"message": {"content": '{"message":"Готово"}'}, "finish_reason": "stop"}]},
        )

    client = LlamaCppClient(enabled_settings(), transport=httpx.MockTransport(handler))
    response = asyncio.run(client.complete(request()))
    assert response.content == '{"message":"Готово"}'

    client = LlamaCppClient(enabled_settings(), transport=httpx.MockTransport(handler))
    parsed = asyncio.run(client.complete_json(request(), AIJsonMessage))
    assert parsed.message == "Готово"


def test_llama_client_maps_timeout_unavailable_and_invalid_json():
    def timeout_handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    timeout_client = LlamaCppClient(enabled_settings(), transport=httpx.MockTransport(timeout_handler))
    with pytest.raises(AITimeoutError):
        asyncio.run(timeout_client.complete(request()))

    def unavailable_handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    unavailable_client = LlamaCppClient(enabled_settings(), transport=httpx.MockTransport(unavailable_handler))
    with pytest.raises(AIUnavailableError):
        asyncio.run(unavailable_client.complete(request()))

    def invalid_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "not-json"}}]})

    invalid_client = LlamaCppClient(enabled_settings(), transport=httpx.MockTransport(invalid_handler))
    with pytest.raises(AIResponseValidationError):
        asyncio.run(invalid_client.complete_json(request(), AIJsonMessage))


def test_ai_health_is_private_and_uses_fake_client(client, db, make_user, login):
    user = make_user("ai-user")
    assert client.get("/api/ai/health", follow_redirects=False).status_code == 303

    login(user.username)
    get_ai_settings.cache_clear()
    disabled = client.get("/api/ai/health")
    assert disabled.status_code == 200
    assert disabled.json()["state"] == "disabled"

    app.dependency_overrides[get_ai_client] = lambda: FakeAIClient(availability=AIAvailability(state="available"))
    try:
        response = client.get("/api/ai/health")
    finally:
        app.dependency_overrides.pop(get_ai_client, None)
        get_ai_settings.cache_clear()

    assert response.status_code == 200
    assert response.json()["state"] == "available"
