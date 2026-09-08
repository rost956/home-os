from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="home-service-tests-"))
TEST_DATABASE = TEST_DATA_DIR / "test.db"
os.environ.update(
    {
        "APP_ENV": "test",
        "SECRET_KEY": "test-secret-key-with-at-least-thirty-two-characters",
        "DATA_DIR": str(TEST_DATA_DIR),
        "DATABASE_URL": f"sqlite:///{TEST_DATABASE.as_posix()}",
        "BACKGROUND_JOBS_ENABLED": "false",
        "REGISTRATION_ENABLED": "true",
        "ENFORCE_SAME_ORIGIN": "false",
        "SECURE_COOKIES": "false",
        # AI-focused tests retain their existing explicit routes; production and
        # development defaults remain frozen through HOME_AI_ENABLED=false.
        "HOME_AI_ENABLED": "true",
    }
)

from app.auth import hash_password  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import AUTH_ATTEMPTS, app  # noqa: E402
from app.models import User  # noqa: E402


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(table.delete())
    AUTH_ATTEMPTS.clear()
    yield


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    with TestClient(app, base_url="http://testserver") as test_client:
        yield test_client


@pytest.fixture
def make_user(db):
    def factory(username: str, password: str = "correct-horse") -> User:
        user = User(username=username, password_hash=hash_password(password))
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    return factory


@pytest.fixture
def login(client):
    def perform(username: str, password: str = "correct-horse"):
        return client.post(
            "/login",
            data={"username": username, "password": password},
            follow_redirects=False,
        )

    return perform
