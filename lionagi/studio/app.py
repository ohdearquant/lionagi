from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles

from lionagi._errors import LionError

from .config import CORS_ORIGINS, HOST
from .registry import iter_studio_routes, load_studio_route_modules

_log = logging.getLogger(__name__)

# Paths that remain reachable without a bearer token regardless of whether
# LIONAGI_STUDIO_AUTH_TOKEN is set.  This is intentionally a very small set:
# only pure liveness probes that carry no application state belong here.
_PUBLIC_PATHS = frozenset({"/health"})

# FastAPI built-in schema/docs routes that are NOT under /api but expose API
# shape and must be bearer-guarded in token mode, just like /api/*.
_GUARDED_NON_API_PATHS = frozenset(
    {
        "/openapi.json",
        "/docs",
        "/redoc",
        "/docs/oauth2-redirect",
    }
)


def _collect_cors_methods(application: FastAPI) -> list[str]:
    """Derive the CORS method allowlist from the app's actual route table.

    Hardcoding is brittle: FastAPI auto-generates HEAD for every GET route, so
    a manual list silently omits served methods (CORS preflight then 400s).
    Walking routes after all routers are mounted keeps the allowlist in sync.
    OPTIONS is always included so CORSMiddleware can answer preflight requests.
    """
    methods: set[str] = {"OPTIONS"}
    for route in application.routes:
        route_methods = getattr(route, "methods", None)
        if route_methods:
            methods.update(route_methods)
    return sorted(methods)


def _emit_startup_warnings() -> None:
    """Emit security warnings once at startup — no-op if conditions are safe."""
    token = os.getenv("LIONAGI_STUDIO_AUTH_TOKEN")
    if not token:
        bind_host = os.getenv("LIONAGI_STUDIO_HOST", HOST)
        if bind_host == "0.0.0.0":  # noqa: S104
            _log.warning(
                "Studio running WITHOUT authentication on host 0.0.0.0 — "
                "ALL API requests are accepted from any network interface. "
                "This is unsafe in containers or cloud deployments. "
                "Set LIONAGI_STUDIO_AUTH_TOKEN to require a bearer token."
            )
        else:
            _log.warning(
                "Studio running WITHOUT authentication — all API requests are "
                "accepted. Set LIONAGI_STUDIO_AUTH_TOKEN to require a bearer token."
            )

    if "*" in CORS_ORIGINS:
        _log.warning(
            "CORS is configured with a wildcard origin ('*'). "
            "Set CORS_ORIGINS to a comma-separated list of allowed origins "
            "to restrict cross-origin access."
        )


def _start_claude_mirror() -> tuple[asyncio.Event, asyncio.Task] | tuple[None, None]:
    """Start the in-process Claude Code mirror tail if enabled; return (stop, task)."""
    from .config import MIRROR_CLAUDE_ENABLED, MIRROR_CLAUDE_INTERVAL, MIRROR_CLAUDE_SINCE

    if not MIRROR_CLAUDE_ENABLED:
        return None, None
    from lionagi.cli.mirror import mirror_forever

    stop = asyncio.Event()
    task = asyncio.create_task(
        mirror_forever(stop, since=MIRROR_CLAUDE_SINCE, interval=MIRROR_CLAUDE_INTERVAL),
        name="claude-mirror-tail",
    )

    def _log_unexpected_exit(t: asyncio.Task) -> None:
        # The retained task handle suppresses asyncio's "exception was never
        # retrieved" warning, so a raised failure is otherwise silent; surface
        # it loudly here instead.
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            _log.error("Claude mirror tail exited unexpectedly", exc_info=exc)

    task.add_done_callback(_log_unexpected_exit)
    _log.info("Claude Code mirror tail started (since=%s)", MIRROR_CLAUDE_SINCE)
    return stop, task


async def _stop_claude_mirror(stop: asyncio.Event | None, task: asyncio.Task | None) -> None:
    """Signal the mirror tail to stop and await it, cancelling as a backstop."""
    if stop is None or task is None:
        return
    stop.set()
    try:
        await asyncio.wait_for(task, timeout=10)
    except (asyncio.TimeoutError, TimeoutError):
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
    except Exception:  # noqa: BLE001
        # a failed tail must not block shutdown
        _log.warning("Claude mirror tail ended with error", exc_info=True)


async def _startup_warmup() -> None:
    """Deferred WAL checkpoint, kept off the critical path so /health serves as
    soon as reconciliation completes rather than waiting on it. Guarded so an
    unexpected failure is logged, not silently dropped, at shutdown."""
    try:
        from .services.db_maintenance import checkpoint_state_db

        await checkpoint_state_db(actor="startup")
    except Exception:  # noqa: BLE001
        _log.warning("Startup WAL checkpoint failed (non-fatal)", exc_info=True)


