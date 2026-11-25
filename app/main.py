import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from starlette.datastructures import MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import SECURITY_HEADERS, register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestIDMiddleware
from app.db import session as db_session
from app.db.session import create_engine, create_session_factory

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = create_engine(app.state.settings, read_only=True)  # the API never writes
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    # Always check the API's login, including the fallback to DATABASE_URL (the owner):
    # production refuses a login that could read article text, other environments warn.
    settings: Settings = app.state.settings
    if settings.is_production and settings.api_database_url is None:
        msg = "production requires API_DATABASE_URL (a news_api_login user)"
        raise db_session.PrivilegeError(msg)
    try:
        await db_session.assert_least_privilege(engine, settings)
    except db_session.PrivilegeError:
        raise
    except (OSError, SQLAlchemyError) as exc:
        if settings.is_production:
            raise
        # The database may be down at startup; /health/ready reports it.
        logger.warning(
            "Least-privilege check skipped: database unreachable (%s)", type(exc).__name__
        )
    try:
        yield
    finally:
        await engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        description="Read-only API of a personal news-intelligence platform: article "
        "metadata and AI briefings, always linking out to the original article.",
        version=settings.app_version,
        debug=settings.debug,
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    app.state.settings = settings

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET"],
            allow_headers=["*"],
        )
    app.add_middleware(HeadAsGetMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)
    app.include_router(api_router)
    app.add_middleware(UICSPMiddleware)
    app.mount("/ui", StaticFiles(directory=UI_DIR, html=True), name="ui")
    return app


UI_DIR = Path(__file__).parent / "web" / "static"
UI_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)


class UICSPMiddleware:
    """Adds a strict Content Security Policy to every /ui response (pure ASGI)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/ui" or path.startswith("/ui/")):
            await self.app(scope, receive, send)
            return

        async def send_with_csp(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Content-Security-Policy"] = UI_CSP
                # Revalidate on every load (ETag makes it cheap): an updated UI is never
                # hidden behind stale cached JS modules.
                headers["Cache-Control"] = "no-cache"
            await send(message)

        await self.app(scope, receive, send_with_csp)


class SecurityHeadersMiddleware:
    """`nosniff` and `Referrer-Policy: no-referrer` on every HTTP response (pure ASGI)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


class HeadAsGetMiddleware:
    """HEAD on API and health routes: answered as GET with the body dropped (RFC 9110).

    FastAPI routes declare GET only (and the OpenAPI document lists GET only); the
    static /ui mount already answers HEAD itself.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or scope["method"] != "HEAD" or path.startswith("/ui"):
            await self.app(scope, receive, send)
            return

        async def send_without_body(message: Message) -> None:
            if message["type"] == "http.response.body":
                message = {"type": "http.response.body", "body": b"", "more_body": False}
                if sent_end:
                    return
                sent_end.append(True)
            await send(message)

        sent_end: list[bool] = []
        await self.app({**scope, "method": "GET"}, receive, send_without_body)


app = create_app()
