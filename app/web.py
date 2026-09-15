import logging
import re
import tempfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import settings

logger = logging.getLogger("home_service.upload")
FILE_UPLOAD_PATH = re.compile(r"^/files/(?:new|[1-9][0-9]*/upload)$")


class RequestBodyTooLarge(Exception):
    pass


class RequestSizeLimitMiddleware:
    """Enforce normal and file-upload request limits, including chunked bodies."""

    def __init__(self, app: ASGIApp, max_bytes: int, upload_max_bytes: int | None = None) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.upload_max_bytes = upload_max_bytes or max_bytes

    def request_limit(self, scope: Scope) -> int:
        path = scope.get("path", "")
        if scope.get("method") == "POST" and FILE_UPLOAD_PATH.fullmatch(path):
            return self.upload_max_bytes
        return self.max_bytes

    async def reject(self, scope: Scope, receive: Receive, send: Send, limit: int) -> None:
        logger.warning("Request body limit exceeded path=%s limit_bytes=%s", scope.get("path", ""), limit)
        await PlainTextResponse(
            "Размер загрузки превышает допустимый лимит.", status_code=413
        )(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        limit = self.request_limit(scope)
        raw_content_length = headers.get(b"content-length")
        if raw_content_length:
            try:
                content_length = int(raw_content_length)
            except ValueError:
                await PlainTextResponse("Invalid Content-Length", status_code=400)(scope, receive, send)
                return
            if content_length > limit:
                await self.reject(scope, receive, send, limit)
                return

        received = 0
        response_started = False
        limit_exceeded = False

        async def receive_limited() -> Message:
            nonlocal limit_exceeded, received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    limit_exceeded = True
                    raise RequestBodyTooLarge
            return message

        async def send_tracked(message: Message) -> None:
            nonlocal response_started
            if limit_exceeded:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive_limited, send_tracked)
        except RequestBodyTooLarge:
            if response_started:
                raise
            limit_exceeded = True
        if limit_exceeded and not response_started:
            await self.reject(scope, receive, send, limit)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = settings.data_dir
MEDIA_DIR = DATA_DIR / "uploads"
RECIPE_MEDIA_DIR = MEDIA_DIR / "recipes"
CHAT_MEDIA_DIR = MEDIA_DIR / "chats"
MOMENT_MEDIA_DIR = MEDIA_DIR / "moments"
BACKUP_DIR = DATA_DIR / "backups"
PUSH_VAPID_FILE = DATA_DIR / "push_vapid.json"
PUSH_VAPID_PRIVATE_KEY_FILE = DATA_DIR / "push_vapid_private.pem"
SHARED_FILES_DIR = settings.file_share_dir
UPLOAD_SPOOL_DIR = settings.data_dir / ".upload_spool"

for path in (
    MEDIA_DIR,
    RECIPE_MEDIA_DIR,
    CHAT_MEDIA_DIR,
    MOMENT_MEDIA_DIR,
    BACKUP_DIR,
    SHARED_FILES_DIR,
    UPLOAD_SPOOL_DIR,
):
    path.mkdir(parents=True, exist_ok=True)
try:
    UPLOAD_SPOOL_DIR.chmod(0o700)
except OSError:
    pass
# Starlette rolls UploadFile parts from RAM into tempfile.SpooledTemporaryFile.
# Keep those files on the persistent data volume instead of the 64 MiB /tmp tmpfs.
tempfile.tempdir = str(UPLOAD_SPOOL_DIR.resolve())

app = FastAPI(title="Дом", docs_url=None if settings.is_production else "/docs", redoc_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    same_site="lax",
    https_only=settings.secure_cookies,
    max_age=settings.session_max_age,
)
app.add_middleware(
    RequestSizeLimitMiddleware,
    max_bytes=16 * 1024 * 1024,
    upload_max_bytes=settings.file_share_max_request_bytes,
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

templates = Jinja2Templates(directory=BASE_DIR / "templates")
