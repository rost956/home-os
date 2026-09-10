# ruff: noqa: E701, E702
import asyncio
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse

from markupsafe import Markup, escape

try:
    from pywebpush import WebPushException, webpush
except Exception:  # pragma: no cover - dependency is installed in Docker, but keep local dev resilient
    WebPushException = Exception
    webpush = None

from fastapi import Depends, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import and_, asc, desc, func, or_, select, update
from sqlalchemy import delete as sql_delete
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .auth import get_current_user, get_current_user_optional, hash_password, verify_password
from .config import settings
from .database import Base, SessionLocal, engine, get_db
from .models import (
    ChatNote,
    ChatThread,
    ChatThreadMessage,
    DebtSplit,
    ExpenseCategory,
    ExpenseItem,
    ExpenseLimit,
    ExpenseList,
    ExpenseListShare,
    IncomeItem,
    MenuItem,
    Moment,
    PlannerItem,
    PlannerReminder,
    PlannerReminderDelivery,
    PushSubscription,
    Recipe,
    RecipeCookingTimer,
    RecurringExpense,
    ShoppingCategoryRule,
    ShoppingItem,
    ShoppingList,
    ShoppingListShare,
    ShoppingPriceHistory,
    TemporaryFileTransfer,
    TemporarySharedFile,
    User,
    Vehicle,
    VehicleFuelEntry,
    VehicleLogEntry,
    VehicleMaintenanceItem,
    WatchItem,
    WishlistItem,
    WishlistShare,
)
from .routers.system import router as system_router
from .services.backups import create_backup_zip, sqlite_database_path
from .services.exports import build_expenses_csv, build_expenses_xlsx
from .services.file_sharing import (
    FileShareValidationError,
    cleanup_all,
    close_uploads,
    ensure_image_preview,
    ensure_storage_capacity,
    existing_transfer_size,
    format_file_size,
    is_previewable_image,
    remove_shared_file,
    remove_transfer_storage,
    safe_original_filename,
    storage_path,
    storage_usage,
    transfer_is_expired,
    validate_uploads,
    write_uploads,
)
from .services.finance import (
    accessible_expense_lists,
    build_finance_snapshot,
    clamp_month_day,
    expense_period_bounds,
    expense_period_start_day,
    format_period_range,
    shifted_month,
    summarize_cashflow,
)
from .services.planner import (
    calendar_occurrences,
    format_planner_date_range,
    iter_occurrences_in_range,
)
from .services.planner_push import PyWebPushSender, run_delivery_cycle
from .services.planner_reminders import (
    parse_reminder_configs,
    reminder_label,
    replace_reminder_configs,
)
from .services.preferences import (
    PALETTE_GROUPS,
    PALETTE_TOKENS,
    default_palette,
    gradient_css_variables,
    load_gradient,
    load_palette,
    palette_css_variables,
    validate_gradient,
    validate_palette,
)
from .services.vehicle_fuel import fuel_segments, fuel_summary
from .services.vehicle_maintenance import STATUS_ORDER, calculate_maintenance
from .timezone import (
    UTC as UTC_TZ,
)
from .timezone import (
    format_msk as format_msk_datetime,
)
from .timezone import (
    msk_date,
    msk_date_to_utc_naive,
    to_msk,
)
from .timezone import (
    msk_day_bounds_utc as msk_day_utc_bounds,
)
from .timezone import (
    now_utc as utc_now_naive,
)
from .timezone import (
    today_msk as msk_today,
)
from .web import (
    BACKUP_DIR,
    CHAT_MEDIA_DIR,
    DATA_DIR,
    MEDIA_DIR,
    MOMENT_MEDIA_DIR,
    PUSH_VAPID_PRIVATE_KEY_FILE,
    RECIPE_MEDIA_DIR,
    SHARED_FILES_DIR,
    app,
    templates,
)

ONLINE_WINDOW_SECONDS = 75
BACKUP_RETENTION_COUNT = 14
IMPORT_MAX_BYTES = 2 * 1024 * 1024
IMPORT_MAX_ITEMS = 10_000
MAX_REQUEST_BYTES = max(16 * 1024 * 1024, settings.file_share_max_transfer_bytes + 2 * 1024 * 1024)
MOMENT_THUMB_DIR = MOMENT_MEDIA_DIR / "thumbs"
MOMENT_THUMB_SIZE = (480, 360)
PUSH_ENDPOINT_HOST_SUFFIXES = tuple(
    dict.fromkeys(
        (
            "fcm.googleapis.com",
            "web.push.apple.com",
            "push.services.mozilla.com",
            "updates.push.services.mozilla.com",
            "notify.windows.com",
            *(
                value.strip().lower().lstrip(".")
                for value in os.getenv("PUSH_ENDPOINT_HOSTS", "").split(",")
                if value.strip()
            ),
        )
    )
)
AUTH_ATTEMPTS: dict[str, list[datetime]] = defaultdict(list)
AUTH_ATTEMPTS_LOCK = threading.Lock()
AUTH_ATTEMPTS_MAX_KEYS = 5_000
TIMER_REMINDER_MINUTES = settings.timer_reminder_minutes
timer_reminder_stop = threading.Event()
logger = logging.getLogger("home_service")
RUSSIAN_MONTH_NAMES = (
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
)


def format_odometer(value: int | None) -> str:
    return f"{max(0, int(value or 0)):,}".replace(",", " ") + " км"


def touch_user_presence(db: Session, user: User | None, commit: bool = True) -> None:
    if not user:
        return
    now_value = utc_now_naive()
    db.execute(update(User).where(User.id == user.id).values(last_seen_at=now_value))
    user.last_seen_at = now_value
    if commit:
        db.commit()