async def _finalize_warmup(task: asyncio.Task | None) -> None:
    """Cancel the warmup task if still running, then await it so it's retrieved
    (avoids an un-retrieved-task warning); a failure is logged, not dropped."""
    if task is None:
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        _log.warning("Startup warmup task failed (non-fatal)", exc_info=True)


@asynccontextmanager
async def lifespan(app_instance):
    from .scheduler.engine import scheduler
    from .services.lifecycle import run_startup_reconciliation

    _emit_startup_warnings()
    await scheduler.start()
    # Corrects phantom/stale-status rows that stateful /api routes read
    # directly, so it must complete before we serve.
    await run_startup_reconciliation()
    mirror_stop, mirror_task = _start_claude_mirror()
    # WAL checkpoint is pure maintenance; defer so readiness isn't gated on it.
    warmup_task = asyncio.create_task(_startup_warmup(), name="studio-startup-warmup")
    yield
    from .services.launches import shutdown_launches

    await _finalize_warmup(warmup_task)
    await _stop_claude_mirror(mirror_stop, mirror_task)
    await shutdown_launches()
    await scheduler.stop()


# Strict Host-header authority grammar: `host` or `host:port` (1-5 digit
# port), or a bracketed IPv6 literal `[addr]` with an optional `:port` after
# the closing bracket -- nothing else. Deliberately stricter than
# `request.url.hostname`, which normalizes/mis-parses authorities like
# "127.0.0.1:8765.evil.com" or "[::1]evil.com" into an accepted hostname.
_HOST_AUTHORITY_RE = re.compile(r"^(?P<host>[A-Za-z0-9.\-]+)(?::(?P<port>\d{1,5}))?$")
_IPV6_AUTHORITY_RE = re.compile(r"^\[(?P<host>[0-9A-Fa-f:]+)\](?::(?P<port>\d{1,5}))?$")


def _parse_host_authority(raw_host: str) -> str | None:
    """Strictly parse a raw Host header value, returning the normalized host
    (lowercased, brackets stripped for IPv6) or None if it doesn't match the
    exact authority grammar above."""
    raw_host = (raw_host or "").strip()
    if not raw_host:
        return None
    match = (
        _IPV6_AUTHORITY_RE.match(raw_host)
        if raw_host.startswith("[")
        else _HOST_AUTHORITY_RE.match(raw_host)
    )
    if not match:
        return None
    host = match.group("host")
    return host.lower() if host else None


async def validate_host_header(request: Request, call_next):
    """Reject requests whose Host header doesn't match an expected value —
    defends against DNS rebinding, where a malicious page points a browser at
    http://127.0.0.1:<port> with an attacker-controlled Host and, once past
    CORS/auth, reaches the daemon as if same-origin. Registered outermost
    (outside CORSMiddleware) so every request, including preflight, is checked
    before anything answers."""
    hostname = _parse_host_authority(request.headers.get("host", ""))
    bind_host = os.getenv("LIONAGI_STUDIO_HOST", HOST)
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    if bind_host not in ("127.0.0.1", "localhost", "::1", "0.0.0.0", ""):  # noqa: S104
        allowed_hosts.add(bind_host.lower())
    if hostname is None or hostname not in allowed_hosts:
        return JSONResponse(
            {"detail": f"Invalid Host header: {request.headers.get('host', '')!r}"},
            status_code=400,
        )
    return await call_next(request)


def _mount_studio_routes(application: FastAPI) -> None:
    """Add every route registered via the @studio_route decorator to `application`.

    Area modules listed in _STUDIO_ROUTE_MODULES are imported here so their
    @studio_route decorators fire and populate _ROUTES; import_module caches
    modules, so calling this once per app instance is cheap and idempotent.
    """
    load_studio_route_modules()
    for _route in iter_studio_routes():
        application.add_api_route(
            f"/api{_route.path}",
            _route.handler,
            methods=[_route.method],
            **(
                {"response_model": _route.response_model}
                if _route.response_model is not None
                else {}
            ),
            dependencies=list(_route.dependencies),
            status_code=_route.status_code,
            tags=list(_route.tags),
            name=_route.name,
            summary=_route.summary,
            description=_route.description,
            **(
                {"response_class": _route.response_class}
                if _route.response_class is not None
                else {}
            ),
            responses=dict(_route.responses) if _route.responses is not None else None,
            include_in_schema=_route.include_in_schema,
        )


def _resolve_frontend_dist() -> Path | None:
    """Return the dist/ directory to serve, or None if absent.

    Reads LIONAGI_STUDIO_FRONTEND_DIST; when unset (e.g. raw uvicorn without
    the CLI), the app starts in API-only mode.
    """
    env_override = os.environ.get("LIONAGI_STUDIO_FRONTEND_DIST")
    if not env_override:
        return None
    p = Path(env_override)
    return p if (p / "index.html").exists() else None


