import asyncio
import base64
import calendar
import ipaddress
import json
import logging
import os
import re
import threading
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from markupsafe import Markup, escape

try:
    from pywebpush import WebPushException, webpush
except Exception:  # pragma: no cover - dependency is installed in Docker, but keep local dev resilient
    WebPushException = Exception
    webpush = None

from fastapi import Depends, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import and_, desc, func, or_, select, update
from sqlalchemy import delete as sql_delete
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .ai.router import router as ai_router
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
    PushSubscription,
    Recipe,
    RecipeCookingTimer,
    RecurringExpense,
    ShoppingCategoryRule,
    ShoppingItem,
    ShoppingList,
    ShoppingListShare,
    ShoppingPriceHistory,
    User,
    WatchItem,
    WishlistItem,
    WishlistShare,
)
from .routers.system import router as system_router
from .services.backups import create_backup_zip, sqlite_database_path
from .services.exports import build_expenses_csv, build_expenses_xlsx
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
    PUSH_VAPID_FILE,
    PUSH_VAPID_PRIVATE_KEY_FILE,
    RECIPE_MEDIA_DIR,
    app,
    templates,
)

ONLINE_WINDOW_SECONDS = 75
BACKUP_RETENTION_COUNT = 14
IMPORT_MAX_BYTES = 2 * 1024 * 1024
IMPORT_MAX_ITEMS = 10_000
MAX_REQUEST_BYTES = 16 * 1024 * 1024
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
templates.env.globals["presence_info"] = presence_info


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def ensure_vapid_keys() -> dict[str, str]:
    """Return persistent Web Push VAPID keys.

    pywebpush is most reliable when the private key is passed as a PEM file path,
    not as a multi-line PEM string. Older project versions stored only the PEM
    text in data/push_vapid.json; here we also materialize it to
    data/push_vapid_private.pem and use that path for sending.
    """
    env_private = os.getenv("VAPID_PRIVATE_KEY", "").strip()
    env_public = os.getenv("VAPID_PUBLIC_KEY", "").strip()
    if env_private and env_public:
        private_text = env_private.replace("\\n", "\n")
        PUSH_VAPID_PRIVATE_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        PUSH_VAPID_PRIVATE_KEY_FILE.write_text(private_text, encoding="utf-8")
        PUSH_VAPID_PRIVATE_KEY_FILE.chmod(0o600)
        return {"private_key_path": str(PUSH_VAPID_PRIVATE_KEY_FILE), "public_key": env_public}

    if PUSH_VAPID_FILE.exists():
        try:
            data = json.loads(PUSH_VAPID_FILE.read_text(encoding="utf-8"))
            private_text = str(data.get("private_key_pem") or "")
            public_key = str(data.get("public_key") or "")
            if private_text and public_key:
                PUSH_VAPID_PRIVATE_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
                if not PUSH_VAPID_PRIVATE_KEY_FILE.exists() or PUSH_VAPID_PRIVATE_KEY_FILE.read_text(encoding="utf-8", errors="ignore") != private_text:
                    PUSH_VAPID_PRIVATE_KEY_FILE.write_text(private_text, encoding="utf-8")
                    PUSH_VAPID_PRIVATE_KEY_FILE.chmod(0o600)
                return {"private_key_path": str(PUSH_VAPID_PRIVATE_KEY_FILE), "public_key": public_key}
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Ignoring invalid stored VAPID keys: %s", exc)

    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
    except Exception:
        return {"private_key_path": "", "public_key": ""}

    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    data = {"private_key_pem": private_pem, "public_key": _b64url(public_bytes)}
    PUSH_VAPID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PUSH_VAPID_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    PUSH_VAPID_PRIVATE_KEY_FILE.write_text(private_pem, encoding="utf-8")
    PUSH_VAPID_FILE.chmod(0o600)
    PUSH_VAPID_PRIVATE_KEY_FILE.chmod(0o600)
    return {"private_key_path": str(PUSH_VAPID_PRIVATE_KEY_FILE), "public_key": data["public_key"]}


