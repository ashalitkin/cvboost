"""create_app: wiring only. Routes in routes.py, security in security.py."""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import jinja2
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.templating import Jinja2Templates

from cvboost import connectors, plugins, stub_alert
from cvboost.config import Settings
from cvboost.core.rules import load_config, rule_version
from cvboost.crm.parse import safe_linkedin_url
from cvboost.cv.render import Renderer, WeasyRenderer
from cvboost.llm.config import LLMConfig, load_llm_config
from cvboost.llm.prompts import registry
from cvboost.storage.db import Store, connect, migrate
from cvboost.web import crm_routes, routes
from cvboost.web.security import (
    HEADERS,
    BasicAuthMiddleware,
    BodyLimitMiddleware,
    SecurityHeadersMiddleware,
    csrf_guard,
    safe_url,
    sanitize_html,
)
from cvboost.worker_config import WorkerConfig, load_worker_config

HERE = Path(__file__).parent


def _dt_filter(tz: ZoneInfo):
    """The `dt` template filter: UTC datetimes shown in the configured zone (worker.yaml)."""

    def _dt(value) -> str:
        return _fmt_dt(value, tz)

    return _dt


def _fmt_dt(value, tz: ZoneInfo) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def create_app(
    settings: Settings,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    renderer: Renderer | None = None,
) -> FastAPI:
    if (settings.web_user is None) != (settings.web_password is None):
        raise ValueError("web_user and web_password must be set together")
    registry.ensure_loaded(settings)  # no profile: the panel works, LLM steps are off
    rules_cfg = load_config(settings.rules_path)
    try:
        llm_cfg: LLMConfig | None = load_llm_config(settings.llm_config_path)
    except ValueError:
        llm_cfg = None
    try:
        worker_cfg = load_worker_config(settings.worker_config_path)
    except ValueError:
        worker_cfg = WorkerConfig()

    conn = connect(settings.db_path)
    try:
        migrate(conn)
        Store(conn).sync_sources(connectors.REGISTRY, clock())
    finally:
        conn.close()

    app = FastAPI(
        title="cvboost",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(csrf_guard)],
    )
    app.state.settings = settings
    app.state.clock = clock
    app.state.renderer = renderer or WeasyRenderer(settings.resume_dir)
    app.state.rules_cfg = rules_cfg
    app.state.rule_version = rule_version(rules_cfg)
    app.state.llm_cfg = llm_cfg
    app.state.worker_cfg = worker_cfg
    # without CVBOOST_WEB_SECRET tokens live until restart (an open tab then needs a reload)
    app.state.csrf_secret = (
        settings.web_secret.encode() if settings.web_secret else secrets.token_bytes(32)
    )

    loaded = plugins.load_plugins()
    app.state.nav = plugins.nav_items(routes.NAV)
    env = jinja2.Environment(
        loader=jinja2.ChoiceLoader(
            [
                jinja2.FileSystemLoader(HERE / "templates"),
                *(jinja2.FileSystemLoader(p.templates_dir) for p in loaded if p.templates_dir),
            ]
        ),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
    )
    env.filters["sanitize_html"] = sanitize_html
    env.filters["safe_url"] = safe_url
    env.filters["linkedin_url"] = safe_linkedin_url
    env.filters["check_platform"] = lambda sources: (
        stub_alert.platform_of(list(sources)) or "the platform"
    )
    env.filters["dt"] = _dt_filter(ZoneInfo(worker_cfg.timezone))
    app.state.templates = Jinja2Templates(env=env)

    for p in loaded:  # more specific mounts first: /static/<plugin>/ before /static
        if p.static_dir:
            app.mount(
                f"/static/{p.name}", StaticFiles(directory=p.static_dir), name=f"static-{p.name}"
            )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    routes.register(app)
    crm_routes.register(app)
    for p in loaded:
        if p.register_web:
            p.register_web(app)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if request.url.path == "/healthz":
            return PlainTextResponse("error", status_code=exc.status_code)
        return routes.error_page(request, exc.status_code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _bad_request(request: Request, exc: RequestValidationError):
        return routes.error_page(request, 400, "Bad request.")

    @app.exception_handler(sqlite3.OperationalError)
    async def _db_busy(request: Request, exc: sqlite3.OperationalError):
        if "locked" in str(exc) or "busy" in str(exc):
            return routes.error_page(request, 503, "The database is busy, try again.")
        return routes.error_page(request, 500, "Internal error.")

    @app.exception_handler(Exception)
    async def _crash(request: Request, exc: Exception):
        # runs in ServerErrorMiddleware, outside our header middleware: add the headers here
        resp = routes.error_page(request, 500, "Internal error.")
        for k, v in HEADERS.items():
            resp.headers.setdefault(k, v)
        resp.headers.setdefault("cache-control", "no-store")
        return resp

    # last added = outermost: headers wrap everything, then host check, then auth
    if settings.web_user is not None and settings.web_password is not None:
        app.add_middleware(
            BasicAuthMiddleware, user=settings.web_user, password=settings.web_password
        )
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.web_hosts))
    app.add_middleware(SecurityHeadersMiddleware)
    return app
