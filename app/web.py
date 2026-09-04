from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import settings


class RequestBodyTooLarge(Exception):
    pass


class RequestSizeLimitMiddleware:
    """Enforce the request limit even when Transfer-Encoding is chunked."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        raw_content_length = headers.get(b"content-length")
        if raw_content_length:
            try:
                content_length = int(raw_content_length)
            except ValueError:
                await PlainTextResponse("Invalid Content-Length", status_code=400)(scope, receive, send)
                return
            if content_length > self.max_bytes:
                await PlainTextResponse("Request body is too large", status_code=413)(scope, receive, send)
                return

        received = 0
        response_started = False

        async def receive_limited() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise RequestBodyTooLarge
            return message

        async def send_tracked(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive_limited, send_tracked)
        except RequestBodyTooLarge:
            if response_started:
                raise
            await PlainTextResponse("Request body is too large", status_code=413)(scope, receive, send)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = settings.data_dir
MEDIA_DIR = DATA_DIR / "uploads"
RECIPE_MEDIA_DIR = MEDIA_DIR / "recipes"
CHAT_MEDIA_DIR = MEDIA_DIR / "chats"
MOMENT_MEDIA_DIR = MEDIA_DIR / "moments"
BACKUP_DIR = DATA_DIR / "backups"
PUSH_VAPID_FILE = DATA_DIR / "push_vapid.json"
PUSH_VAPID_PRIVATE_KEY_FILE = DATA_DIR / "push_vapid_private.pem"

for path in (MEDIA_DIR, RECIPE_MEDIA_DIR, CHAT_MEDIA_DIR, MOMENT_MEDIA_DIR, BACKUP_DIR):
    path.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Дом", docs_url=None if settings.is_production else "/docs", redoc_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    same_site="lax",
    https_only=settings.secure_cookies,
    max_age=settings.session_max_age,
)
app.add_middleware(RequestSizeLimitMiddleware, max_bytes=16 * 1024 * 1024)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

templates = Jinja2Templates(directory=BASE_DIR / "templates")