def send_push_to_user(db: Session, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """Send Web Push notifications to all devices of the user.

    Returns diagnostics so the UI can show whether a push was actually sent.
    Broken subscriptions are removed automatically.
    """
    result: dict[str, Any] = {"ok": False, "sent": 0, "failed": 0, "removed": 0, "errors": [], "subscriptions": 0}
    if webpush is None:
        result["errors"].append("pywebpush не установлен в контейнере")
        return result

    keys = ensure_vapid_keys()
    private_key_path = keys.get("private_key_path")
    if not private_key_path:
        result["errors"].append("не удалось создать VAPID-ключи")
        return result

    subscriptions = db.scalars(select(PushSubscription).where(PushSubscription.user_id == user_id)).all()
    result["subscriptions"] = len(subscriptions)
    if not subscriptions:
        result["errors"].append("у пользователя нет push-подписок")
        return result

    claims = {"sub": os.getenv("VAPID_SUBJECT", "mailto:admin@example.com")}
    dead_ids: list[int] = []
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
                dead_ids.append(sub.id)
            logger.warning("Web Push failed for user_id=%s status=%s", user_id, status_code or "unknown")

    if dead_ids:
        for sub_id in dead_ids:
            sub = db.get(PushSubscription, sub_id)
            if sub:
                db.delete(sub)
                result["removed"] += 1
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
        "users": {"theme", "expense_period_start_day", "last_seen_at"},
        "recipes": {"image_path", "tags", "is_favorite"},
        "watch_items": {"last_watched_at"},
        "chat_threads": {"is_pinned"},
        "chat_thread_messages": {"reply_to_id", "attachment_path"},
        "recipe_cooking_timers": {"last_reminded_at"},
        "expense_items": {"include_in_analytics", "include_in_forecast"},
        "expense_list_shares": {"can_edit"},
        "shopping_list_shares": {"can_edit"},
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
        user_columns = [row[1] for row in connection.exec_driver_sql("PRAGMA table_info(users)").fetchall()]
        if "theme" not in user_columns:
            connection.exec_driver_sql(
                "ALTER TABLE users ADD COLUMN theme VARCHAR(20) NOT NULL DEFAULT 'light'"
            )
        if "expense_period_start_day" not in user_columns:
            connection.exec_driver_sql("ALTER TABLE users ADD COLUMN expense_period_start_day INTEGER NOT NULL DEFAULT 1")
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


@asynccontextmanager
async def app_lifespan(_app):
    timer_reminder_stop.clear()
    on_startup()
    try:
        yield
    finally:
        timer_reminder_stop.set()


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


def clamp_month_day(year: int, month: int, day: int) -> date:
    safe_day = max(1, min(31, int(day or 1)))
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(safe_day, last_day))


def add_months(day: date, months: int) -> date:
    month_index = (day.month - 1) + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    return clamp_month_day(year, month, day.day)


def shifted_month(year: int, month: int, months: int) -> tuple[int, int]:
    month_index = (month - 1) + months
    return year + month_index // 12, month_index % 12 + 1


def expense_period_start_day(user: User | None) -> int:
    value = getattr(user, "expense_period_start_day", 1) or 1
    return max(1, min(31, int(value)))


def expense_period_bounds(day: date, start_day: int) -> tuple[date, date]:
    current_start = clamp_month_day(day.year, day.month, start_day)
    if day < current_start:
        year, month = shifted_month(day.year, day.month, -1)
        current_start = clamp_month_day(year, month, start_day)
    next_year, next_month = shifted_month(current_start.year, current_start.month, 1)
    next_start = clamp_month_day(next_year, next_month, start_day)
    return current_start, next_start - timedelta(days=1)


def previous_expense_period_bounds(period_start: date, start_day: int) -> tuple[date, date]:
    year, month = shifted_month(period_start.year, period_start.month, -1)
    previous_start = clamp_month_day(year, month, start_day)
    return previous_start, period_start - timedelta(days=1)


