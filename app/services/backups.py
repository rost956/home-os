from __future__ import annotations

import os
import sqlite3
import threading
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from sqlalchemy.engine import Engine

_backup_lock = threading.Lock()


def sqlite_database_path(engine: Engine) -> Path | None:
    if not engine.url.drivername.startswith("sqlite") or not engine.url.database:
        return None
    if engine.url.database == ":memory:":
        return None
    return Path(engine.url.database).resolve()


def _sqlite_snapshot(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=30)
    target_connection = sqlite3.connect(target, timeout=30)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()


def create_backup_zip(
    *,
    engine: Engine,
    data_dir: Path,
    backup_dir: Path,
    prefix: str = "backup",
    include_data_files: bool = True,
    retention: int = 14,
) -> Path:
    """Create an atomic, consistent SQLite backup and optionally include uploads/keys."""
    database_path = sqlite_database_path(engine)
    if database_path is None or not database_path.is_file():
        raise FileNotFoundError("SQLite database file does not exist")

    with _backup_lock:
        backup_dir.mkdir(parents=True, exist_ok=True)
        resolved_data_dir = data_dir.resolve()
        resolved_backup_dir = backup_dir.resolve()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = backup_dir / f"{prefix}_{stamp}.zip"
        if target.exists():
            target = backup_dir / f"{prefix}_{stamp}_{uuid.uuid4().hex[:6]}.zip"
        temporary_zip = backup_dir / f".{target.name}.{uuid.uuid4().hex}.tmp"
        temporary_db = backup_dir / f".snapshot.{uuid.uuid4().hex}.db"
        try:
            _sqlite_snapshot(database_path, temporary_db)
            with zipfile.ZipFile(temporary_zip, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(temporary_db, "data/app.db")
                if include_data_files and data_dir.exists():
                    for path in data_dir.rglob("*"):
                        resolved = path.resolve()
                        if (
                            not path.is_file()
                            or resolved_data_dir not in resolved.parents
                            or resolved == database_path
                            or resolved_backup_dir == resolved
                            or resolved_backup_dir in resolved.parents
                        ):
                            continue
                        if path.name.endswith(("-wal", "-shm")):
                            continue
                        archive.write(path, Path("data") / path.relative_to(data_dir))
            os.replace(temporary_zip, target)
            os.chmod(target, 0o600)
        finally:
            temporary_db.unlink(missing_ok=True)
            temporary_zip.unlink(missing_ok=True)

        backups = sorted(backup_dir.glob(f"{prefix}_*.zip"), key=lambda path: path.stat().st_mtime, reverse=True)
        for stale in backups[max(1, retention):]:
            stale.unlink(missing_ok=True)
        return target
