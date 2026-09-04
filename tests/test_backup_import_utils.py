from __future__ import annotations

import json
import sqlite3
import zipfile
from datetime import date

import pytest

from app.database import engine
from app.main import expense_period_bounds
from app.models import ExpenseItem
from app.services.backups import create_backup_zip
from app.web import DATA_DIR


def test_consistent_sqlite_backup_contains_current_data(db, make_user, tmp_path):
    make_user("backup-user")
    archive = create_backup_zip(
        engine=engine,
        data_dir=DATA_DIR,
        backup_dir=tmp_path / "backups",
        include_data_files=False,
        retention=2,
    )
    with zipfile.ZipFile(archive) as payload:
        payload.extract("data/app.db", tmp_path / "restore")
    restored = sqlite3.connect(tmp_path / "restore" / "data" / "app.db")
    try:
        assert restored.execute("SELECT username FROM users").fetchone()[0] == "backup-user"
    finally:
        restored.close()


def test_web_backup_does_not_follow_symlinks_outside_data(db, make_user, tmp_path):
    make_user("backup-user")
    source_data = tmp_path / "source-data"
    source_data.mkdir()
    (source_data / "inside.txt").write_text("inside", encoding="utf-8")
    external = tmp_path / "external.txt"
    external.write_text("secret", encoding="utf-8")
    try:
        (source_data / "external-link.txt").symlink_to(external)
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this platform")

    archive = create_backup_zip(
        engine=engine,
        data_dir=source_data,
        backup_dir=tmp_path / "backups",
        include_data_files=True,
    )

    with zipfile.ZipFile(archive) as payload:
        assert "data/inside.txt" in payload.namelist()
        assert "data/external-link.txt" not in payload.namelist()


def test_import_rejects_non_object_and_skips_invalid_expense(client, db, make_user, login):
    make_user("alice")
    login("alice")
    invalid_root = client.post("/import-data", data={"data_text": "[]"})
    assert invalid_root.status_code == 400

    payload = {
        "expense_lists": [
            {
                "title": "Импорт",
                "categories": [
                    {"name": "Еда", "items": [{"title": "Ошибка", "amount": "-100"}]},
                ],
            }
        ]
    }
    response = client.post(
        "/import-data",
        data={"data_text": json.dumps(payload, ensure_ascii=False)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert db.query(ExpenseItem).count() == 0


def test_expense_period_handles_short_months():
    assert expense_period_bounds(date(2026, 2, 28), 31) == (date(2026, 2, 28), date(2026, 3, 30))
    assert expense_period_bounds(date(2026, 3, 1), 31) == (date(2026, 2, 28), date(2026, 3, 30))


def test_export_requires_authentication(client):
    response = client.get("/export/data.json", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