def format_period_range(period_start: date, period_end: date) -> str:
    if period_start.year == period_end.year:
        return f"{period_start.strftime('%d.%m')}–{period_end.strftime('%d.%m.%Y')}"
    return f"{period_start.strftime('%d.%m.%Y')}–{period_end.strftime('%d.%m.%Y')}"


def expense_forecast_from_lists(
    expense_lists: list[ExpenseList],
    period_start: date,
    period_end: date,
    recurring: list[RecurringExpense] | None = None,
) -> dict[str, Any]:
    """Forecast future days from category weekday patterns and scheduled payments."""
    today = msk_today()
    daily: dict[date, Decimal] = defaultdict(lambda: Decimal("0.00"))
    category_daily: dict[str, dict[date, Decimal]] = defaultdict(lambda: defaultdict(lambda: Decimal("0.00")))
    for expense_list in expense_lists:
        for category in expense_list.categories:
            for item in category.items:
                if not item.include_in_analytics or not item.include_in_forecast:
                    continue
                item_day = msk_date(item.created_at)
                if item_day < today - timedelta(days=364):
                    continue
                daily[item_day] += item.amount
                category_daily[category.name][item_day] += item.amount

    recurring = recurring or []
    recurring_by_day: dict[date, Decimal] = defaultdict(lambda: Decimal("0.00"))
    future_start = max(today + timedelta(days=1), period_start)
    for recurring_item in recurring:
        cursor = date(future_start.year, future_start.month, 1)
        while cursor <= period_end:
            due_date = clamp_month_day(cursor.year, cursor.month, recurring_item.day_of_month)
            if future_start <= due_date <= period_end:
                recurring_by_day[due_date] += recurring_item.amount
            next_year, next_month = shifted_month(cursor.year, cursor.month, 1)
            cursor = date(next_year, next_month, 1)

    if daily:
        first_day = min(daily)
        history_days = (today - first_day).days + 1
    else:
        first_day = today
        history_days = 0

    def robust_weekday_rate(values: list[Decimal]) -> Decimal:
        if not values:
            return Decimal("0.00")
        positive = sorted(value for value in values if value > 0)
        cap = positive[min(len(positive) - 1, max(0, int(len(positive) * .9) - 1))] if positive else Decimal("0.00")
        weighted_total = Decimal("0.00")
        weights = Decimal("0.00")
        for index, value in enumerate(values):
            weight = Decimal("1") + Decimal("2") * Decimal(index + 1) / Decimal(len(values))
            weighted_total += min(value, cap) * weight
            weights += weight
        return (weighted_total / weights).quantize(Decimal("0.01")) if weights else Decimal("0.00")

    weekday_rates: dict[str, dict[int, Decimal]] = {}
    if history_days:
        for category_name, amounts in category_daily.items():
            category_first_day = min(amounts)
            category_history_days = (today - category_first_day).days + 1
            category_dates = [category_first_day + timedelta(days=offset) for offset in range(category_history_days)]
            weekday_rates[category_name] = {
                weekday: robust_weekday_rate([amounts[day] for day in category_dates if day.weekday() == weekday])
                for weekday in range(7)
            }

    period_days = max(1, (period_end - period_start).days + 1)
    recurring_daily_share = sum((item.amount for item in recurring), Decimal("0.00")) / Decimal(period_days)
    daily_forecast: dict[date, Decimal] = {}
    for offset in range(max(0, (period_end - future_start).days + 1)):
        forecast_day = future_start + timedelta(days=offset)
        baseline = sum((rates[forecast_day.weekday()] for rates in weekday_rates.values()), Decimal("0.00"))
        baseline = max(Decimal("0.00"), baseline - recurring_daily_share)
        daily_forecast[forecast_day] = (baseline + recurring_by_day[forecast_day]).quantize(Decimal("0.01"))

    actual = sum((daily[day] for day in daily if period_start <= day <= min(today, period_end)), Decimal("0.00"))
    forecast = (actual + sum(daily_forecast.values(), Decimal("0.00"))).quantize(Decimal("0.01"))
    daily_rate = (sum(daily_forecast.values(), Decimal("0.00")) / Decimal(len(daily_forecast))).quantize(Decimal("0.01")) if daily_forecast else Decimal("0.00")
    confidence = "низкая" if history_days < 21 else "средняя" if history_days < 75 else "высокая"
    return {
        "daily_rate": daily_rate,
        "forecast": forecast,
        "confidence": confidence,
        "days": history_days,
        "daily_forecast": daily_forecast,
        "recurring_by_day": recurring_by_day,
        "method": "категории, дни недели и регулярные платежи",
    }


