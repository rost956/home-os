from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

FALSE_VALUES = {"0", "false", "no", "off"}
TRUE_VALUES = {"1", "true", "yes", "on"}
INSECURE_SECRETS = {
    "change-me",
    "change-me-in-production",
    "change-me-to-a-long-random-string",
    "development-only-secret",
}


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise RuntimeError(f"{name} must be one of: true, false, 1, 0")


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _host_from_address(address: str) -> str | None:
    clean = address.strip()
    if not clean:
        return None
    parsed = urlparse(clean if "://" in clean else f"//{clean}")
    return parsed.hostname


def _allowed_hosts(app_env: str) -> tuple[str, ...]:
    configured = os.getenv("ALLOWED_HOSTS", "")
    addresses = os.getenv("CADDY_SITE_ADDRESS", "")
    hosts = {
        host
        for host in (
            *(_host_from_address(item) for item in configured.split(",")),
            *(_host_from_address(item) for item in addresses.split(",")),
        )
        if host
    }
    hosts.update({"localhost", "127.0.0.1", "testserver"})
    if app_env != "production":
        hosts.add("*")
    return tuple(sorted(hosts))


@dataclass(frozen=True)
class Settings:
    app_env: str
    secret_key: str
    database_url: str
    data_dir: Path
    allowed_hosts: tuple[str, ...]
    registration_enabled: bool
    home_ai_enabled: bool
    enforce_same_origin: bool
    secure_cookies: bool
    background_jobs_enabled: bool
    session_max_age: int
    timer_reminder_minutes: int

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


def load_settings() -> Settings:
    app_env = os.getenv("APP_ENV", "development").strip().lower() or "development"
    if app_env not in {"development", "test", "testing", "production"}:
        raise RuntimeError("APP_ENV must be development, test, testing, or production")
    secret_key = os.getenv("SECRET_KEY", "").strip()
    if app_env == "production":
        if len(secret_key) < 32 or secret_key.lower() in INSECURE_SECRETS:
            raise RuntimeError("Production SECRET_KEY must be a random value of at least 32 characters")
    elif not secret_key:
        secret_key = secrets.token_urlsafe(48)

    data_dir = Path(os.getenv("DATA_DIR", "data")).expanduser()
    database_url = os.getenv("DATABASE_URL", f"sqlite:///{(data_dir / 'app.db').as_posix()}").strip()
    session_days = env_int("SESSION_MAX_AGE_DAYS", 30, 1, 365)
    return Settings(
        app_env=app_env,
        secret_key=secret_key,
        database_url=database_url,
        data_dir=data_dir,
        allowed_hosts=_allowed_hosts(app_env),
        registration_enabled=env_bool("REGISTRATION_ENABLED", app_env != "production"),
        home_ai_enabled=env_bool("HOME_AI_ENABLED", False),
        enforce_same_origin=env_bool("ENFORCE_SAME_ORIGIN", app_env == "production"),
        secure_cookies=env_bool("SECURE_COOKIES", app_env == "production"),
        background_jobs_enabled=env_bool("BACKGROUND_JOBS_ENABLED", app_env not in {"test", "testing"}),
        session_max_age=session_days * 24 * 60 * 60,
        timer_reminder_minutes=env_int("TIMER_REMINDER_MINUTES", 120, 30, 10_080),
    )


settings = load_settings()
