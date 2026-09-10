"""Safe storage, upload and lifecycle operations for temporary file transfers."""
from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable

from fastapi import UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import TemporaryFileTransfer, TemporarySharedFile

logger = logging.getLogger("home_service.file_sharing")
COPY_CHUNK_SIZE = 1024 * 1024
STORAGE_KEY_RE = re.compile(r"[a-f0-9]{32}")
PREVIEW_SIZE = (1200, 1200)


class FileShareValidationError(ValueError):
    pass


@dataclass
class CleanupSummary:
    transfers_removed: int = 0
    files_removed: int = 0
    bytes_freed: int = 0
    failures: int = 0
    orphans_removed: int = 0


def utc_naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo is None else value.astimezone(UTC).replace(tzinfo=None)


def transfer_is_expired(transfer: TemporaryFileTransfer, now: datetime) -> bool:
    return utc_naive(now) >= transfer.expires_at


def format_file_size(size_bytes: int) -> str:
    value = float(max(0, size_bytes))
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            return f"{value:.1f} {unit}" if unit != "Б" else f"{int(value)} {unit}"
        value /= 1024
    return f"{int(size_bytes)} Б"


def safe_original_filename(value: str | None) -> str:
    name = Path((value or "").replace("\\", "/")).name.strip().replace("\x00", "")
    if not name or name in {".", ".."}:
        raise FileShareValidationError("Укажите корректное имя файла.")
    return name[:255]


def upload_size(upload: UploadFile) -> int:
    try:
        upload.file.seek(0, 2)
        size = upload.file.tell()
        upload.file.seek(0)
    except (AttributeError, OSError) as exc:
        raise FileShareValidationError("Не удалось прочитать загружаемый файл.") from exc
    return size


def validate_uploads(uploads: Iterable[UploadFile], *, max_file_bytes: int, max_transfer_bytes: int, existing_size_bytes: int) -> list[tuple[UploadFile, str, int]]:
    prepared: list[tuple[UploadFile, str, int]] = []
    total = existing_size_bytes
    for upload in uploads:
        if upload is None or not upload.filename:
            continue
        filename, size = safe_original_filename(upload.filename), upload_size(upload)
        if size > max_file_bytes:
            raise FileShareValidationError(f"Файл «{filename}» превышает допустимый размер.")
        total += size
        if total > max_transfer_bytes:
            raise FileShareValidationError("Общий размер передачи превышает допустимый лимит.")
        prepared.append((upload, filename, size))
    return prepared


def close_uploads(uploads: Iterable[UploadFile]) -> None:
    for upload in uploads:
        if upload is not None:
            upload.file.close()


def storage_directory(root: Path, transfer_id: int) -> Path:
    if transfer_id <= 0:
        raise ValueError("Transfer must be persisted before file storage is used")
    resolved_root = root.resolve()
    target = (resolved_root / str(transfer_id)).resolve()
    if target.parent != resolved_root:
        raise ValueError("Unsafe transfer storage path")
    return target


def storage_path(root: Path, transfer_id: int, storage_key: str) -> Path:
    if not STORAGE_KEY_RE.fullmatch(storage_key):
        raise ValueError("Unsafe storage key")
    target_dir = storage_directory(root, transfer_id)
    target = (target_dir / storage_key).resolve()
    if target.parent != target_dir:
        raise ValueError("Unsafe shared file path")
    return target


def preview_path(root: Path, transfer_id: int, storage_key: str) -> Path:
    source = storage_path(root, transfer_id, storage_key)
    directory = storage_directory(root, transfer_id) / "previews"
    target = (directory / f"{source.name}.jpg").resolve()
    if target.parent != directory.resolve():
        raise ValueError("Unsafe shared preview path")
    return target