def active_recurring_for_lists(db: Session, user: User, expense_lists: list[ExpenseList]) -> list[RecurringExpense]:
    list_ids = [expense_list.id for expense_list in expense_lists]
    if not list_ids:
        return []
    return db.scalars(
        select(RecurringExpense).where(
            RecurringExpense.owner_id == user.id,
            RecurringExpense.is_active.is_(True),
            RecurringExpense.expense_list_id.in_(list_ids),
        )
    ).all()


def accessible_expense_lists(db: Session, user: User) -> list[ExpenseList]:
    owned = db.scalars(
        select(ExpenseList)
        .options(
            selectinload(ExpenseList.owner),
            selectinload(ExpenseList.categories).selectinload(ExpenseCategory.items),
            selectinload(ExpenseList.shares),
        )
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
    return list(owned) + list(shared)


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
    user: User | None = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if user:
        db.execute(sql_delete(PushSubscription).where(PushSubscription.user_id == user.id))
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
    if clean_theme not in {"light", "dark"}:
        raise HTTPException(status_code=400, detail="Некорректная тема")
    user.theme = clean_theme
    db.add(user)
    db.commit()
    return JSONResponse({"theme": clean_theme})


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
    by_day_income: dict[date, Decimal] = defaultdict(lambda: Decimal("0.00"))
    for item in incomes:
        item_day = msk_date(item.received_at)
        if date_from <= item_day <= min(date_to, today):
            by_day_income[item_day] += item.amount
    income_total = sum(by_day_income.values(), Decimal("0.00"))
    lists = accessible_expense_lists(db, user)
    expense_total = Decimal("0.00")
    by_day_expense: dict[date, Decimal] = defaultdict(lambda: Decimal("0.00"))
    for expense_list in lists:
        for category in expense_list.categories:
            for item in category.items:
                if item.include_in_analytics and date_from <= msk_date(item.created_at) <= date_to:
                    expense_total += item.amount
                    by_day_expense[msk_date(item.created_at)] += item.amount
    balance = income_total - expense_total
    savings_rate = (balance / income_total * Decimal("100")).quantize(Decimal("0.1")) if income_total else None
    forecast_info = expense_forecast_from_lists(
        lists,
        period_start,
        period_end,
        active_recurring_for_lists(db, user, lists),
    )
    period_income = sum((item.amount for item in incomes if period_start <= msk_date(item.received_at) <= period_end), Decimal("0.00"))
    forecast_balance = period_income - forecast_info["forecast"]
    chart_days: list[dict[str, Any]] = []
    chart_max = Decimal("0.00")
    cumulative_balance = Decimal("0.00")
    current_day = date_from
    while current_day <= date_to:
        income = by_day_income[current_day]
        expense = by_day_expense[current_day]
        forecast_expense = forecast_info["daily_forecast"].get(current_day, Decimal("0.00"))
        cumulative_balance += income - expense - forecast_expense
        chart_max = max(chart_max, income, expense, forecast_expense)
        chart_days.append({"label": current_day.strftime("%d.%m"), "income": income, "expense": expense, "forecast_expense": forecast_expense, "net": income - expense - forecast_expense, "balance": cumulative_balance, "is_forecast": forecast_expense > 0, "has_recurring": forecast_info["recurring_by_day"].get(current_day, Decimal("0.00")) > 0})
        current_day += timedelta(days=1)
    for day in chart_days:
        day["income_height"] = float(day["income"] / chart_max * 100) if chart_max else 0
        day["expense_height"] = float(day["expense"] / chart_max * 100) if chart_max else 0
        day["forecast_height"] = float(day["forecast_expense"] / chart_max * 100) if chart_max else 0
    return render(request, "finance.html", {"user": user, "from_date": date_from.isoformat(), "to_date": date_to.isoformat(), "income_total": income_total, "expense_total": expense_total, "balance": balance, "savings_rate": savings_rate, "forecast_info": forecast_info, "forecast_balance": forecast_balance, "period_label": format_period_range(period_start, period_end), "cashflow_chart": chart_days, "chart_has_forecast": any(day["is_forecast"] for day in chart_days)})


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
    by_category: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    by_day_category: dict[date, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: Decimal("0.00")))
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
                by_category[category.name] += item.amount
                by_day_category[item_date][category.name] += item.amount
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

    category_names = [name for name, _ in sorted(by_category.items(), key=lambda pair: pair[1], reverse=True)]
    category_colors = category_color_map(category_names)

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
        for name in category_names:
            value = by_day_category[current].get(name, Decimal("0.00"))
            if value <= 0:
                continue
            segments.append({
                "name": name,
                "total": value,
                "height": float((value / max_day_total) * 100) if max_day_total else 0,
                "color": category_colors[name],
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
            "name": name,
            "total": value,
            "percent": int((value / max_category_total) * 100) if max_category_total else 0,
            "color": category_colors[name],
        }
        for name, value in sorted(by_category.items(), key=lambda pair: pair[1], reverse=True)
    ]
    top_items.sort(key=lambda item: item["amount"], reverse=True)

    current_month_start, current_month_end = current_period_start, current_period_end
    previous_month_start, previous_month_end = previous_expense_period_bounds(current_month_start, period_start_day)
    current_month_total = Decimal("0.00")
    previous_month_total = Decimal("0.00")
    current_month_by_category: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    period_lists = accessible_lists if selected_list_id else list(owned) + list(shared)
    for expense_list in period_lists:
        for category in expense_list.categories:
            for item in category.items:
                if not item.include_in_analytics:
                    continue
                item_date = msk_date(item.created_at)
                if current_month_start <= item_date <= current_month_end:
                    current_month_total += item.amount
                    current_month_by_category[category.name.strip().casefold()] += item.amount
                elif previous_month_start <= item_date <= previous_month_end:
                    previous_month_total += item.amount
    month_diff = current_month_total - previous_month_total
    forecast_info = expense_forecast_from_lists(
        period_lists,
        current_month_start,
        current_month_end,
        active_recurring_for_lists(db, user, period_lists),
    )
    forecast = forecast_info["forecast"]
    limits = db.scalars(select(ExpenseLimit).where(ExpenseLimit.owner_id == user.id).order_by(ExpenseLimit.category_name)).all()
    limit_rows = []
    limit_total = sum((limit.monthly_limit for limit in limits), Decimal("0.00"))
    limit_spent = Decimal("0.00")
    for limit in limits:
        spent = current_month_by_category.get(limit.category_name.strip().casefold(), Decimal("0.00"))
        limit_spent += spent
        percent = int((spent / limit.monthly_limit) * 100) if limit.monthly_limit else 0
        limit_rows.append({"category": limit.category_name, "limit": limit.monthly_limit, "spent": spent, "left": limit.monthly_limit - spent, "percent": min(percent, 160)})
    limit_left = limit_total - limit_spent
    limit_percent = int((limit_spent / limit_total) * 100) if limit_total else 0

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
    return {"public_key": keys.get("public_key", "")}