def presence_info(target: User | None) -> dict[str, Any]:
    if not target or not getattr(target, "last_seen_at", None):
        return {"is_online": False, "text": "давно не был в сети", "last_seen_at": None}

    last_seen = target.last_seen_at
    if last_seen.tzinfo is not None:
        last_seen = last_seen.astimezone(UTC_TZ).replace(tzinfo=None)

    delta = utc_now_naive() - last_seen
    is_online = delta <= timedelta(seconds=ONLINE_WINDOW_SECONDS)
    if is_online:
        text = "онлайн"
    else:
        minutes = max(1, int(delta.total_seconds() // 60))
        if minutes < 60:
            text = f"был в сети {minutes} мин. назад"
        else:
            seen_msk = to_msk(last_seen)
            today = msk_today()
            if seen_msk and seen_msk.date() == today:
                text = f"был в сети сегодня в {seen_msk.strftime('%H:%M')}"
            elif seen_msk and seen_msk.date() == today - timedelta(days=1):
                text = f"был в сети вчера в {seen_msk.strftime('%H:%M')}"
            elif seen_msk:
                text = f"был в сети {seen_msk.strftime('%d.%m.%Y в %H:%M')}"
            else:
                text = "давно не был в сети"

    return {
        "is_online": is_online,
        "text": text,
        "last_seen_at": format_msk_datetime(last_seen),
    }


templates.env.filters["msk_datetime"] = format_msk_datetime
templates.env.filters["fmt_odometer"] = format_odometer
templates.env.filters["palette_css"] = palette_css_variables
templates.env.filters["gradient_css"] = gradient_css_variables
templates.env.filters["planner_date_range"] = format_planner_date_range
templates.env.filters["planner_reminder_label"] = reminder_label
templates.env.filters["file_size"] = format_file_size
templates.env.globals["presence_info"] = presence_info


def ensure_vapid_keys() -> dict[str, str]:
    """Resolve configured VAPID data without exposing or generating private keys."""
    if not settings.push_enabled:
        return {"available": "", "reason": "Push-уведомления отключены"}
    public_key = settings.vapid_public_key
    private_key = settings.vapid_private_key
    if not public_key or not private_key:
        return {"available": "", "reason": "VAPID-конфигурация не заполнена"}
    if not re.fullmatch(r"[A-Za-z0-9_-]{80,120}", public_key):
        return {"available": "", "reason": "Некорректный HOME_VAPID_PUBLIC_KEY"}
    if not (settings.vapid_subject.startswith("mailto:") or settings.vapid_subject.startswith("https://")):
        return {"available": "", "reason": "Некорректный HOME_VAPID_SUBJECT"}
    private_value = private_key
    if "BEGIN" in private_key:
        private_text = private_key.replace("\\n", "\n")
        PUSH_VAPID_PRIVATE_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        PUSH_VAPID_PRIVATE_KEY_FILE.write_text(private_text, encoding="utf-8")
        PUSH_VAPID_PRIVATE_KEY_FILE.chmod(0o600)
        private_value = str(PUSH_VAPID_PRIVATE_KEY_FILE)
    return {
        "available": "1",
        "private_key_path": private_value,
        "public_key": public_key,
        "subject": settings.vapid_subject,
    }


def send_push_to_user(db: Session, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """Send Web Push notifications to all devices of the user.

    Returns diagnostics so the UI can show whether a push was actually sent.
    Broken subscriptions are removed automatically.
    """
    result: dict[str, Any] = {"ok": False, "sent": 0, "failed": 0, "removed": 0, "errors": [], "subscriptions": 0}
    if not settings.push_enabled:
        result["errors"].append("Push-уведомления отключены")
        return result
    if webpush is None:
        result["errors"].append("pywebpush не установлен в контейнере")
        return result

    keys = ensure_vapid_keys()
    private_key_path = keys.get("private_key_path")
    if not keys.get("available") or not private_key_path:
        result["errors"].append(keys.get("reason") or "VAPID-конфигурация недоступна")
        return result

    subscriptions = db.scalars(
        select(PushSubscription).where(
            PushSubscription.user_id == user_id,
            PushSubscription.disabled_at.is_(None),
        )
    ).all()
    result["subscriptions"] = len(subscriptions)
    if not subscriptions:
        result["errors"].append("у пользователя нет push-подписок")
        return result

    claims = {"sub": keys["subject"]}
    for sub in subscriptions:
        info = {"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}}
        try:
            webpush(
                subscription_info=info,
                data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=private_key_path,
                vapid_claims=claims,
                ttl=60 * 60 * 24,
                timeout=12,
            )
            sub.last_used_at = utc_now_naive()
            result["sent"] += 1
        except Exception as exc:
            result["failed"] += 1
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            error_text = str(exc)
            if status_code:
                error_text = f"HTTP {status_code}: {error_text}"
            result["errors"].append(error_text[:500])
            if status_code in (404, 410):
                sub.disabled_at = utc_now_naive()
                result["removed"] += 1
            logger.warning("Web Push failed for user_id=%s status=%s", user_id, status_code or "unknown")

    result["ok"] = result["sent"] > 0
    db.commit()
    return result


def send_push_to_user_isolated(user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """Send a push from async code without sharing its SQLAlchemy session across threads."""
    with SessionLocal() as db:
        return send_push_to_user(db, user_id, payload)


def message_push_payload(thread: ChatThread, sender: User, text: str) -> dict[str, Any]:
    body = text.strip() or "Новое сообщение"
    if len(body) > 110:
        body = body[:107].rstrip() + "…"
    return {
        "title": f"{thread.title} · @{sender.username}",
        "body": body,
        "url": f"/chats/{thread.id}",
        "tag": f"chat-{thread.id}",
        "icon": "/static/icon-192.png",
        "badge": "/static/icon-192.png",
    }


class ConnectionManager:
    def __init__(self) -> None:
        self.active_by_thread: dict[int, list[tuple[int, WebSocket]]] = {}

    async def connect(self, thread_id: int, user_id: int, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_by_thread.setdefault(thread_id, []).append((user_id, websocket))

    def disconnect(self, thread_id: int, websocket: WebSocket) -> None:
        sockets = self.active_by_thread.get(thread_id, [])
        sockets[:] = [(user_id, socket) for user_id, socket in sockets if socket is not websocket]
        if not sockets:
            self.active_by_thread.pop(thread_id, None)

    def active_user_ids(self, thread_id: int) -> set[int]:
        return {user_id for user_id, _ in self.active_by_thread.get(thread_id, [])}

    async def broadcast(self, thread_id: int, payload: dict[str, Any]) -> None:
        sockets = list(self.active_by_thread.get(thread_id, []))
        for _, socket in sockets:
            try:
                await socket.send_json(payload)
            except Exception:
                self.disconnect(thread_id, socket)


chat_manager = ConnectionManager()


def schema_change_required() -> bool:
    database_path = sqlite_database_path(engine)
    if database_path is None or not database_path.is_file() or database_path.stat().st_size == 0:
        return False
    inspector = sa_inspect(engine)
    existing_tables = set(inspector.get_table_names())
    if not existing_tables:
        return False
    if set(Base.metadata.tables) - existing_tables:
        return True
    required_columns = {
        "users": {"theme", "expense_period_start_day", "ui_palette_json", "last_seen_at"},
        "recipes": {"image_path", "tags", "is_favorite"},
        "watch_items": {"last_watched_at"},
        "chat_threads": {"is_pinned"},
        "chat_thread_messages": {"reply_to_id", "attachment_path"},
        "recipe_cooking_timers": {"last_reminded_at"},
        "push_subscriptions": {"updated_at", "last_seen_at", "disabled_at"},
        "expense_items": {"include_in_analytics", "include_in_forecast"},
        "expense_list_shares": {"can_edit"},
        "shopping_list_shares": {"can_edit"},
        "planner_items": {"end_date", "recurrence_frequency", "recurrence_interval", "recurrence_until"},
        "wishlist_items": {"priority", "status", "goal_amount", "saved_amount", "expense_item_id", "expense_prev_status", "expense_prev_is_done"},
        "ai_user_settings": {
            "user_id",
            "enabled",
            "allow_general",
            "allow_finance",
            "allow_recipes",
            "allow_menu",
            "allow_planner",
            "allow_wishlist",
            "allow_chat",
            "allow_today",
        },
        "ai_actions": {
            "public_id",
            "owner_id",
            "action_type",
            "payload_version",
            "proposed_payload_json",
            "confirmed_payload_json",
            "status",
            "expires_at",
            "claim_token",
            "claimed_at",
            "resolved_at",
        },
    }
    for table_name, columns in required_columns.items():
        if table_name in existing_tables:
            actual = {column["name"] for column in inspector.get_columns(table_name)}
            if columns - actual:
                return True
    return False


def ensure_runtime_schema() -> None:
    """Small SQLite migrations for existing local databases."""
    if not engine.url.drivername.startswith("sqlite"):
        return
    with engine.begin() as connection:
        PlannerReminder.__table__.create(bind=connection, checkfirst=True)
        PlannerReminderDelivery.__table__.create(bind=connection, checkfirst=True)
        TemporaryFileTransfer.__table__.create(bind=connection, checkfirst=True)
        TemporarySharedFile.__table__.create(bind=connection, checkfirst=True)

        user_columns = [row[1] for row in connection.exec_driver_sql("PRAGMA table_info(users)").fetchall()]
        if "theme" not in user_columns:
            connection.exec_driver_sql(
                "ALTER TABLE users ADD COLUMN theme VARCHAR(20) NOT NULL DEFAULT 'system'"
            )
        if "expense_period_start_day" not in user_columns:
            connection.exec_driver_sql("ALTER TABLE users ADD COLUMN expense_period_start_day INTEGER NOT NULL DEFAULT 1")
        if "ui_palette_json" not in user_columns:
            connection.exec_driver_sql("ALTER TABLE users ADD COLUMN ui_palette_json TEXT")
        if "last_seen_at" not in user_columns:
            connection.exec_driver_sql("ALTER TABLE users ADD COLUMN last_seen_at DATETIME")

        recipe_columns = [row[1] for row in connection.exec_driver_sql("PRAGMA table_info(recipes)").fetchall()]
        if recipe_columns and "image_path" not in recipe_columns:
            connection.exec_driver_sql("ALTER TABLE recipes ADD COLUMN image_path VARCHAR(500)")
        if recipe_columns and "tags" not in recipe_columns:
            connection.exec_driver_sql("ALTER TABLE recipes ADD COLUMN tags VARCHAR(250)")
        if recipe_columns and "is_favorite" not in recipe_columns:
            connection.exec_driver_sql("ALTER TABLE recipes ADD COLUMN is_favorite BOOLEAN NOT NULL DEFAULT 0")

        watch_columns = [row[1] for row in connection.exec_driver_sql("PRAGMA table_info(watch_items)").fetchall()]
        if watch_columns and "last_watched_at" not in watch_columns:
            connection.exec_driver_sql("ALTER TABLE watch_items ADD COLUMN last_watched_at DATETIME")

        thread_columns = [row[1] for row in connection.exec_driver_sql("PRAGMA table_info(chat_threads)").fetchall()]
        if thread_columns and "is_pinned" not in thread_columns:
            connection.exec_driver_sql("ALTER TABLE chat_threads ADD COLUMN is_pinned BOOLEAN NOT NULL DEFAULT 0")

        message_columns = [row[1] for row in connection.exec_driver_sql("PRAGMA table_info(chat_thread_messages)").fetchall()]
        if message_columns and "reply_to_id" not in message_columns:
            connection.exec_driver_sql("ALTER TABLE chat_thread_messages ADD COLUMN reply_to_id INTEGER")
        if message_columns and "attachment_path" not in message_columns:
            connection.exec_driver_sql("ALTER TABLE chat_thread_messages ADD COLUMN attachment_path VARCHAR(500)")

        timer_columns = [row[1] for row in connection.exec_driver_sql("PRAGMA table_info(recipe_cooking_timers)").fetchall()]
        if timer_columns and "last_reminded_at" not in timer_columns:
            connection.exec_driver_sql("ALTER TABLE recipe_cooking_timers ADD COLUMN last_reminded_at DATETIME")

        push_columns = [row[1] for row in connection.exec_driver_sql("PRAGMA table_info(push_subscriptions)").fetchall()]
        if push_columns and "updated_at" not in push_columns:
            connection.exec_driver_sql("ALTER TABLE push_subscriptions ADD COLUMN updated_at DATETIME")
            connection.exec_driver_sql("UPDATE push_subscriptions SET updated_at = COALESCE(last_used_at, created_at)")
        if push_columns and "last_seen_at" not in push_columns:
            connection.exec_driver_sql("ALTER TABLE push_subscriptions ADD COLUMN last_seen_at DATETIME")
        if push_columns and "disabled_at" not in push_columns:
            connection.exec_driver_sql("ALTER TABLE push_subscriptions ADD COLUMN disabled_at DATETIME")
        if push_columns:
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_push_subscriptions_disabled_at ON push_subscriptions (disabled_at)"
            )

        expense_item_columns = [row[1] for row in connection.exec_driver_sql("PRAGMA table_info(expense_items)").fetchall()]
        if expense_item_columns and "include_in_analytics" not in expense_item_columns:
            connection.exec_driver_sql("ALTER TABLE expense_items ADD COLUMN include_in_analytics BOOLEAN NOT NULL DEFAULT 1")
        if expense_item_columns and "include_in_forecast" not in expense_item_columns:
            connection.exec_driver_sql("ALTER TABLE expense_items ADD COLUMN include_in_forecast BOOLEAN NOT NULL DEFAULT 1")

        expense_share_columns = [row[1] for row in connection.exec_driver_sql("PRAGMA table_info(expense_list_shares)").fetchall()]
        if expense_share_columns and "can_edit" not in expense_share_columns:
            connection.exec_driver_sql("ALTER TABLE expense_list_shares ADD COLUMN can_edit BOOLEAN NOT NULL DEFAULT 1")

        shopping_share_columns = [row[1] for row in connection.exec_driver_sql("PRAGMA table_info(shopping_list_shares)").fetchall()]
        if shopping_share_columns and "can_edit" not in shopping_share_columns:
            connection.exec_driver_sql("ALTER TABLE shopping_list_shares ADD COLUMN can_edit BOOLEAN NOT NULL DEFAULT 1")

        planner_columns = [row[1] for row in connection.exec_driver_sql("PRAGMA table_info(planner_items)").fetchall()]
        if planner_columns and "end_date" not in planner_columns:
            connection.exec_driver_sql("ALTER TABLE planner_items ADD COLUMN end_date DATE")
        if planner_columns and "recurrence_frequency" not in planner_columns:
            connection.exec_driver_sql("ALTER TABLE planner_items ADD COLUMN recurrence_frequency VARCHAR(12)")
        if planner_columns and "recurrence_interval" not in planner_columns:
            connection.exec_driver_sql("ALTER TABLE planner_items ADD COLUMN recurrence_interval INTEGER NOT NULL DEFAULT 1")
        if planner_columns and "recurrence_until" not in planner_columns:
            connection.exec_driver_sql("ALTER TABLE planner_items ADD COLUMN recurrence_until DATE")

        wishlist_columns = [row[1] for row in connection.exec_driver_sql("PRAGMA table_info(wishlist_items)").fetchall()]
        wishlist_defaults = {
            "priority": "VARCHAR(20) NOT NULL DEFAULT 'medium'",
            "status": "VARCHAR(30) NOT NULL DEFAULT 'want'",
            "goal_amount": "NUMERIC(10, 2)",
            "saved_amount": "NUMERIC(10, 2)",
            "expense_item_id": "INTEGER",
            "expense_prev_status": "VARCHAR(30)",
            "expense_prev_is_done": "BOOLEAN",
        }
        for column_name, column_sql in wishlist_defaults.items():
            if wishlist_columns and column_name not in wishlist_columns:
                connection.exec_driver_sql(f"ALTER TABLE wishlist_items ADD COLUMN {column_name} {column_sql}")

        ai_action_columns = [row[1] for row in connection.exec_driver_sql("PRAGMA table_info(ai_actions)").fetchall()]
        if ai_action_columns and "claim_token" not in ai_action_columns:
            connection.exec_driver_sql("ALTER TABLE ai_actions ADD COLUMN claim_token VARCHAR(36)")
        if ai_action_columns and "claimed_at" not in ai_action_columns:
            connection.exec_driver_sql("ALTER TABLE ai_actions ADD COLUMN claimed_at DATETIME")


def on_startup() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    RECIPE_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    CHAT_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    MOMENT_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    MOMENT_THUMB_DIR.mkdir(parents=True, exist_ok=True)
    SHARED_FILES_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if schema_change_required():
        try:
            create_backup_zip(
                engine=engine,
                data_dir=DATA_DIR,
                backup_dir=BACKUP_DIR,
                prefix="schema",
                include_data_files=False,
                retention=5,
            )
        except (FileNotFoundError, OSError):
            pass
    Base.metadata.create_all(bind=engine)
    ensure_runtime_schema()
    if settings.background_jobs_enabled:
        auto_daily_backup()
        with SessionLocal() as db:
            for startup_user in db.scalars(select(User)).all():
                apply_due_recurring_expenses(db, startup_user)
        if not any(thread.name == "timer-reminders" for thread in threading.enumerate()):
            threading.Thread(target=timer_reminder_loop, name="timer-reminders", daemon=True).start()


def run_planner_push_cycle() -> None:
    keys = ensure_vapid_keys()
    if webpush is None or not keys.get("available"):
        return
    sender = PyWebPushSender(webpush, keys["private_key_path"], keys["subject"])
    with SessionLocal() as db:
        run_delivery_cycle(
            db,
            sender,
            catchup=timedelta(minutes=settings.push_catchup_minutes),
        )


async def planner_push_scheduler() -> None:
    logger.info("Planner push scheduler started interval=%ss", settings.push_poll_seconds)
    try:
        while True:
            try:
                await asyncio.to_thread(run_planner_push_cycle)
            except Exception as exc:
                logger.warning("Planner push scheduler cycle failed: %s", type(exc).__name__)
            await asyncio.sleep(settings.push_poll_seconds)
    finally:
        logger.info("Planner push scheduler stopped")


def run_file_share_cleanup(*, owner_id: int | None = None):
    with SessionLocal() as db:
        summary = cleanup_all(
            db,
            root=SHARED_FILES_DIR,
            now=utc_now_naive(),
            orphan_grace=timedelta(hours=settings.file_share_orphan_grace_hours),
            owner_id=owner_id,
        )
    logger.info(
        "Temporary file cleanup completed transfers=%s files=%s bytes=%s failures=%s orphans=%s",
        summary.transfers_removed, summary.files_removed, summary.bytes_freed, summary.failures, summary.orphans_removed,
    )
    return summary


async def file_share_cleanup_scheduler() -> None:
    logger.info("Temporary file cleanup scheduler started interval=%ss", settings.file_share_cleanup_seconds)
    try:
        while True:
            try:
                await asyncio.to_thread(run_file_share_cleanup)
            except Exception as exc:
                logger.warning("Temporary file cleanup cycle failed: %s", type(exc).__name__)
            await asyncio.sleep(settings.file_share_cleanup_seconds)
    finally:
        logger.info("Temporary file cleanup scheduler stopped")


@asynccontextmanager
async def app_lifespan(_app):
    timer_reminder_stop.clear()
    on_startup()
    push_task = None
    file_cleanup_task = None
    if settings.background_jobs_enabled and settings.push_enabled:
        keys = ensure_vapid_keys()
        if webpush is None:
            logger.error("Planner push unavailable: pywebpush is not installed")
        elif not keys.get("available"):
            logger.error("Planner push unavailable: %s", keys.get("reason", "invalid configuration"))
        else:
            push_task = asyncio.create_task(planner_push_scheduler(), name="planner-push-scheduler")
    if settings.background_jobs_enabled:
        await asyncio.to_thread(run_file_share_cleanup)
        file_cleanup_task = asyncio.create_task(file_share_cleanup_scheduler(), name="file-share-cleanup-scheduler")
    try:
        yield
    finally:
        timer_reminder_stop.set()
        if push_task is not None:
            push_task.cancel()
            with suppress(asyncio.CancelledError):
                await push_task
        if file_cleanup_task is not None:
            file_cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await file_cleanup_task


app.router.lifespan_context = app_lifespan


@app.middleware("http")
async def protect_unsafe_requests(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_BYTES:
                return Response("Request body is too large", status_code=413)
        except ValueError:
            return Response("Invalid Content-Length", status_code=400)
    if settings.background_jobs_enabled:
        auto_daily_backup()
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        fetch_site = request.headers.get("sec-fetch-site", "").lower()
        if fetch_site in {"cross-site", "same-site"}:
            return Response("Invalid request origin", status_code=403)
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin:
            if not same_origin(origin, request.headers.get("host", "")):
                return Response("Invalid request origin", status_code=403)
        elif settings.enforce_same_origin:
            return Response("Missing request origin", status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; connect-src 'self' ws: wss:; font-src 'self'; object-src 'none'; "
        "base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
    )
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def render(request: Request, template: str, context: dict):
    context.setdefault("user", None)
    context.setdefault("registration_enabled", settings.registration_enabled)
    context.setdefault("home_ai_enabled", settings.home_ai_enabled)
    push_keys = ensure_vapid_keys()
    context.setdefault("push_feature_enabled", settings.push_enabled)
    context.setdefault("push_available", bool(push_keys.get("available") and webpush is not None))
    context.setdefault("push_unavailable_reason", push_keys.get("reason") or "Web Push library unavailable")
    user = context["user"]
    raw_palette = load_palette(user.ui_palette_json) if user else {}
    context.setdefault("ui_palette", raw_palette)
    context.setdefault("ui_gradient", load_gradient(user.ui_palette_json) if user else {})
    return templates.TemplateResponse(request, template, context)


def same_origin(origin: str, host: str) -> bool:
    parsed = urlparse(origin)
    return bool(parsed.scheme in {"http", "https"} and parsed.netloc.lower().rstrip(".") == host.lower().rstrip("."))


def auth_attempt_key(request: Request, username: str = "") -> str:
    address = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    try:
        proxy_address = ipaddress.ip_address(address)
        forwarded_address = ipaddress.ip_address(forwarded) if forwarded else None
    except ValueError:
        proxy_address = None
        forwarded_address = None
    if forwarded_address and proxy_address and (proxy_address.is_private or proxy_address.is_loopback):
        address = str(forwarded_address)
    return f"{address}:{normalize_username(username) or '-'}"


def check_auth_rate_limit(key: str) -> None:
    now = utc_now_naive()
    with AUTH_ATTEMPTS_LOCK:
        attempts = [point for point in AUTH_ATTEMPTS.get(key, []) if now - point < timedelta(minutes=15)]
        if attempts:
            AUTH_ATTEMPTS[key] = attempts
        else:
            AUTH_ATTEMPTS.pop(key, None)
        if len(attempts) >= 10:
            raise HTTPException(status_code=429, detail="Слишком много неудачных попыток. Попробуйте через 15 минут.")


def record_auth_failure(key: str) -> None:
    with AUTH_ATTEMPTS_LOCK:
        if key not in AUTH_ATTEMPTS and len(AUTH_ATTEMPTS) >= AUTH_ATTEMPTS_MAX_KEYS:
            now = utc_now_naive()
            stale_keys = [
                attempt_key
                for attempt_key, points in AUTH_ATTEMPTS.items()
                if not points or now - points[-1] >= timedelta(minutes=15)
            ]
            for attempt_key in stale_keys:
                AUTH_ATTEMPTS.pop(attempt_key, None)
            while len(AUTH_ATTEMPTS) >= AUTH_ATTEMPTS_MAX_KEYS:
                AUTH_ATTEMPTS.pop(next(iter(AUTH_ATTEMPTS)))
        AUTH_ATTEMPTS[key].append(utc_now_naive())


def clear_auth_failures(key: str) -> None:
    with AUTH_ATTEMPTS_LOCK:
        AUTH_ATTEMPTS.pop(key, None)


def is_backup_admin(user: User) -> bool:
    configured = normalize_username(os.getenv("BACKUP_ADMIN_USERNAME", ""))
    return bool(configured and normalize_username(user.username) == configured)


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=status.HTTP_303_SEE_OTHER)


def redirect_notice(path: str, message: str) -> RedirectResponse:
    separator = "&" if "?" in path else "?"
    return redirect(f"{path}{separator}notice={quote(message)}")


def parse_money(value: str | None) -> Decimal | None:
    if value is None or value.strip() == "":
        return None
    normalized = value.strip().replace(",", ".")
    if len(normalized) > 24:
        raise HTTPException(status_code=400, detail="Некорректная сумма")
    try:
        number = Decimal(normalized)
        if not number.is_finite() or abs(number) > Decimal("99999999.99"):
            raise InvalidOperation
        return number.quantize(Decimal("0.01"))
    except InvalidOperation:
        raise HTTPException(status_code=400, detail="Некорректная сумма")


def require_positive_money(value: str | None, field_name: str = "Сумма") -> Decimal:
    number = parse_money(value)
    if number is None or number <= 0:
        raise HTTPException(status_code=400, detail=f"Поле «{field_name}» должно быть больше нуля")
    return number


def optional_nonnegative_money(value: str | None, field_name: str) -> Decimal | None:
    number = parse_money(value)
    if number is not None and number < 0:
        raise HTTPException(status_code=400, detail=f"Поле «{field_name}» не может быть отрицательным")
    return number


def clean_required_text(value: str, field_name: str, max_length: int) -> str:
    clean = value.strip()
    if not clean:
        raise HTTPException(status_code=400, detail=f"Поле «{field_name}» обязательно")
    return clean[:max_length]


def clean_optional_text(value: Any, max_length: int) -> str | None:
    clean = _clean_optional_string(value)
    return clean[:max_length] if clean else None


def parse_import_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def parse_optional_int(
    value: str | None,
    field_name: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    if value is None or value.strip() == "":
        return None
    if len(value.strip()) > 24:
        raise HTTPException(status_code=400, detail=f"Поле «{field_name}» должно быть числом")
    try:
        number = int(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Поле «{field_name}» должно быть целым числом")
    if minimum is not None and number < minimum:
        raise HTTPException(status_code=400, detail=f"Поле «{field_name}» должно быть не меньше {minimum}")
    if maximum is not None and number > maximum:
        raise HTTPException(status_code=400, detail=f"Поле «{field_name}» должно быть не больше {maximum}")
    return number


def parse_optional_decimal(
    value: str | None,
    field_name: str,
    minimum: Decimal | None = None,
) -> Decimal | None:
    if value is None or value.strip() == "":
        return None
    normalized = value.strip().replace(",", ".")
    if len(normalized) > 24:
        raise HTTPException(status_code=400, detail=f"Поле «{field_name}» должно быть числом")
    try:
        number = Decimal(normalized)
        if not number.is_finite():
            raise InvalidOperation
    except InvalidOperation:
        raise HTTPException(status_code=400, detail=f"Поле «{field_name}» должно быть числом")
    if minimum is not None and number < minimum:
        raise HTTPException(status_code=400, detail=f"Поле «{field_name}» должно быть не меньше {format_decimal(minimum)}")
    return number.quantize(Decimal("0.01"))


def format_decimal(value: Any) -> str:
    if value is None:
        return ""
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    if number == number.to_integral_value():
        return str(int(number))
    text = format(number.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(".", ",")


templates.env.filters["fmt_decimal"] = format_decimal


_URL_RE = re.compile(r"(https?://[^\s<]+)")
_LINK_TEMPLATE = Markup('<a href="{}" target="_blank" rel="noopener">{}</a>')

def linkify_text(value: Any) -> Markup:
    text = "" if value is None else str(value)
    parts = []
    last = 0
    for match in _URL_RE.finditer(text):
        parts.append(escape(text[last:match.start()]))
        url = match.group(1).rstrip('.,);]')
        trailing = match.group(1)[len(url):]
        parts.append(_LINK_TEMPLATE.format(url, url))
        parts.append(escape(trailing))
        last = match.end()
    parts.append(escape(text[last:]))
    return Markup("").join(parts)

templates.env.filters["linkify"] = linkify_text


def clean_http_url(value: Any, field_name: str = "Ссылка", max_length: int = 700) -> str | None:
    clean = _clean_optional_string(value)
    if clean is None:
        return None
    if len(clean) > max_length:
        raise HTTPException(status_code=400, detail=f"Поле «{field_name}» слишком длинное")
    parsed = urlparse(clean)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail=f"Поле «{field_name}» должно содержать безопасную http(s)-ссылку")
    return clean


def push_endpoint_is_allowed(endpoint: str) -> bool:
    try:
        parsed = urlparse(endpoint)
        port = parsed.port
    except ValueError:
        return False
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
        or not parsed.path
    ):
        return False
    return any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in PUSH_ENDPOINT_HOST_SUFFIXES)


def safe_http_url(value: Any) -> str:
    try:
        return clean_http_url(value) or ""
    except HTTPException:
        return ""


templates.env.filters["http_url"] = safe_http_url


def parse_date(value: str | None, default: date | None = None) -> date | None:
    if value is None or value.strip() == "":
        return default
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="Некорректная дата")


WISHLIST_PRIORITY_LABELS = {"low": "низкий", "medium": "средний", "high": "высокий"}
WISHLIST_STATUS_LABELS = {
    "want": "хочу",
    "saving": "коплю",
    "bought": "куплено",
    "paused": "отложено",
    "declined": "передумал",
}


def parse_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC_TZ).replace(tzinfo=None)
    return parsed


def wishlist_status_label(value: str | None) -> str:
    return WISHLIST_STATUS_LABELS.get(value or "", value or "")


def wishlist_priority_label(value: str | None) -> str:
    return WISHLIST_PRIORITY_LABELS.get(value or "", value or "")


templates.env.filters["wish_status"] = wishlist_status_label
templates.env.filters["wish_priority"] = wishlist_priority_label

WATCH_KIND_LABELS = {"movie": "фильм", "series": "сериал"}
WATCH_STATUS_LABELS = {"planned": "смотреть", "watching": "смотрю", "done": "готово", "paused": "пауза"}


def watch_kind_label(value: str | None) -> str:
    return WATCH_KIND_LABELS.get(value or "", value or "")


def watch_status_label(value: str | None) -> str:
    return WATCH_STATUS_LABELS.get(value or "", value or "")


templates.env.filters["watch_kind"] = watch_kind_label
templates.env.filters["watch_status"] = watch_status_label

CATEGORY_COLORS = [
    "#2563eb",
    "#16a34a",
    "#f97316",
    "#dc2626",
    "#7c3aed",
    "#0891b2",
    "#ca8a04",
    "#db2777",
    "#4f46e5",
    "#059669",
    "#ea580c",
    "#9333ea",
]


def category_color_map(category_names: list[str]) -> dict[str, str]:
    return {name: CATEGORY_COLORS[index % len(CATEGORY_COLORS)] for index, name in enumerate(category_names)}


def week_start_for(day: date) -> date:
    return day - timedelta(days=day.weekday())


def parse_recipe_steps(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    text = raw.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        if isinstance(data, list):
            result = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                step_text = str(item.get("text", "")).strip()
                if not step_text:
                    continue
                minutes = item.get("minutes")
                try:
                    minutes_value = int(minutes) if minutes not in (None, "") else None
                except (TypeError, ValueError):
                    minutes_value = None
                result.append({"text": step_text, "minutes": minutes_value})
            if result:
                return result
    except json.JSONDecodeError:
        pass

    result = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        cleaned = line
        if ". " in cleaned[:5]:
            cleaned = cleaned.split(". ", 1)[1].strip()
        result.append({"text": cleaned, "minutes": None})
    return result


def serialize_recipe_steps(step_text: list[str] | None, step_minutes: list[str] | None) -> str:
    step_text = step_text or []
    step_minutes = step_minutes or []
    if len(step_text) > 100:
        raise HTTPException(status_code=400, detail="В рецепте не может быть больше 100 этапов")
    result = []
    for index, text_value in enumerate(step_text):
        clean_text = text_value.strip()
        if not clean_text:
            continue
        minutes_raw = step_minutes[index] if index < len(step_minutes) else ""
        minutes_value = parse_optional_int(minutes_raw, "Длительность этапа", 0, 10_000)
        result.append({"text": clean_text[:5000], "minutes": minutes_value})
    if not result:
        raise HTTPException(status_code=400, detail="Добавь хотя бы один этап приготовления")
    return json.dumps(result, ensure_ascii=False)


def recipe_steps_for_form(recipe: Recipe | None) -> list[dict[str, Any]]:
    if not recipe:
        return [{"text": "", "minutes": None}]
    return parse_recipe_steps(recipe.steps) or [{"text": "", "minutes": None}]


def cooking_steps_payload(recipe: Recipe, ingredients_text: str | None = None) -> list[dict[str, Any]]:
    steps = [{"title": "Подготовьте ингредиенты", "text": ingredients_text or recipe.ingredients, "minutes": None}]
    for index, step in enumerate(parse_recipe_steps(recipe.steps), start=1):
        steps.append({"title": f"Этап {index}", "text": step["text"], "minutes": step.get("minutes")})
    return steps


def cooking_timer_seconds(timer: RecipeCookingTimer | None) -> int:
    if not timer:
        return 0
    elapsed = max(0, int(timer.elapsed_seconds or 0))
    if timer.is_running and timer.started_at:
        started_at = timer.started_at
        if started_at.tzinfo is not None:
            started_at = started_at.astimezone(UTC_TZ).replace(tzinfo=None)
        elapsed += max(0, int((utc_now_naive() - started_at).total_seconds()))
    return elapsed


def cooking_timer_minutes(seconds: int) -> int:
    if seconds <= 0:
        return 0
    return max(1, (seconds + 59) // 60)


def cooking_timer_status(timer: RecipeCookingTimer | None) -> dict[str, Any]:
    seconds = cooking_timer_seconds(timer)
    return {
        "exists": timer is not None,
        "is_running": bool(timer and timer.is_running),
        "seconds": seconds,
        "minutes": cooking_timer_minutes(seconds),
        "started_at": format_msk_datetime(timer.started_at) if timer and timer.started_at else "",
        "stopped_at": format_msk_datetime(timer.stopped_at) if timer and timer.stopped_at else "",
    }


def get_or_create_cooking_timer(db: Session, user_id: int, recipe_id: int) -> RecipeCookingTimer:
    timer = db.scalar(
        select(RecipeCookingTimer).where(
            RecipeCookingTimer.owner_id == user_id,
            RecipeCookingTimer.recipe_id == recipe_id,
        )
    )
    if timer:
        return timer
    timer = RecipeCookingTimer(owner_id=user_id, recipe_id=recipe_id, elapsed_seconds=0, is_running=False)
    db.add(timer)
    db.flush()
    return timer


def send_overdue_timer_reminders() -> None:
    now = utc_now_naive()
    with SessionLocal() as db:
        timers = db.scalars(
            select(RecipeCookingTimer)
            .options(selectinload(RecipeCookingTimer.recipe))
            .where(RecipeCookingTimer.is_running.is_(True))
        ).all()
        for timer in timers:
            if cooking_timer_seconds(timer) < TIMER_REMINDER_MINUTES * 60:
                continue
            if timer.last_reminded_at and now - timer.last_reminded_at < timedelta(hours=2):
                continue
            recipe_title = timer.recipe.title if timer.recipe else "Рецепт"
            send_push_to_user(db, timer.owner_id, {
                "title": "Таймер всё ещё идёт",
                "body": f"{recipe_title}: прошло уже {cooking_timer_minutes(cooking_timer_seconds(timer))} мин.",
                "url": f"/recipes/{timer.recipe_id}",
                "tag": f"timer-{timer.id}",
                "icon": "/static/icon-192.png",
                "badge": "/static/icon-192.png",
            })
            timer.last_reminded_at = now
        db.commit()


def timer_reminder_loop() -> None:
    while not timer_reminder_stop.wait(60):
        try:
            send_overdue_timer_reminders()
        except Exception as exc:
            logger.warning("Timer reminder failed: %s", type(exc).__name__)


def _decimal_from_token(token: str) -> Decimal | None:
    clean = token.strip().replace(" ", "")
    if "/" in clean:
        left, right = clean.split("/", 1)
        try:
            denominator = Decimal(right.replace(",", "."))
            if denominator == 0:
                return None
            return Decimal(left.replace(",", ".")) / denominator
        except InvalidOperation:
            return None
    try:
        return Decimal(clean.replace(",", "."))
    except InvalidOperation:
        return None


def _format_scaled_number(value: Decimal) -> str:
    rounded = value.quantize(Decimal("0.01"))
    if rounded == rounded.to_integral_value():
        return str(int(rounded))
    text = format(rounded.normalize(), "f").rstrip("0").rstrip(".")
    return text.replace(".", ",")


_AMOUNT_PATTERN = re.compile(r"(?<![\w])(?P<number>\d+\s*/\s*\d+|\d+(?:[,.]\d+)?)(?![\w%])")


def scale_amount_text(text: str, ratio: Decimal) -> str:
    def replace(match: re.Match[str]) -> str:
        value = _decimal_from_token(match.group("number"))
        if value is None:
            return match.group("number")
        return _format_scaled_number(value * ratio)

    return _AMOUNT_PATTERN.sub(replace, text)


def scaled_ingredients_payload(raw: str, ratio: Decimal | None) -> list[dict[str, str | bool]]:
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return []

    result: list[dict[str, str | bool]] = []
    for line in lines:
        if ratio is None:
            scaled = line
        else:
            scaled = line
            for delimiter in (" — ", " – ", " - ", ": "):
                if delimiter in line:
                    name, amount = line.split(delimiter, 1)
                    scaled = f"{name}{delimiter}{scale_amount_text(amount, ratio)}"
                    break
            else:
                scaled = scale_amount_text(line, ratio)
        result.append({"original": line, "scaled": scaled, "changed": scaled != line})
    return result


def ingredients_text_from_payload(items: list[dict[str, str | bool]]) -> str:
    return "\n".join(str(item["scaled"]) for item in items)


RECIPE_JSON_PROMPT = """Ты должен преобразовать рецепт в строго валидный JSON для импорта в домашний веб-сервис.

На входе может быть ссылка на сайт с рецептом или текст рецепта. Верни только JSON без Markdown, без пояснений и без комментариев.

Формат JSON:
{
  "title": "Название рецепта",
  "source_url": "Ссылка на источник или null",
  "cook_time_minutes": 45,
  "cost": null,
  "servings": 4,
  "tags": ["быстро", "ужин"],
  "ingredients": [
    {"name": "ингредиент", "amount": "количество"}
  ],
  "steps": [
    {"text": "Описание этапа приготовления", "minutes": 10}
  ]
}

Правила:
- JSON должен быть валидным: двойные кавычки, без trailing comma.
- title обязателен.
- source_url укажи ссылкой, если она есть; иначе null.
- cook_time_minutes — общее время в минутах; если неизвестно, null.
- cost — примерная стоимость в рублях числом; если неизвестно, null.
- servings — количество порций числом; можно дробным, например 2.5; если неизвестно, null.
- tags — массив коротких тегов, например ["быстро", "дёшево", "ужин"].
- ingredients — массив объектов с name и amount. Если количество неизвестно, amount = "по вкусу" или "не указано".
- steps — массив этапов по порядку. Каждый этап должен быть понятным и коротким.
- minutes у этапа — длительность этапа в минутах; если неизвестно, null.
- Не добавляй этап "подготовьте ингредиенты": сервис добавит его сам.

Источник или текст рецепта:
[ВСТАВЬ СЮДА ССЫЛКУ ИЛИ РЕЦЕПТ]
"""



def recipe_json_prompt_text() -> str:
    return RECIPE_JSON_PROMPT


def image_signature_matches(data: bytes, extension: str) -> bool:
    if extension in {".jpg", ".jpeg"}:
        return data.startswith(b"\xff\xd8\xff")
    if extension == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if extension == ".gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if extension == ".webp":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    return False


def save_image_upload(upload: UploadFile | None, target_dir: Path, url_prefix: str) -> str | None:
    if upload is None or not upload.filename:
        return None
    source_name = Path(upload.filename).name
    extension = Path(source_name).suffix.lower()
    allowed_extensions = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    allowed_content_types = {"image/jpeg", "image/pjpeg", "image/png", "image/webp", "image/gif"}
    content_type = (upload.content_type or "").lower()
    if extension not in allowed_extensions or (content_type and content_type not in allowed_content_types and content_type != "application/octet-stream"):
        raise HTTPException(status_code=400, detail="Фото должно быть JPG, PNG, WEBP или GIF")

    file_name = f"{uuid.uuid4().hex}{extension}"
    target = target_dir / file_name
    written = 0
    first_chunk = b""
    upload.file.seek(0)
    with target.open("wb") as destination:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            if not first_chunk:
                first_chunk = chunk[:32]
            written += len(chunk)
            if written > 8 * 1024 * 1024:
                destination.close()
                target.unlink(missing_ok=True)
                raise HTTPException(status_code=400, detail="Фото не должно быть больше 8 МБ")
            destination.write(chunk)
    if not first_chunk or not image_signature_matches(first_chunk, extension):
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Содержимое файла не соответствует формату изображения")
    return f"{url_prefix}/{file_name}"


def save_recipe_image(upload: UploadFile | None) -> str | None:
    return save_image_upload(upload, RECIPE_MEDIA_DIR, "/media/recipes")


def save_moment_photo(upload: UploadFile | None) -> str | None:
    return save_image_upload(upload, MOMENT_MEDIA_DIR, "/media/moments")


def moment_thumbnail_name(photo_path: str) -> str:
    return f"{Path(photo_path).stem}.jpg"


def moment_thumbnail_path(photo_path: str) -> Path:
    if not photo_path.startswith("/media/moments/"):
        raise ValueError("Unsafe moment photo path")
    target = (MOMENT_THUMB_DIR / moment_thumbnail_name(photo_path)).resolve()
    if target.parent != MOMENT_THUMB_DIR.resolve():
        raise ValueError("Unsafe moment thumbnail path")
    return target


def ensure_moment_thumbnail(photo_path: str) -> Path:
    """Create a persistent, EXIF-corrected calendar thumbnail once on demand."""
    source_name = Path(photo_path).name
    source = (MOMENT_MEDIA_DIR / source_name).resolve()
    if source.parent != MOMENT_MEDIA_DIR.resolve() or not source.is_file():
        raise FileNotFoundError(source_name)
    target = moment_thumbnail_path(photo_path)
    if target.is_file():
        return target
    MOMENT_THUMB_DIR.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    try:
        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail(MOMENT_THUMB_SIZE)
            if image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            image.save(temporary, format="JPEG", quality=82, optimize=True)
        temporary.replace(target)
    except (OSError, UnidentifiedImageError):
        temporary.unlink(missing_ok=True)
        raise
    return target


def delete_moment_photo(photo_path: str | None) -> None:
    if photo_path:
        try:
            moment_thumbnail_path(photo_path).unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
    delete_media_file(photo_path)


def delete_media_file(image_path: str | None) -> None:
    if not image_path or not image_path.startswith("/media/"):
        return
    relative = image_path.removeprefix("/media/").lstrip("/")
    media_root = MEDIA_DIR.resolve()
    target = (MEDIA_DIR / relative).resolve()
    if media_root != target.parent and media_root not in target.parents:
        return
    try:
        target.unlink(missing_ok=True)
    except OSError:
        pass


def delete_recipe_image(image_path: str | None) -> None:
    delete_media_file(image_path)


def save_chat_attachment(upload: UploadFile | None) -> str | None:
    if upload is None or not upload.filename:
        return None
    source_name = Path(upload.filename).name
    extension = Path(source_name).suffix.lower()
    allowed = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".pdf", ".txt", ".csv"}
    if extension not in allowed:
        raise HTTPException(status_code=400, detail="Файл должен быть JPG, PNG, WEBP, GIF, PDF, TXT или CSV")
    file_name = f"{uuid.uuid4().hex}{extension}"
    target = CHAT_MEDIA_DIR / file_name
    max_size = 12 * 1024 * 1024
    written = 0
    first_chunk = b""
    upload.file.seek(0)
    with target.open("wb") as destination:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            if not first_chunk:
                first_chunk = chunk[:4096]
            written += len(chunk)
            if written > max_size:
                destination.close()
                target.unlink(missing_ok=True)
                raise HTTPException(status_code=400, detail="Файл не должен быть больше 12 МБ")
            destination.write(chunk)
    valid_content = True
    if extension in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        valid_content = image_signature_matches(first_chunk, extension)
    elif extension == ".pdf":
        valid_content = first_chunk.startswith(b"%PDF-")
    elif extension in {".txt", ".csv"}:
        valid_content = b"\x00" not in first_chunk
    if not first_chunk or not valid_content:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Содержимое файла не соответствует его расширению")
    return f"/media/chats/{file_name}"


def split_ingredient_line(line: str) -> tuple[str, str | None]:
    clean = line.strip().lstrip("-•* ").strip()
    clean = re.sub(r"^\d+[.)]\s*", "", clean)
    if not clean:
        return "", None
    for delimiter in (" — ", " – ", " - ", ": "):
        if delimiter in clean:
            name, amount = clean.split(delimiter, 1)
            return name.strip(), amount.strip() or None
    match = re.match(r"^(?P<name>.*?)(?:\s+)(?P<amount>\d+[\d\s,./]*\s*\D*)$", clean)
    if match and len(match.group("name").strip()) >= 2:
        return match.group("name").strip(), match.group("amount").strip()
    return clean, None


SHOPPING_DEPARTMENT_RULES: list[tuple[str, list[str]]] = [
    ("Мясо", [
        "кур", "цыплен", "бедро", "голень", "грудк", "филе кур", "свин", "шея", "карбонад", "лопатк",
        "гов", "теля", "фарш", "индей", "мяс", "бекон", "ветчин", "колбас", "сосиск", "сардель",
        "котлет", "ребр", "печень", "сердц", "желуд", "утк", "гус", "баранин", "корейк", "грудинк",
        "шашлык", "купат", "пельмени мяс", "наггетс"
    ]),
    ("Рыба и морепродукты", [
        "рыб", "минтай", "лосос", "сёмг", "семг", "форел", "тунец", "треск", "скумбр", "сельд",
        "хек", "горбуш", "кета", "палтус", "камбал", "сардин", "анчоус", "кревет", "мидии", "кальмар",
        "краб", "икра", "морепр", "устриц", "осьминог", "угорь", "филе рыб", "роллмопс"
    ]),
    ("Овощи", [
        "лук", "морков", "карто", "томат", "помид", "огур", "перец", "капуст", "чеснок", "зелень",
        "укроп", "петруш", "кинз", "базилик", "салат", "баклаж", "кабач", "цукини", "тыкв", "свекл",
        "редис", "реп", "дайкон", "шпинат", "руккол", "сельдер", "броккол", "цветная", "авокадо", "кукуруз",
        "фасоль струч", "горошек", "имбир", "чили", "порей", "шампин", "гриб", "вешенк", "опят", "лисичк"
    ]),
    ("Фрукты и ягоды", [
        "яблок", "груш", "банан", "апельс", "мандар", "лимон", "лайм", "грейпфрут", "виноград", "киви",
        "персик", "нектар", "абрикос", "слив", "ананас", "манго", "гранат", "арбуз", "дын", "ягод",
        "клубник", "землян", "малин", "черник", "голубик", "ежевик", "смород", "клюкв", "брусник", "вишн", "черешн"
    ]),
    ("Молочка и яйца", [
        "молок", "сыр", "сметан", "слив", "творог", "йогур", "кефир", "ряжен", "простокваш", "масло слив",
        "моцарел", "пармез", "чеддер", "гауда", "фета", "брынз", "рикот", "маскарп", "плавлен", "сулугуни",
        "яйц", "желт", "белок", "омлет", "морожен", "сгущ", "топлен", "айран"
    ]),
    ("Крупы, макароны и хлеб", [
        "рис", "греч", "булгур", "киноа", "перлов", "пшен", "ячнев", "манк", "овся", "геркулес",
        "хлоп", "кускус", "нут", "чечев", "горох", "фасоль", "макарон", "паста", "спагет", "лапш",
        "вермиш", "рожк", "мука", "крахмал", "хлеб", "батон", "лаваш", "пита", "тортиль", "булоч",
        "сухар", "паниров", "крошк", "тесто", "дрожж", "разрыхл", "блины", "вафл"
    ]),
    ("Специи и приправы", [
        "соль", "перец", "паприк", "карри", "спец", "приправа", "лавр", "чеснок суш", "зира", "кумин",
        "кориандр", "куркум", "орегано", "тимьян", "розмар", "мускат", "корица", "ванил", "кардамон",
        "гвоздик", "хмели", "шафран", "сумак", "чили", "сухой чеснок", "сухой лук", "итальянские", "прованские"
    ]),
    ("Соусы и консервы", [
        "соус", "кетчуп", "майонез", "горчиц", "аджик", "ткемали", "сальса", "песто", "томатная паста",
        "паста томат", "консерв", "оливк", "маслин", "каперс", "кукуруза конс", "горошек конс", "фасоль конс",
        "тунец конс", "сайра", "шпрот", "лечо", "маринад", "солень", "огурцы мар", "перец мар"
    ]),
    ("Бакалея", [
        "масло раст", "подсолнеч", "оливков", "кунжут", "сахар", "пудра", "мёд", "мед", "уксус", "сода",
        "какао", "шоколад", "кофе", "чай", "сироп", "джем", "варенье", "желатин", "агар", "орех",
        "семеч", "кунжут", "изюм", "курага", "чернослив", "финик", "кокос", "чипсы", "сухофрукт"
    ]),
    ("Заморозка", [
        "заморож", "замороз", "пельм", "вареник", "хинкали", "наггет", "овощная смесь", "ягоды зам", "картофель фри"
    ]),
    ("Напитки", [
        "вода", "сок", "морс", "лимонад", "кола", "газиров", "квас", "компот", "энергетик", "молочный коктейль"
    ]),
    ("Бытовое", [
        "губк", "салфет", "пакет", "плёнк", "пленк", "фольг", "бумага", "пергамент", "моющ", "средство",
        "мыло", "шампун", "порошок", "капсул", "таблетки для посуд", "перчатк", "мешки", "мусор"
    ]),
]


def normalize_shopping_keyword(value: str) -> str:
    text = value.lower().replace("ё", "е")
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\b\d+[\d\s,./]*\s*(?:г|гр|кг|мл|л|шт|штук|ч\.?\s*л\.?|ст\.?\s*л\.?)\b", " ", text)
    text = re.sub(r"[^a-zа-я0-9\s-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def remember_price_history(db: Session, user: User, item: ShoppingItem, price: Decimal | None) -> None:
    if price is None:
        return
    normalized = normalize_shopping_keyword(item.title)
    if not normalized:
        return
    db.add(
        ShoppingPriceHistory(
            owner_id=user.id,
            shopping_item_id=item.id,
            title=item.title.strip(),
            normalized_title=normalized[:180],
            amount=item.amount,
            department=item.department,
            price=price,
        )
    )


def latest_price_index(db: Session, user: User) -> dict[str, ShoppingPriceHistory]:
    rows = db.scalars(
        select(ShoppingPriceHistory)
        .where(ShoppingPriceHistory.owner_id == user.id)
        .order_by(desc(ShoppingPriceHistory.created_at), desc(ShoppingPriceHistory.id))
    ).all()
    result: dict[str, ShoppingPriceHistory] = {}
    for row in rows:
        result.setdefault(row.normalized_title, row)
    return result


def recipe_price_estimate(db: Session, user: User, recipe: Recipe, ratio: Decimal | None = None) -> dict[str, Any]:
    prices = latest_price_index(db, user)
    rows: list[dict[str, Any]] = []
    total = Decimal("0.00")
    matched = 0
    for ingredient in scaled_ingredients_payload(recipe.ingredients, ratio):
        name, amount = split_ingredient_line(str(ingredient["scaled"]))
        normalized = normalize_shopping_keyword(name)
        price = prices.get(normalized)
        if not price:
            for key, candidate in prices.items():
                if normalized and (normalized in key or key in normalized):
                    price = candidate
                    break
        rows.append({"title": name or str(ingredient["scaled"]), "amount": amount, "price": price})
        if price:
            total += Decimal(str(price.price))
            matched += 1
    return {"rows": rows, "total": total.quantize(Decimal("0.01")), "matched": matched}


def user_category_rules(db: Session | None, user: User | None) -> list[ShoppingCategoryRule]:
    if db is None or user is None:
        return []
    return db.scalars(
        select(ShoppingCategoryRule)
        .where(ShoppingCategoryRule.owner_id == user.id)
        .order_by(func.length(ShoppingCategoryRule.keyword).desc(), ShoppingCategoryRule.keyword)
    ).all()


def guess_department(title: str, db: Session | None = None, user: User | None = None) -> str:
    text = normalize_shopping_keyword(title)
    if not text:
        return "Прочее"

    for rule in user_category_rules(db, user):
        keyword = normalize_shopping_keyword(rule.keyword)
        if keyword and (keyword == text or keyword in text or text in keyword):
            return rule.department

    for department, words in SHOPPING_DEPARTMENT_RULES:
        if any(word.replace("ё", "е") in text for word in words):
            return department
    return "Прочее"


def remember_shopping_department(db: Session, user: User, title: str, department: str) -> None:
    clean_department = department.strip() or "Прочее"
    keyword = normalize_shopping_keyword(title)
    if not keyword:
        return
    # Слишком длинные строки режем, чтобы правило оставалось читаемым.
    keyword = keyword[:120]
    existing = db.scalar(select(ShoppingCategoryRule).where(ShoppingCategoryRule.owner_id == user.id, ShoppingCategoryRule.keyword == keyword))
    if existing:
        existing.department = clean_department
    else:
        db.add(ShoppingCategoryRule(owner_id=user.id, keyword=keyword, department=clean_department))

def ingredients_to_shopping_items(raw: str, ratio: Decimal | None = None, db: Session | None = None, user: User | None = None) -> list[dict[str, str | None]]:
    payload = scaled_ingredients_payload(raw, ratio)
    result = []
    for item in payload:
        name, amount = split_ingredient_line(str(item["scaled"]))
        if not name:
            continue
        result.append({"title": name, "amount": amount, "department": guess_department(name, db, user)})
    return result


def shopping_department_options(db: Session, user: User, extra: list[str] | None = None) -> list[str]:
    departments = {department for department, _ in SHOPPING_DEPARTMENT_RULES}
    departments.update(rule.department for rule in user_category_rules(db, user) if rule.department)
    if extra:
        departments.update(one.strip() for one in extra if one and one.strip())
    departments.add("Прочее")
    return sorted(departments, key=str.casefold)


def resolve_department_choice(department: str | None, department_custom: str | None = None) -> str:
    custom = (department_custom or "").strip()
    selected = (department or "").strip()
    if selected == "__custom__":
        return custom or "Прочее"
    return selected or custom


def require_shopping_access(db: Session, list_id: int, user: User, write: bool = False) -> ShoppingList:
    shopping_list = db.scalar(
        select(ShoppingList)
        .options(
            selectinload(ShoppingList.owner),
            selectinload(ShoppingList.shares).selectinload(ShoppingListShare.user),
            selectinload(ShoppingList.items),
        )
        .where(ShoppingList.id == list_id)
    )
    if not shopping_list:
        raise HTTPException(status_code=404, detail="Список покупок не найден")
    is_owner = shopping_list.owner_id == user.id
    share = next((share for share in shopping_list.shares if share.user_id == user.id), None)
    is_shared = share is not None
    if not is_owner and not is_shared:
        raise HTTPException(status_code=403, detail="Нет доступа к списку покупок")
    if write and not is_owner and not share.can_edit:
        raise HTTPException(status_code=403, detail="Read-only access")
    return shopping_list


def find_or_create_expense_category(db: Session, expense_list: ExpenseList, name: str) -> ExpenseCategory:
    clean = name.strip() or "Прочее"
    for category in expense_list.categories:
        if category.name.lower() == clean.lower():
            return category
    category = ExpenseCategory(expense_list_id=expense_list.id, name=clean)
    db.add(category)
    db.flush()
    return category


def month_bounds(day: date) -> tuple[date, date]:
    start = day.replace(day=1)
    if start.month == 12:
        next_month = start.replace(year=start.year + 1, month=1)
    else:
        next_month = start.replace(month=start.month + 1)
    return start, next_month - timedelta(days=1)


def expense_category_options(db: Session, user: User) -> list[str]:
    names: set[str] = set()
    for expense_list in accessible_expense_lists(db, user):
        for category in expense_list.categories:
            clean = category.name.strip()
            if clean:
                names.add(clean)
    names.update({limit.category_name for limit in db.scalars(select(ExpenseLimit).where(ExpenseLimit.owner_id == user.id)).all() if limit.category_name})
    return sorted(names, key=str.casefold)


def ensure_limit_category_in_owned_lists(db: Session, user: User, category_name: str) -> int:
    clean = category_name.strip()
    if not clean:
        return 0
    lists = db.scalars(
        select(ExpenseList)
        .options(selectinload(ExpenseList.categories))
        .where(ExpenseList.owner_id == user.id)
    ).all()
    created = 0
    for expense_list in lists:
        exists = any(category.name.strip().casefold() == clean.casefold() for category in expense_list.categories)
        if not exists:
            db.add(ExpenseCategory(expense_list_id=expense_list.id, name=clean))
            created += 1
    return created


def seed_expense_list_limit_categories(db: Session, user: User, expense_list: ExpenseList) -> None:
    limits = db.scalars(select(ExpenseLimit).where(ExpenseLimit.owner_id == user.id).order_by(ExpenseLimit.category_name)).all()
    existing = {category.name.strip().casefold() for category in expense_list.categories}
    for limit in limits:
        clean = limit.category_name.strip()
        if clean and clean.casefold() not in existing:
            db.add(ExpenseCategory(expense_list_id=expense_list.id, name=clean))
            existing.add(clean.casefold())


def generate_backup_zip() -> Path:
    return create_backup_zip(
        engine=engine,
        data_dir=DATA_DIR,
        backup_dir=BACKUP_DIR,
        retention=BACKUP_RETENTION_COUNT,
    )


def auto_daily_backup() -> None:
    try:
        db_path = sqlite_database_path(engine)
        if db_path is None or not db_path.exists():
            return
        latest = max(BACKUP_DIR.glob("backup_*.zip"), key=lambda p: p.stat().st_mtime, default=None)
        if latest and datetime.now().timestamp() - latest.stat().st_mtime < 23 * 60 * 60:
            return
        generate_backup_zip()
    except (FileNotFoundError, OSError):
        pass


def apply_due_recurring_expenses(db: Session, user: User, today: date | None = None) -> int:
    current_day = today or msk_today()
    current_month = current_day.strftime("%Y-%m")
    recurring = db.scalars(select(RecurringExpense).options(selectinload(RecurringExpense.expense_list).selectinload(ExpenseList.categories)).where(RecurringExpense.owner_id == user.id, RecurringExpense.is_active.is_(True))).all()
    added = 0
    changed = False
    for item in recurring:
        if item.expense_list is None:
            db.delete(item)
            changed = True
            continue
        due_date = clamp_month_day(current_day.year, current_day.month, item.day_of_month)
        if item.last_applied_month == current_month or current_day < due_date:
            continue
        category = find_or_create_expense_category(db, item.expense_list, item.category_name)
        db.add(ExpenseItem(category_id=category.id, title=item.title, amount=item.amount, created_at=msk_date_to_utc_naive(due_date)))
        item.last_applied_month = current_month
        added += 1
        changed = True
    if changed:
        db.commit()
    return added


def _clean_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "null":
        return None
    return text


def normalize_recipe_import_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("JSON должен быть объектом")

    title = _clean_optional_string(payload.get("title") or payload.get("name"))
    if not title:
        raise ValueError("В JSON нет обязательного поля title")
    if len(title) > 150:
        title = title[:150].rstrip()

    source_url = clean_http_url(payload.get("source_url") or payload.get("source") or payload.get("url"), "Источник", 500)
    cook_time_minutes = parse_optional_int(
        None if payload.get("cook_time_minutes") is None else str(payload.get("cook_time_minutes")),
        "Время готовки",
        0,
        10_000,
    )
    cost = optional_nonnegative_money(
        None if payload.get("cost") is None else str(payload.get("cost")),
        "Стоимость",
    )
    servings = parse_optional_decimal(None if payload.get("servings") is None else str(payload.get("servings")), "Порции", Decimal("0.1"))

    ingredients_raw = payload.get("ingredients")
    if isinstance(ingredients_raw, str):
        ingredients = ingredients_raw.strip()
    elif isinstance(ingredients_raw, list):
        lines: list[str] = []
        for item in ingredients_raw:
            if isinstance(item, dict):
                name = _clean_optional_string(item.get("name") or item.get("title") or item.get("ingredient"))
                amount = _clean_optional_string(item.get("amount") or item.get("quantity") or item.get("count"))
                note = _clean_optional_string(item.get("note"))
                if not name:
                    continue
                line = name
                if amount:
                    line += f" — {amount}"
                if note:
                    line += f" ({note})"
                lines.append(line)
            else:
                text = _clean_optional_string(item)
                if text:
                    lines.append(text)
        ingredients = "\n".join(lines).strip()
    else:
        ingredients = ""
    if not ingredients:
        raise ValueError("В JSON нет ингредиентов")

    steps_raw = payload.get("steps") or payload.get("cooking_steps") or payload.get("instructions")
    step_texts: list[str] = []
    step_minutes: list[str] = []
    if isinstance(steps_raw, str):
        for line in steps_raw.splitlines():
            clean = line.strip()
            if clean:
                step_texts.append(clean)
                step_minutes.append("")
    elif isinstance(steps_raw, list):
        for item in steps_raw:
            if isinstance(item, dict):
                text = _clean_optional_string(item.get("text") or item.get("description") or item.get("step"))
                minutes = item.get("minutes")
            else:
                text = _clean_optional_string(item)
                minutes = None
            if not text:
                continue
            step_texts.append(text)
            step_minutes.append("" if minutes in (None, "") else str(minutes))
    if not step_texts:
        raise ValueError("В JSON нет этапов приготовления")

    tags_raw = payload.get("tags")
    if isinstance(tags_raw, list):
        tags = ", ".join(str(tag).strip() for tag in tags_raw if str(tag).strip())
    else:
        tags = _clean_optional_string(tags_raw)

    return {
        "title": title,
        "source_url": source_url,
        "tags": tags,
        "ingredients": ingredients,
        "cook_time_minutes": cook_time_minutes,
        "cost": cost,
        "servings": servings,
        "steps": serialize_recipe_steps(step_texts, step_minutes),
    }

def normalize_username(username: str) -> str:
    return username.strip().lower().lstrip("@")


def require_expense_access(db: Session, list_id: int, user: User, write: bool = False) -> ExpenseList:
    expense_list = db.scalar(
        select(ExpenseList)
        .options(
            selectinload(ExpenseList.owner),
            selectinload(ExpenseList.shares).selectinload(ExpenseListShare.user),
            selectinload(ExpenseList.categories).selectinload(ExpenseCategory.items),
        )
        .where(ExpenseList.id == list_id)
    )
    if not expense_list:
        raise HTTPException(status_code=404, detail="Список трат не найден")

    is_owner = expense_list.owner_id == user.id
    share = next((share for share in expense_list.shares if share.user_id == user.id), None)
    is_shared = share is not None
    if not is_owner and not is_shared:
        raise HTTPException(status_code=403, detail="Нет доступа к списку трат")
    if write and not is_owner and not share.can_edit:
        raise HTTPException(status_code=403, detail="Read-only access")
    return expense_list


def unlink_wishlist_expenses(db: Session, expense_item_ids: list[int]) -> None:
    if not expense_item_ids:
        return
    db.execute(
        update(WishlistItem)
        .where(WishlistItem.expense_item_id.in_(expense_item_ids))
        .values(
            expense_item_id=None,
            expense_prev_status=None,
            expense_prev_is_done=None,
        )
    )


def prepare_expense_list_delete(db: Session, expense_list: ExpenseList) -> None:
    item_ids = [item.id for category in expense_list.categories for item in category.items]
    unlink_wishlist_expenses(db, item_ids)
    db.execute(sql_delete(RecurringExpense).where(RecurringExpense.expense_list_id == expense_list.id))


def prepare_shopping_items_delete(db: Session, item_ids: list[int]) -> None:
    if item_ids:
        db.execute(
            update(ShoppingPriceHistory)
            .where(ShoppingPriceHistory.shopping_item_id.in_(item_ids))
            .values(shopping_item_id=None)
        )


EXPENSE_CATEGORY_PREVIEW_LIMIT = 5


def sorted_expense_items(items: list[ExpenseItem]) -> list[ExpenseItem]:
    return sorted(items, key=lambda item: item.created_at or datetime.min, reverse=True)


def expense_category_rows(expense_list: ExpenseList, preview_limit: int = EXPENSE_CATEGORY_PREVIEW_LIMIT) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for category in expense_list.categories:
        items = sorted_expense_items(category.items)
        rows.append(
            {
                "category": category,
                "recent_items": items[:preview_limit],
                "item_count": len(items),
                "hidden_count": max(0, len(items) - preview_limit),
            }
        )
    return rows


def safe_expense_return(return_to: str | None, fallback: str) -> str:
    if return_to and return_to.startswith("/expenses/") and "//" not in return_to:
        return return_to
    return fallback


def require_expense_category_access(db: Session, category_id: int, user: User) -> tuple[ExpenseCategory, ExpenseList]:
    category = db.scalar(
        select(ExpenseCategory)
        .options(
            selectinload(ExpenseCategory.items),
            selectinload(ExpenseCategory.expense_list).selectinload(ExpenseList.owner),
            selectinload(ExpenseCategory.expense_list).selectinload(ExpenseList.shares).selectinload(ExpenseListShare.user),
            selectinload(ExpenseCategory.expense_list).selectinload(ExpenseList.categories),
        )
        .where(ExpenseCategory.id == category_id)
    )
    if not category:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    expense_list = require_expense_access(db, category.expense_list_id, user)
    category = next((item for item in expense_list.categories if item.id == category_id), category)
    return category, expense_list


def require_chat_access(db: Session, thread_id: int, user: User) -> ChatThread:
    thread = db.scalar(
        select(ChatThread)
        .options(
            selectinload(ChatThread.user_a),
            selectinload(ChatThread.user_b),
            selectinload(ChatThread.created_by),
        )
        .where(ChatThread.id == thread_id)
    )
    if not thread:
        raise HTTPException(status_code=404, detail="Чат не найден")
    if user.id not in (thread.user_a_id, thread.user_b_id):
        raise HTTPException(status_code=403, detail="Нет доступа к этому чату")
    return thread


@app.get("/media/recipes/{filename}")
def recipe_media(filename: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    safe_name = Path(filename).name
    expected_path = f"/media/recipes/{safe_name}"
    recipe = db.scalar(select(Recipe).where(Recipe.image_path == expected_path))
    path = RECIPE_MEDIA_DIR / safe_name
    if not recipe or not path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})


@app.get("/media/moments/thumb/{filename}")
def moment_thumbnail_media(filename: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    source_name = Path(filename).name
    expected_path = f"/media/moments/{source_name}"
    moment = db.scalar(select(Moment).where(Moment.photo_path == expected_path, Moment.owner_id == user.id))
    if not moment:
        raise HTTPException(status_code=404, detail="Файл не найден")
    try:
        path = ensure_moment_thumbnail(expected_path)
    except (FileNotFoundError, OSError, UnidentifiedImageError):
        raise HTTPException(status_code=404, detail="Файл не найден") from None
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})


@app.get("/media/moments/{filename}")
def moment_media(filename: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    safe_name = Path(filename).name
    expected_path = f"/media/moments/{safe_name}"
    moment = db.scalar(select(Moment).where(Moment.photo_path == expected_path, Moment.owner_id == user.id))
    path = MOMENT_MEDIA_DIR / safe_name
    if not moment or not path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})


@app.get("/media/chats/{filename}")
def chat_media(filename: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    safe_name = Path(filename).name
    expected_path = f"/media/chats/{safe_name}"
    message = db.scalar(select(ChatThreadMessage).where(ChatThreadMessage.attachment_path == expected_path))
    path = CHAT_MEDIA_DIR / safe_name
    if not message or not path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
    require_chat_access(db, message.thread_id, user)
    return FileResponse(
        path,
        filename=safe_name,
        headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"},
    )


def other_chat_user(thread: ChatThread, user: User) -> User:
    return thread.user_b if thread.user_a_id == user.id else thread.user_a


def mark_thread_read(db: Session, thread_id: int, user: User) -> None:
    db.execute(
        update(ChatThreadMessage)
        .where(
            ChatThreadMessage.thread_id == thread_id,
            ChatThreadMessage.sender_id != user.id,
            ChatThreadMessage.is_read.is_(False),
        )
        .values(is_read=True)
    )
    db.commit()


def chat_message_payload(
    message: ChatThreadMessage,
    sender: User | None = None,
    reply_message: ChatThreadMessage | None = None,
) -> dict[str, Any]:
    sender = sender or message.sender
    reply_message = reply_message or message.reply_to
    return {
        "type": "message",
        "id": message.id,
        "thread_id": message.thread_id,
        "sender_id": message.sender_id,
        "sender_username": sender.username,
        "text": message.text,
        "reply_to_id": message.reply_to_id,
        "reply_to_text": reply_message.text if reply_message else None,
        "reply_to_sender_username": (
            reply_message.sender.username if reply_message and reply_message.sender else None
        ),
        "attachment_path": message.attachment_path,
        "created_at": format_msk_datetime(message.created_at),
    }


def unread_total_for_user(db: Session, user: User) -> int:
    return int(
        db.scalar(
            select(func.count(ChatThreadMessage.id))
            .join(ChatThread)
            .where(
                or_(ChatThread.user_a_id == user.id, ChatThread.user_b_id == user.id),
                ChatThreadMessage.sender_id != user.id,
                ChatThreadMessage.is_read.is_(False),
            )
        )
        or 0
    )


def require_wishlist_access(db: Session, owner_username: str, user: User) -> User:
    owner = db.scalar(select(User).where(User.username == normalize_username(owner_username)))
    if not owner:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if owner.id == user.id:
        return owner
    share = db.scalar(
        select(WishlistShare).where(WishlistShare.owner_id == owner.id, WishlistShare.user_id == user.id)
    )
    if not share:
        raise HTTPException(status_code=403, detail="Нет доступа к этому списку хотелок")
    return owner


@app.get("/")
def index(user: User | None = Depends(get_current_user_optional)):
    if user:
        return redirect("/today")
    return redirect("/login")


@app.get("/health", include_in_schema=False)
def health(db: Session = Depends(get_db)):
    db.execute(select(1)).scalar_one()
    return {"status": "ok"}


@app.get("/register")
def register_page(request: Request, user: User | None = Depends(get_current_user_optional)):
    if user:
        return redirect("/recipes")
    if not settings.registration_enabled:
        raise HTTPException(status_code=404, detail="Регистрация отключена")
    return render(request, "register.html", {"user": None})


@app.post("/register")
def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    if not settings.registration_enabled:
        raise HTTPException(status_code=403, detail="Регистрация отключена")
    username = normalize_username(username)
    attempt_key = auth_attempt_key(request, username)
    check_auth_rate_limit(attempt_key)
    record_auth_failure(attempt_key)
    context = {"user": None, "username": username}
    if not re.fullmatch(r"[\w.-]{3,50}", username, flags=re.UNICODE):
        return render(request, "register.html", {**context, "error": "Ник: 3–50 символов, только буквы, цифры, точка, дефис и подчёркивание"})
    if not 8 <= len(password) <= 256:
        return render(request, "register.html", {**context, "error": "Пароль должен содержать от 8 до 256 символов"})
    if password != password_confirm:
        return render(request, "register.html", {**context, "error": "Пароли не совпадают"})

    user = User(username=username, password_hash=hash_password(password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return render(request, "register.html", {**context, "error": "Такой ник уже занят"})

    clear_auth_failures(attempt_key)
    request.session.clear()
    request.session["user_id"] = user.id
    return redirect("/recipes")


@app.get("/login")
def login_page(request: Request, user: User | None = Depends(get_current_user_optional)):
    if user:
        return redirect("/recipes")
    return render(request, "login.html", {"user": None})


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    username = normalize_username(username)
    attempt_key = auth_attempt_key(request, username)
    check_auth_rate_limit(attempt_key)
    if len(username) > 50 or len(password) > 256:
        record_auth_failure(attempt_key)
        return render(request, "login.html", {"error": "Неверный ник или пароль", "user": None})
    user = db.scalar(select(User).where(User.username == username))
    if not user or not verify_password(password, user.password_hash):
        record_auth_failure(attempt_key)
        return render(request, "login.html", {"error": "Неверный ник или пароль", "user": None})

    clear_auth_failures(attempt_key)
    request.session.clear()
    request.session["user_id"] = user.id
    return redirect("/recipes")


@app.post("/logout")
def logout(
    request: Request,
    push_endpoint: str = Form(""),
    user: User | None = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if user and push_endpoint:
        now = utc_now_naive()
        db.execute(
            update(PushSubscription)
            .where(
                PushSubscription.user_id == user.id,
                PushSubscription.endpoint == push_endpoint[:600],
                PushSubscription.disabled_at.is_(None),
            )
            .values(disabled_at=now, updated_at=now)
        )
        db.commit()
    request.session.clear()
    return redirect("/login")


@app.post("/settings/theme")
def update_theme(
    theme: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    clean_theme = theme.strip().lower()
    if clean_theme not in {"system", "light", "dark"}:
        raise HTTPException(status_code=400, detail="Некорректная тема")
    user.theme = clean_theme
    db.add(user)
    db.commit()
    return JSONResponse({"theme": clean_theme})


@app.get("/settings")
def settings_page(request: Request, user: User = Depends(get_current_user)):
    return render(request, "settings.html", {"user": user, "palette_tokens": PALETTE_TOKENS, "palette_groups": PALETTE_GROUPS, "palette_defaults": default_palette(user.theme)})


@app.post("/settings")
async def save_settings(
    request: Request,
    appearance: str = Form("system"),
    financial_period_start_day: str = Form("1"),
    reset_palette: str | None = Form(None), gradient_enabled: str | None = Form(None), gradient_start_color: str = Form(""), gradient_end_color: str = Form(""), gradient_angle: str = Form("135"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    clean_appearance = appearance.strip().lower()
    if clean_appearance not in {"system", "light", "dark"}:
        return render(request, "settings.html", {"user": user, "palette_tokens": PALETTE_TOKENS, "palette_groups": PALETTE_GROUPS, "palette_defaults": default_palette(user.theme), "error": "Выберите корректную тему."})
    try:
        start_day = int(financial_period_start_day)
    except ValueError:
        start_day = 0
    if not 1 <= start_day <= 31:
        return render(
            request,
            "settings.html",
            {"user": user, "palette_tokens": PALETTE_TOKENS, "palette_groups": PALETTE_GROUPS, "palette_defaults": default_palette(user.theme), "error": "День начала финансового периода должен быть от 1 до 31."},
        )

    submitted_palette = {
        key.removeprefix("color_"): value
        for key, value in (await request.form()).items()
        if key.startswith("color_") and value.strip()
    }
    palette, palette_error = validate_palette(submitted_palette, theme=clean_appearance)
    if palette_error:
        return render(request, "settings.html", {"user": user, "palette_tokens": PALETTE_TOKENS, "palette_groups": PALETTE_GROUPS, "palette_defaults": default_palette(user.theme), "error": palette_error})
    gradient, gradient_error = validate_gradient(gradient_enabled is not None, gradient_start_color, gradient_end_color, gradient_angle)
    if gradient_error:
        return render(request, "settings.html", {"user": user, "palette_tokens": PALETTE_TOKENS, "palette_groups": PALETTE_GROUPS, "palette_defaults": default_palette(user.theme), "error": gradient_error})

    user.theme = clean_appearance
    user.expense_period_start_day = start_day
    user.ui_palette_json = None if reset_palette == "1" else json.dumps({**palette, **gradient}, separators=(",", ":"))
    db.commit()
    return redirect_notice("/settings", "Настройки сохранены")


@app.post("/settings/expense-period")
def update_expense_period(
    expense_period_start_day_value: int = Form(..., alias="expense_period_start_day"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user.expense_period_start_day = max(1, min(31, int(expense_period_start_day_value or 1)))
    db.add(user)
    db.commit()
    return redirect_notice("/expenses/planning", "День начала финансового месяца сохранён")


def vehicle_form_values(
    *,
    display_name: str = "",
    make: str = "",
    model: str = "",
    year: str = "",
    license_plate: str = "",
    vin: str = "",
    current_odometer: str = "0",
    notes: str = "",
) -> dict[str, str]:
    return {
        "display_name": display_name, "make": make, "model": model, "year": year,
        "license_plate": license_plate, "vin": vin, "current_odometer": current_odometer, "notes": notes,
    }


def validate_vehicle_form(values: dict[str, str]) -> dict[str, Any]:
    make = values["make"].strip()
    model = values["model"].strip()
    if not make or not model:
        raise ValueError("Укажите марку и модель автомобиля.")
    if len(make) > 80 or len(model) > 80:
        raise ValueError("Марка и модель не должны быть длиннее 80 символов.")
    try:
        year = int(values["year"])
    except ValueError as exc:
        raise ValueError("Укажите корректный год выпуска.") from exc
    if not 1886 <= year <= msk_today().year + 1:
        raise ValueError("Год выпуска находится вне допустимого диапазона.")
    try:
        odometer = int(values["current_odometer"])
    except ValueError as exc:
        raise ValueError("Пробег должен быть целым числом.") from exc
    if not 0 <= odometer <= 10_000_000:
        raise ValueError("Пробег должен быть неотрицательным и реалистичным.")
    display_name = values["display_name"].strip()
    plate = values["license_plate"].strip()
    vin = values["vin"].strip().upper()
    notes = values["notes"].strip()
    if len(display_name) > 100 or len(plate) > 40 or len(vin) > 40 or len(notes) > 4000:
        raise ValueError("Одно из полей слишком длинное.")
    if vin and len(vin) < 5:
        raise ValueError("VIN должен содержать не менее 5 символов.")
    return {"display_name": display_name or None, "make": make, "model": model, "year": year, "license_plate": plate or None, "vin": vin or None, "current_odometer": odometer, "notes": notes or None}


def require_owned_vehicle(db: Session, vehicle_id: int, user: User) -> Vehicle:
    vehicle = db.scalar(select(Vehicle).where(Vehicle.id == vehicle_id, Vehicle.owner_id == user.id))
    if not vehicle:
        raise HTTPException(status_code=404, detail="Автомобиль не найден")
    return vehicle


def render_vehicle_form(request: Request, *, user: User, form: dict[str, str], vehicle: Vehicle | None = None, error: str | None = None):
    return render(request, "vehicle_form.html", {"user": user, "form": form, "vehicle": vehicle, "error": error})


@app.get("/vehicles")
def vehicles_page(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vehicles = db.scalars(select(Vehicle).where(Vehicle.owner_id == user.id).order_by(Vehicle.updated_at.desc(), Vehicle.id.desc())).all()
    return render(request, "vehicles.html", {"user": user, "vehicles": vehicles})


@app.get("/vehicles/new")
def vehicle_new_page(request: Request, user: User = Depends(get_current_user)):
    return render_vehicle_form(request, user=user, form=vehicle_form_values(year=str(msk_today().year)))


@app.post("/vehicles")
def vehicle_create(
    request: Request, display_name: str = Form(""), make: str = Form(""), model: str = Form(""), year: str = Form(""),
    license_plate: str = Form(""), vin: str = Form(""), current_odometer: str = Form("0"), notes: str = Form(""),
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    form = vehicle_form_values(display_name=display_name, make=make, model=model, year=year, license_plate=license_plate, vin=vin, current_odometer=current_odometer, notes=notes)
    try:
        data = validate_vehicle_form(form)
    except ValueError as exc:
        return render_vehicle_form(request, user=user, form=form, error=str(exc))
    vehicle = Vehicle(owner_id=user.id, **data)
    db.add(vehicle)
    db.commit()
    return redirect_notice(f"/vehicles/{vehicle.id}", "Автомобиль добавлен")


@app.get("/vehicles/{vehicle_id}")
def vehicle_overview(request: Request, vehicle_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vehicle = require_owned_vehicle(db, vehicle_id, user)
    log_summary = db.execute(
        select(
            func.count(VehicleLogEntry.id),
            func.coalesce(func.sum(VehicleLogEntry.cost), 0),
            func.max(VehicleLogEntry.occurred_on),
        ).where(VehicleLogEntry.vehicle_id == vehicle.id)
    ).one()
    recent_entries = db.scalars(
        select(VehicleLogEntry)
        .where(VehicleLogEntry.vehicle_id == vehicle.id)
        .order_by(desc(VehicleLogEntry.occurred_on), desc(VehicleLogEntry.id))
        .limit(3)
    ).all()
    maintenance = maintenance_states(vehicle, db)
    maintenance_counts = {status: sum(row["state"].status == status for row in maintenance) for status in STATUS_ORDER}
    fuel_entries = db.scalars(select(VehicleFuelEntry).where(VehicleFuelEntry.vehicle_id == vehicle.id).order_by(desc(VehicleFuelEntry.occurred_on), desc(VehicleFuelEntry.id))).all()
    fuel_data = fuel_summary(fuel_entries)
    return render(request, "vehicle_detail.html", {
        "user": user, "vehicle": vehicle,
        "log_summary": {"count": log_summary[0], "cost": log_summary[1], "latest_date": log_summary[2]},
        "recent_entries": recent_entries, "vehicle_log_type_labels": VEHICLE_LOG_TYPE_LABELS,
        "maintenance": maintenance[:5], "maintenance_counts": maintenance_counts,
        "latest_fuel": fuel_entries[0] if fuel_entries else None, "fuel_summary": fuel_data,
    })


@app.get("/vehicles/{vehicle_id}/edit")
def vehicle_edit_page(request: Request, vehicle_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vehicle = require_owned_vehicle(db, vehicle_id, user)
    form = vehicle_form_values(display_name=vehicle.display_name or "", make=vehicle.make, model=vehicle.model, year=str(vehicle.year), license_plate=vehicle.license_plate or "", vin=vehicle.vin or "", current_odometer=str(vehicle.current_odometer), notes=vehicle.notes or "")
    return render_vehicle_form(request, user=user, vehicle=vehicle, form=form)


@app.post("/vehicles/{vehicle_id}/edit")
def vehicle_update(
    request: Request, vehicle_id: int, display_name: str = Form(""), make: str = Form(""), model: str = Form(""), year: str = Form(""),
    license_plate: str = Form(""), vin: str = Form(""), current_odometer: str = Form("0"), notes: str = Form(""),
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    vehicle = require_owned_vehicle(db, vehicle_id, user)
    form = vehicle_form_values(display_name=display_name, make=make, model=model, year=year, license_plate=license_plate, vin=vin, current_odometer=current_odometer, notes=notes)
    try:
        data = validate_vehicle_form(form)
    except ValueError as exc:
        return render_vehicle_form(request, user=user, vehicle=vehicle, form=form, error=str(exc))
    for field, value in data.items():
        setattr(vehicle, field, value)
    db.commit()
    return redirect_notice(f"/vehicles/{vehicle.id}", "Данные автомобиля сохранены")


@app.post("/vehicles/{vehicle_id}/delete")
def vehicle_delete(vehicle_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vehicle = require_owned_vehicle(db, vehicle_id, user)
    db.delete(vehicle)
    db.commit()
    return redirect_notice("/vehicles", "Автомобиль удалён")


VEHICLE_LOG_TYPE_LABELS = {
    "maintenance": "Техническое обслуживание", "repair": "Ремонт", "diagnostics": "Диагностика",
    "modification": "Доработка", "event": "Событие", "accident": "ДТП", "note": "Заметка", "other": "Другое",
}
VEHICLE_LOG_PAGE_SIZE = 25


def vehicle_log_form_values(entry: VehicleLogEntry | None = None, **values: str) -> dict[str, str]:
    form = {
        "occurred_on": entry.occurred_on.isoformat() if entry else msk_today().isoformat(),
        "odometer": str(entry.odometer) if entry and entry.odometer is not None else "",
        "entry_type": entry.entry_type if entry else "maintenance", "title": entry.title if entry else "",
        "description": (entry.description or "") if entry else "",
        "cost": format_decimal(entry.cost) if entry and entry.cost is not None else "",
        "service_location": (entry.service_location or "") if entry else "", "notes": (entry.notes or "") if entry else "",
    }
    form.update(values)
    return form


def validate_vehicle_log_form(values: dict[str, str]) -> dict[str, Any]:
    try:
        occurred_on = date.fromisoformat(values["occurred_on"].strip())
    except ValueError as exc:
        raise ValueError("Укажите корректную дату записи.") from exc
    entry_type = values["entry_type"].strip()
    if entry_type not in VEHICLE_LOG_TYPE_LABELS:
        raise ValueError("Выберите тип записи из списка.")
    title = values["title"].strip()
    if not title:
        raise ValueError("Укажите название записи.")
    if len(title) > 160:
        raise ValueError("Название не должно быть длиннее 160 символов.")
    description, service_location, notes = values["description"].strip(), values["service_location"].strip(), values["notes"].strip()
    if len(description) > 10_000 or len(service_location) > 180 or len(notes) > 4_000:
        raise ValueError("Одно из полей слишком длинное.")
    try:
        odometer = parse_optional_int(values["odometer"], "Пробег", 0, 10_000_000)
        cost = optional_nonnegative_money(values["cost"], "Стоимость")
    except HTTPException as exc:
        raise ValueError(str(exc.detail)) from exc
    return {"occurred_on": occurred_on, "odometer": odometer, "entry_type": entry_type, "title": title,
            "description": description or None, "cost": cost, "service_location": service_location or None, "notes": notes or None}


def require_owned_vehicle_log_entry(db: Session, vehicle_id: int, entry_id: int) -> VehicleLogEntry:
    entry = db.scalar(select(VehicleLogEntry).where(VehicleLogEntry.id == entry_id, VehicleLogEntry.vehicle_id == vehicle_id))
    if not entry:
        raise HTTPException(status_code=404, detail="Запись бортового журнала не найдена")
    return entry


def render_vehicle_log_form(request: Request, *, user: User, vehicle: Vehicle, form: dict[str, str], entry: VehicleLogEntry | None = None, error: str | None = None):
    return render(request, "vehicle_log_form.html", {"user": user, "vehicle": vehicle, "entry": entry, "form": form, "error": error, "vehicle_log_type_labels": VEHICLE_LOG_TYPE_LABELS})


def vehicle_log_filters(entry_type: str, date_from: str, date_to: str, q: str) -> tuple[dict[str, str], list[Any]]:
    filters = {"entry_type": entry_type.strip(), "date_from": date_from.strip(), "date_to": date_to.strip(), "q": q.strip()}
    clauses: list[Any] = []
    if filters["entry_type"]:
        if filters["entry_type"] not in VEHICLE_LOG_TYPE_LABELS:
            filters["entry_type"] = ""
        else:
            clauses.append(VehicleLogEntry.entry_type == filters["entry_type"])
    try:
        if filters["date_from"]:
            clauses.append(VehicleLogEntry.occurred_on >= date.fromisoformat(filters["date_from"]))
        if filters["date_to"]:
            clauses.append(VehicleLogEntry.occurred_on <= date.fromisoformat(filters["date_to"]))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Некорректный диапазон дат") from exc
    if filters["q"]:
        term = f"%{filters['q'][:160]}%"
        clauses.append(or_(VehicleLogEntry.title.ilike(term), VehicleLogEntry.description.ilike(term), VehicleLogEntry.service_location.ilike(term)))
    return filters, clauses


def vehicle_log_ordering(sort: str) -> tuple[str, tuple[Any, ...]]:
    ordering = {
        "date_desc": (desc(VehicleLogEntry.occurred_on), desc(VehicleLogEntry.id)), "date_asc": (asc(VehicleLogEntry.occurred_on), asc(VehicleLogEntry.id)),
        "odometer_desc": (VehicleLogEntry.odometer.is_(None), desc(VehicleLogEntry.odometer), desc(VehicleLogEntry.id)), "odometer_asc": (VehicleLogEntry.odometer.is_(None), asc(VehicleLogEntry.odometer), desc(VehicleLogEntry.id)),
        "cost_desc": (VehicleLogEntry.cost.is_(None), desc(VehicleLogEntry.cost), desc(VehicleLogEntry.id)), "cost_asc": (VehicleLogEntry.cost.is_(None), asc(VehicleLogEntry.cost), desc(VehicleLogEntry.id)),
    }
    normalized = sort if sort in ordering else "date_desc"
    return normalized, ordering[normalized]


@app.get("/vehicles/{vehicle_id}/log")
def vehicle_log_page(request: Request, vehicle_id: int, entry_type: str = "", date_from: str = "", date_to: str = "", q: str = "", sort: str = "date_desc", page: int = 1, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vehicle = require_owned_vehicle(db, vehicle_id, user)
    filters, clauses = vehicle_log_filters(entry_type, date_from, date_to, q)
    sort, ordering = vehicle_log_ordering(sort)
    base = select(VehicleLogEntry).where(VehicleLogEntry.vehicle_id == vehicle.id, *clauses)
    total_count, total_cost, first_date, last_date = db.execute(select(func.count(VehicleLogEntry.id), func.coalesce(func.sum(VehicleLogEntry.cost), 0), func.min(VehicleLogEntry.occurred_on), func.max(VehicleLogEntry.occurred_on)).where(VehicleLogEntry.vehicle_id == vehicle.id, *clauses)).one()
    page_count = max(1, (total_count + VEHICLE_LOG_PAGE_SIZE - 1) // VEHICLE_LOG_PAGE_SIZE)
    page = max(1, min(page, page_count))
    entries = db.scalars(base.order_by(*ordering).offset((page - 1) * VEHICLE_LOG_PAGE_SIZE).limit(VEHICLE_LOG_PAGE_SIZE)).all()
    query_string = urlencode({key: value for key, value in {**filters, "sort": sort}.items() if value})
    return render(request, "vehicle_log.html", {"user": user, "vehicle": vehicle, "entries": entries, "filters": filters, "sort": sort, "page": page, "page_count": page_count, "query_string": query_string, "vehicle_log_type_labels": VEHICLE_LOG_TYPE_LABELS, "summary": {"count": total_count, "cost": total_cost, "first_date": first_date, "last_date": last_date}})


@app.get("/vehicles/{vehicle_id}/log/new")
def vehicle_log_new_page(request: Request, vehicle_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vehicle = require_owned_vehicle(db, vehicle_id, user)
    return render_vehicle_log_form(request, user=user, vehicle=vehicle, form=vehicle_log_form_values())


@app.post("/vehicles/{vehicle_id}/log")
def vehicle_log_create(request: Request, vehicle_id: int, occurred_on: str = Form(""), odometer: str = Form(""), entry_type: str = Form(""), title: str = Form(""), description: str = Form(""), cost: str = Form(""), service_location: str = Form(""), notes: str = Form(""), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vehicle = require_owned_vehicle(db, vehicle_id, user)
    form = vehicle_log_form_values(occurred_on=occurred_on, odometer=odometer, entry_type=entry_type, title=title, description=description, cost=cost, service_location=service_location, notes=notes)
    try:
        data = validate_vehicle_log_form(form)
    except ValueError as exc:
        return render_vehicle_log_form(request, user=user, vehicle=vehicle, form=form, error=str(exc))
    entry = VehicleLogEntry(vehicle_id=vehicle.id, **data)
    db.add(entry)
    db.commit()
    return redirect_notice(f"/vehicles/{vehicle.id}/log/{entry.id}", "Запись добавлена в бортовой журнал")


@app.get("/vehicles/{vehicle_id}/log/print")
def vehicle_log_print(request: Request, vehicle_id: int, entry_type: str = "", date_from: str = "", date_to: str = "", q: str = "", user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vehicle = require_owned_vehicle(db, vehicle_id, user)
    filters, clauses = vehicle_log_filters(entry_type, date_from, date_to, q)
    entries = db.scalars(select(VehicleLogEntry).where(VehicleLogEntry.vehicle_id == vehicle.id, *clauses).order_by(asc(VehicleLogEntry.occurred_on), asc(VehicleLogEntry.id))).all()
    count, total_cost = db.execute(select(func.count(VehicleLogEntry.id), func.coalesce(func.sum(VehicleLogEntry.cost), 0)).where(VehicleLogEntry.vehicle_id == vehicle.id, *clauses)).one()
    return render(request, "vehicle_log_print.html", {"vehicle": vehicle, "entries": entries, "filters": filters, "summary": {"count": count, "cost": total_cost}, "vehicle_log_type_labels": VEHICLE_LOG_TYPE_LABELS})


@app.get("/vehicles/{vehicle_id}/log/{entry_id}")
def vehicle_log_detail(request: Request, vehicle_id: int, entry_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vehicle = require_owned_vehicle(db, vehicle_id, user)
    entry = require_owned_vehicle_log_entry(db, vehicle.id, entry_id)
    return render(request, "vehicle_log_detail.html", {"user": user, "vehicle": vehicle, "entry": entry, "vehicle_log_type_labels": VEHICLE_LOG_TYPE_LABELS})


@app.get("/vehicles/{vehicle_id}/log/{entry_id}/edit")
def vehicle_log_edit_page(request: Request, vehicle_id: int, entry_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vehicle = require_owned_vehicle(db, vehicle_id, user)
    entry = require_owned_vehicle_log_entry(db, vehicle.id, entry_id)
    return render_vehicle_log_form(request, user=user, vehicle=vehicle, entry=entry, form=vehicle_log_form_values(entry))


@app.post("/vehicles/{vehicle_id}/log/{entry_id}/edit")
def vehicle_log_update(request: Request, vehicle_id: int, entry_id: int, occurred_on: str = Form(""), odometer: str = Form(""), entry_type: str = Form(""), title: str = Form(""), description: str = Form(""), cost: str = Form(""), service_location: str = Form(""), notes: str = Form(""), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vehicle = require_owned_vehicle(db, vehicle_id, user)
    entry = require_owned_vehicle_log_entry(db, vehicle.id, entry_id)
    form = vehicle_log_form_values(occurred_on=occurred_on, odometer=odometer, entry_type=entry_type, title=title, description=description, cost=cost, service_location=service_location, notes=notes)
    try:
        data = validate_vehicle_log_form(form)
    except ValueError as exc:
        return render_vehicle_log_form(request, user=user, vehicle=vehicle, entry=entry, form=form, error=str(exc))
    for field, value in data.items():
        setattr(entry, field, value)
    db.commit()
    return redirect_notice(f"/vehicles/{vehicle.id}/log/{entry.id}", "Запись бортового журнала сохранена")


@app.post("/vehicles/{vehicle_id}/log/{entry_id}/delete")
def vehicle_log_delete(vehicle_id: int, entry_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vehicle = require_owned_vehicle(db, vehicle_id, user)
    entry = require_owned_vehicle_log_entry(db, vehicle.id, entry_id)
    db.delete(entry)
    db.commit()
    return redirect_notice(f"/vehicles/{vehicle.id}/log", "Запись удалена")


VEHICLE_MAINTENANCE_CATEGORIES = {"engine": "Двигатель", "transmission": "Трансмиссия", "filters": "Фильтры", "fluids": "Жидкости", "brakes": "Тормоза", "suspension": "Подвеска", "tires": "Шины", "electrical": "Электрика", "body": "Кузов", "other": "Другое"}


def maintenance_states(vehicle: Vehicle, db: Session) -> list[dict[str, Any]]:
    rows = []
    for item in db.scalars(select(VehicleMaintenanceItem).where(VehicleMaintenanceItem.vehicle_id == vehicle.id)).all():
        state = calculate_maintenance(current_odometer=vehicle.current_odometer, today=msk_today(), last_odometer=item.last_service_odometer, last_date=item.last_service_date, interval_km=item.interval_km, interval_months=item.interval_months)
        due = min([value for value in (state.remaining_km, state.remaining_days) if value is not None], default=10**12)
        rows.append({"item": item, "state": state, "due": due})
    return sorted(rows, key=lambda row: (STATUS_ORDER[row["state"].status], row["due"], row["item"].name.lower()))


def maintenance_form_values(item: VehicleMaintenanceItem | None = None, **values: str) -> dict[str, str]:
    form = {"name": item.name if item else "", "category": item.category if item else "engine", "last_service_date": item.last_service_date.isoformat() if item and item.last_service_date else "", "last_service_odometer": str(item.last_service_odometer) if item and item.last_service_odometer is not None else "", "interval_km": str(item.interval_km) if item and item.interval_km else "", "interval_months": str(item.interval_months) if item and item.interval_months else "", "notes": item.notes or "" if item else ""}
    form.update(values)
    return form


def validate_maintenance_form(form: dict[str, str]) -> dict[str, Any]:
    name, category, notes = form["name"].strip(), form["category"].strip(), form["notes"].strip()
    if not name or len(name) > 160: raise ValueError("Укажите название до 160 символов.")
    if category not in VEHICLE_MAINTENANCE_CATEGORIES: raise ValueError("Выберите категорию из списка.")
    if len(notes) > 4000: raise ValueError("Заметки слишком длинные.")
    try:
        interval_km = parse_optional_int(form["interval_km"], "Интервал пробега", 1, 10_000_000)
        interval_months = parse_optional_int(form["interval_months"], "Интервал месяцев", 1, 1_200)
        last_odometer = parse_optional_int(form["last_service_odometer"], "Пробег обслуживания", 0, 10_000_000)
        last_date = parse_date(form["last_service_date"])
    except HTTPException as exc: raise ValueError(str(exc.detail)) from exc
    if interval_km is None and interval_months is None: raise ValueError("Задайте хотя бы один интервал.")
    if interval_km is not None and last_odometer is None: raise ValueError("Для интервала по пробегу укажите последний пробег обслуживания.")
    if interval_months is not None and last_date is None: raise ValueError("Для интервала по времени укажите дату последнего обслуживания.")
    return {"name": name, "category": category, "last_service_date": last_date, "last_service_odometer": last_odometer, "interval_km": interval_km, "interval_months": interval_months, "notes": notes or None}


def require_maintenance_item(db: Session, vehicle_id: int, item_id: int) -> VehicleMaintenanceItem:
    item = db.scalar(select(VehicleMaintenanceItem).where(VehicleMaintenanceItem.id == item_id, VehicleMaintenanceItem.vehicle_id == vehicle_id))
    if not item: raise HTTPException(status_code=404, detail="Позиция обслуживания не найдена")
    return item


def render_maintenance_form(request: Request, user: User, vehicle: Vehicle, form: dict[str, str], item: VehicleMaintenanceItem | None = None, error: str | None = None):
    return render(request, "vehicle_maintenance_form.html", {"user": user, "vehicle": vehicle, "form": form, "item": item, "error": error, "categories": VEHICLE_MAINTENANCE_CATEGORIES})


@app.get("/vehicles/{vehicle_id}/maintenance")
def vehicle_maintenance_page(request: Request, vehicle_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vehicle = require_owned_vehicle(db, vehicle_id, user)
    return render(request, "vehicle_maintenance.html", {"user": user, "vehicle": vehicle, "rows": maintenance_states(vehicle, db), "categories": VEHICLE_MAINTENANCE_CATEGORIES, "today": msk_today()})


@app.get("/vehicles/{vehicle_id}/maintenance/new")
def vehicle_maintenance_new(request: Request, vehicle_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vehicle = require_owned_vehicle(db, vehicle_id, user)
    return render_maintenance_form(request, user, vehicle, maintenance_form_values())


@app.post("/vehicles/{vehicle_id}/maintenance")
def vehicle_maintenance_create(request: Request, vehicle_id: int, name: str = Form(""), category: str = Form("other"), last_service_date: str = Form(""), last_service_odometer: str = Form(""), interval_km: str = Form(""), interval_months: str = Form(""), notes: str = Form(""), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vehicle = require_owned_vehicle(db, vehicle_id, user); form = maintenance_form_values(name=name, category=category, last_service_date=last_service_date, last_service_odometer=last_service_odometer, interval_km=interval_km, interval_months=interval_months, notes=notes)
    try: data = validate_maintenance_form(form)
    except ValueError as exc: return render_maintenance_form(request, user, vehicle, form, error=str(exc))
    db.add(VehicleMaintenanceItem(vehicle_id=vehicle.id, **data)); db.commit()
    return redirect_notice(f"/vehicles/{vehicle.id}/maintenance", "Позиция обслуживания добавлена")


@app.get("/vehicles/{vehicle_id}/maintenance/{item_id}/edit")
def vehicle_maintenance_edit(request: Request, vehicle_id: int, item_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vehicle = require_owned_vehicle(db, vehicle_id, user); item = require_maintenance_item(db, vehicle.id, item_id)
    return render_maintenance_form(request, user, vehicle, maintenance_form_values(item), item)


@app.post("/vehicles/{vehicle_id}/maintenance/{item_id}/edit")
def vehicle_maintenance_update(request: Request, vehicle_id: int, item_id: int, name: str = Form(""), category: str = Form("other"), last_service_date: str = Form(""), last_service_odometer: str = Form(""), interval_km: str = Form(""), interval_months: str = Form(""), notes: str = Form(""), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vehicle = require_owned_vehicle(db, vehicle_id, user); item = require_maintenance_item(db, vehicle.id, item_id); form = maintenance_form_values(name=name, category=category, last_service_date=last_service_date, last_service_odometer=last_service_odometer, interval_km=interval_km, interval_months=interval_months, notes=notes)
    try: data = validate_maintenance_form(form)
    except ValueError as exc: return render_maintenance_form(request, user, vehicle, form, item, str(exc))
    for field, value in data.items(): setattr(item, field, value)
    db.commit(); return redirect_notice(f"/vehicles/{vehicle.id}/maintenance", "Позиция обслуживания сохранена")


@app.post("/vehicles/{vehicle_id}/maintenance/{item_id}/delete")
def vehicle_maintenance_delete(vehicle_id: int, item_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vehicle = require_owned_vehicle(db, vehicle_id, user); db.delete(require_maintenance_item(db, vehicle.id, item_id)); db.commit()
    return redirect_notice(f"/vehicles/{vehicle.id}/maintenance", "Позиция обслуживания удалена")


@app.post("/vehicles/{vehicle_id}/maintenance/{item_id}/service")
def vehicle_maintenance_service(request: Request, vehicle_id: int, item_id: int, occurred_on: str = Form(""), odometer: str = Form(""), cost: str = Form(""), service_location: str = Form(""), notes: str = Form(""), add_to_log: str | None = Form(None), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vehicle = require_owned_vehicle(db, vehicle_id, user); item = require_maintenance_item(db, vehicle.id, item_id)
    try:
        service_date = date.fromisoformat(occurred_on); service_odometer = parse_optional_int(odometer, "Пробег", 0, 10_000_000); service_cost = optional_nonnegative_money(cost, "Стоимость")
    except (ValueError, HTTPException) as exc:
        return redirect_notice(f"/vehicles/{vehicle.id}/maintenance", f"Не удалось отметить обслуживание: {getattr(exc, 'detail', 'некорректные данные')}")
    if item.interval_km is not None and service_odometer is None: return redirect_notice(f"/vehicles/{vehicle.id}/maintenance", "Укажите пробег обслуживания")
    if item.last_service_odometer is not None and service_odometer is not None and service_odometer < item.last_service_odometer: return redirect_notice(f"/vehicles/{vehicle.id}/maintenance", "Пробег не может быть меньше предыдущего обслуживания")
    if item.last_service_date and service_date < item.last_service_date: return redirect_notice(f"/vehicles/{vehicle.id}/maintenance", "Дата не может быть раньше предыдущего обслуживания")
    item.last_service_date = service_date if item.interval_months is not None else item.last_service_date
    item.last_service_odometer = service_odometer if item.interval_km is not None else item.last_service_odometer
    if service_odometer is not None: vehicle.current_odometer = max(vehicle.current_odometer, service_odometer)
    if add_to_log: db.add(VehicleLogEntry(vehicle_id=vehicle.id, occurred_on=service_date, odometer=service_odometer, entry_type="maintenance", title=item.name, description=notes.strip() or None, cost=service_cost, service_location=service_location.strip() or None))
    db.commit()
    return redirect_notice(f"/vehicles/{vehicle.id}/maintenance", "Обслуживание отмечено")


def require_fuel_entry(db: Session, vehicle_id: int, entry_id: int) -> VehicleFuelEntry:
    entry = db.scalar(select(VehicleFuelEntry).where(VehicleFuelEntry.id == entry_id, VehicleFuelEntry.vehicle_id == vehicle_id))
    if not entry: raise HTTPException(status_code=404, detail="Заправка не найдена")
    return entry


def validate_fuel(occurred_on: str, odometer: str, liters: str, total_cost: str, station: str, notes: str) -> dict[str, Any]:
    try:
        result = {"occurred_on": date.fromisoformat(occurred_on), "odometer": parse_optional_int(odometer, "Пробег", 0, 10_000_000), "liters": parse_optional_decimal(liters, "Литры", Decimal("0.001")), "total_cost": require_positive_money(total_cost, "Стоимость")}
    except (ValueError, HTTPException) as exc: raise ValueError(str(getattr(exc, "detail", "Некорректные данные"))) from exc
    if result["odometer"] is None or result["liters"] is None: raise ValueError("Укажите дату, пробег и количество литров.")
    if len(station.strip()) > 180 or len(notes.strip()) > 4000: raise ValueError("Одно из полей слишком длинное.")
    result.update({"price_per_liter": (result["total_cost"] / result["liters"]).quantize(Decimal("0.001")), "fuel_station": station.strip() or None, "notes": notes.strip() or None})
    return result


@app.get("/vehicles/{vehicle_id}/fuel")
def vehicle_fuel_page(request: Request, vehicle_id: int, date_from: str = "", date_to: str = "", sort: str = "date_desc", page: int = 1, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vehicle = require_owned_vehicle(db, vehicle_id, user); entries = db.scalars(select(VehicleFuelEntry).where(VehicleFuelEntry.vehicle_id == vehicle.id).order_by(desc(VehicleFuelEntry.occurred_on), desc(VehicleFuelEntry.id))).all()
    if date_from: entries = [entry for entry in entries if entry.occurred_on >= date.fromisoformat(date_from)]
    if date_to: entries = [entry for entry in entries if entry.occurred_on <= date.fromisoformat(date_to)]
    all_entries = db.scalars(select(VehicleFuelEntry).where(VehicleFuelEntry.vehicle_id == vehicle.id)).all()
    segments = [segment for segment in fuel_segments(all_entries) if (not date_from or segment.end.occurred_on >= date.fromisoformat(date_from)) and (not date_to or segment.end.occurred_on <= date.fromisoformat(date_to))]
    sort_fields = {"date_desc": ("occurred_on", True), "date_asc": ("occurred_on", False), "odometer_desc": ("odometer", True), "odometer_asc": ("odometer", False), "liters_desc": ("liters", True), "liters_asc": ("liters", False), "cost_desc": ("total_cost", True), "cost_asc": ("total_cost", False)}
    sort = sort if sort in sort_fields else "date_desc"; field, reverse = sort_fields[sort]; entries.sort(key=lambda entry: (getattr(entry, field), entry.id), reverse=reverse)
    total, page_size = len(entries), 25; page_count = max(1, (total + page_size - 1) // page_size); page = max(1, min(page, page_count)); display_entries = entries[(page - 1) * page_size:page * page_size]
    query = urlencode({key: value for key, value in {"date_from": date_from, "date_to": date_to, "sort": sort}.items() if value})
    return render(request, "vehicle_fuel.html", {"user": user, "vehicle": vehicle, "entries": display_entries, "summary": fuel_summary(entries, segments), "segments": segments, "filters": {"date_from": date_from, "date_to": date_to}, "sort": sort, "page": page, "page_count": page_count, "query_string": query})


@app.get("/vehicles/{vehicle_id}/fuel/new")
def vehicle_fuel_new(request: Request, vehicle_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return render(request, "vehicle_fuel_form.html", {"user": user, "vehicle": require_owned_vehicle(db, vehicle_id, user), "entry": None, "today": msk_today()})


@app.post("/vehicles/{vehicle_id}/fuel")
def vehicle_fuel_create(request: Request, vehicle_id: int, occurred_on: str = Form(""), odometer: str = Form(""), liters: str = Form(""), total_cost: str = Form(""), full_tank: str | None = Form(None), fuel_station: str = Form(""), notes: str = Form(""), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vehicle = require_owned_vehicle(db, vehicle_id, user)
    try: data = validate_fuel(occurred_on, odometer, liters, total_cost, fuel_station, notes)
    except ValueError as exc: return render(request, "vehicle_fuel_form.html", {"user": user, "vehicle": vehicle, "entry": None, "today": msk_today(), "error": str(exc), "form": locals()})
    entry = VehicleFuelEntry(vehicle_id=vehicle.id, full_tank=full_tank is not None, **data); db.add(entry); vehicle.current_odometer = max(vehicle.current_odometer, data["odometer"]); db.commit()
    return redirect_notice(f"/vehicles/{vehicle.id}/fuel", "Заправка добавлена")


@app.get("/vehicles/{vehicle_id}/fuel/{entry_id}/edit")
def vehicle_fuel_edit_page(request: Request, vehicle_id: int, entry_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vehicle = require_owned_vehicle(db, vehicle_id, user); entry = require_fuel_entry(db, vehicle.id, entry_id)
    return render(request, "vehicle_fuel_form.html", {"user": user, "vehicle": vehicle, "entry": entry, "today": msk_today(), "form": {"occurred_on": entry.occurred_on.isoformat(), "odometer": entry.odometer, "liters": entry.liters, "total_cost": entry.total_cost, "full_tank": entry.full_tank, "fuel_station": entry.fuel_station or "", "notes": entry.notes or ""}})


@app.post("/vehicles/{vehicle_id}/fuel/{entry_id}/edit")
def vehicle_fuel_update(request: Request, vehicle_id: int, entry_id: int, occurred_on: str = Form(""), odometer: str = Form(""), liters: str = Form(""), total_cost: str = Form(""), full_tank: str | None = Form(None), fuel_station: str = Form(""), notes: str = Form(""), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vehicle = require_owned_vehicle(db, vehicle_id, user); entry = require_fuel_entry(db, vehicle.id, entry_id)
    try: data = validate_fuel(occurred_on, odometer, liters, total_cost, fuel_station, notes)
    except ValueError as exc: return render(request, "vehicle_fuel_form.html", {"user": user, "vehicle": vehicle, "entry": entry, "today": msk_today(), "error": str(exc), "form": locals()})
    for key, value in data.items(): setattr(entry, key, value)
    entry.full_tank = full_tank is not None; vehicle.current_odometer = max(vehicle.current_odometer, data["odometer"]); db.commit()
    return redirect_notice(f"/vehicles/{vehicle.id}/fuel", "Заправка сохранена")


@app.post("/vehicles/{vehicle_id}/fuel/{entry_id}/delete")
def vehicle_fuel_delete(vehicle_id: int, entry_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vehicle = require_owned_vehicle(db, vehicle_id, user); db.delete(require_fuel_entry(db, vehicle.id, entry_id)); db.commit(); return redirect_notice(f"/vehicles/{vehicle.id}/fuel", "Заправка удалена")


@app.post("/recipes/{recipe_id}/favorite")
def recipe_favorite_toggle(recipe_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    recipe = db.get(Recipe, recipe_id)
    if not recipe or recipe.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Recipe not found")
    recipe.is_favorite = not recipe.is_favorite
    db.commit()
    return redirect("/recipes")


@app.get("/recipes")
def recipes_page(
    request: Request,
    q: str = "",
    tag: str = "",
    max_time: str = "",
    max_cost: str = "",
    servings_min: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = select(Recipe).options(selectinload(Recipe.owner)).order_by(desc(Recipe.created_at))
    q_clean = q.strip()
    tag_clean = tag.strip().lower()
    if q_clean:
        like = f"%{q_clean}%"
        stmt = stmt.where(or_(Recipe.title.ilike(like), Recipe.ingredients.ilike(like), Recipe.steps.ilike(like), Recipe.tags.ilike(like)))
    if tag_clean:
        stmt = stmt.where(Recipe.tags.ilike(f"%{tag_clean}%"))
    max_time_value = parse_optional_int(max_time, "Время", 0) if max_time.strip() else None
    max_cost_value = optional_nonnegative_money(max_cost, "Стоимость") if max_cost.strip() else None
    servings_min_value = parse_optional_decimal(servings_min, "Порции", Decimal("0.1")) if servings_min.strip() else None
    if max_time_value is not None:
        stmt = stmt.where(Recipe.cook_time_minutes <= max_time_value)
    if max_cost_value is not None:
        stmt = stmt.where(Recipe.cost <= max_cost_value)
    if servings_min_value is not None:
        stmt = stmt.where(Recipe.servings >= servings_min_value)
    recipes = db.scalars(stmt).all()
    tag_counts: dict[str, int] = defaultdict(int)
    for recipe in db.scalars(select(Recipe).where(Recipe.owner_id == user.id)).all():
        for one_tag in (recipe.tags or "").replace(";", ",").split(","):
            clean = one_tag.strip()
            if clean:
                tag_counts[clean] += 1
    return render(request, "recipes.html", {
        "user": user,
        "recipes": recipes,
        "q": q_clean,
        "tag": tag_clean,
        "max_time": max_time,
        "max_cost": max_cost,
        "servings_min": servings_min,
        "tags": sorted(tag_counts),
    })


@app.get("/recipes/new")
def recipe_new_page(request: Request, user: User = Depends(get_current_user)):
    return render(request, "recipe_form.html", {"user": user, "recipe": None, "action": "/recipes/new", "steps": recipe_steps_for_form(None)})


@app.post("/recipes/new")
def recipe_create(
    title: str = Form(...),
    source_url: str = Form(""),
    ingredients: str = Form(...),
    tags: str = Form(""),
    cook_time_minutes: str = Form(""),
    cost: str = Form(""),
    servings: str = Form(""),
    image_file: UploadFile | None = File(None),
    step_text: list[str] | None = Form(None),
    step_minutes: list[str] | None = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    recipe = Recipe(
        owner_id=user.id,
        title=clean_required_text(title, "Название", 150),
        source_url=clean_http_url(source_url, "Источник", 500),
        ingredients=clean_required_text(ingredients, "Ингредиенты", 20_000),
        tags=tags.strip()[:250] or None,
        cook_time_minutes=parse_optional_int(cook_time_minutes, "Время готовки", 0, 10_000),
        cost=optional_nonnegative_money(cost, "Стоимость"),
        servings=parse_optional_decimal(servings, "Порции", Decimal("0.1")),
        image_path=save_recipe_image(image_file),
        steps=serialize_recipe_steps(step_text, step_minutes),
    )
    db.add(recipe)
    db.commit()
    return redirect_notice(f"/recipes/{recipe.id}", "Рецепт создан")



@app.get("/recipes/import")
def recipe_import_page(request: Request, user: User = Depends(get_current_user)):
    return render(
        request,
        "recipe_import.html",
        {
            "user": user,
            "recipe_json": "",
            "prompt_text": recipe_json_prompt_text(),
        },
    )


@app.post("/recipes/import")
def recipe_import_create(
    request: Request,
    recipe_json: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    raw_json = recipe_json.strip()
    try:
        payload = json.loads(raw_json)
        normalized = normalize_recipe_import_payload(payload)
    except json.JSONDecodeError as exc:
        return render(
            request,
            "recipe_import.html",
            {
                "user": user,
                "recipe_json": raw_json,
                "prompt_text": recipe_json_prompt_text(),
                "error": f"JSON не читается: {exc.msg}",
            },
        )
    except (ValueError, HTTPException) as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return render(
            request,
            "recipe_import.html",
            {
                "user": user,
                "recipe_json": raw_json,
                "prompt_text": recipe_json_prompt_text(),
                "error": detail,
            },
        )

    recipe = Recipe(owner_id=user.id, **normalized)
    db.add(recipe)
    db.commit()
    return redirect_notice(f"/recipes/{recipe.id}", "Рецепт импортирован")


@app.get("/recipes/{recipe_id}")
def recipe_detail_page(
    request: Request,
    recipe_id: int,
    servings: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    recipe = db.scalar(select(Recipe).options(selectinload(Recipe.owner)).where(Recipe.id == recipe_id))
    if not recipe:
        raise HTTPException(status_code=404, detail="Рецепт не найден")

    base_servings = Decimal(str(recipe.servings)) if recipe.servings is not None else None
    target_servings = parse_optional_decimal(servings, "Порции", Decimal("0.1")) if servings.strip() else base_servings
    scale_ratio = None
    if base_servings is not None and base_servings > 0 and target_servings is not None:
        scale_ratio = target_servings / base_servings

    scaled_ingredients = scaled_ingredients_payload(recipe.ingredients, scale_ratio)
    scaled_ingredients_text = ingredients_text_from_payload(scaled_ingredients)
    price_estimate = recipe_price_estimate(db, user, recipe, scale_ratio)
    timer = db.scalar(
        select(RecipeCookingTimer).where(
            RecipeCookingTimer.owner_id == user.id,
            RecipeCookingTimer.recipe_id == recipe.id,
        )
    )

    return render(
        request,
        "recipe_detail.html",
        {
            "user": user,
            "recipe": recipe,
            "steps": parse_recipe_steps(recipe.steps),
            "cook_steps": cooking_steps_payload(recipe, scaled_ingredients_text),
            "base_servings": base_servings,
            "target_servings": target_servings,
            "scale_ratio": scale_ratio,
            "scaled_ingredients": scaled_ingredients,
            "price_estimate": price_estimate,
            "timer_status": cooking_timer_status(timer),
        },
    )


@app.post("/recipes/{recipe_id}/cost-from-prices")
def recipe_cost_from_prices(
    recipe_id: int,
    servings: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    recipe = db.get(Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Рецепт не найден")
    if recipe.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Обновить стоимость можно только в своём рецепте")
    base_servings = Decimal(str(recipe.servings)) if recipe.servings is not None else None
    target_servings = parse_optional_decimal(servings, "Порции", Decimal("0.1")) if servings.strip() else base_servings
    ratio = target_servings / base_servings if base_servings and target_servings else None
    estimate = recipe_price_estimate(db, user, recipe, ratio)
    if estimate["total"] <= 0:
        return redirect(f"/recipes/{recipe.id}")
    recipe.cost = estimate["total"]
    recipe.updated_at = utc_now_naive()
    db.commit()
    suffix = f"?servings={format_decimal(target_servings)}" if target_servings is not None and target_servings != base_servings else ""
    return redirect_notice(f"/recipes/{recipe.id}{suffix}", "Стоимость рецепта обновлена")


@app.get("/recipes/{recipe_id}/timer/status")
def recipe_timer_status(
    recipe_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    timer = db.scalar(
        select(RecipeCookingTimer).where(
            RecipeCookingTimer.owner_id == user.id,
            RecipeCookingTimer.recipe_id == recipe_id,
        )
    )
    return JSONResponse(cooking_timer_status(timer))


@app.post("/recipes/{recipe_id}/timer/start")
def recipe_timer_start(
    recipe_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    recipe = db.get(Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Рецепт не найден")
    timer = get_or_create_cooking_timer(db, user.id, recipe_id)
    if not timer.is_running:
        timer.started_at = utc_now_naive()
        timer.stopped_at = None
        timer.last_reminded_at = None
        timer.is_running = True
    db.commit()
    db.refresh(timer)
    return JSONResponse(cooking_timer_status(timer))


@app.post("/recipes/{recipe_id}/timer/stop")
def recipe_timer_stop(
    recipe_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    timer = db.scalar(
        select(RecipeCookingTimer).where(
            RecipeCookingTimer.owner_id == user.id,
            RecipeCookingTimer.recipe_id == recipe_id,
        )
    )
    if not timer:
        return JSONResponse(cooking_timer_status(None))
    if timer.is_running:
        total_seconds = cooking_timer_seconds(timer)
        timer.elapsed_seconds = total_seconds
        timer.started_at = None
        timer.stopped_at = utc_now_naive()
        timer.is_running = False
        db.commit()
        db.refresh(timer)
    return JSONResponse(cooking_timer_status(timer))


@app.post("/recipes/{recipe_id}/timer/reset")
def recipe_timer_reset(
    recipe_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    timer = db.scalar(
        select(RecipeCookingTimer).where(
            RecipeCookingTimer.owner_id == user.id,
            RecipeCookingTimer.recipe_id == recipe_id,
        )
    )
    if timer:
        timer.elapsed_seconds = 0
        timer.started_at = None
        timer.stopped_at = None
        timer.is_running = False
        timer.last_reminded_at = None
        db.commit()
        db.refresh(timer)
    return JSONResponse(cooking_timer_status(timer))


@app.post("/recipes/{recipe_id}/timer/record")
def recipe_timer_record(
    recipe_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    recipe = db.get(Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Рецепт не найден")
    if recipe.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Записать время можно только в свой рецепт")
    timer = db.scalar(
        select(RecipeCookingTimer).where(
            RecipeCookingTimer.owner_id == user.id,
            RecipeCookingTimer.recipe_id == recipe_id,
        )
    )
    seconds = cooking_timer_seconds(timer)
    minutes = cooking_timer_minutes(seconds)
    if minutes <= 0:
        raise HTTPException(status_code=400, detail="Таймер ещё не засёк время")
    if timer and timer.is_running:
        timer.elapsed_seconds = seconds
        timer.started_at = None
        timer.stopped_at = utc_now_naive()
        timer.is_running = False
    recipe.cook_time_minutes = minutes
    recipe.updated_at = utc_now_naive()
    db.commit()
    return JSONResponse({"ok": True, "minutes": minutes, "status": cooking_timer_status(timer)})


@app.get("/recipes/{recipe_id}/edit")
def recipe_edit_page(
    request: Request,
    recipe_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    recipe = db.get(Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Рецепт не найден")
    if recipe.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Редактировать можно только свой рецепт")
    return render(request, "recipe_form.html", {"user": user, "recipe": recipe, "action": f"/recipes/{recipe.id}/edit", "steps": recipe_steps_for_form(recipe)})


@app.post("/recipes/{recipe_id}/edit")
def recipe_update(
    recipe_id: int,
    title: str = Form(...),
    source_url: str = Form(""),
    ingredients: str = Form(...),
    tags: str = Form(""),
    cook_time_minutes: str = Form(""),
    cost: str = Form(""),
    servings: str = Form(""),
    image_file: UploadFile | None = File(None),
    remove_image: str | None = Form(None),
    step_text: list[str] | None = Form(None),
    step_minutes: list[str] | None = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    recipe = db.get(Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Рецепт не найден")
    if recipe.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Редактировать можно только свой рецепт")

    recipe.title = clean_required_text(title, "Название", 150)
    recipe.source_url = clean_http_url(source_url, "Источник", 500)
    recipe.ingredients = clean_required_text(ingredients, "Ингредиенты", 20_000)
    recipe.tags = tags.strip()[:250] or None
    recipe.cook_time_minutes = parse_optional_int(cook_time_minutes, "Время готовки", 0, 10_000)
    recipe.cost = optional_nonnegative_money(cost, "Стоимость")
    recipe.servings = parse_optional_decimal(servings, "Порции", Decimal("0.1"))
    recipe.steps = serialize_recipe_steps(step_text, step_minutes)
    previous_image_path = recipe.image_path
    new_image_path = save_recipe_image(image_file)
    if new_image_path:
        recipe.image_path = new_image_path
    elif remove_image == "1":
        recipe.image_path = None
    try:
        db.commit()
    except Exception:
        db.rollback()
        if new_image_path:
            delete_recipe_image(new_image_path)
        raise
    if previous_image_path and previous_image_path != recipe.image_path:
        delete_recipe_image(previous_image_path)
    return redirect_notice(f"/recipes/{recipe.id}", "Рецепт сохранён")


@app.post("/recipes/{recipe_id}/delete")
def recipe_delete(
    recipe_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    recipe = db.get(Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Рецепт не найден")
    if recipe.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Удалять можно только свой рецепт")
    delete_recipe_image(recipe.image_path)
    db.execute(sql_delete(MenuItem).where(MenuItem.recipe_id == recipe.id))
    db.execute(sql_delete(RecipeCookingTimer).where(RecipeCookingTimer.recipe_id == recipe.id))
    db.delete(recipe)
    db.commit()
    return redirect_notice("/recipes", "Рецепт удалён")


@app.get("/income")
def income_page(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    items = db.scalars(select(IncomeItem).where(IncomeItem.owner_id == user.id).order_by(desc(IncomeItem.received_at))).all()
    current_start, current_end = expense_period_bounds(msk_today(), expense_period_start_day(user))
    period_total = sum((item.amount for item in items if current_start <= msk_date(item.received_at) <= current_end), Decimal("0.00"))
    return render(request, "income.html", {"user": user, "items": items, "today": msk_today(), "period_total": period_total, "period_label": format_period_range(current_start, current_end)})


@app.post("/income")
def income_create(
    title: str = Form(...),
    amount: str = Form(...),
    income_date: str = Form(""),
    source: str = Form(""),
    note: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    amount_value = require_positive_money(amount)
    received_on = parse_date(income_date, msk_today()) or msk_today()
    db.add(
        IncomeItem(
            owner_id=user.id,
            title=clean_required_text(title, "Название", 150),
            amount=amount_value,
            source=clean_optional_text(source, 120),
            note=clean_optional_text(note, 4000),
            received_at=msk_date_to_utc_naive(received_on),
        )
    )
    db.commit()
    return redirect_notice("/income", "Доход добавлен")


@app.post("/income/{income_id}/update")
def income_update(
    income_id: int,
    title: str = Form(...),
    amount: str = Form(...),
    income_date: str = Form(...),
    source: str = Form(""),
    note: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = db.get(IncomeItem, income_id)
    if not item or item.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Income not found")
    amount_value = require_positive_money(amount)
    item.title = clean_required_text(title, "Название", 150)
    item.amount = amount_value
    item.source = clean_optional_text(source, 120)
    item.note = clean_optional_text(note, 4000)
    item.received_at = msk_date_to_utc_naive(parse_date(income_date, msk_date(item.received_at)) or msk_today(), item.received_at)
    db.commit()
    return redirect_notice("/income", "Доход обновлён")


@app.post("/income/{income_id}/delete")
def income_delete(income_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = db.get(IncomeItem, income_id)
    if not item or item.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Income not found")
    db.delete(item)
    db.commit()
    return redirect_notice("/income", "Доход удалён")


@app.get("/finance")
def finance_page(
    request: Request,
    from_date: str = "",
    to_date: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    today = msk_today()
    period_start, period_end = expense_period_bounds(today, expense_period_start_day(user))
    date_from = parse_date(from_date, period_start) or period_start
    date_to = parse_date(to_date, period_end) or period_end
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    incomes = db.scalars(select(IncomeItem).where(IncomeItem.owner_id == user.id)).all()
    lists = accessible_expense_lists(db, user)
    cashflow = summarize_cashflow(
        lists,
        incomes,
        date_from=date_from,
        date_to=date_to,
        today=today,
    )
    snapshot = build_finance_snapshot(db, user, today=today)
    forecast_info = snapshot.forecast.as_legacy_dict()
    chart_days: list[dict[str, Any]] = []
    chart_max = Decimal("0.00")
    cumulative_balance = Decimal("0.00")
    current_day = date_from
    while current_day <= date_to:
        income = cashflow.income_by_day.get(current_day, Decimal("0.00"))
        expense = cashflow.expense_by_day.get(current_day, Decimal("0.00"))
        forecast_expense = forecast_info["daily_forecast"].get(current_day, Decimal("0.00"))
        cumulative_balance += income - expense - forecast_expense
        chart_max = max(chart_max, income, expense, forecast_expense)
        chart_days.append({"label": current_day.strftime("%d.%m"), "income": income, "expense": expense, "forecast_expense": forecast_expense, "net": income - expense - forecast_expense, "balance": cumulative_balance, "is_forecast": forecast_expense > 0, "has_recurring": forecast_info["recurring_by_day"].get(current_day, Decimal("0.00")) > 0})
        current_day += timedelta(days=1)
    for day in chart_days:
        day["income_height"] = float(day["income"] / chart_max * 100) if chart_max else 0
        day["expense_height"] = float(day["expense"] / chart_max * 100) if chart_max else 0
        day["forecast_height"] = float(day["forecast_expense"] / chart_max * 100) if chart_max else 0
    return render(request, "finance.html", {"user": user, "from_date": date_from.isoformat(), "to_date": date_to.isoformat(), "income_total": cashflow.income_total, "expense_total": cashflow.expense_total, "balance": cashflow.balance, "savings_rate": cashflow.savings_rate, "forecast_info": forecast_info, "period_income": snapshot.period_income_total, "forecast_balance": snapshot.forecast_balance, "period_label": format_period_range(period_start, period_end), "cashflow_chart": chart_days, "chart_has_forecast": any(day["is_forecast"] for day in chart_days)})


@app.get("/expenses")
def expenses_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    owned = db.scalars(
        select(ExpenseList)
        .options(selectinload(ExpenseList.categories).selectinload(ExpenseCategory.items), selectinload(ExpenseList.shares))
        .where(ExpenseList.owner_id == user.id)
        .order_by(desc(ExpenseList.created_at))
    ).all()
    shared = db.scalars(
        select(ExpenseList)
        .join(ExpenseListShare)
        .options(selectinload(ExpenseList.owner), selectinload(ExpenseList.categories).selectinload(ExpenseCategory.items))
        .where(ExpenseListShare.user_id == user.id)
        .order_by(desc(ExpenseList.created_at))
    ).all()
    recent_expenses = []
    for expense_list in list(owned) + list(shared):
        for category in expense_list.categories:
            for item in category.items:
                recent_expenses.append({"item": item, "category": category, "expense_list": expense_list})
    recent_expenses.sort(key=lambda row: row["item"].created_at, reverse=True)
    return render(request, "expenses.html", {"user": user, "owned": owned, "shared": shared, "recent_expenses": recent_expenses[:8]})


@app.get("/expenses/analytics")
def expenses_analytics_page(
    request: Request,
    from_date: str = "",
    to_date: str = "",
    list_id: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    today = msk_today()
    period_start_day = expense_period_start_day(user)
    current_period_start, current_period_end = expense_period_bounds(today, period_start_day)
    date_from = parse_date(from_date, current_period_start)
    date_to = parse_date(to_date, today)
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from

    owned = db.scalars(
        select(ExpenseList)
        .options(selectinload(ExpenseList.categories).selectinload(ExpenseCategory.items))
        .where(ExpenseList.owner_id == user.id)
        .order_by(ExpenseList.title)
    ).all()
    shared = db.scalars(
        select(ExpenseList)
        .join(ExpenseListShare)
        .options(selectinload(ExpenseList.owner), selectinload(ExpenseList.categories).selectinload(ExpenseCategory.items))
        .where(ExpenseListShare.user_id == user.id)
        .order_by(ExpenseList.title)
    ).all()
    accessible_lists = list(owned) + list(shared)

    selected_list_id = parse_optional_int(list_id, "Список", 1) if list_id.strip() else None
    if selected_list_id:
        accessible_lists = [item for item in accessible_lists if item.id == selected_list_id]

    by_day: dict[date, Decimal] = defaultdict(lambda: Decimal("0.00"))
    forecast_eligible_by_day: dict[date, Decimal] = defaultdict(lambda: Decimal("0.00"))
    by_category: dict[int, Decimal] = defaultdict(lambda: Decimal("0.00"))
    by_day_category: dict[date, dict[int, Decimal]] = defaultdict(lambda: defaultdict(lambda: Decimal("0.00")))
    category_metadata: dict[int, dict[str, str]] = {}
    top_items = []

    for expense_list in accessible_lists:
        for category in expense_list.categories:
            for item in category.items:
                if not item.include_in_analytics:
                    continue
                item_date = msk_date(item.created_at)
                if date_from and item_date < date_from:
                    continue
                if date_to and item_date > date_to:
                    continue
                by_day[item_date] += item.amount
                if item.include_in_forecast:
                    forecast_eligible_by_day[item_date] += item.amount
                category_metadata[category.id] = {"name": category.name, "list_title": expense_list.title}
                by_category[category.id] += item.amount
                by_day_category[item_date][category.id] += item.amount
                top_items.append({
                    "title": item.title,
                    "amount": item.amount,
                    "date": item_date,
                    "category": category.name,
                    "list_title": expense_list.title,
                })

    total = sum(by_day.values(), Decimal("0.00"))
    days_count = ((date_to - date_from).days + 1) if date_from and date_to else max(len(by_day), 1)
    forecast_eligible_total = sum(forecast_eligible_by_day.values(), Decimal("0.00"))
    average = (forecast_eligible_total / Decimal(days_count)).quantize(Decimal("0.01")) if days_count else Decimal("0.00")
    max_day_total = max(by_day.values(), default=Decimal("0.00"))
    max_category_total = max(by_category.values(), default=Decimal("0.00"))

    category_ids = [category_id for category_id, _ in sorted(by_category.items(), key=lambda pair: pair[1], reverse=True)]
    category_labels = {
        category_id: f"{category_metadata[category_id]['name']} · {category_metadata[category_id]['list_title']}"
        for category_id in category_ids
    }
    category_colors = category_color_map([category_labels[category_id] for category_id in category_ids])

    daily_chart = []
    chart_dates: list[date] = []
    if date_from and date_to:
        current = date_from
        while current <= date_to:
            chart_dates.append(current)
            current += timedelta(days=1)
    else:
        chart_dates = [current for current, _ in sorted(by_day.items())]

    for current in chart_dates:
        day_total = by_day[current]
        segments = []
        for category_id in category_ids:
            value = by_day_category[current].get(category_id, Decimal("0.00"))
            if value <= 0:
                continue
            segments.append({
                "name": category_metadata[category_id]["name"],
                "list_title": category_metadata[category_id]["list_title"],
                "id": category_id,
                "total": value,
                "height": float((value / max_day_total) * 100) if max_day_total else 0,
                "color": category_colors[category_labels[category_id]],
            })
        daily_chart.append({
            "date": current.isoformat(),
            "label": current.strftime("%d.%m"),
            "total": day_total,
            "height": float((day_total / max_day_total) * 100) if max_day_total else 0,
            "segments": segments,
        })

    category_chart = [
        {
            "name": category_metadata[category_id]["name"],
            "list_title": category_metadata[category_id]["list_title"],
            "id": category_id,
            "total": value,
            "percent": int((value / max_category_total) * 100) if max_category_total else 0,
            "color": category_colors[category_labels[category_id]],
        }
        for category_id, value in sorted(by_category.items(), key=lambda pair: pair[1], reverse=True)
    ]
    top_items.sort(key=lambda item: item["amount"], reverse=True)

    current_month_start, current_month_end = current_period_start, current_period_end
    period_lists = accessible_lists if selected_list_id else list(owned) + list(shared)
    snapshot = build_finance_snapshot(
        db,
        user,
        today=today,
        expense_list_ids={item.id for item in period_lists} if selected_list_id else None,
    )
    current_month_total = snapshot.period_expense_total
    previous_month_total = snapshot.previous_period_expense_total
    month_diff = snapshot.period_difference
    forecast_info = snapshot.forecast.as_legacy_dict()
    forecast = forecast_info["forecast"]
    limit_rows = [
        {
            "category": item.category,
            "limit": item.limit,
            "spent": item.spent,
            "left": item.left,
            "percent": item.percent,
        }
        for item in snapshot.budget.categories
    ]

    return render(
        request,
        "expense_analytics.html",
        {
            "user": user,
            "expense_lists": list(owned) + list(shared),
            "selected_list_id": selected_list_id,
            "from_date": date_from.isoformat() if date_from else "",
            "to_date": date_to.isoformat() if date_to else "",
            "total": total,
            "average": average,
            "days_count": days_count,
            "daily_chart": daily_chart,
            "category_chart": category_chart,
            "top_items": top_items[:7],
            "current_month_total": current_month_total,
            "previous_month_total": previous_month_total,
            "month_diff": month_diff,
            "forecast": forecast,
            "forecast_info": forecast_info,
            "limit_rows": limit_rows,
            "limit_total": snapshot.budget.limit_total,
            "limit_spent": snapshot.budget.spent,
            "limit_left": snapshot.budget.left,
            "limit_percent": snapshot.budget.percent,
            "period_start_day": period_start_day,
            "current_period_start": current_month_start,
            "current_period_end": current_month_end,
            "current_period_label": format_period_range(current_month_start, current_month_end),
        },
    )


@app.get("/expenses/categories/{category_id}/analytics")
def expense_category_analytics_page(
    request: Request,
    category_id: int,
    from_date: str = "",
    to_date: str = "",
    list_id: str = "",
    sort: str = "date_desc",
    page: int = 1,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    category, expense_list = require_expense_category_access(db, category_id, user)
    today = msk_today()
    default_start, _default_end = expense_period_bounds(today, expense_period_start_day(user))
    date_from = parse_date(from_date, default_start) or default_start
    date_to = parse_date(to_date, today) or today
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    allowed_sorts = {
        "date_desc": (desc(ExpenseItem.created_at), desc(ExpenseItem.id)),
        "date_asc": (asc(ExpenseItem.created_at), asc(ExpenseItem.id)),
        "amount_desc": (desc(ExpenseItem.amount), desc(ExpenseItem.id)),
        "amount_asc": (asc(ExpenseItem.amount), asc(ExpenseItem.id)),
    }
    selected_sort = sort if sort in allowed_sorts else "date_desc"
    safe_page = max(1, page)
    page_size = 25
    start_utc, _ = msk_day_utc_bounds(date_from)
    _, end_utc = msk_day_utc_bounds(date_to)
    filters = (
        ExpenseItem.category_id == category.id,
        ExpenseItem.include_in_analytics.is_(True),
        ExpenseItem.created_at >= start_utc,
        ExpenseItem.created_at <= end_utc,
    )
    total_count = db.scalar(select(func.count()).select_from(ExpenseItem).where(*filters)) or 0
    total_amount = db.scalar(select(func.coalesce(func.sum(ExpenseItem.amount), 0)).where(*filters)) or Decimal("0.00")
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    safe_page = min(safe_page, total_pages)
    items = db.scalars(
        select(ExpenseItem)
        .where(*filters)
        .order_by(*allowed_sorts[selected_sort])
        .offset((safe_page - 1) * page_size)
        .limit(page_size)
    ).all()
    return render(
        request,
        "expense_category_analytics.html",
        {
            "user": user,
            "category": category,
            "expense_list": expense_list,
            "items": items,
            "from_date": date_from.isoformat(),
            "to_date": date_to.isoformat(),
            "selected_sort": selected_sort,
            "total_count": total_count,
            "total_amount": total_amount,
            "page": safe_page,
            "total_pages": total_pages,
            "return_list_id": list_id,
        },
    )


@app.post("/expenses/lists")
def expense_list_create(
    title: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    expense_list = ExpenseList(owner_id=user.id, title=clean_required_text(title, "Название", 120))
    db.add(expense_list)
    db.flush()
    seed_expense_list_limit_categories(db, user, expense_list)
    db.commit()
    return redirect_notice(f"/expenses/lists/{expense_list.id}", "Список создан")


@app.get("/expenses/lists/{list_id}")
def expense_list_page(
    request: Request,
    list_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    expense_list = require_expense_access(db, list_id, user)
    return render(
        request,
        "expense_list.html",
        {
            "user": user,
            "expense_list": expense_list,
            "category_rows": expense_category_rows(expense_list),
            "preview_limit": EXPENSE_CATEGORY_PREVIEW_LIMIT,
            "today": msk_today(),
            "category_options": expense_category_options(db, user),
        },
    )


@app.get("/expenses/categories/{category_id}")
def expense_category_page(
    request: Request,
    category_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    category, expense_list = require_expense_category_access(db, category_id, user)
    return render(
        request,
        "expense_category.html",
        {
            "user": user,
            "expense_list": expense_list,
            "category": category,
            "items": sorted_expense_items(category.items),
            "today": msk_today(),
        },
    )


@app.post("/expenses/lists/{list_id}/share")
def expense_list_share(
    request: Request,
    list_id: int,
    username: str = Form(...),
    can_edit: str = Form("1"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    expense_list = require_expense_access(db, list_id, user)
    if expense_list.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Делиться списком может только владелец")

    target = db.scalar(select(User).where(User.username == normalize_username(username)))
    if not target:
        return render(
            request,
            "expense_list.html",
            {
                "user": user,
                "expense_list": expense_list,
                "category_rows": expense_category_rows(expense_list),
                "preview_limit": EXPENSE_CATEGORY_PREVIEW_LIMIT,
                "today": msk_today(),
                "category_options": expense_category_options(db, user),
                "error": "Пользователь не найден",
            },
        )
    if target.id == user.id:
        return redirect(f"/expenses/lists/{list_id}")

    exists = db.scalar(
        select(ExpenseListShare).where(
            and_(ExpenseListShare.expense_list_id == list_id, ExpenseListShare.user_id == target.id)
        )
    )
    if exists:
        exists.can_edit = can_edit == "1"
    else:
        db.add(ExpenseListShare(expense_list_id=list_id, user_id=target.id, can_edit=can_edit == "1"))
    db.commit()
    return redirect(f"/expenses/lists/{list_id}")


@app.post("/expenses/lists/{list_id}/share/{share_id}/delete")
def expense_list_share_delete(
    list_id: int,
    share_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    expense_list = require_expense_access(db, list_id, user)
    if expense_list.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Закрыть доступ может только владелец")
    share = db.get(ExpenseListShare, share_id)
    if not share or share.expense_list_id != list_id:
        raise HTTPException(status_code=404, detail="Доступ не найден")
    db.delete(share)
    db.commit()
    return redirect(f"/expenses/lists/{list_id}")


@app.post("/expenses/lists/{list_id}/categories")
def expense_category_create(
    list_id: int,
    name: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    expense_list = require_expense_access(db, list_id, user, write=True)
    clean = clean_required_text(name, "Категория", 120)
    if not any(category.name.strip().casefold() == clean.casefold() for category in expense_list.categories):
        db.add(ExpenseCategory(expense_list_id=expense_list.id, name=clean))
    db.commit()
    return redirect_notice(f"/expenses/lists/{list_id}", "Категория добавлена")


@app.post("/expenses/categories/{category_id}/rename")
def expense_category_rename(
    category_id: int,
    name: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    category = db.scalar(
        select(ExpenseCategory).options(selectinload(ExpenseCategory.expense_list)).where(ExpenseCategory.id == category_id)
    )
    if not category:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    expense_list = require_expense_access(db, category.expense_list_id, user, write=True)
    clean = clean_required_text(name, "Категория", 120)
    duplicate = any(item.id != category.id and item.name.strip().casefold() == clean.casefold() for item in expense_list.categories)
    if duplicate:
        raise HTTPException(status_code=409, detail="Категория с таким названием уже существует")
    category.name = clean
    db.commit()
    return redirect(f"/expenses/lists/{expense_list.id}")


@app.post("/expenses/categories/{category_id}/delete")
def expense_category_delete(
    category_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    category = db.scalar(
        select(ExpenseCategory).options(selectinload(ExpenseCategory.expense_list)).where(ExpenseCategory.id == category_id)
    )
    if not category:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    expense_list = require_expense_access(db, category.expense_list_id, user, write=True)
    unlink_wishlist_expenses(db, [item.id for item in category.items])
    db.delete(category)
    db.commit()
    return redirect(f"/expenses/lists/{expense_list.id}")


@app.post("/expenses/lists/{list_id}/items")
def expense_item_create(
    list_id: int,
    category_id: int = Form(...),
    title: str = Form(...),
    amount: str = Form(...),
    expense_date: str = Form(""),
    exclude_from_analytics: str | None = Form(None),
    exclude_from_forecast: str | None = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    expense_list = require_expense_access(db, list_id, user, write=True)
    category = db.get(ExpenseCategory, category_id)
    if not category or category.expense_list_id != expense_list.id:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    amount_value = require_positive_money(amount)
    selected_date = parse_date(expense_date, msk_today()) or msk_today()
    db.add(
        ExpenseItem(
            category_id=category.id,
            title=clean_required_text(title, "Название", 150),
            amount=amount_value,
            created_at=msk_date_to_utc_naive(selected_date),
            include_in_analytics=exclude_from_analytics != "1",
            include_in_forecast=exclude_from_forecast != "1",
        )
    )
    db.commit()
    return redirect(f"/expenses/lists/{list_id}")


@app.post("/expenses/items/{item_id}/update")
def expense_item_update(
    item_id: int,
    category_id: int = Form(...),
    title: str = Form(...),
    amount: str = Form(...),
    expense_date: str = Form(...),
    return_to: str = Form(""),
    exclude_from_analytics: str | None = Form(None),
    exclude_from_forecast: str | None = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = db.scalar(
        select(ExpenseItem)
        .options(selectinload(ExpenseItem.category).selectinload(ExpenseCategory.expense_list))
        .where(ExpenseItem.id == item_id)
    )
    if not item:
        raise HTTPException(status_code=404, detail="Трата не найдена")
    expense_list = require_expense_access(db, item.category.expense_list_id, user, write=True)
    target_category = db.get(ExpenseCategory, category_id)
    if not target_category or target_category.expense_list_id != expense_list.id:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    amount_value = require_positive_money(amount)
    selected_date = parse_date(expense_date, msk_date(item.created_at) or msk_today()) or msk_today()
    item.category_id = target_category.id
    item.title = clean_required_text(title, "Название", 150)
    item.amount = amount_value
    item.include_in_analytics = exclude_from_analytics != "1"
    item.include_in_forecast = exclude_from_forecast != "1"
    item.created_at = msk_date_to_utc_naive(selected_date, item.created_at)
    db.commit()
    return redirect(safe_expense_return(return_to, f"/expenses/lists/{expense_list.id}"))


@app.post("/expenses/items/{item_id}/delete")
def expense_item_delete(
    item_id: int,
    return_to: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = db.scalar(
        select(ExpenseItem)
        .options(selectinload(ExpenseItem.category).selectinload(ExpenseCategory.expense_list))
        .where(ExpenseItem.id == item_id)
    )
    if not item:
        raise HTTPException(status_code=404, detail="Трата не найдена")
    expense_list = require_expense_access(db, item.category.expense_list_id, user, write=True)
    unlink_wishlist_expenses(db, [item.id])
    db.delete(item)
    db.commit()
    return redirect(safe_expense_return(return_to, f"/expenses/lists/{expense_list.id}"))


@app.post("/expenses/lists/{list_id}/delete")
def expense_list_delete(
    list_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    expense_list = require_expense_access(db, list_id, user)
    if expense_list.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Удалить список может только владелец")
    prepare_expense_list_delete(db, expense_list)
    db.delete(expense_list)
    db.commit()
    return redirect("/expenses")


@app.post("/presence/ping")
def presence_ping(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    touch_user_presence(db, user)
    return {"ok": True, **presence_info(user)}


@app.get("/api/chats/{thread_id}/presence")
def chat_target_presence(
    thread_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    thread = require_chat_access(db, thread_id, user)
    target = other_chat_user(thread, user)
    return {"user_id": target.id, "username": target.username, **presence_info(target)}


@app.get("/api/chats/{thread_id}/messages")
def chat_messages_since(
    thread_id: int,
    after_id: int = 0,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    thread = require_chat_access(db, thread_id, user)
    messages = db.scalars(
        select(ChatThreadMessage)
        .options(
            selectinload(ChatThreadMessage.sender),
            selectinload(ChatThreadMessage.reply_to).selectinload(ChatThreadMessage.sender),
        )
        .where(ChatThreadMessage.thread_id == thread.id, ChatThreadMessage.id > max(0, after_id))
        .order_by(ChatThreadMessage.id)
        .limit(200)
    ).all()
    if any(message.sender_id != user.id and not message.is_read for message in messages):
        mark_thread_read(db, thread.id, user)
    return {
        "messages": [chat_message_payload(message) for message in messages],
        "has_more": len(messages) == 200,
    }


@app.get("/chats")
def chats_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    threads = db.scalars(
        select(ChatThread)
        .options(selectinload(ChatThread.user_a), selectinload(ChatThread.user_b), selectinload(ChatThread.messages))
        .where(or_(ChatThread.user_a_id == user.id, ChatThread.user_b_id == user.id))
        .order_by(desc(ChatThread.is_pinned), desc(ChatThread.updated_at), desc(ChatThread.created_at))
    ).all()
    unread_by_thread = {
        row[0]: row[1]
        for row in db.execute(
            select(ChatThreadMessage.thread_id, func.count(ChatThreadMessage.id))
            .join(ChatThread)
            .where(
                or_(ChatThread.user_a_id == user.id, ChatThread.user_b_id == user.id),
                ChatThreadMessage.sender_id != user.id,
                ChatThreadMessage.is_read.is_(False),
            )
            .group_by(ChatThreadMessage.thread_id)
        ).all()
    }
    return render(request, "chats.html", {"user": user, "threads": threads, "unread_by_thread": unread_by_thread})


@app.post("/chats")
def chat_create(
    request: Request,
    username: str = Form(...),
    title: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    target = db.scalar(select(User).where(User.username == normalize_username(username)))
    if not target or target.id == user.id:
        threads = db.scalars(
            select(ChatThread)
            .options(selectinload(ChatThread.user_a), selectinload(ChatThread.user_b), selectinload(ChatThread.messages))
            .where(or_(ChatThread.user_a_id == user.id, ChatThread.user_b_id == user.id))
            .order_by(desc(ChatThread.is_pinned), desc(ChatThread.updated_at), desc(ChatThread.created_at))
        ).all()
        return render(request, "chats.html", {"user": user, "threads": threads, "unread_by_thread": {}, "error": "Пользователь не найден"})

    clean_title = (title.strip() or f"Чат с @{target.username}")[:160]
    thread = ChatThread(
        title=clean_title,
        created_by_id=user.id,
        user_a_id=user.id,
        user_b_id=target.id,
    )
    db.add(thread)
    db.commit()
    return redirect(f"/chats/{thread.id}")


@app.get("/chats/{thread_id}")
def chat_thread_page(
    request: Request,
    thread_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    thread = require_chat_access(db, thread_id, user)
    touch_user_presence(db, user, commit=False)
    mark_thread_read(db, thread.id, user)
    messages = db.scalars(
        select(ChatThreadMessage)
        .options(selectinload(ChatThreadMessage.sender), selectinload(ChatThreadMessage.reply_to).selectinload(ChatThreadMessage.sender))
        .where(ChatThreadMessage.thread_id == thread.id)
        .order_by(ChatThreadMessage.created_at, ChatThreadMessage.id)
    ).all()
    notes = db.scalars(
        select(ChatNote)
        .options(selectinload(ChatNote.author))
        .where(ChatNote.thread_id == thread.id)
        .order_by(desc(ChatNote.created_at))
    ).all()
    return render(
        request,
        "chat_thread.html",
        {"user": user, "thread": thread, "target": other_chat_user(thread, user), "messages": messages, "notes": notes},
    )


@app.post("/chats/{thread_id}")
def chat_send(
    thread_id: int,
    text: str = Form(""),
    reply_to_id: str = Form(""),
    attachment: UploadFile | None = File(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    thread = require_chat_access(db, thread_id, user)
    clean_text = text.strip()[:4000]
    attachment_path = save_chat_attachment(attachment)
    reply_value = int(reply_to_id) if reply_to_id.strip().isdigit() else None
    if reply_value:
        reply_exists = db.scalar(
            select(ChatThreadMessage.id).where(
                ChatThreadMessage.id == reply_value,
                ChatThreadMessage.thread_id == thread.id,
            )
        )
        if not reply_exists:
            reply_value = None
    if clean_text or attachment_path:
        other_id = thread.user_b_id if thread.user_a_id == user.id else thread.user_a_id
        other_is_active = other_id in chat_manager.active_user_ids(thread.id)
        message = ChatThreadMessage(
            thread_id=thread.id,
            sender_id=user.id,
            text=clean_text or "Файл",
            reply_to_id=reply_value,
            attachment_path=attachment_path,
            is_read=other_is_active,
        )
        db.add(message)
        thread.updated_at = datetime.now(UTC_TZ).replace(tzinfo=None)
        db.commit()
        # Push notification is sent to the recipient's subscribed devices even if the
        # user is currently active in another tab/device. This makes Android/PWA
        # notification shade delivery more predictable.
        send_push_to_user(db, other_id, message_push_payload(thread, user, message.text))
    return redirect(f"/chats/{thread.id}")


@app.post("/chats/{thread_id}/rename")
def chat_rename(
    thread_id: int,
    title: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    thread = require_chat_access(db, thread_id, user)
    thread.title = (title.strip() or thread.title)[:160]
    thread.updated_at = datetime.now(UTC_TZ).replace(tzinfo=None)
    db.commit()
    return redirect(f"/chats/{thread.id}")


@app.post("/chats/{thread_id}/delete")
def chat_delete(
    thread_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    thread = require_chat_access(db, thread_id, user)
    if thread.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="Удалить чат может только создатель")
    for message in thread.messages:
        delete_media_file(message.attachment_path)
    db.delete(thread)
    db.commit()
    return redirect("/chats")


@app.post("/chats/{thread_id}/pin")
def chat_pin_toggle(
    thread_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    thread = require_chat_access(db, thread_id, user)
    thread.is_pinned = not thread.is_pinned
    db.commit()
    return redirect(f"/chats/{thread.id}")


@app.post("/chats/{thread_id}/notes")
def chat_note_add(
    thread_id: int,
    text: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    thread = require_chat_access(db, thread_id, user)
    clean = text.strip()[:4000]
    if clean:
        db.add(ChatNote(thread_id=thread.id, author_id=user.id, text=clean))
        db.commit()
    return redirect(f"/chats/{thread.id}")


@app.post("/chats/{thread_id}/notes/{note_id}/delete")
def chat_note_delete(
    thread_id: int,
    note_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    thread = require_chat_access(db, thread_id, user)
    note = db.get(ChatNote, note_id)
    if note and note.thread_id == thread.id and note.author_id == user.id:
        db.delete(note)
        db.commit()
    return redirect(f"/chats/{thread.id}")


@app.websocket("/ws/chats/{thread_id}")
async def chat_socket(websocket: WebSocket, thread_id: int):
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host", "")
    if (origin and not same_origin(origin, host)) or (settings.enforce_same_origin and not origin):
        await websocket.close(code=4403)
        return
    user_id = websocket.session.get("user_id")
    if not user_id:
        await websocket.close(code=4401)
        return

    db = SessionLocal()
    try:
        user = db.get(User, int(user_id))
        if not user:
            await websocket.close(code=4401)
            return
        thread = db.get(ChatThread, thread_id)
        if not thread or user.id not in (thread.user_a_id, thread.user_b_id):
            await websocket.close(code=4403)
            return

        await chat_manager.connect(thread_id, user.id, websocket)
        touch_user_presence(db, user)
        await chat_manager.broadcast(thread_id, {"type": "presence", "user_id": user.id, "username": user.username, **presence_info(user)})
        mark_thread_read(db, thread_id, user)
        while True:
            packet = await websocket.receive()
            if packet.get("type") == "websocket.disconnect":
                break
            raw_message = packet.get("text")
            if not isinstance(raw_message, str):
                await websocket.close(code=1003)
                break
            if len(raw_message.encode("utf-8")) > 32 * 1024:
                await websocket.close(code=1009)
                break
            try:
                data = json.loads(raw_message)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            event_type = str(data.get("type", "message"))

            if event_type == "ping":
                touch_user_presence(db, user)
                await chat_manager.broadcast(thread_id, {"type": "presence", "user_id": user.id, "username": user.username, **presence_info(user)})
                continue

            if event_type == "typing":
                await chat_manager.broadcast(
                    thread_id,
                    {
                        "type": "typing",
                        "user_id": user.id,
                        "username": user.username,
                        "is_typing": bool(data.get("is_typing")),
                    },
                )
                continue

            clean_text = str(data.get("text", "")).strip()[:4000]
            reply_value = data.get("reply_to_id")
            try:
                reply_value = int(reply_value) if reply_value else None
            except (TypeError, ValueError):
                reply_value = None
            reply_message = None
            if reply_value:
                reply_message = db.scalar(
                    select(ChatThreadMessage)
                    .options(selectinload(ChatThreadMessage.sender))
                    .where(ChatThreadMessage.id == reply_value, ChatThreadMessage.thread_id == thread_id)
                )
                if not reply_message:
                    reply_value = None
            if not clean_text:
                continue
            touch_user_presence(db, user, commit=False)
            other_id = thread.user_b_id if thread.user_a_id == user.id else thread.user_a_id
            message = ChatThreadMessage(thread_id=thread_id, sender_id=user.id, text=clean_text, reply_to_id=reply_value, is_read=other_id in chat_manager.active_user_ids(thread_id))
            thread.updated_at = utc_now_naive()
            db.add(message)
            db.commit()
            db.refresh(message)
            push_payload = message_push_payload(thread, user, message.text)
            await chat_manager.broadcast(thread_id, chat_message_payload(message, user, reply_message))
            await chat_manager.broadcast(thread_id, {"type": "presence", "user_id": user.id, "username": user.username, **presence_info(user)})
            await asyncio.to_thread(send_push_to_user_isolated, other_id, push_payload)
    except WebSocketDisconnect:
        pass
    finally:
        chat_manager.disconnect(thread_id, websocket)
        try:
            if 'user' in locals() and user:
                await chat_manager.broadcast(thread_id, {"type": "presence", "user_id": user.id, "username": user.username, **presence_info(user)})
        except RuntimeError:
            pass
        db.close()


@app.get("/api/push/vapid-public-key")
def push_vapid_public_key(user: User = Depends(get_current_user)):
    keys = ensure_vapid_keys()
    available = bool(keys.get("available") and webpush is not None)
    return {
        "enabled": settings.push_enabled,
        "available": available,
        "reason": "" if available else keys.get("reason") or "Web Push library unavailable",
        "public_key": keys.get("public_key", "") if available else "",
    }


@app.post("/api/push/subscriptions")
@app.post("/api/push/subscribe")
async def push_subscribe(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    push_keys = ensure_vapid_keys()
    if webpush is None or not push_keys.get("available"):
        raise HTTPException(status_code=503, detail=push_keys.get("reason") or "Web Push unavailable")
    try:
        data = await request.json()
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Некорректный JSON")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Некорректная push-подписка")
    endpoint = str(data.get("endpoint", "")).strip()
    keys = data.get("keys") or {}
    p256dh = str(keys.get("p256dh", "")).strip()
    auth = str(keys.get("auth", "")).strip()
    if (
        not endpoint
        or len(endpoint) > 600
        or not push_endpoint_is_allowed(endpoint)
        or not p256dh
        or len(p256dh) > 300
        or not re.fullmatch(r"[A-Za-z0-9_-]+", p256dh)
        or not auth
        or len(auth) > 120
        or not re.fullmatch(r"[A-Za-z0-9_-]+", auth)
    ):
        raise HTTPException(status_code=400, detail="Некорректная push-подписка")

    subscription = db.scalar(select(PushSubscription).where(PushSubscription.endpoint == endpoint))
    if subscription:
        if subscription.user_id != user.id:
            db.execute(
                sql_delete(PlannerReminderDelivery).where(
                    PlannerReminderDelivery.push_subscription_id == subscription.id
                )
            )
        subscription.user_id = user.id
        subscription.p256dh = p256dh
        subscription.auth = auth
        subscription.user_agent = (request.headers.get("user-agent") or "")[:500] or None
        subscription.updated_at = utc_now_naive()
        subscription.last_seen_at = utc_now_naive()
        subscription.last_used_at = utc_now_naive()
        subscription.disabled_at = None
    else:
        db.add(PushSubscription(
            user_id=user.id,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            user_agent=(request.headers.get("user-agent") or "")[:500] or None,
            last_used_at=utc_now_naive(),
            last_seen_at=utc_now_naive(),
        ))
    db.commit()
    return {"ok": True}


@app.post("/api/push/subscriptions/unsubscribe")
@app.post("/api/push/unsubscribe")
async def push_unsubscribe(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        data = await request.json()
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Некорректный JSON")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Некорректная push-подписка")
    endpoint = str(data.get("endpoint", "")).strip()
    if endpoint:
        sub = db.scalar(select(PushSubscription).where(PushSubscription.endpoint == endpoint, PushSubscription.user_id == user.id))
        if sub:
            sub.disabled_at = utc_now_naive()
            sub.updated_at = utc_now_naive()
            db.commit()
    return {"ok": True}


@app.get("/api/push/status")
def push_status(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    keys = ensure_vapid_keys()
    subscriptions = db.scalars(
        select(PushSubscription).where(
            PushSubscription.user_id == user.id,
            PushSubscription.disabled_at.is_(None),
        )
    ).all()
    available = bool(keys.get("available") and webpush is not None)
    return {
        "ok": True,
        "enabled": settings.push_enabled,
        "available": available,
        "reason": "" if available else keys.get("reason") or "Web Push library unavailable",
        "subscriptions": len(subscriptions),
    }


@app.post("/api/push/test")
def push_test(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    result = send_push_to_user(db, user.id, {
        "title": "Уведомления включены",
        "body": "Это тестовое push-уведомление с сервера. Если оно пришло, чат тоже будет приходить в шторку.",
        "url": "/chats",
        "tag": f"push-test-{uuid.uuid4().hex[:8]}",
        "icon": "/static/icon-192.png",
        "badge": "/static/icon-192.png",
    })
    return result


@app.get("/api/notifications/unread")
def notifications_unread(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    threads = db.scalars(
        select(ChatThread)
        .options(selectinload(ChatThread.user_a), selectinload(ChatThread.user_b))
        .where(or_(ChatThread.user_a_id == user.id, ChatThread.user_b_id == user.id))
    ).all()
    items = []
    total = 0
    for thread in threads:
        count = int(
            db.scalar(
                select(func.count(ChatThreadMessage.id)).where(
                    ChatThreadMessage.thread_id == thread.id,
                    ChatThreadMessage.sender_id != user.id,
                    ChatThreadMessage.is_read.is_(False),
                )
            )
            or 0
        )
        if count:
            total += count
            items.append(
                {
                    "thread_id": thread.id,
                    "title": thread.title,
                    "from_username": other_chat_user(thread, user).username,
                    "count": count,
                }
            )
    return {"unread_total": total, "threads": items}


@app.get("/menu")
def menu_page(
    request: Request,
    from_date: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    selected_date = parse_date(from_date, msk_today()) or msk_today()
    start_date = week_start_for(selected_date)
    end_date = start_date + timedelta(days=6)
    today = msk_today()
    default_plan_date = today if start_date <= today <= end_date else selected_date
    recipes = db.scalars(select(Recipe).where(Recipe.owner_id == user.id).order_by(Recipe.title)).all()
    items = db.scalars(
        select(MenuItem)
        .options(selectinload(MenuItem.recipe))
        .join(Recipe, MenuItem.recipe_id == Recipe.id)
        .where(
            MenuItem.owner_id == user.id,
            MenuItem.plan_date >= datetime.combine(start_date, time.min),
            MenuItem.plan_date <= datetime.combine(end_date, time.max),
        )
        .order_by(MenuItem.plan_date, MenuItem.meal_name, MenuItem.id)
    ).all()

    by_day: dict[date, list[MenuItem]] = defaultdict(list)
    for item in items:
        by_day[item.plan_date.date()].append(item)

    days = []
    current = start_date
    while current <= end_date:
        days.append({
            "date": current,
            "label": current.strftime("%d.%m"),
            "weekday": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"][current.weekday()],
            "is_today": current == today,
            "entries": by_day[current],
        })
        current += timedelta(days=1)

    return render(
        request,
        "menu.html",
        {
            "user": user,
            "recipes": recipes,
            "days": days,
            "from_date": start_date.isoformat(),
            "default_plan_date": default_plan_date.isoformat(),
            "week_title": f"{start_date.strftime('%d.%m')} — {end_date.strftime('%d.%m')}",
            "prev_date": (start_date - timedelta(days=7)).isoformat(),
            "next_date": (start_date + timedelta(days=7)).isoformat(),
        },
    )


@app.post("/menu")
def menu_add(
    plan_date: str = Form(...),
    meal_name: str = Form(...),
    recipe_id: int = Form(...),
    note: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    selected_date = parse_date(plan_date)
    if selected_date is None:
        raise HTTPException(status_code=400, detail="Укажи дату")
    recipe = db.get(Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Рецепт не найден")
    db.add(
        MenuItem(
            owner_id=user.id,
            recipe_id=recipe.id,
            plan_date=datetime.combine(selected_date, time.min),
            meal_name=clean_required_text(meal_name or "Приём пищи", "Приём пищи", 80),
            note=clean_optional_text(note, 250),
        )
    )
    db.commit()
    return redirect(f"/menu?from_date={week_start_for(selected_date).isoformat()}")


@app.post("/menu/{item_id}/delete")
def menu_delete(
    item_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = db.get(MenuItem, item_id)
    if not item or item.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Пункт меню не найден")
    target_week = week_start_for(item.plan_date.date()).isoformat()
    db.delete(item)
    db.commit()
    return redirect(f"/menu?from_date={target_week}")


@app.get("/wishlist")
def wishlist_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    items = db.scalars(
        select(WishlistItem)
        .where(WishlistItem.owner_id == user.id)
        .order_by(WishlistItem.is_done, desc(WishlistItem.created_at))
    ).all()
    shares_given = db.scalars(
        select(WishlistShare).options(selectinload(WishlistShare.user)).where(WishlistShare.owner_id == user.id)
    ).all()
    shared_owners = db.scalars(
        select(WishlistShare).options(selectinload(WishlistShare.owner)).where(WishlistShare.user_id == user.id)
    ).all()
    expense_lists = accessible_expense_lists(db, user)
    return render(
        request,
        "wishlist.html",
        {"user": user, "items": items, "shares_given": shares_given, "shared_owners": shared_owners, "expense_lists": expense_lists},
    )


@app.post("/wishlist")
def wishlist_add(
    title: str = Form(...),
    url: str = Form(""),
    price: str = Form(""),
    priority: str = Form("medium"),
    status_value: str = Form("want", alias="status"),
    goal_amount: str = Form(""),
    saved_amount: str = Form(""),
    note: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db.add(
        WishlistItem(
            owner_id=user.id,
            title=clean_required_text(title, "Название", 150),
            url=clean_http_url(url, "Ссылка", 500),
            price=optional_nonnegative_money(price, "Цена"),
            priority=priority if priority in {"low", "medium", "high"} else "medium",
            status=status_value if status_value in {"want", "saving", "bought", "paused", "declined"} else "want",
            goal_amount=optional_nonnegative_money(goal_amount, "Цель"),
            saved_amount=optional_nonnegative_money(saved_amount, "Накоплено"),
            note=clean_optional_text(note, 4000),
        )
    )
    db.commit()
    return redirect("/wishlist")


@app.post("/wishlist/share")
def wishlist_share(
    request: Request,
    username: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    target = db.scalar(select(User).where(User.username == normalize_username(username)))
    if not target or target.id == user.id:
        items = db.scalars(select(WishlistItem).where(WishlistItem.owner_id == user.id)).all()
        return render(request, "wishlist.html", {"user": user, "items": items, "shares_given": [], "shared_owners": [], "error": "Пользователь не найден"})
    exists = db.scalar(select(WishlistShare).where(WishlistShare.owner_id == user.id, WishlistShare.user_id == target.id))
    if not exists:
        db.add(WishlistShare(owner_id=user.id, user_id=target.id))
        db.commit()
    return redirect("/wishlist")


@app.post("/wishlist/share/{share_id}/delete")
def wishlist_share_delete(
    share_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    share = db.get(WishlistShare, share_id)
    if not share or share.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Доступ не найден")
    db.delete(share)
    db.commit()
    return redirect("/wishlist")


@app.get("/wishlist/shared/{owner_username}")
def wishlist_shared_page(
    request: Request,
    owner_username: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    owner = require_wishlist_access(db, owner_username, user)
    items = db.scalars(
        select(WishlistItem)
        .where(WishlistItem.owner_id == owner.id)
        .order_by(WishlistItem.is_done, desc(WishlistItem.created_at))
    ).all()
    return render(request, "wishlist_shared.html", {"user": user, "owner": owner, "items": items})


@app.post("/wishlist/{item_id}/toggle")
def wishlist_toggle(
    item_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = db.get(WishlistItem, item_id)
    if not item or item.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Хотелка не найдена")
    item.is_done = not item.is_done
    db.commit()
    return redirect("/wishlist")


@app.post("/wishlist/{item_id}/update")
def wishlist_update(
    item_id: int,
    title: str = Form(...),
    url: str = Form(""),
    price: str = Form(""),
    priority: str = Form("medium"),
    status_value: str = Form("want", alias="status"),
    goal_amount: str = Form(""),
    saved_amount: str = Form(""),
    note: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = db.get(WishlistItem, item_id)
    if not item or item.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Хотелка не найдена")
    item.title = clean_required_text(title, "Название", 150)
    item.url = clean_http_url(url, "Ссылка", 500)
    item.price = optional_nonnegative_money(price, "Цена")
    item.priority = priority if priority in {"low", "medium", "high"} else "medium"
    item.status = status_value if status_value in {"want", "saving", "bought", "paused", "declined"} else "want"
    item.goal_amount = optional_nonnegative_money(goal_amount, "Цель")
    item.saved_amount = optional_nonnegative_money(saved_amount, "Накоплено")
    item.note = clean_optional_text(note, 4000)
    item.is_done = item.status == "bought"
    db.commit()
    return redirect("/wishlist")


@app.post("/wishlist/{item_id}/to-expense")
def wishlist_to_expense(
    item_id: int,
    expense_list_id: int = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = db.get(WishlistItem, item_id)
    if not item or item.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Хотелка не найдена")
    amount = item.price or item.goal_amount
    if amount is None:
        return redirect("/wishlist")
    expense_list = require_expense_access(db, expense_list_id, user, write=True)
    category = find_or_create_expense_category(db, expense_list, "Хотелки")
    expense_item = ExpenseItem(category_id=category.id, title=item.title, amount=amount)
    db.add(expense_item)
    db.flush()
    item.expense_prev_status = item.status
    item.expense_prev_is_done = item.is_done
    item.expense_item_id = expense_item.id
    item.status = "bought"
    item.is_done = True
    db.commit()
    return redirect("/wishlist")


@app.post("/wishlist/{item_id}/undo-expense")
def wishlist_undo_expense(
    item_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = db.get(WishlistItem, item_id)
    if not item or item.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Хотелка не найдена")
    if item.expense_item_id:
        expense_item = db.get(ExpenseItem, item.expense_item_id)
        if expense_item:
            # Проверка доступа через связанный список трат. Если список уже удалён, просто очищаем связь.
            try:
                require_expense_access(db, expense_item.category.expense_list_id, user, write=True)
                db.delete(expense_item)
            except HTTPException:
                pass
    item.status = item.expense_prev_status or "want"
    item.is_done = bool(item.expense_prev_is_done) if item.expense_prev_is_done is not None else False
    item.expense_item_id = None
    item.expense_prev_status = None
    item.expense_prev_is_done = None
    db.commit()
    return redirect("/wishlist")


@app.post("/wishlist/{item_id}/delete")
def wishlist_delete(
    item_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = db.get(WishlistItem, item_id)
    if not item or item.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Хотелка не найдена")
    db.delete(item)
    db.commit()
    return redirect("/wishlist")


@app.get("/watch")
def watch_page(
    request: Request,
    q: str = "",
    status_filter: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = select(WatchItem).where(WatchItem.owner_id == user.id)
    q_clean = q.strip()
    if q_clean:
        like = f"%{q_clean}%"
        stmt = stmt.where(or_(WatchItem.title.ilike(like), WatchItem.note.ilike(like)))
    clean_status = status_filter.strip()
    if clean_status:
        stmt = stmt.where(WatchItem.status == clean_status)
    items = db.scalars(stmt.order_by(WatchItem.status, desc(WatchItem.updated_at), desc(WatchItem.created_at))).all()
    return render(
        request,
        "watch.html",
        {
            "user": user,
            "items": items,
            "q": q_clean,
            "status_filter": clean_status,
            "kind_labels": WATCH_KIND_LABELS,
            "status_labels": WATCH_STATUS_LABELS,
        },
    )


@app.post("/watch")
def watch_create(
    title: str = Form(...),
    kind: str = Form("movie"),
    status_value: str = Form("planned"),
    season: str = Form(""),
    episode: str = Form(""),
    minute: str = Form(""),
    watch_url: str = Form(""),
    note: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db.add(
        WatchItem(
            owner_id=user.id,
            title=clean_required_text(title, "Название", 180),
            kind=kind if kind in WATCH_KIND_LABELS else "movie",
            status=status_value if status_value in WATCH_STATUS_LABELS else "planned",
            season=parse_optional_int(season, "Сезон", 1, 999) if season.strip() else None,
            episode=parse_optional_int(episode, "Серия", 1, 99_999) if episode.strip() else None,
            minute=parse_optional_int(minute, "Минута", 0, 99_999) if minute.strip() else None,
            watch_url=clean_http_url(watch_url, "Ссылка на просмотр", 700),
            note=clean_optional_text(note, 4000),
        )
    )
    db.commit()
    return redirect_notice("/watch", "Добавлено в список")


@app.post("/watch/{item_id}/update")
def watch_update(
    item_id: int,
    title: str = Form(...),
    kind: str = Form("movie"),
    status_value: str = Form("planned"),
    season: str = Form(""),
    episode: str = Form(""),
    minute: str = Form(""),
    watch_url: str = Form(""),
    note: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = db.get(WatchItem, item_id)
    if not item or item.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    item.title = clean_required_text(title, "Название", 180)
    item.kind = kind if kind in WATCH_KIND_LABELS else "movie"
    item.status = status_value if status_value in WATCH_STATUS_LABELS else "planned"
    item.season = parse_optional_int(season, "Сезон", 1, 999) if season.strip() else None
    item.episode = parse_optional_int(episode, "Серия", 1, 99_999) if episode.strip() else None
    item.minute = parse_optional_int(minute, "Минута", 0, 99_999) if minute.strip() else None
    item.watch_url = clean_http_url(watch_url, "Ссылка на просмотр", 700)
    item.note = clean_optional_text(note, 4000)
    item.updated_at = utc_now_naive()
    db.commit()
    return redirect_notice("/watch", "Запись обновлена")


@app.post("/watch/{item_id}/delete")
def watch_delete(item_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = db.get(WatchItem, item_id)
    if not item or item.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    db.delete(item)
    db.commit()
    return redirect_notice("/watch", "Запись удалена")


@app.post("/watch/{item_id}/progress")
def watch_progress(
    item_id: int,
    episode_step: int = Form(0),
    minute_step: int = Form(0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = db.get(WatchItem, item_id)
    if not item or item.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Watch item not found")
    if episode_step not in {-1, 0, 1} or minute_step not in {-10, -5, 0, 5, 10}:
        raise HTTPException(status_code=400, detail="Некорректное изменение прогресса")
    if episode_step:
        item.episode = max(1, (item.episode or 0) + episode_step)
        item.minute = 0
    if minute_step:
        item.minute = max(0, (item.minute or 0) + minute_step)
    item.status = "watching"
    item.last_watched_at = utc_now_naive()
    db.commit()
    return redirect_notice("/watch", "Progress saved")


@app.get("/moments")
def moments_page(
    request: Request,
    year: int | None = None,
    month: str = "",
    day: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        if "-" in month:
            month_start = date.fromisoformat(f"{month.strip()}-01")
        elif year is not None and month.strip():
            month_start = date(year, int(month), 1)
        else:
            month_start = msk_today().replace(day=1)
    except (TypeError, ValueError):
        month_start = msk_today().replace(day=1)
    next_year, next_month = shifted_month(month_start.year, month_start.month, 1)
    next_month_start = date(next_year, next_month, 1)
    moments = db.scalars(
        select(Moment)
        .where(
            Moment.owner_id == user.id,
            Moment.happened_on >= month_start,
            Moment.happened_on < next_month_start,
        )
        .order_by(Moment.happened_on, Moment.created_at, Moment.id)
    ).all()
    moments_by_day: dict[date, list[Moment]] = {}
    for moment in moments:
        moments_by_day.setdefault(moment.happened_on, []).append(moment)
    selected_day = parse_date(day, None)
    if selected_day and not (month_start <= selected_day < next_month_start):
        selected_day = None
    month_end = next_month_start - timedelta(days=1)
    calendar_start = month_start - timedelta(days=month_start.weekday())
    calendar_end = month_end + timedelta(days=6 - month_end.weekday())
    calendar_days = []
    current_day = calendar_start
    while current_day <= calendar_end:
        calendar_days.append({
            "day": current_day,
            "number": current_day.day,
            "items": moments_by_day.get(current_day, []),
            "is_current_month": current_day.month == month_start.month,
            "is_today": current_day == msk_today(),
        })
        current_day += timedelta(days=1)
    return render(
        request,
        "moments.html",
        {
            "user": user,
            "month": month_start.month,
            "year": month_start.year,
            "today": msk_today(),
            "moment_count": len(moments),
            "month_label": f"{RUSSIAN_MONTH_NAMES[month_start.month - 1].capitalize()} {month_start.year}",
            "calendar_days": calendar_days,
            "calendar_weekdays": ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"),
            "previous_month": (month_start - timedelta(days=1)).replace(day=1),
            "next_month": next_month_start,
            "selected_day": selected_day,
            "selected_moments": moments_by_day.get(selected_day, []) if selected_day else [],
            "selected_day_label": selected_day.strftime("%d.%m.%Y") if selected_day else "",
            "new_moment_day": selected_day or msk_today(),
        },
    )


@app.post("/moments")
def moment_create(
    title: str = Form(...),
    description: str = Form(""),
    happened_on: str = Form(""),
    photo_file: UploadFile | None = File(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    clean_title = title.strip()
    if not clean_title:
        raise HTTPException(status_code=400, detail="Укажите заголовок")
    moment_day = parse_date(happened_on, msk_today()) or msk_today()
    db.add(
        Moment(
            owner_id=user.id,
            title=clean_title[:180],
            description=clean_optional_text(description, 20_000),
            happened_on=moment_day,
            photo_path=save_moment_photo(photo_file),
        )
    )
    db.commit()
    return redirect_notice(f"/moments?month={moment_day.strftime('%Y-%m')}", "Момент сохранён")


@app.post("/moments/{moment_id}/update")
def moment_update(
    moment_id: int,
    title: str = Form(...),
    description: str = Form(""),
    happened_on: str = Form(""),
    photo_file: UploadFile | None = File(None),
    remove_photo: str | None = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    moment = db.get(Moment, moment_id)
    if not moment or moment.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Момент не найден")
    clean_title = title.strip()
    if not clean_title:
        raise HTTPException(status_code=400, detail="Укажите заголовок")
    moment.title = clean_title[:180]
    moment.description = clean_optional_text(description, 20_000)
    moment.happened_on = parse_date(happened_on, moment.happened_on) or moment.happened_on
    previous_photo_path = moment.photo_path
    new_photo_path = save_moment_photo(photo_file)
    if new_photo_path:
        moment.photo_path = new_photo_path
    elif remove_photo == "1":
        moment.photo_path = None
    try:
        db.commit()
    except Exception:
        db.rollback()
        if new_photo_path:
            delete_moment_photo(new_photo_path)
        raise
    if previous_photo_path and previous_photo_path != moment.photo_path:
        delete_moment_photo(previous_photo_path)
    return redirect_notice(f"/moments?month={moment.happened_on.strftime('%Y-%m')}", "Момент обновлён")


@app.post("/moments/{moment_id}/delete")
def moment_delete(moment_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    moment = db.get(Moment, moment_id)
    if not moment or moment.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Момент не найден")
    month = moment.happened_on.strftime("%Y-%m")
    delete_moment_photo(moment.photo_path)
    db.delete(moment)
    db.commit()
    return redirect_notice(f"/moments?month={month}", "Момент удалён")


def parse_planner_time(value: str | None) -> str | None:
    clean_value = (value or "").strip()
    if not clean_value:
        return None
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clean_value):
        raise HTTPException(status_code=400, detail="Укажите время в формате ЧЧ:ММ")
    return clean_value


def planner_return_url(day: date) -> str:
    return f"/planner?month={day.strftime('%Y-%m')}&day={day.isoformat()}"


def parse_planner_end_date(value: str, start_date: date) -> date | None:
    clean_value = value.strip()
    if not clean_value:
        return None
    try:
        end_date = date.fromisoformat(clean_value)
    except ValueError as exc:
        raise ValueError("Укажите корректную дату окончания") from exc
    if end_date < start_date:
        raise ValueError("Дата окончания не может быть раньше даты начала")
    return None if end_date == start_date else end_date


def parse_planner_recurrence(frequency: str, interval: str, until: str, start_date: date) -> tuple[str | None, int, date | None]:
    clean_frequency = frequency.strip().lower()
    if not clean_frequency:
        return None, 1, None
    if clean_frequency not in {"daily", "weekly", "monthly", "yearly"}:
        raise ValueError("Выберите допустимую периодичность")
    try:
        clean_interval = int(interval)
    except ValueError as exc:
        raise ValueError("Интервал повторения должен быть целым числом") from exc
    if clean_interval < 1:
        raise ValueError("Интервал повторения должен быть не меньше 1")
    try:
        clean_until = date.fromisoformat(until) if until.strip() else None
    except ValueError as exc:
        raise ValueError("Укажите корректную дату окончания повторений") from exc
    if clean_until and clean_until < start_date:
        raise ValueError("Дата окончания повторений не может быть раньше даты начала")
    return clean_frequency, clean_interval, clean_until


def render_planner_page(
    request: Request,
    user: User,
    db: Session,
    month: str = "",
    day: str = "",
    create_form: dict[str, str] | None = None,
    edit_forms: dict[int, dict[str, str]] | None = None,
    validation_error: str | None = None,
):
    try:
        month_start = date.fromisoformat(f"{month.strip()}-01") if month.strip() else msk_today().replace(day=1)
    except ValueError:
        month_start = msk_today().replace(day=1)
    selected_day = parse_date(day, msk_today()) or msk_today()
    next_year, next_month = shifted_month(month_start.year, month_start.month, 1)
    next_month_start = date(next_year, next_month, 1)
    if not month_start <= selected_day < next_month_start:
        selected_day = month_start
    previous_year, previous_month = shifted_month(month_start.year, month_start.month, -1)
    month_end = next_month_start - timedelta(days=1)
    calendar_start = month_start - timedelta(days=month_start.weekday())
    calendar_end = month_end + timedelta(days=6 - month_end.weekday())
    items = db.scalars(
        select(PlannerItem)
        .options(selectinload(PlannerItem.reminders))
        .where(
            PlannerItem.owner_id == user.id,
            or_(
                and_(PlannerItem.recurrence_frequency.is_(None), PlannerItem.scheduled_for <= calendar_end, func.coalesce(PlannerItem.end_date, PlannerItem.scheduled_for) >= calendar_start),
                and_(PlannerItem.recurrence_frequency.is_not(None), PlannerItem.scheduled_for <= calendar_end, or_(PlannerItem.recurrence_until.is_(None), PlannerItem.recurrence_until >= calendar_start - timedelta(days=366))),
            ),
        )
        .order_by(PlannerItem.scheduled_for, PlannerItem.start_time.is_(None), PlannerItem.start_time, PlannerItem.id)
    ).all()
    occurrences_by_day = calendar_occurrences(items, calendar_start, calendar_end)
    calendar_days = []
    current_day = calendar_start
    while current_day <= calendar_end:
        calendar_days.append({
            "day": current_day,
            "number": current_day.day,
            "items": occurrences_by_day.get(current_day, []),
            "is_current_month": current_day.month == month_start.month,
            "is_selected": current_day == selected_day,
            "is_today": current_day == msk_today(),
        })
        current_day += timedelta(days=1)
    selected_items = [occurrence.item for occurrence in occurrences_by_day.get(selected_day, [])]
    upcoming_candidates = db.scalars(
        select(PlannerItem)
        .options(selectinload(PlannerItem.reminders))
        .where(PlannerItem.owner_id == user.id, PlannerItem.is_done.is_(False), PlannerItem.scheduled_for <= msk_today() + timedelta(days=366))
    ).all()
    upcoming = [occurrence for item in upcoming_candidates for occurrence in iter_occurrences_in_range(item, msk_today(), msk_today() + timedelta(days=366))][:6]
    return render(request, "planner.html", {
        "user": user,
        "today": msk_today(),
        "month": month_start.strftime("%Y-%m"),
        "month_label": f"{RUSSIAN_MONTH_NAMES[month_start.month - 1].capitalize()} {month_start.year}",
        "previous_month": f"{previous_year:04d}-{previous_month:02d}",
        "next_month": f"{next_year:04d}-{next_month:02d}",
        "selected_day": selected_day,
        "selected_items": selected_items,
        "upcoming": upcoming,
        "calendar_days": calendar_days,
        "calendar_weekdays": ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"),
        "month_item_count": sum(1 for item in items for _ in iter_occurrences_in_range(item, month_start, month_end)),
        "planner_create_form": create_form or {
            "title": "", "scheduled_for": selected_day.isoformat(), "end_date": selected_day.isoformat(),
            "start_time": "", "end_time": "", "description": "", "color": "#2563eb", "reminders": [],
        },
        "planner_edit_forms": edit_forms or {},
        "planner_validation_error": validation_error,
    })


@app.get("/planner")
def planner_page(
    request: Request,
    month: str = "",
    day: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return render_planner_page(request, user, db, month, day)


@app.post("/planner")
def planner_create(
    request: Request,
    title: str = Form(...),
    scheduled_for: str = Form(""),
    end_date: str = Form(""),
    recurrence_frequency: str = Form(""),
    recurrence_interval: str = Form("1"),
    recurrence_until: str = Form(""),
    start_time: str = Form(""),
    end_time: str = Form(""),
    description: str = Form(""),
    color: str = Form("#2563eb"),
    reminder_offset_value: list[str] = Form(default=[]),
    reminder_offset_unit: list[str] = Form(default=[]),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    clean_title = title.strip()
    if not clean_title:
        raise HTTPException(status_code=400, detail="Укажите название")
    item_day = parse_date(scheduled_for, msk_today()) or msk_today()
    try:
        item_end_date = parse_planner_end_date(end_date, item_day)
        item_frequency, item_interval, item_until = parse_planner_recurrence(recurrence_frequency, recurrence_interval, recurrence_until, item_day)
        reminder_configs = parse_reminder_configs(reminder_offset_value, reminder_offset_unit)
    except ValueError as exc:
        return render_planner_page(
            request, user, db, item_day.strftime("%Y-%m"), item_day.isoformat(),
            create_form={"title": title, "scheduled_for": scheduled_for, "end_date": end_date,
                         "start_time": start_time, "end_time": end_time, "description": description, "color": color,
                         "reminders": [{"offset_value": value, "offset_unit": unit} for value, unit in zip(reminder_offset_value, reminder_offset_unit)]},
            validation_error=str(exc),
        )
    item_start_time = parse_planner_time(start_time)
    item_end_time = parse_planner_time(end_time)
    if item_start_time and item_end_time and item_end_time <= item_start_time:
        raise HTTPException(status_code=400, detail="Время окончания должно быть позже начала")
    item_color = color.strip().lower()
    if not re.fullmatch(r"#[0-9a-f]{6}", item_color):
        item_color = "#2563eb"
    item = PlannerItem(
        owner_id=user.id,
        title=clean_title[:180],
        description=clean_optional_text(description, 10_000),
        scheduled_for=item_day,
        end_date=item_end_date,
        recurrence_frequency=item_frequency, recurrence_interval=item_interval, recurrence_until=item_until,
        start_time=item_start_time,
        end_time=item_end_time,
        color=item_color,
    )
    replace_reminder_configs(item, reminder_configs)
    db.add(item)
    db.commit()
    return redirect_notice(planner_return_url(item_day), "Событие добавлено")


@app.post("/planner/{item_id}/update")
def planner_update(
    request: Request,
    item_id: int,
    title: str = Form(...),
    scheduled_for: str = Form(""),
    end_date: str = Form(""),
    recurrence_frequency: str = Form(""),
    recurrence_interval: str = Form("1"),
    recurrence_until: str = Form(""),
    start_time: str = Form(""),
    end_time: str = Form(""),
    description: str = Form(""),
    color: str = Form("#2563eb"),
    is_done: str | None = Form(None),
    reminder_offset_value: list[str] = Form(default=[]),
    reminder_offset_unit: list[str] = Form(default=[]),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = db.get(PlannerItem, item_id)
    if not item or item.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Событие не найдено")
    clean_title = title.strip()
    if not clean_title:
        raise HTTPException(status_code=400, detail="Укажите название")
    item_day = parse_date(scheduled_for, item.scheduled_for) or item.scheduled_for
    try:
        item_end_date = parse_planner_end_date(end_date, item_day)
        item_frequency, item_interval, item_until = parse_planner_recurrence(recurrence_frequency, recurrence_interval, recurrence_until, item_day)
        reminder_configs = parse_reminder_configs(reminder_offset_value, reminder_offset_unit)
    except ValueError as exc:
        return render_planner_page(
            request, user, db, item_day.strftime("%Y-%m"), item_day.isoformat(),
            edit_forms={item.id: {"title": title, "scheduled_for": scheduled_for, "end_date": end_date,
                                  "start_time": start_time, "end_time": end_time, "description": description,
                                  "color": color, "is_done": is_done or "",
                                  "reminders": [{"offset_value": value, "offset_unit": unit} for value, unit in zip(reminder_offset_value, reminder_offset_unit)]}},
            validation_error=str(exc),
        )
    item_start_time = parse_planner_time(start_time)
    item_end_time = parse_planner_time(end_time)
    if item_start_time and item_end_time and item_end_time <= item_start_time:
        raise HTTPException(status_code=400, detail="Время окончания должно быть позже начала")
    item.title = clean_title[:180]
    item.description = clean_optional_text(description, 10_000)
    item.scheduled_for = item_day
    item.end_date = item_end_date
    item.recurrence_frequency = item_frequency
    item.recurrence_interval = item_interval
    item.recurrence_until = item_until
    item.start_time = item_start_time
    item.end_time = item_end_time
    item.color = color.strip().lower() if re.fullmatch(r"#[0-9a-fA-F]{6}", color.strip()) else "#2563eb"
    item.is_done = is_done == "1"
    replace_reminder_configs(item, reminder_configs)
    db.commit()
    return redirect_notice(planner_return_url(item_day), "Событие обновлено")


@app.post("/planner/{item_id}/toggle")
def planner_toggle(item_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = db.get(PlannerItem, item_id)
    if not item or item.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Событие не найдено")
    item.is_done = not item.is_done
    db.commit()
    return redirect_notice(planner_return_url(item.scheduled_for), "Статус обновлён")


@app.post("/planner/{item_id}/delete")
def planner_delete(item_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = db.get(PlannerItem, item_id)
    if not item or item.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Событие не найдено")
    item_day = item.scheduled_for
    db.delete(item)
    db.commit()
    return redirect_notice(planner_return_url(item_day), "Событие удалено")


@app.get("/today")
def today_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    today = msk_today()
    menu_items = db.scalars(
        select(MenuItem)
        .options(selectinload(MenuItem.recipe))
        .where(
            MenuItem.owner_id == user.id,
            MenuItem.plan_date >= datetime.combine(today, time.min),
            MenuItem.plan_date <= datetime.combine(today, time.max),
        )
        .order_by(MenuItem.meal_name, MenuItem.id)
    ).all()
    day_start, day_end = msk_day_utc_bounds(today)
    today_expenses = db.scalars(
        select(ExpenseItem)
        .join(ExpenseCategory)
        .join(ExpenseList)
        .options(selectinload(ExpenseItem.category).selectinload(ExpenseCategory.expense_list))
        .where(
            or_(ExpenseList.owner_id == user.id, ExpenseList.id.in_(select(ExpenseListShare.expense_list_id).where(ExpenseListShare.user_id == user.id))),
            ExpenseItem.created_at >= day_start,
            ExpenseItem.created_at <= day_end,
        )
        .order_by(desc(ExpenseItem.created_at))
    ).all()
    shopping_items = db.scalars(
        select(ShoppingItem)
        .join(ShoppingList)
        .where(
            or_(ShoppingList.owner_id == user.id, ShoppingList.id.in_(select(ShoppingListShare.shopping_list_id).where(ShoppingListShare.user_id == user.id))),
            ShoppingItem.is_done.is_(False),
        )
        .order_by(ShoppingItem.department, ShoppingItem.title)
        .limit(12)
    ).all()
    wishlist_items = db.scalars(
        select(WishlistItem)
        .where(WishlistItem.owner_id == user.id, WishlistItem.status.in_(["saving", "want"]))
        .order_by(WishlistItem.priority.desc(), desc(WishlistItem.created_at))
        .limit(5)
    ).all()
    watch_items = db.scalars(
        select(WatchItem)
        .where(WatchItem.owner_id == user.id, WatchItem.status.in_(["watching", "planned"]))
        .order_by(WatchItem.status.desc(), desc(WatchItem.updated_at))
        .limit(5)
    ).all()
    active_shopping_count = int(
        db.scalar(
            select(func.count(ShoppingItem.id))
            .join(ShoppingList)
            .where(
                or_(ShoppingList.owner_id == user.id, ShoppingList.id.in_(select(ShoppingListShare.shopping_list_id).where(ShoppingListShare.user_id == user.id))),
                ShoppingItem.is_done.is_(False),
            )
        )
        or 0
    )
    active_watch_count = int(db.scalar(select(func.count(WatchItem.id)).where(WatchItem.owner_id == user.id, WatchItem.status.in_(["watching", "planned"]))) or 0)
    return render(request, "today.html", {
        "user": user,
        "today": today,
        "menu_items": menu_items,
        "today_expenses": today_expenses,
        "today_expense_total": sum((item.amount for item in today_expenses), Decimal("0.00")),
        "shopping_items": shopping_items,
        "active_shopping_count": active_shopping_count,
        "wishlist_items": wishlist_items,
        "watch_items": watch_items,
        "active_watch_count": active_watch_count,
        "unread_total": unread_total_for_user(db, user),
    })


@app.get("/search")
def global_search_page(
    request: Request,
    q: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q_clean = q.strip()
    results = {"users": [], "recipes": [], "expenses": [], "wishlist": [], "shopping": [], "chats": [], "watch": []}
    if q_clean:
        like = f"%{q_clean}%"
        results["users"] = db.scalars(select(User).where(User.id != user.id, User.username.ilike(like)).order_by(User.username).limit(20)).all()
        results["recipes"] = db.scalars(select(Recipe).where(Recipe.owner_id == user.id, or_(Recipe.title.ilike(like), Recipe.ingredients.ilike(like), Recipe.tags.ilike(like))).order_by(desc(Recipe.created_at)).limit(20)).all()
        results["expenses"] = db.scalars(
            select(ExpenseItem)
            .join(ExpenseCategory)
            .join(ExpenseList)
            .options(selectinload(ExpenseItem.category).selectinload(ExpenseCategory.expense_list))
            .where(
                or_(ExpenseList.owner_id == user.id, ExpenseList.id.in_(select(ExpenseListShare.expense_list_id).where(ExpenseListShare.user_id == user.id))),
                ExpenseItem.title.ilike(like),
            )
            .order_by(desc(ExpenseItem.created_at)).limit(20)
        ).all()
        results["wishlist"] = db.scalars(select(WishlistItem).where(WishlistItem.owner_id == user.id, or_(WishlistItem.title.ilike(like), WishlistItem.note.ilike(like))).order_by(desc(WishlistItem.created_at)).limit(20)).all()
        results["shopping"] = db.scalars(
            select(ShoppingItem)
            .join(ShoppingList)
            .options(selectinload(ShoppingItem.shopping_list))
            .where(
                or_(ShoppingList.owner_id == user.id, ShoppingList.id.in_(select(ShoppingListShare.shopping_list_id).where(ShoppingListShare.user_id == user.id))),
                ShoppingItem.title.ilike(like),
            ).limit(20)
        ).all()
        results["chats"] = db.scalars(
            select(ChatThread)
            .where(or_(ChatThread.user_a_id == user.id, ChatThread.user_b_id == user.id), ChatThread.title.ilike(like))
            .order_by(desc(ChatThread.updated_at)).limit(20)
        ).all()
        results["watch"] = db.scalars(select(WatchItem).where(WatchItem.owner_id == user.id, or_(WatchItem.title.ilike(like), WatchItem.note.ilike(like))).order_by(desc(WatchItem.updated_at)).limit(20)).all()
    return render(request, "search.html", {"user": user, "q": q_clean, "results": results})


@app.post("/recipes/{recipe_id}/shopping")
def recipe_to_shopping(
    recipe_id: int,
    servings: str = Form(""),
    shopping_list_id: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    recipe = db.get(Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Рецепт не найден")
    base_servings = Decimal(str(recipe.servings)) if recipe.servings is not None else None
    target_servings = parse_optional_decimal(servings, "Порции", Decimal("0.1")) if servings.strip() else base_servings
    ratio = target_servings / base_servings if base_servings and target_servings else None
    if shopping_list_id.strip():
        shopping_list = require_shopping_access(db, int(shopping_list_id), user, write=True)
    else:
        shopping_list = ShoppingList(owner_id=user.id, title=f"Покупки: {recipe.title[:80]}")
        db.add(shopping_list)
        db.flush()
    for item in ingredients_to_shopping_items(recipe.ingredients, ratio, db, user):
        db.add(ShoppingItem(shopping_list_id=shopping_list.id, title=str(item["title"]), amount=item.get("amount"), department=str(item["department"])))
    db.commit()
    return redirect_notice(f"/shopping/lists/{shopping_list.id}", "Список покупок создан")


@app.get("/shopping")
def shopping_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    owned = db.scalars(select(ShoppingList).options(selectinload(ShoppingList.items), selectinload(ShoppingList.shares)).where(ShoppingList.owner_id == user.id).order_by(desc(ShoppingList.created_at))).all()
    shared = db.scalars(select(ShoppingList).join(ShoppingListShare).options(selectinload(ShoppingList.owner), selectinload(ShoppingList.items)).where(ShoppingListShare.user_id == user.id).order_by(desc(ShoppingList.created_at))).all()
    return render(request, "shopping.html", {"user": user, "owned": owned, "shared": shared, "all_lists": list(owned) + list(shared), "category_rules": user_category_rules(db, user)})


@app.post("/shopping/lists")
def shopping_list_create(title: str = Form(...), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    shopping_list = ShoppingList(owner_id=user.id, title=clean_required_text(title or "Покупки", "Название", 150))
    db.add(shopping_list)
    db.commit()
    return redirect(f"/shopping/lists/{shopping_list.id}")


@app.post("/shopping/lists/merge")
def shopping_lists_merge(
    target_list_id: int = Form(...),
    source_list_ids: list[int] = Form(default=[]),
    delete_sources: str | None = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    target = require_shopping_access(db, target_list_id, user, write=True)
    source_ids = [int(one) for one in source_list_ids if int(one) != target.id]
    for source_id in source_ids:
        source = require_shopping_access(db, source_id, user, write=True)
        for item in source.items:
            db.add(
                ShoppingItem(
                    shopping_list_id=target.id,
                    title=item.title,
                    amount=item.amount,
                    department=item.department,
                    price=item.price,
                    is_done=item.is_done,
                )
            )
        if delete_sources == "1" and source.owner_id == user.id:
            prepare_shopping_items_delete(db, [item.id for item in source.items])
            db.delete(source)
    db.commit()
    return redirect(f"/shopping/lists/{target.id}")


@app.get("/shopping/lists/{list_id}")
def shopping_list_page(request: Request, list_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    shopping_list = require_shopping_access(db, list_id, user)
    expense_lists = accessible_expense_lists(db, user)
    departments = defaultdict(list)
    for item in sorted(shopping_list.items, key=lambda one: (one.is_done, one.department, one.title)):
        departments[item.department].append(item)
    rules = user_category_rules(db, user)
    department_options = shopping_department_options(db, user, [item.department for item in shopping_list.items])
    return render(request, "shopping_list.html", {"user": user, "shopping_list": shopping_list, "departments": dict(departments), "expense_lists": expense_lists, "department_options": department_options, "category_rules": rules})


@app.post("/shopping/lists/{list_id}/items")
def shopping_item_add(
    list_id: int,
    title: str = Form(...),
    amount: str = Form(""),
    department: str = Form(""),
    department_custom: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    shopping_list = require_shopping_access(db, list_id, user, write=True)
    clean_department = resolve_department_choice(department, department_custom)
    clean_title = clean_required_text(title, "Название", 180)
    db.add(
        ShoppingItem(
            shopping_list_id=shopping_list.id,
            title=clean_title,
            amount=clean_optional_text(amount, 120),
            department=(clean_department or guess_department(clean_title, db, user))[:80],
        )
    )
    db.commit()
    return redirect_notice(f"/shopping/lists/{list_id}", "Позиция добавлена")


@app.post("/shopping/items/{item_id}/toggle")
def shopping_item_toggle(item_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = db.get(ShoppingItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Позиция не найдена")
    shopping_list = require_shopping_access(db, item.shopping_list_id, user, write=True)
    item.is_done = not item.is_done
    db.commit()
    return redirect_notice(f"/shopping/lists/{shopping_list.id}", "Статус обновлён")


@app.post("/shopping/items/{item_id}/department")
def shopping_item_department(
    item_id: int,
    department: str = Form(...),
    department_custom: str = Form(""),
    remember: str | None = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = db.get(ShoppingItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Позиция не найдена")
    shopping_list = require_shopping_access(db, item.shopping_list_id, user, write=True)
    item.department = (resolve_department_choice(department, department_custom) or "Прочее")[:80]
    if remember == "1":
        remember_shopping_department(db, user, item.title, item.department)
    db.commit()
    return redirect_notice(f"/shopping/lists/{shopping_list.id}", "Раздел обновлён")


@app.post("/shopping/rules/{rule_id}/delete")
def shopping_rule_delete(rule_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rule = db.get(ShoppingCategoryRule, rule_id)
    if not rule or rule.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Правило не найдено")
    db.delete(rule)
    db.commit()
    return redirect("/shopping")


@app.post("/shopping/items/{item_id}/price")
def shopping_item_price(item_id: int, price: str = Form(""), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = db.get(ShoppingItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Позиция не найдена")
    shopping_list = require_shopping_access(db, item.shopping_list_id, user, write=True)
    item.price = optional_nonnegative_money(price, "Цена")
    remember_price_history(db, user, item, item.price)
    db.commit()
    return redirect(f"/shopping/lists/{shopping_list.id}")


@app.post("/shopping/items/{item_id}/to-expense")
def shopping_item_to_expense(
    item_id: int,
    expense_list_id: int = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = db.get(ShoppingItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Позиция не найдена")
    shopping_list = require_shopping_access(db, item.shopping_list_id, user, write=True)
    if item.price is None:
        return redirect(f"/shopping/lists/{shopping_list.id}")
    expense_list = require_expense_access(db, expense_list_id, user, write=True)
    category = find_or_create_expense_category(db, expense_list, "Покупки")
    db.add(ExpenseItem(category_id=category.id, title=item.title, amount=item.price))
    item.is_done = True
    db.commit()
    return redirect(f"/shopping/lists/{shopping_list.id}")


@app.post("/shopping/items/{item_id}/delete")
def shopping_item_delete(item_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = db.get(ShoppingItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Позиция не найдена")
    shopping_list = require_shopping_access(db, item.shopping_list_id, user, write=True)
    prepare_shopping_items_delete(db, [item.id])
    db.delete(item)
    db.commit()
    return redirect(f"/shopping/lists/{shopping_list.id}")


@app.post("/shopping/lists/{list_id}/share")
def shopping_list_share(list_id: int, username: str = Form(...), can_edit: str = Form("1"), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    shopping_list = require_shopping_access(db, list_id, user)
    if shopping_list.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Делиться списком может только владелец")
    target = db.scalar(select(User).where(User.username == normalize_username(username)))
    if target and target.id != user.id:
        exists = db.scalar(select(ShoppingListShare).where(ShoppingListShare.shopping_list_id == list_id, ShoppingListShare.user_id == target.id))
        if exists:
            exists.can_edit = can_edit == "1"
        else:
            db.add(ShoppingListShare(shopping_list_id=list_id, user_id=target.id, can_edit=can_edit == "1"))
        db.commit()
    return redirect(f"/shopping/lists/{list_id}")


@app.post("/shopping/lists/{list_id}/share/{share_id}/delete")
def shopping_list_share_delete(list_id: int, share_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    shopping_list = require_shopping_access(db, list_id, user)
    if shopping_list.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Закрыть доступ может только владелец")
    share = db.get(ShoppingListShare, share_id)
    if share and share.shopping_list_id == list_id:
        db.delete(share)
        db.commit()
    return redirect(f"/shopping/lists/{list_id}")


@app.post("/shopping/lists/{list_id}/delete")
def shopping_list_delete(list_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    shopping_list = require_shopping_access(db, list_id, user)
    if shopping_list.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Удалить список может только владелец")
    prepare_shopping_items_delete(db, [item.id for item in shopping_list.items])
    db.delete(shopping_list)
    db.commit()
    return redirect("/shopping")


@app.post("/menu/shopping")
def menu_to_shopping(
    from_date: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    selected_date = parse_date(from_date, msk_today()) or msk_today()
    start_date = week_start_for(selected_date)
    end_date = start_date + timedelta(days=6)
    items = db.scalars(
        select(MenuItem)
        .options(selectinload(MenuItem.recipe))
        .where(MenuItem.owner_id == user.id, MenuItem.plan_date >= datetime.combine(start_date, time.min), MenuItem.plan_date <= datetime.combine(end_date, time.max))
    ).all()
    shopping_list = ShoppingList(owner_id=user.id, title=f"Покупки на меню {start_date.strftime('%d.%m')}–{end_date.strftime('%d.%m')}")
    db.add(shopping_list)
    db.flush()
    for menu_item in items:
        if not menu_item.recipe:
            continue
        for item in ingredients_to_shopping_items(menu_item.recipe.ingredients, None, db, user):
            db.add(ShoppingItem(shopping_list_id=shopping_list.id, title=str(item["title"]), amount=item.get("amount"), department=str(item["department"])))
    db.commit()
    return redirect(f"/shopping/lists/{shopping_list.id}")


@app.get("/expenses/planning")
def expense_planning_page(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    limits = db.scalars(select(ExpenseLimit).where(ExpenseLimit.owner_id == user.id).order_by(ExpenseLimit.category_name)).all()
    recurring = db.scalars(select(RecurringExpense).options(selectinload(RecurringExpense.expense_list)).where(RecurringExpense.owner_id == user.id).order_by(RecurringExpense.day_of_month)).all()
    expense_lists = accessible_expense_lists(db, user)
    today = msk_today()
    period_start_day = expense_period_start_day(user)
    current_month_start, current_month_end = expense_period_bounds(today, period_start_day)
    current_month_by_category: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    for expense_list in expense_lists:
        for category in expense_list.categories:
            for item in category.items:
                item_date = msk_date(item.created_at)
                if item_date and current_month_start <= item_date <= current_month_end:
                    current_month_by_category[category.name.strip().casefold()] += item.amount

    limit_total = sum((limit.monthly_limit for limit in limits), Decimal("0.00"))
    limit_spent = sum(
        (current_month_by_category.get(limit.category_name.strip().casefold(), Decimal("0.00")) for limit in limits),
        Decimal("0.00"),
    )
    limit_left = limit_total - limit_spent
    limit_percent = int((limit_spent / limit_total) * 100) if limit_total else 0
    return render(
        request,
        "expense_planning.html",
        {
            "user": user,
            "limits": limits,
            "recurring": recurring,
            "expense_lists": expense_lists,
            "category_options": expense_category_options(db, user),
            "limit_total": limit_total,
            "limit_spent": limit_spent,
            "limit_left": limit_left,
            "limit_percent": min(limit_percent, 160),
            "period_start_day": period_start_day,
            "current_period_start": current_month_start,
            "current_period_end": current_month_end,
            "current_period_label": format_period_range(current_month_start, current_month_end),
        },
    )


@app.post("/expenses/limits")
def expense_limit_save(category_name: str = Form(...), monthly_limit: str = Form(...), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    amount = require_positive_money(monthly_limit, "Лимит")
    clean = clean_required_text(category_name, "Категория", 120)
    existing = next(
        (
            item
            for item in db.scalars(select(ExpenseLimit).where(ExpenseLimit.owner_id == user.id)).all()
            if item.category_name.strip().casefold() == clean.casefold()
        ),
        None,
    )
    if existing:
        existing.monthly_limit = amount
    else:
        db.add(ExpenseLimit(owner_id=user.id, category_name=clean, monthly_limit=amount))
    created = ensure_limit_category_in_owned_lists(db, user, clean)
    db.commit()
    message = "Лимит сохранён"
    if created:
        message += f", категория добавлена в списки трат: {created}"
    return redirect_notice("/expenses/planning", message)


@app.post("/expenses/limits/{limit_id}/update")
def expense_limit_update(
    limit_id: int,
    category_name: str = Form(...),
    monthly_limit: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    limit = db.get(ExpenseLimit, limit_id)
    if not limit or limit.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Лимит не найден")

    clean = clean_required_text(category_name, "Категория", 120)
    amount = require_positive_money(monthly_limit, "Лимит")

    existing = next(
        (
            item
            for item in db.scalars(
                select(ExpenseLimit).where(ExpenseLimit.owner_id == user.id, ExpenseLimit.id != limit.id)
            ).all()
            if item.category_name.strip().casefold() == clean.casefold()
        ),
        None,
    )
    if existing:
        existing.monthly_limit = amount
        db.delete(limit)
    else:
        limit.category_name = clean
        limit.monthly_limit = amount
    created = ensure_limit_category_in_owned_lists(db, user, clean)
    db.commit()
    message = "Лимит обновлён"
    if created:
        message += f", категория добавлена в списки трат: {created}"
    return redirect_notice("/expenses/planning", message)


@app.post("/expenses/limits/{limit_id}/delete")
def expense_limit_delete(limit_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    limit = db.get(ExpenseLimit, limit_id)
    if not limit or limit.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Лимит не найден")
    db.delete(limit)
    db.commit()
    return redirect("/expenses/planning")


@app.post("/expenses/recurring")
def recurring_expense_create(
    expense_list_id: int = Form(...),
    category_name: str = Form(...),
    title: str = Form(...),
    amount: str = Form(...),
    day_of_month: int = Form(1),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    expense_list = require_expense_access(db, expense_list_id, user)
    if expense_list.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Повторяющиеся траты можно создавать только в своих списках")
    db.add(
        RecurringExpense(
            owner_id=user.id,
            expense_list_id=expense_list.id,
            category_name=clean_required_text(category_name or "Регулярное", "Категория", 120),
            title=clean_required_text(title, "Название", 150),
            amount=require_positive_money(amount),
            day_of_month=max(1, min(31, int(day_of_month or 1))),
        )
    )
    db.commit()
    return redirect("/expenses/planning")


@app.post("/expenses/recurring/apply")
def recurring_expense_apply(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    apply_due_recurring_expenses(db, user)
    return redirect("/expenses/planning")


@app.post("/expenses/recurring/{recurring_id}/delete")
def recurring_expense_delete(recurring_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = db.get(RecurringExpense, recurring_id)
    if not item or item.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Платёж не найден")
    db.delete(item)
    db.commit()
    return redirect("/expenses/planning")


@app.get("/expenses/splits")
def splits_page(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    splits = db.scalars(select(DebtSplit).options(selectinload(DebtSplit.debtor)).where(DebtSplit.owner_id == user.id).order_by(DebtSplit.is_settled, desc(DebtSplit.created_at))).all()
    incoming = db.scalars(select(DebtSplit).options(selectinload(DebtSplit.owner)).where(DebtSplit.debtor_id == user.id).order_by(DebtSplit.is_settled, desc(DebtSplit.created_at))).all()
    return render(request, "expense_splits.html", {"user": user, "splits": splits, "incoming": incoming})


@app.post("/expenses/splits")
def split_create(
    username: str = Form(...),
    title: str = Form(...),
    total_amount: str = Form(...),
    debtor_amount: str = Form(""),
    note: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    target = db.scalar(select(User).where(User.username == normalize_username(username)))
    if not target or target.id == user.id:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    total = require_positive_money(total_amount, "Общая сумма")
    debtor = optional_nonnegative_money(debtor_amount, "Доля") if debtor_amount.strip() else (total / Decimal("2")).quantize(Decimal("0.01"))
    if debtor is None or debtor > total:
        raise HTTPException(status_code=400, detail="Доля должна быть от нуля до общей суммы")
    db.add(
        DebtSplit(
            owner_id=user.id,
            debtor_id=target.id,
            title=clean_required_text(title, "Название", 150),
            total_amount=total,
            debtor_amount=debtor,
            note=clean_optional_text(note, 4000),
        )
    )
    db.commit()
    return redirect("/expenses/splits")


@app.post("/expenses/splits/{split_id}/toggle")
def split_toggle(split_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    split = db.get(DebtSplit, split_id)
    if not split or split.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Долг не найден")
    split.is_settled = not split.is_settled
    db.commit()
    return redirect("/expenses/splits")


@app.post("/expenses/splits/{split_id}/delete")
def split_delete(split_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    split = db.get(DebtSplit, split_id)
    if not split or split.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Долг не найден")
    db.delete(split)
    db.commit()
    return redirect("/expenses/splits")


def expense_export_rows(db: Session, user: User) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for expense_list in accessible_expense_lists(db, user):
        for category in expense_list.categories:
            for item in category.items:
                rows.append(
                    {
                        "date": format_msk_datetime(item.created_at, "%Y-%m-%d"),
                        "display_date": format_msk_datetime(item.created_at, "%d.%m.%Y"),
                        "list_title": expense_list.title,
                        "category": category.name,
                        "title": item.title,
                        "amount": item.amount,
                        "owner": expense_list.owner.username,
                        "include_in_analytics": item.include_in_analytics,
                        "include_in_forecast": item.include_in_forecast,
                        "sort_date": item.created_at,
                    }
                )
    rows.sort(key=lambda row: row["sort_date"], reverse=True)
    return rows


@app.get("/expenses/export.xlsx")
def expenses_export_xlsx(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = expense_export_rows(db, user)
    filename = f"expenses_{msk_today().isoformat()}.xlsx"
    return Response(
        content=build_expenses_xlsx(rows),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/expenses/export.csv")
def expenses_export_csv(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    content = build_expenses_csv(expense_export_rows(db, user))
    return Response(content=content, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=expenses.csv"})


FILE_SHARE_TTLS = {
    "1h": (timedelta(hours=1), "1 час"),
    "24h": (timedelta(days=1), "24 часа"),
    "3d": (timedelta(days=3), "3 дня"),
    "7d": (timedelta(days=7), "7 дней"),
}


def new_file_share_token(db: Session) -> str:
    while True:
        token = secrets.token_urlsafe(32)
        if db.scalar(select(TemporaryFileTransfer.id).where(TemporaryFileTransfer.public_token == token)) is None:
            return token


def public_share_base_url(request: Request) -> str:
    """Preserve HTTPS when the app is reached through Caddy's TLS proxy."""
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",", 1)[0].strip().lower()
    if scheme not in {"http", "https"}:
        scheme = request.url.scheme
    return str(request.base_url.replace(scheme=scheme)).rstrip("/")


def require_owned_file_transfer(db: Session, transfer_id: int, user: User) -> TemporaryFileTransfer:
    transfer = db.scalar(
        select(TemporaryFileTransfer)
        .options(selectinload(TemporaryFileTransfer.files))
        .where(TemporaryFileTransfer.id == transfer_id, TemporaryFileTransfer.owner_id == user.id)
    )
    if not transfer:
        raise HTTPException(status_code=404, detail="Передача не найдена")
    return transfer


def file_download_response(shared_file: TemporarySharedFile) -> FileResponse:
    try:
        path = storage_path(SHARED_FILES_DIR, shared_file.transfer_id, shared_file.storage_key)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Файл не найден") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=safe_original_filename(shared_file.original_filename),
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def file_transfer_form_values(title: str = "", description: str = "", ttl: str = "24h") -> dict[str, str]:
    return {"title": title, "description": description, "ttl": ttl if ttl in FILE_SHARE_TTLS else "24h"}


def render_file_transfer_form(request: Request, user: User, form: dict[str, str], error: str | None = None):
    return render(request, "file_transfer_form.html", {"user": user, "form": form, "ttl_options": FILE_SHARE_TTLS, "error": error, "max_file_mb": settings.file_share_max_file_bytes // 1024 // 1024, "max_transfer_mb": settings.file_share_max_transfer_bytes // 1024 // 1024})


@app.get("/files")
def file_transfers_page(request: Request, status_filter: str = "all", sort: str = "newest", user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    now = utc_now_naive()
    status_filter = status_filter if status_filter in {"all", "active", "expired"} else "all"
    sort = sort if sort in {"newest", "expires", "largest"} else "newest"
    filters = [TemporaryFileTransfer.owner_id == user.id]
    if status_filter == "active":
        filters.append(TemporaryFileTransfer.expires_at > now)
    elif status_filter == "expired":
        filters.append(TemporaryFileTransfer.expires_at <= now)
    transfers = db.scalars(
        select(TemporaryFileTransfer)
        .options(selectinload(TemporaryFileTransfer.files))
        .where(*filters)
        .order_by(TemporaryFileTransfer.created_at.desc(), TemporaryFileTransfer.id.desc())
    ).all()
    if sort == "expires":
        transfers.sort(key=lambda transfer: (transfer.expires_at, transfer.id))
    elif sort == "largest":
        transfers.sort(key=lambda transfer: (transfer.total_size_bytes, transfer.id), reverse=True)
    all_transfers = db.scalars(select(TemporaryFileTransfer).options(selectinload(TemporaryFileTransfer.files)).where(TemporaryFileTransfer.owner_id == user.id)).all()
    summary = {"bytes": sum(transfer.total_size_bytes for transfer in all_transfers), "active": sum(not transfer_is_expired(transfer, now) for transfer in all_transfers), "expired": sum(transfer_is_expired(transfer, now) for transfer in all_transfers)}
    return render(request, "file_transfers.html", {"user": user, "transfers": transfers, "now": now, "summary": summary, "status_filter": status_filter, "sort": sort, "max_file_mb": settings.file_share_max_file_bytes // 1024 // 1024, "max_transfer_mb": settings.file_share_max_transfer_bytes // 1024 // 1024, "max_storage_mb": settings.file_share_max_storage_bytes // 1024 // 1024, "max_user_storage_mb": settings.file_share_max_user_storage_bytes // 1024 // 1024})


@app.get("/files/new")
def file_transfer_new(request: Request, user: User = Depends(get_current_user)):
    return render_file_transfer_form(request, user, file_transfer_form_values())


@app.post("/files/new")
def file_transfer_create(request: Request, title: str = Form(""), description: str = Form(""), ttl: str = Form("24h"), files: list[UploadFile] = File([]), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    form = file_transfer_form_values(title=title, description=description, ttl=ttl)
    try:
        clean_title, clean_description = title.strip(), description.strip()
        if not clean_title or len(clean_title) > 160: raise FileShareValidationError("Укажите название передачи до 160 символов.")
        if len(clean_description) > 4_000: raise FileShareValidationError("Описание слишком длинное.")
        if ttl not in FILE_SHARE_TTLS: raise FileShareValidationError("Выберите срок действия ссылки.")
        prepared = validate_uploads(files, max_file_bytes=settings.file_share_max_file_bytes, max_transfer_bytes=settings.file_share_max_transfer_bytes, existing_size_bytes=0)
        incoming_bytes = sum(item[2] for item in prepared)
        ensure_storage_capacity(SHARED_FILES_DIR, incoming_bytes=incoming_bytes, current_bytes=storage_usage(db), max_storage_bytes=settings.file_share_max_storage_bytes, max_user_storage_bytes=settings.file_share_max_user_storage_bytes, current_user_bytes=storage_usage(db, owner_id=user.id), min_free_bytes=settings.file_share_min_free_bytes)
        transfer = TemporaryFileTransfer(owner_id=user.id, title=clean_title, description=clean_description or None, public_token=new_file_share_token(db), expires_at=utc_now_naive() + FILE_SHARE_TTLS[ttl][0])
        db.add(transfer); db.flush()
        write_uploads(db, transfer, prepared, root=SHARED_FILES_DIR, max_file_bytes=settings.file_share_max_file_bytes, max_transfer_bytes=settings.file_share_max_transfer_bytes, existing_size_bytes=0)
        db.commit()
    except FileShareValidationError as exc:
        db.rollback()
        return render_file_transfer_form(request, user, form, str(exc))
    except Exception:
        db.rollback()
        if "transfer" in locals() and transfer.id:
            try: remove_transfer_storage(SHARED_FILES_DIR, transfer.id)
            except FileShareValidationError: pass
        logger.exception("Temporary file transfer creation failed owner_id=%s", user.id)
        return render_file_transfer_form(request, user, form, "Не удалось создать передачу. Попробуйте ещё раз.")
    finally:
        close_uploads(files)
    logger.info("Temporary file transfer created transfer_id=%s owner_id=%s", transfer.id, user.id)
    return redirect_notice(f"/files/{transfer.id}", "Передача создана")


@app.get("/files/{transfer_id}")
def file_transfer_detail(request: Request, transfer_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    transfer = require_owned_file_transfer(db, transfer_id, user)
    share_url = f"{public_share_base_url(request)}/share/{transfer.public_token}"
    return render(request, "file_transfer_detail.html", {"user": user, "transfer": transfer, "share_url": share_url, "now": utc_now_naive(), "ttl_options": FILE_SHARE_TTLS, "max_file_mb": settings.file_share_max_file_bytes // 1024 // 1024, "max_transfer_mb": settings.file_share_max_transfer_bytes // 1024 // 1024})


@app.post("/files/{transfer_id}/upload")
def file_transfer_upload(transfer_id: int, files: list[UploadFile] = File([]), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    transfer = require_owned_file_transfer(db, transfer_id, user)
    uploaded_files: list[TemporarySharedFile] = []
    try:
        existing_size = existing_transfer_size(db, transfer.id)
        prepared = validate_uploads(files, max_file_bytes=settings.file_share_max_file_bytes, max_transfer_bytes=settings.file_share_max_transfer_bytes, existing_size_bytes=existing_size)
        if not prepared: raise FileShareValidationError("Выберите хотя бы один файл.")
        incoming_bytes = sum(item[2] for item in prepared)
        ensure_storage_capacity(SHARED_FILES_DIR, incoming_bytes=incoming_bytes, current_bytes=storage_usage(db), max_storage_bytes=settings.file_share_max_storage_bytes, max_user_storage_bytes=settings.file_share_max_user_storage_bytes, current_user_bytes=storage_usage(db, owner_id=user.id), min_free_bytes=settings.file_share_min_free_bytes)
        uploaded_files = write_uploads(db, transfer, prepared, root=SHARED_FILES_DIR, max_file_bytes=settings.file_share_max_file_bytes, max_transfer_bytes=settings.file_share_max_transfer_bytes, existing_size_bytes=existing_size)
        db.commit()
    except FileShareValidationError as exc:
        db.rollback()
        return redirect_notice(f"/files/{transfer.id}", str(exc))
    except Exception:
        db.rollback()
        for shared_file in uploaded_files:
            try: remove_shared_file(SHARED_FILES_DIR, shared_file)
            except FileShareValidationError: pass
        logger.exception("Temporary file upload failed transfer_id=%s", transfer.id)
        return redirect_notice(f"/files/{transfer.id}", "Не удалось загрузить файлы. Попробуйте ещё раз.")
    finally:
        close_uploads(files)
    logger.info("Temporary shared files uploaded transfer_id=%s count=%s", transfer.id, len(prepared))
    return redirect_notice(f"/files/{transfer.id}", "Файлы добавлены")


@app.get("/files/{transfer_id}/download/{file_id}")
def file_transfer_owner_download(transfer_id: int, file_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    transfer = require_owned_file_transfer(db, transfer_id, user)
    shared_file = next((item for item in transfer.files if item.id == file_id), None)
    if not shared_file: raise HTTPException(status_code=404, detail="Файл не найден")
    return file_download_response(shared_file)


@app.post("/files/{transfer_id}/files/{file_id}/delete")
def file_transfer_file_delete(transfer_id: int, file_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    transfer = require_owned_file_transfer(db, transfer_id, user)
    shared_file = next((item for item in transfer.files if item.id == file_id), None)
    if not shared_file: raise HTTPException(status_code=404, detail="Файл не найден")
    try:
        remove_shared_file(SHARED_FILES_DIR, shared_file)
        db.delete(shared_file); db.commit()
    except FileShareValidationError as exc:
        db.rollback()
        return redirect_notice(f"/files/{transfer.id}", str(exc))
    logger.info("Temporary shared file deleted transfer_id=%s file_id=%s", transfer.id, file_id)
    return redirect_notice(f"/files/{transfer.id}", "Файл удалён")


@app.post("/files/{transfer_id}/delete")
def file_transfer_delete(transfer_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    transfer = require_owned_file_transfer(db, transfer_id, user)
    try:
        remove_transfer_storage(SHARED_FILES_DIR, transfer.id)
        db.delete(transfer); db.commit()
    except FileShareValidationError as exc:
        db.rollback()
        return redirect_notice(f"/files/{transfer.id}", str(exc))
    logger.info("Temporary file transfer deleted transfer_id=%s owner_id=%s", transfer.id, user.id)
    return redirect_notice("/files", "Передача удалена")


@app.post("/files/cleanup-expired")
def file_transfer_cleanup_expired(user: User = Depends(get_current_user)):
    summary = run_file_share_cleanup(owner_id=user.id)
    return redirect_notice("/files?status_filter=expired", f"Удалено истёкших передач: {summary.transfers_removed}")


@app.post("/files/{transfer_id}/extend")
def file_transfer_extend(transfer_id: int, ttl: str = Form("24h"), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    transfer = require_owned_file_transfer(db, transfer_id, user)
    if ttl not in FILE_SHARE_TTLS:
        return redirect_notice(f"/files/{transfer.id}", "Выберите корректный срок продления.")
    transfer.expires_at = utc_now_naive() + FILE_SHARE_TTLS[ttl][0]
    db.commit()
    logger.info("Temporary file transfer extended transfer_id=%s owner_id=%s", transfer.id, user.id)
    return redirect_notice(f"/files/{transfer.id}", "Срок действия ссылки продлён")


@app.post("/files/{transfer_id}/rotate-link")
def file_transfer_rotate_link(transfer_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    transfer = require_owned_file_transfer(db, transfer_id, user)
    transfer.public_token = new_file_share_token(db)
    db.commit()
    logger.info("Temporary file transfer link rotated transfer_id=%s owner_id=%s", transfer.id, user.id)
    return redirect_notice(f"/files/{transfer.id}", "Ссылка заменена; старая больше не работает")


def public_file_transfer(token: str, db: Session) -> TemporaryFileTransfer:
    transfer = db.scalar(select(TemporaryFileTransfer).options(selectinload(TemporaryFileTransfer.files)).where(TemporaryFileTransfer.public_token == token))
    if not transfer: raise HTTPException(status_code=404, detail="Передача не найдена")
    return transfer


@app.get("/share/{token}")
def public_file_transfer_page(request: Request, token: str, db: Session = Depends(get_db)):
    transfer = public_file_transfer(token, db)
    expired = transfer_is_expired(transfer, utc_now_naive())
    preview_file_ids = set() if expired else {shared_file.id for shared_file in transfer.files if is_previewable_image(SHARED_FILES_DIR, shared_file)}
    response = render(request, "public_file_transfer.html", {"transfer": transfer, "expired": expired, "preview_file_ids": preview_file_ids})
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    if expired:
        response.status_code = status.HTTP_410_GONE
        logger.info("Expired public transfer access transfer_id=%s", transfer.id)
    return response


@app.get("/share/{token}/files/{file_id}/download")
def public_file_transfer_download(token: str, file_id: int, db: Session = Depends(get_db)):
    transfer = public_file_transfer(token, db)
    if transfer_is_expired(transfer, utc_now_naive()):
        logger.info("Expired public transfer download transfer_id=%s", transfer.id)
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Срок действия ссылки истёк")
    shared_file = next((item for item in transfer.files if item.id == file_id), None)
    if not shared_file: raise HTTPException(status_code=404, detail="Файл не найден")
    return file_download_response(shared_file)


@app.get("/share/{token}/files/{file_id}/preview")
def public_file_transfer_preview(token: str, file_id: int, db: Session = Depends(get_db)):
    transfer = public_file_transfer(token, db)
    if transfer_is_expired(transfer, utc_now_naive()):
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Срок действия ссылки истёк")
    shared_file = next((item for item in transfer.files if item.id == file_id), None)
    if not shared_file:
        raise HTTPException(status_code=404, detail="Файл не найден")
    preview = ensure_image_preview(SHARED_FILES_DIR, shared_file)
    if preview is None or not preview.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(preview, media_type="image/jpeg", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.get("/export/data.json")
def export_data_json(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    recipes = db.scalars(select(Recipe).where(Recipe.owner_id == user.id)).all()
    incomes = db.scalars(select(IncomeItem).where(IncomeItem.owner_id == user.id)).all()
    moments = db.scalars(select(Moment).where(Moment.owner_id == user.id).order_by(Moment.happened_on, Moment.id)).all()
    planner_items = db.scalars(
        select(PlannerItem)
        .options(selectinload(PlannerItem.reminders))
        .where(PlannerItem.owner_id == user.id)
        .order_by(PlannerItem.scheduled_for, PlannerItem.start_time, PlannerItem.id)
    ).all()
    wishlist = db.scalars(select(WishlistItem).where(WishlistItem.owner_id == user.id)).all()
    watch_items = db.scalars(select(WatchItem).where(WatchItem.owner_id == user.id)).all()
    shopping = db.scalars(select(ShoppingList).options(selectinload(ShoppingList.items)).where(ShoppingList.owner_id == user.id)).all()
    expenses = accessible_expense_lists(db, user)
    payload = {
        "version": 2,
        "settings": {"theme": user.theme, "expense_period_start_day": expense_period_start_day(user)},
        "recipes": [
            {
                "title": recipe.title,
                "source_url": recipe.source_url,
                "tags": recipe.tags,
                "cook_time_minutes": recipe.cook_time_minutes,
                "cost": str(recipe.cost) if recipe.cost is not None else None,
                "servings": str(recipe.servings) if recipe.servings is not None else None,
                "ingredients": recipe.ingredients,
                "steps": parse_recipe_steps(recipe.steps),
            }
            for recipe in recipes
        ],
        "income": [
            {
                "title": item.title,
                "source": item.source,
                "amount": str(item.amount),
                "received_at": item.received_at.isoformat(),
                "note": item.note,
            }
            for item in incomes
        ],
        "moments": [
            {
                "title": item.title,
                "description": item.description,
                "happened_on": item.happened_on.isoformat(),
            }
            for item in moments
        ],
        "planner": [
            {
                "title": item.title,
                "description": item.description,
                "scheduled_for": item.scheduled_for.isoformat(),
                "end_date": item.end_date.isoformat() if item.end_date else None,
                "recurrence_frequency": item.recurrence_frequency,
                "recurrence_interval": item.recurrence_interval,
                "recurrence_until": item.recurrence_until.isoformat() if item.recurrence_until else None,
                "start_time": item.start_time,
                "end_time": item.end_time,
                "color": item.color,
                "is_done": item.is_done,
                "reminders": [
                    {
                        "offset_value": reminder.offset_value,
                        "offset_unit": reminder.offset_unit,
                        "relation": reminder.relation,
                    }
                    for reminder in item.reminders
                ],
            }
            for item in planner_items
        ],
        "watch": [
            {
                "title": item.title,
                "kind": item.kind,
                "status": item.status,
                "season": item.season,
                "episode": item.episode,
                "minute": item.minute,
                "watch_url": item.watch_url,
                "note": item.note,
            }
            for item in watch_items
        ],
        "wishlist": [
            {
                "title": item.title,
                "url": item.url,
                "price": str(item.price) if item.price is not None else None,
                "priority": item.priority,
                "status": item.status,
                "goal_amount": str(item.goal_amount) if item.goal_amount is not None else None,
                "saved_amount": str(item.saved_amount) if item.saved_amount is not None else None,
                "note": item.note,
            }
            for item in wishlist
        ],
        "shopping_lists": [
            {
                "title": shopping_list.title,
                "items": [
                    {
                        "title": item.title,
                        "amount": item.amount,
                        "department": item.department,
                        "price": str(item.price) if item.price is not None else None,
                        "is_done": item.is_done,
                    }
                    for item in shopping_list.items
                ],
            }
            for shopping_list in shopping
        ],
        "expense_lists": [
            {
                "title": expense_list.title,
                "categories": [
                    {
                        "name": category.name,
                        "items": [
                            {
                                "title": item.title,
                                "amount": str(item.amount),
                                "created_at": item.created_at.isoformat(),
                                "include_in_analytics": item.include_in_analytics,
                                "include_in_forecast": item.include_in_forecast,
                            }
                            for item in category.items
                        ],
                    }
                    for category in expense_list.categories
                ],
            }
            for expense_list in expenses
            if expense_list.owner_id == user.id
        ],
    }
    return JSONResponse(payload, headers={"Content-Disposition": "attachment; filename=home_service_export.json"})


@app.get("/backup/download")
def backup_download(user: User = Depends(get_current_user)):
    if not is_backup_admin(user):
        raise HTTPException(status_code=403, detail="Полный бэкап доступен только владельцу сервиса")
    path = generate_backup_zip()
    return FileResponse(path, media_type="application/zip", filename=path.name)


@app.get("/backup/files/{filename}")
def backup_file_download(filename: str, user: User = Depends(get_current_user)):
    if not is_backup_admin(user):
        raise HTTPException(status_code=403, detail="Полный бэкап доступен только владельцу сервиса")
    if not re.fullmatch(r"backup_\d{8}_\d{6}(?:_[0-9a-f]{6})?\.zip", filename):
        raise HTTPException(status_code=404, detail="Backup not found")
    path = BACKUP_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Backup not found")
    return FileResponse(path, media_type="application/zip", filename=path.name)


@app.get("/backup")
def backup_page(request: Request, user: User = Depends(get_current_user)):
    backups = sorted(BACKUP_DIR.glob("backup_*.zip"), reverse=True)[:10]
    return render(request, "backup.html", {"user": user, "backups": backups, "can_download_backup": is_backup_admin(user)})


@app.post("/backup/create")
def backup_create(user: User = Depends(get_current_user)):
    if not is_backup_admin(user):
        raise HTTPException(status_code=403, detail="Полный бэкап доступен только владельцу сервиса")
    generate_backup_zip()
    return redirect("/backup")


@app.post("/import-data")
def import_data_json(
    request: Request,
    data_file: UploadFile | None = File(None),
    data_text: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    raw = data_text.strip()
    if data_file is not None and data_file.filename:
        uploaded = data_file.file.read(IMPORT_MAX_BYTES + 1)
        if len(uploaded) > IMPORT_MAX_BYTES:
            raise HTTPException(status_code=413, detail="JSON-файл не должен быть больше 2 МБ")
        try:
            raw = uploaded.decode("utf-8").strip()
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="JSON-файл должен быть в UTF-8")
    elif len(raw.encode("utf-8")) > IMPORT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="JSON не должен быть больше 2 МБ")
    if not raw:
        return redirect("/backup")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return render(
            request,
            "backup.html",
            {
                "user": user,
                "backups": sorted(BACKUP_DIR.glob("backup_*.zip"), reverse=True)[:10],
                "can_download_backup": is_backup_admin(user),
                "error": "JSON не читается",
            },
        )
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Корневой элемент JSON должен быть объектом")

    collection_names = (
        "recipes",
        "wishlist",
        "income",
        "moments",
        "planner",
        "watch",
        "shopping_lists",
        "expense_lists",
    )
    for name in collection_names:
        if name in payload and not isinstance(payload[name], list):
            raise HTTPException(status_code=400, detail=f"Поле «{name}» должно быть списком")
    nested_count = sum(len(payload.get(name, [])) for name in collection_names)
    nested_count += sum(len(item.get("items", [])) for item in payload.get("shopping_lists", []) if isinstance(item, dict) and isinstance(item.get("items", []), list))
    for raw_list in payload.get("expense_lists", []):
        if not isinstance(raw_list, dict) or not isinstance(raw_list.get("categories", []), list):
            continue
        nested_count += len(raw_list.get("categories", []))
        nested_count += sum(
            len(category.get("items", []))
            for category in raw_list.get("categories", [])
            if isinstance(category, dict) and isinstance(category.get("items", []), list)
        )
    if nested_count > IMPORT_MAX_ITEMS:
        raise HTTPException(status_code=413, detail="В JSON слишком много записей")

    imported = 0
    skipped = 0
    try:
        raw_settings = payload.get("settings")
        if isinstance(raw_settings, dict):
            theme = str(raw_settings.get("theme") or "").strip().lower()
            if theme in {"light", "dark"}:
                user.theme = theme
            try:
                period_day = int(raw_settings.get("expense_period_start_day", 1))
                user.expense_period_start_day = max(1, min(31, period_day))
            except (TypeError, ValueError):
                skipped += 1

        for raw_recipe in payload.get("recipes", []):
            try:
                normalized = normalize_recipe_import_payload(raw_recipe)
            except (ValueError, HTTPException, TypeError):
                skipped += 1
                continue
            db.add(Recipe(owner_id=user.id, **normalized))
            imported += 1

        for raw_wish in payload.get("wishlist", []):
            try:
                if not isinstance(raw_wish, dict):
                    raise ValueError
                title = clean_required_text(str(raw_wish.get("title") or ""), "Название", 150)
                priority = str(raw_wish.get("priority") or "medium")
                status_value = str(raw_wish.get("status") or "want")
                db.add(WishlistItem(
                    owner_id=user.id,
                    title=title,
                    url=clean_http_url(raw_wish.get("url"), "Ссылка", 500),
                    price=optional_nonnegative_money(None if raw_wish.get("price") is None else str(raw_wish.get("price")), "Цена"),
                    priority=priority if priority in WISHLIST_PRIORITY_LABELS else "medium",
                    status=status_value if status_value in WISHLIST_STATUS_LABELS else "want",
                    goal_amount=optional_nonnegative_money(None if raw_wish.get("goal_amount") is None else str(raw_wish.get("goal_amount")), "Цель"),
                    saved_amount=optional_nonnegative_money(None if raw_wish.get("saved_amount") is None else str(raw_wish.get("saved_amount")), "Накоплено"),
                    note=clean_optional_text(raw_wish.get("note"), 4000),
                ))
                imported += 1
            except (ValueError, HTTPException, TypeError):
                skipped += 1

        for raw_income in payload.get("income", []):
            try:
                if not isinstance(raw_income, dict):
                    raise ValueError
                amount = require_positive_money(str(raw_income.get("amount") or ""))
                db.add(IncomeItem(
                    owner_id=user.id,
                    title=clean_required_text(str(raw_income.get("title") or ""), "Название", 150),
                    source=clean_optional_text(raw_income.get("source"), 120),
                    amount=amount,
                    received_at=parse_datetime(raw_income.get("received_at")) or utc_now_naive(),
                    note=clean_optional_text(raw_income.get("note"), 4000),
                ))
                imported += 1
            except (ValueError, HTTPException, TypeError):
                skipped += 1

        for raw_moment in payload.get("moments", []):
            try:
                if not isinstance(raw_moment, dict):
                    raise ValueError
                moment_day = parse_date(str(raw_moment.get("happened_on") or ""), msk_today()) or msk_today()
                db.add(Moment(
                    owner_id=user.id,
                    title=clean_required_text(str(raw_moment.get("title") or ""), "Заголовок", 180),
                    description=clean_optional_text(raw_moment.get("description"), 20_000),
                    happened_on=moment_day,
                ))
                imported += 1
            except (ValueError, HTTPException, TypeError):
                skipped += 1

        for raw_item in payload.get("planner", []):
            try:
                if not isinstance(raw_item, dict):
                    raise ValueError
                item_day = parse_date(str(raw_item.get("scheduled_for") or ""), msk_today()) or msk_today()
                item_end_date = parse_planner_end_date(str(raw_item.get("end_date") or ""), item_day)
                recurrence_frequency, recurrence_interval, recurrence_until = parse_planner_recurrence(str(raw_item.get("recurrence_frequency") or ""), str(raw_item.get("recurrence_interval") or "1"), str(raw_item.get("recurrence_until") or ""), item_day)
                start_time = parse_planner_time(str(raw_item.get("start_time") or ""))
                end_time = parse_planner_time(str(raw_item.get("end_time") or ""))
                if start_time and end_time and end_time <= start_time:
                    raise ValueError
                raw_color = str(raw_item.get("color") or "#2563eb").lower()
                item = PlannerItem(
                    owner_id=user.id,
                    title=clean_required_text(str(raw_item.get("title") or ""), "Название", 180),
                    description=clean_optional_text(raw_item.get("description"), 10_000),
                    scheduled_for=item_day,
                    end_date=item_end_date,
                    recurrence_frequency=recurrence_frequency, recurrence_interval=recurrence_interval, recurrence_until=recurrence_until,
                    start_time=start_time,
                    end_time=end_time,
                    color=raw_color if re.fullmatch(r"#[0-9a-f]{6}", raw_color) else "#2563eb",
                    is_done=parse_import_bool(raw_item.get("is_done")),
                )
                raw_reminders = raw_item.get("reminders") or []
                if not isinstance(raw_reminders, list) or any(not isinstance(value, dict) for value in raw_reminders):
                    raise ValueError
                reminder_configs = parse_reminder_configs(
                    [str(value.get("offset_value", "")) for value in raw_reminders],
                    [str(value.get("offset_unit", "")) for value in raw_reminders],
                )
                replace_reminder_configs(item, reminder_configs)
                db.add(item)
                imported += 1
            except (ValueError, HTTPException, TypeError):
                skipped += 1

        for raw_item in payload.get("watch", []):
            try:
                if not isinstance(raw_item, dict):
                    raise ValueError
                kind = str(raw_item.get("kind") or "movie")
                status_value = str(raw_item.get("status") or "planned")
                season_raw = str(raw_item.get("season") or "")
                episode_raw = str(raw_item.get("episode") or "")
                minute_raw = str(raw_item.get("minute") if raw_item.get("minute") is not None else "")
                db.add(WatchItem(
                    owner_id=user.id,
                    title=clean_required_text(str(raw_item.get("title") or ""), "Название", 180),
                    kind=kind if kind in WATCH_KIND_LABELS else "movie",
                    status=status_value if status_value in WATCH_STATUS_LABELS else "planned",
                    season=parse_optional_int(season_raw, "Сезон", 1, 999),
                    episode=parse_optional_int(episode_raw, "Серия", 1, 99_999),
                    minute=parse_optional_int(minute_raw, "Минута", 0, 99_999),
                    watch_url=clean_http_url(raw_item.get("watch_url"), "Ссылка на просмотр", 700),
                    note=clean_optional_text(raw_item.get("note"), 4000),
                ))
                imported += 1
            except (ValueError, HTTPException, TypeError):
                skipped += 1

        for raw_list in payload.get("shopping_lists", []):
            if not isinstance(raw_list, dict) or not isinstance(raw_list.get("items", []), list):
                skipped += 1
                continue
            try:
                shopping_list = ShoppingList(
                    owner_id=user.id,
                    title=clean_required_text(str(raw_list.get("title") or ""), "Название", 150),
                )
                db.add(shopping_list)
                db.flush()
            except (ValueError, HTTPException, TypeError):
                skipped += 1
                continue
            imported += 1
            for raw_item in raw_list.get("items", []):
                try:
                    if not isinstance(raw_item, dict):
                        raise ValueError
                    db.add(ShoppingItem(
                        shopping_list_id=shopping_list.id,
                        title=clean_required_text(str(raw_item.get("title") or ""), "Название", 180),
                        amount=clean_optional_text(raw_item.get("amount"), 120),
                        department=(str(raw_item.get("department") or "Прочее").strip() or "Прочее")[:80],
                        price=optional_nonnegative_money(None if raw_item.get("price") is None else str(raw_item.get("price")), "Цена"),
                        is_done=parse_import_bool(raw_item.get("is_done")),
                    ))
                    imported += 1
                except (ValueError, HTTPException, TypeError):
                    skipped += 1

        for raw_list in payload.get("expense_lists", []):
            if not isinstance(raw_list, dict) or not isinstance(raw_list.get("categories", []), list):
                skipped += 1
                continue
            try:
                expense_list = ExpenseList(
                    owner_id=user.id,
                    title=clean_required_text(str(raw_list.get("title") or ""), "Название", 120),
                )
                db.add(expense_list)
                db.flush()
            except (ValueError, HTTPException, TypeError):
                skipped += 1
                continue
            imported += 1
            for raw_category in raw_list.get("categories", []):
                if not isinstance(raw_category, dict) or not isinstance(raw_category.get("items", []), list):
                    skipped += 1
                    continue
                try:
                    category = ExpenseCategory(
                        expense_list_id=expense_list.id,
                        name=clean_required_text(str(raw_category.get("name") or ""), "Категория", 120),
                    )
                    db.add(category)
                    db.flush()
                except (ValueError, HTTPException, TypeError):
                    skipped += 1
                    continue
                imported += 1
                for raw_item in raw_category.get("items", []):
                    try:
                        if not isinstance(raw_item, dict):
                            raise ValueError
                        imported_date = parse_datetime(raw_item.get("created_at")) if raw_item.get("created_at") else None
                        db.add(ExpenseItem(
                            category_id=category.id,
                            title=clean_required_text(str(raw_item.get("title") or ""), "Название", 150),
                            amount=require_positive_money(str(raw_item.get("amount") or "")),
                            created_at=imported_date or utc_now_naive(),
                            include_in_analytics=parse_import_bool(raw_item.get("include_in_analytics"), True),
                            include_in_forecast=parse_import_bool(raw_item.get("include_in_forecast"), True),
                        ))
                        imported += 1
                    except (ValueError, HTTPException, TypeError):
                        skipped += 1
        db.commit()
    except (IntegrityError, OSError):
        db.rollback()
        logger.exception("Data import failed for user_id=%s", user.id)
        raise HTTPException(status_code=400, detail="Не удалось импортировать данные: изменения отменены")
    message = f"Импортировано записей: {imported}"
    if skipped:
        message += f", пропущено некорректных: {skipped}"
    return redirect_notice("/backup", message)


app.include_router(system_router)
if settings.home_ai_enabled:
    # Keep Home AI code and its database schema intact, but do not expose it or
    # initialize its runtime integration while the feature is frozen.
    from .ai.router import router as ai_router

    app.include_router(ai_router)