def is_previewable_image(root: Path, shared_file: TemporarySharedFile) -> bool:
    """Decode-validate an image; filename and client MIME never determine previewability."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(storage_path(root, shared_file.transfer_id, shared_file.storage_key)) as image:
                image.verify()
        return True
    except (OSError, UnidentifiedImageError, ValueError, Image.DecompressionBombError):
        return False


def ensure_image_preview(root: Path, shared_file: TemporarySharedFile) -> Path | None:
    if not is_previewable_image(root, shared_file):
        return None
    target = preview_path(root, shared_file.transfer_id, shared_file.storage_key)
    if target.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(storage_path(root, shared_file.transfer_id, shared_file.storage_key)) as image:
                image = ImageOps.exif_transpose(image)
                image.thumbnail(PREVIEW_SIZE)
                if image.mode not in {"RGB", "L"}:
                    image = image.convert("RGB")
                image.save(temporary, format="JPEG", quality=82, optimize=True)
        os.replace(temporary, target)
    except (OSError, UnidentifiedImageError, ValueError, Image.DecompressionBombError):
        temporary.unlink(missing_ok=True)
        return None
    return target


def existing_transfer_size(db: Session, transfer_id: int) -> int:
    return int(db.scalar(select(func.coalesce(func.sum(TemporarySharedFile.size_bytes), 0)).where(TemporarySharedFile.transfer_id == transfer_id)) or 0)


def storage_usage(db: Session, *, owner_id: int | None = None) -> int:
    statement = select(func.coalesce(func.sum(TemporarySharedFile.size_bytes), 0)).join(TemporaryFileTransfer)
    if owner_id is not None:
        statement = statement.where(TemporaryFileTransfer.owner_id == owner_id)
    return int(db.scalar(statement) or 0)


def ensure_storage_capacity(root: Path, *, incoming_bytes: int, current_bytes: int, max_storage_bytes: int, max_user_storage_bytes: int, current_user_bytes: int, min_free_bytes: int) -> None:
    if max_storage_bytes and current_bytes + incoming_bytes > max_storage_bytes:
        raise FileShareValidationError("Недостаточно места в общем хранилище файлов.")
    if max_user_storage_bytes and current_user_bytes + incoming_bytes > max_user_storage_bytes:
        raise FileShareValidationError("Превышен лимит личного хранилища файлов.")
    try:
        free_bytes = shutil.disk_usage(root).free
    except OSError as exc:
        raise FileShareValidationError("Не удалось проверить свободное место на диске.") from exc
    if free_bytes - incoming_bytes < min_free_bytes:
        raise FileShareValidationError("Недостаточно свободного места на диске для загрузки.")


def write_uploads(db: Session, transfer: TemporaryFileTransfer, prepared: list[tuple[UploadFile, str, int]], *, root: Path, max_file_bytes: int, max_transfer_bytes: int, existing_size_bytes: int) -> list[TemporarySharedFile]:
    """Write privately first, then atomically publish completed files."""
    if not prepared:
        return []
    directory = storage_directory(root, transfer.id)
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    records: list[TemporarySharedFile] = []
    running_total = existing_size_bytes
    try:
        for upload, original_filename, expected_size in prepared:
            storage_key = secrets.token_hex(16)
            target = storage_path(root, transfer.id, storage_key)
            temporary = directory / f".{storage_key}.tmp"
            actual_size = 0
            try:
                with temporary.open("xb") as destination:
                    while chunk := upload.file.read(COPY_CHUNK_SIZE):
                        actual_size += len(chunk)
                        if actual_size > max_file_bytes or running_total + actual_size > max_transfer_bytes:
                            raise FileShareValidationError("Фактический размер загрузки превышает допустимый лимит.")
                        destination.write(chunk)
                if actual_size != expected_size:
                    raise FileShareValidationError("Размер загруженного файла изменился во время чтения.")
                os.replace(temporary, target)
                written.append(target)
            finally:
                temporary.unlink(missing_ok=True)
            try:
                target.chmod(0o600)
            except OSError:
                pass
            running_total += actual_size
            record = TemporarySharedFile(transfer_id=transfer.id, original_filename=original_filename, storage_key=storage_key, size_bytes=actual_size, content_type=(upload.content_type or "")[:255] or None)
            db.add(record)
            records.append(record)
        db.flush()
    except Exception:
        for target in written:
            target.unlink(missing_ok=True)
        raise
    return records


def remove_shared_file(root: Path, shared_file: TemporarySharedFile) -> None:
    try:
        storage_path(root, shared_file.transfer_id, shared_file.storage_key).unlink(missing_ok=True)
        preview_path(root, shared_file.transfer_id, shared_file.storage_key).unlink(missing_ok=True)
    except OSError as exc:
        raise FileShareValidationError("Не удалось удалить файл с диска.") from exc


def remove_transfer_storage(root: Path, transfer_id: int) -> None:
    directory = storage_directory(root, transfer_id)
    if not directory.exists():
        return
    try:
        # rmtree unlinks symlinks inside this directory and does not traverse them.
        shutil.rmtree(directory)
    except OSError as exc:
        raise FileShareValidationError("Не удалось удалить файлы передачи с диска.") from exc


def cleanup_transfer(db: Session, transfer: TemporaryFileTransfer, *, root: Path) -> CleanupSummary:
    """Delete physical data before metadata, leaving failures retryable."""
    summary = CleanupSummary(files_removed=len(transfer.files), bytes_freed=transfer.total_size_bytes)
    remove_transfer_storage(root, transfer.id)
    db.delete(transfer)
    return summary


def cleanup_expired_transfers(db: Session, *, root: Path, now: datetime, owner_id: int | None = None) -> CleanupSummary:
    summary = CleanupSummary()
    statement = select(TemporaryFileTransfer).where(TemporaryFileTransfer.expires_at <= utc_naive(now)).order_by(TemporaryFileTransfer.id)
    if owner_id is not None:
        statement = statement.where(TemporaryFileTransfer.owner_id == owner_id)
    for transfer in list(db.scalars(statement)):
        try:
            result = cleanup_transfer(db, transfer, root=root)
            db.commit()
            summary.transfers_removed += 1
            summary.files_removed += result.files_removed
            summary.bytes_freed += result.bytes_freed
        except Exception as exc:
            db.rollback()
            summary.failures += 1
            logger.error("Temporary transfer cleanup failed transfer_id=%s error=%s", transfer.id, type(exc).__name__)
    return summary


def cleanup_orphans(db: Session, *, root: Path, now: datetime, grace: timedelta) -> CleanupSummary:
    """Remove only old numeric transfer directories that have no DB row."""
    summary = CleanupSummary()
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    known_ids = {str(value) for value in db.scalars(select(TemporaryFileTransfer.id))}
    threshold = utc_naive(now).timestamp() - grace.total_seconds()
    for child in root.iterdir():
        if not child.name.isdigit() or child.name in known_ids:
            continue
        try:
            if child.stat(follow_symlinks=False).st_mtime > threshold:
                continue
            if child.is_symlink():
                child.unlink()
            elif child.is_dir() and child.resolve().parent == root:
                shutil.rmtree(child)
            else:
                continue
            summary.orphans_removed += 1
        except OSError as exc:
            summary.failures += 1
            logger.error("Temporary transfer orphan cleanup failed path=%s error=%s", child.name, type(exc).__name__)
    for transfer_id in known_ids:
        directory = root / transfer_id
        if not directory.is_dir() or directory.is_symlink():
            continue
        for partial in directory.glob(".*.tmp"):
            try:
                if partial.is_symlink() or partial.stat().st_mtime <= threshold:
                    partial.unlink(missing_ok=True)
                    summary.orphans_removed += 1
            except OSError as exc:
                summary.failures += 1
                logger.error("Temporary partial cleanup failed transfer_id=%s error=%s", transfer_id, type(exc).__name__)
    return summary


def cleanup_all(db: Session, *, root: Path, now: datetime, orphan_grace: timedelta, owner_id: int | None = None) -> CleanupSummary:
    summary = cleanup_expired_transfers(db, root=root, now=now, owner_id=owner_id)
    if owner_id is None:
        orphan_summary = cleanup_orphans(db, root=root, now=now, grace=orphan_grace)
        summary.failures += orphan_summary.failures
        summary.orphans_removed += orphan_summary.orphans_removed
    return summary