def _mount_spa(application: FastAPI, dist: Path) -> None:
    """Mount static assets and register an SPA 404 fallback.

    Uses a 404 exception handler (not a catch-all route) for the SPA fallback:
    a catch-all /{full_path:path} route intercepts /api/shows before FastAPI's
    trailing-slash redirect fires, whereas an exception handler runs only after
    all routes have been tried and none matched.
    """
    assets_dir = dist / "assets"
    if assets_dir.is_dir():
        application.mount("/assets", StaticFiles(directory=str(assets_dir)), name="spa-assets")

    index_path = dist / "index.html"

    @application.exception_handler(404)
    async def _spa_fallback(request: Request, exc: Exception) -> FileResponse | JSONResponse:
        # /api/* paths that reach here stay 404 JSON, not the SPA HTML shell.
        path = request.url.path
        if path.startswith("/api/") or path == "/api":
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if request.method not in ("GET", "HEAD"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return FileResponse(
            str(index_path),
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
            },
        )


def create_app() -> FastAPI:
    """Build and return a fresh Studio FastAPI app instance, so callers that
    need a clean app (notably tests that monkeypatch service globals first) can
    get one without `importlib.reload`-ing this module's shared singleton."""
    application = FastAPI(title="Lion Studio Server", lifespan=lifespan)

    @application.exception_handler(LionError)
    async def _lion_error_handler(request: Request, exc: LionError) -> JSONResponse:
        """Translate domain errors raised by service logic into HTTP responses."""
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.message},
        )

    @application.middleware("http")
    async def require_studio_bearer_token(request: Request, call_next):
        # CORS preflight arrives without an Authorization header by design;
        # let it through so CORSMiddleware can answer with Allow-* headers.
        if request.method == "OPTIONS":
            return await call_next(request)
        token = os.getenv("LIONAGI_STUDIO_AUTH_TOKEN")
        path = request.url.path
        if token and request.headers.get("authorization") != f"Bearer {token}":
            # All /api/* paths and the FastAPI schema/docs endpoints are
            # gated. Non-API GET/HEAD (SPA shell, hashed assets, liveness)
            # stay public -- browsers navigate without an Authorization
            # header, so gating the shell would make the UI unloadable.
            is_api = path == "/api" or path.startswith("/api/")
            is_guarded_non_api = path in _GUARDED_NON_API_PATHS
            is_public_static = (
                request.method in ("GET", "HEAD") and not is_api and not is_guarded_non_api
            )
            if path not in _PUBLIC_PATHS and not is_public_static:
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)

    @application.middleware("http")
    async def require_json_content_type(request: Request, call_next):
        """Reject state-changing /api requests that don't declare a JSON body.

        FastAPI parses request bodies as JSON regardless of the declared
        Content-Type, so a cross-site "simple request" (text/plain, no CORS
        preflight) carrying a JSON-shaped body would otherwise reach route
        handlers unchecked -- the classic form-based JSON CSRF vector. The SPA
        always sends `application/json` on requests that carry a body (see
        apps/studio/frontend/src/lib/api.ts `fetchJson`) and sends no body at all
        for the handful of routes that need none, so this only rejects traffic
        the frontend itself never produces.
        """
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return await call_next(request)
        path = request.url.path
        if not (path == "/api" or path.startswith("/api/")):
            return await call_next(request)
        content_length = request.headers.get("content-length")
        has_body = content_length not in (None, "0") or "transfer-encoding" in request.headers
        if has_body:
            content_type = request.headers.get("content-type", "")
            media_type = content_type.split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                return JSONResponse(
                    {"detail": "Content-Type must be application/json"},
                    status_code=415,
                )
        return await call_next(request)

    _mount_studio_routes(application)

    @application.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok"}

    # Mount the SPA before CORSMiddleware so _collect_cors_methods sees the
    # Mount entry (the 404 handler itself is order-independent).
    dist = _resolve_frontend_dist()
    if dist is not None:
        _mount_spa(application, dist)

    # Starlette wraps middleware LIFO (most-recently-added sees the request
    # first). CORSMiddleware is added after every router/mount so its method
    # allowlist reflects the full route table. Host validation is added
    # after CORS, making it OUTERMOST: every request -- including preflight
    # OPTIONS -- has its Host checked before CORS can answer, closing the
    # window where an invalid-Host request could get a valid preflight
    # response. Request order: Host validation -> CORS -> Content-Type/CSRF
    # check -> bearer-token gate -> route. A real preflight never reaches the
    # bearer-token/Content-Type middlewares because CORS answers it first.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_methods=_collect_cors_methods(application),
        allow_headers=["*"],
    )
    application.add_middleware(BaseHTTPMiddleware, dispatch=validate_host_header)

    return application


app = create_app()
