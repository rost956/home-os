#!/usr/bin/env python3
"""Create and restore consistent SQLite backups using only the standard library."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

DATABASE_MEMBER = "data/app.db"


def verify_database(path: Path) -> None:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        result = connection.execute("PRAGMA quick_check").fetchone()
    finally:
        connection.close()
    if not result or result[0] != "ok":
        raise RuntimeError(f"SQLite integrity check failed: {result[0] if result else 'no result'}")


def snapshot_database(source: Path, target: Path) -> None:
    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=30)
    target_connection = sqlite3.connect(target, timeout=30)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()
    verify_database(target)


def create_backup(
    database: Path,
    backup_dir: Path,
    retention: int = 14,
    prefix: str = "deploy",
    *,
    data_dir: Path | None = None,
    include_data_files: bool = False,
) -> Path:
    database = database.resolve()
    backup_dir = backup_dir.resolve()
    data_dir = (data_dir or database.parent).resolve()
    if not database.is_file():
        raise FileNotFoundError(f"Database not found: {database}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = backup_dir / f"{prefix}_{stamp}.zip"
    if destination.exists():
        destination = backup_dir / f"{prefix}_{stamp}_{uuid.uuid4().hex[:8]}.zip"
    fd, temporary_name = tempfile.mkstemp(prefix=".sqlite-backup-", suffix=".db", dir=backup_dir)
    os.close(fd)
    temporary_database = Path(temporary_name)
    temporary_archive = destination.with_suffix(".zip.tmp")
    try:
        temporary_database.unlink(missing_ok=True)
        snapshot_database(database, temporary_database)
        manifest = {
            "created_at": datetime.now(UTC).isoformat(),
            "database_member": DATABASE_MEMBER,
            "source_name": database.name,
            "includes_data_files": include_data_files,
        }
        with zipfile.ZipFile(temporary_archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(temporary_database, DATABASE_MEMBER)
            if include_data_files and data_dir.is_dir():
                for path in data_dir.rglob("*"):
                    resolved = path.resolve()
                    if (
                        not path.is_file()
                        or data_dir not in resolved.parents
                        or resolved == database
                        or backup_dir == resolved
                        or backup_dir in resolved.parents
                        or path.name in {f"{database.name}-wal", f"{database.name}-shm"}
                        or path.name.startswith((".sqlite-backup-", ".sqlite-restore-", ".restore-files-"))
                    ):
                        continue
                    archive.write(path, (PurePosixPath("data") / PurePosixPath(path.relative_to(data_dir).as_posix())).as_posix())
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=True, indent=2))
        os.replace(temporary_archive, destination)
        os.chmod(destination, 0o600)
    finally:
        temporary_database.unlink(missing_ok=True)
        temporary_archive.unlink(missing_ok=True)

    backups = sorted(backup_dir.glob(f"{prefix}_*.zip"), key=lambda item: item.stat().st_mtime, reverse=True)
    for stale in backups[max(1, retention) :]:
        stale.unlink(missing_ok=True)
    return destination


def restore_backup(archive_path: Path, database: Path, backup_dir: Path) -> Path:
    archive_path = archive_path.resolve()
    database = database.resolve()
    backup_dir = backup_dir.resolve()
    if not archive_path.is_file():
        raise FileNotFoundError(f"Backup not found: {archive_path}")

    database.parent.mkdir(parents=True, exist_ok=True)
    if database.exists():
        create_backup(
            database,
            backup_dir,
            retention=14,
            prefix="before_restore",
            data_dir=database.parent,
            include_data_files=True,
        )

    fd, temporary_name = tempfile.mkstemp(prefix=".sqlite-restore-", suffix=".db", dir=database.parent)
    os.close(fd)
    temporary_database = Path(temporary_name)
    try:
        with tempfile.TemporaryDirectory(prefix=".restore-files-", dir=database.parent) as staging_name:
            staging_dir = Path(staging_name)
            with zipfile.ZipFile(archive_path) as archive:
                members = archive.infolist()
                database_member = archive.getinfo(DATABASE_MEMBER)
                if database_member.is_dir() or database_member.file_size <= 0:
                    raise RuntimeError("Backup does not contain a usable database")
                total_size = sum(member.file_size for member in members)
                if total_size > shutil.disk_usage(database.parent).free:
                    raise RuntimeError("Not enough free disk space to restore this backup")
                for member in members:
                    member_path = PurePosixPath(member.filename)
                    is_symlink = (member.external_attr >> 16) & 0o170000 == 0o120000
                    if (
                        member.filename == "manifest.json"
                        or member.is_dir()
                        or member.filename == DATABASE_MEMBER
                    ):
                        continue
                    if is_symlink or member_path.is_absolute() or ".." in member_path.parts or not member_path.parts or member_path.parts[0] != "data":
                        raise RuntimeError(f"Unsafe backup member: {member.filename}")
                    relative_path = Path(*member_path.parts[1:])
                    staged_path = staging_dir / relative_path
                    staged_path.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source, staged_path.open("wb") as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
                    os.chmod(staged_path, 0o600)
                with archive.open(database_member) as source, temporary_database.open("wb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)

            verify_database(temporary_database)
            for staged_path in staging_dir.rglob("*"):
                if not staged_path.is_file():
                    continue
                relative_path = staged_path.relative_to(staging_dir)
                destination = (database.parent / relative_path).resolve()
                if database.parent not in destination.parents:
                    raise RuntimeError(f"Unsafe restore destination: {destination}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged_path, destination)
            database.with_name(f"{database.name}-wal").unlink(missing_ok=True)
            database.with_name(f"{database.name}-shm").unlink(missing_ok=True)
            os.replace(temporary_database, database)
    finally:
        temporary_database.unlink(missing_ok=True)
    return database


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("backup", "restore"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--database", type=Path, required=True)
        subparser.add_argument("--backup-dir", type=Path, required=True)
        if command == "backup":
            subparser.add_argument("--retention", type=int, default=14)
            subparser.add_argument("--prefix", default="deploy")
            subparser.add_argument("--include-files", action="store_true")
        else:
            subparser.add_argument("--archive", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "backup":
        result = create_backup(
            args.database,
            args.backup_dir,
            args.retention,
            args.prefix,
            data_dir=args.database.parent,
            include_data_files=args.include_files,
        )
    else:
        result = restore_backup(args.archive, args.database, args.backup_dir)
    print(result)


if __name__ == "__main__":
    main()
