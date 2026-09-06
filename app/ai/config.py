from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlparse

from app.config import env_bool, env_int


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _base_url(value: str) -> str:
    clean = value.strip().rstrip("/")
    parsed = urlparse(clean)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("AI_BASE_URL must be an absolute http(s) URL without credentials, query, or fragment")
    return clean


@dataclass(frozen=True)
class AISettings:
    enabled: bool
    base_url: str | None
    model: str | None
    api_key: str | None
    connect_timeout_seconds: int
    read_timeout_seconds: int
    max_tokens: int
    context_budget: int
    max_concurrency: int
    temperature: float


def load_ai_settings() -> AISettings:
    enabled = env_bool("AI_ENABLED", False)
    raw_base_url = os.getenv("AI_BASE_URL", "").strip()
    raw_model = os.getenv("AI_MODEL", "").strip()
    if enabled and not raw_base_url:
        raise RuntimeError("AI_BASE_URL must be set when AI_ENABLED=true")
    if enabled and not raw_model:
        raise RuntimeError("AI_MODEL must be set when AI_ENABLED=true")
    if len(raw_model) > 200:
        raise RuntimeError("AI_MODEL must be at most 200 characters")

    return AISettings(
        enabled=enabled,
        base_url=_base_url(raw_base_url) if raw_base_url else None,
        model=raw_model or None,
        api_key=os.getenv("AI_API_KEY", "").strip() or None,
        connect_timeout_seconds=env_int("AI_CONNECT_TIMEOUT_SECONDS", 5, 1, 60),
        read_timeout_seconds=env_int("AI_READ_TIMEOUT_SECONDS", 60, 5, 180),
        max_tokens=env_int("AI_MAX_TOKENS", 512, 16, 2048),
        context_budget=env_int("AI_CONTEXT_BUDGET", 6000, 256, 16000),
        max_concurrency=env_int("AI_MAX_CONCURRENCY", 1, 1, 4),
        temperature=_env_float("AI_TEMPERATURE", 0.2, 0.0, 2.0),
    )


@lru_cache(maxsize=1)
def get_ai_settings() -> AISettings:
    return load_ai_settings()