@app.post("/api/push/subscribe")
async def push_subscribe(
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
    keys = data.get("keys") or {}
    p256dh = str(keys.get("p256dh", "")).strip()
    auth = str(keys.get("auth", "")).strip()
    if (
        not endpoint
        or len(endpoint) > 600
        or not push_endpoint_is_allowed(endpoint)
        or not p256dh
        or len(p256dh) > 300
        or not auth
        or len(auth) > 120
    ):
        raise HTTPException(status_code=400, detail="Некорректная push-подписка")

    subscription = db.scalar(select(PushSubscription).where(PushSubscription.endpoint == endpoint))
    if subscription:
        subscription.user_id = user.id
        subscription.p256dh = p256dh
        subscription.auth = auth
        subscription.user_agent = (request.headers.get("user-agent") or "")[:500] or None
        subscription.last_used_at = utc_now_naive()
    else:
        db.add(PushSubscription(
            user_id=user.id,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            user_agent=(request.headers.get("user-agent") or "")[:500] or None,
            last_used_at=utc_now_naive(),
        ))
    db.commit()
    return {"ok": True}


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
            db.delete(sub)
            db.commit()
    return {"ok": True}


@app.get("/api/push/status")
def push_status(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    keys = ensure_vapid_keys()
    subscriptions = db.scalars(select(PushSubscription).where(PushSubscription.user_id == user.id)).all()
    return {
        "ok": True,
        "pywebpush_loaded": webpush is not None,
        "public_key_exists": bool(keys.get("public_key")),
        "private_key_file_exists": bool(keys.get("private_key_path") and Path(keys["private_key_path"]).exists()),
        "subscriptions": len(subscriptions),
        "endpoints": [sub.endpoint[:80] + "…" for sub in subscriptions],
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
    month: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        month_start = date.fromisoformat(f"{month.strip()}-01") if month.strip() else msk_today().replace(day=1)
    except ValueError:
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
        .order_by(desc(Moment.happened_on), desc(Moment.created_at))
    ).all()
    day_groups: list[dict[str, Any]] = []
    for moment in moments:
        if not day_groups or day_groups[-1]["day"] != moment.happened_on:
            day_groups.append({"day": moment.happened_on, "label": moment.happened_on.strftime("%d.%m.%Y"), "items": []})
        day_groups[-1]["items"].append(moment)
    moments_by_day = {group["day"]: group["items"] for group in day_groups}
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
            "month": month_start.strftime("%Y-%m"),
            "today": msk_today(),
            "day_groups": day_groups,
            "moment_count": len(moments),
            "month_label": f"{RUSSIAN_MONTH_NAMES[month_start.month - 1].capitalize()} {month_start.year}",
            "calendar_days": calendar_days,
            "calendar_weekdays": ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"),
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
            delete_media_file(new_photo_path)
        raise
    if previous_photo_path and previous_photo_path != moment.photo_path:
        delete_media_file(previous_photo_path)
    return redirect_notice(f"/moments?month={moment.happened_on.strftime('%Y-%m')}", "Момент обновлён")


@app.post("/moments/{moment_id}/delete")
def moment_delete(moment_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    moment = db.get(Moment, moment_id)
    if not moment or moment.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Момент не найден")
    month = moment.happened_on.strftime("%Y-%m")
    delete_media_file(moment.photo_path)
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


@app.get("/planner")
def planner_page(
    request: Request,
    month: str = "",
    day: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
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
    items = db.scalars(
        select(PlannerItem)
        .where(
            PlannerItem.owner_id == user.id,
            PlannerItem.scheduled_for >= month_start,
            PlannerItem.scheduled_for < next_month_start,
        )
        .order_by(PlannerItem.scheduled_for, PlannerItem.start_time.is_(None), PlannerItem.start_time, PlannerItem.id)
    ).all()
    items_by_day: dict[date, list[PlannerItem]] = defaultdict(list)
    for item in items:
        items_by_day[item.scheduled_for].append(item)
    month_end = next_month_start - timedelta(days=1)
    calendar_start = month_start - timedelta(days=month_start.weekday())
    calendar_end = month_end + timedelta(days=6 - month_end.weekday())
    calendar_days = []
    current_day = calendar_start
    while current_day <= calendar_end:
        calendar_days.append({
            "day": current_day,
            "number": current_day.day,
            "items": items_by_day.get(current_day, []),
            "is_current_month": current_day.month == month_start.month,
            "is_selected": current_day == selected_day,
            "is_today": current_day == msk_today(),
        })
        current_day += timedelta(days=1)
    selected_items = items_by_day.get(selected_day, [])
    upcoming = db.scalars(
        select(PlannerItem)
        .where(
            PlannerItem.owner_id == user.id,
            PlannerItem.scheduled_for >= msk_today(),
            PlannerItem.is_done.is_(False),
        )
        .order_by(PlannerItem.scheduled_for, PlannerItem.start_time.is_(None), PlannerItem.start_time, PlannerItem.id)
        .limit(6)
    ).all()
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
        "month_item_count": len(items),
    })


@app.post("/planner")
def planner_create(
    title: str = Form(...),
    scheduled_for: str = Form(""),
    start_time: str = Form(""),
    end_time: str = Form(""),
    description: str = Form(""),
    color: str = Form("#2563eb"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    clean_title = title.strip()
    if not clean_title:
        raise HTTPException(status_code=400, detail="Укажите название")
    item_day = parse_date(scheduled_for, msk_today()) or msk_today()
    item_start_time = parse_planner_time(start_time)
    item_end_time = parse_planner_time(end_time)
    if item_start_time and item_end_time and item_end_time <= item_start_time:
        raise HTTPException(status_code=400, detail="Время окончания должно быть позже начала")
    item_color = color.strip().lower()
    if not re.fullmatch(r"#[0-9a-f]{6}", item_color):
        item_color = "#2563eb"
    db.add(PlannerItem(
        owner_id=user.id,
        title=clean_title[:180],
        description=clean_optional_text(description, 10_000),
        scheduled_for=item_day,
        start_time=item_start_time,
        end_time=item_end_time,
        color=item_color,
    ))
    db.commit()
    return redirect_notice(planner_return_url(item_day), "Событие добавлено")


@app.post("/planner/{item_id}/update")
def planner_update(
    item_id: int,
    title: str = Form(...),
    scheduled_for: str = Form(""),
    start_time: str = Form(""),
    end_time: str = Form(""),
    description: str = Form(""),
    color: str = Form("#2563eb"),
    is_done: str | None = Form(None),
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
    item_start_time = parse_planner_time(start_time)
    item_end_time = parse_planner_time(end_time)
    if item_start_time and item_end_time and item_end_time <= item_start_time:
        raise HTTPException(status_code=400, detail="Время окончания должно быть позже начала")
    item.title = clean_title[:180]
    item.description = clean_optional_text(description, 10_000)
    item.scheduled_for = item_day
    item.start_time = item_start_time
    item.end_time = item_end_time
    item.color = color.strip().lower() if re.fullmatch(r"#[0-9a-fA-F]{6}", color.strip()) else "#2563eb"
    item.is_done = is_done == "1"
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


@app.get("/export/data.json")
def export_data_json(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    recipes = db.scalars(select(Recipe).where(Recipe.owner_id == user.id)).all()
    incomes = db.scalars(select(IncomeItem).where(IncomeItem.owner_id == user.id)).all()
    moments = db.scalars(select(Moment).where(Moment.owner_id == user.id).order_by(Moment.happened_on, Moment.id)).all()
    planner_items = db.scalars(select(PlannerItem).where(PlannerItem.owner_id == user.id).order_by(PlannerItem.scheduled_for, PlannerItem.start_time, PlannerItem.id)).all()
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
                "start_time": item.start_time,
                "end_time": item.end_time,
                "color": item.color,
                "is_done": item.is_done,
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
                start_time = parse_planner_time(str(raw_item.get("start_time") or ""))
                end_time = parse_planner_time(str(raw_item.get("end_time") or ""))
                if start_time and end_time and end_time <= start_time:
                    raise ValueError
                raw_color = str(raw_item.get("color") or "#2563eb").lower()
                db.add(PlannerItem(
                    owner_id=user.id,
                    title=clean_required_text(str(raw_item.get("title") or ""), "Название", 180),
                    description=clean_optional_text(raw_item.get("description"), 10_000),
                    scheduled_for=item_day,
                    start_time=start_time,
                    end_time=end_time,
                    color=raw_color if re.fullmatch(r"#[0-9a-f]{6}", raw_color) else "#2563eb",
                    is_done=parse_import_bool(raw_item.get("is_done")),
                ))
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
app.include_router(ai_router)
