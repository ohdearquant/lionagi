from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
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
        # The task handle is retained (returned, awaited only at shutdown), so a
        # task that raises never triggers asyncio's "exception was never
        # retrieved" warning — its failure is otherwise completely silent and the
        # studio runs on with no live mirroring. Surface it loudly here instead.
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
    """Deferred startup maintenance that must not gate readiness: the WAL
    checkpoint. Kept off the critical path so /health serves the instant uvicorn
    binds, and so the checkpoint does not block readiness while contending with
    the mirror's first connection open on a cold first-run DB. The whole body
    (including the import) is guarded so an unexpected failure is logged, not
    silently dropped when the task is finalized at shutdown.

    Stale-session reconciliation is deliberately NOT deferred — it runs pre-yield
    in lifespan() because stateful /api routes read the session rows it corrects.
    """
    try:
        from .services.db_maintenance import checkpoint_state_db

        await checkpoint_state_db(actor="startup")
    except Exception:  # noqa: BLE001
        _log.warning("Startup WAL checkpoint failed (non-fatal)", exc_info=True)


async def _finalize_warmup(task: asyncio.Task | None) -> None:
    """Settle the background warmup task before shutdown proceeds: cancel it if
    still running, then await so it is retrieved (never a pending/un-retrieved
    task warning). An unexpected failure is logged rather than silently dropped."""
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
    # Reconciliation corrects phantom / stale-status session and invocation rows
    # that stateful /api routes (sessions, runs, stats) read directly, so it must
    # complete before we serve — keep it pre-yield.
    await run_startup_reconciliation()
    mirror_stop, mirror_task = _start_claude_mirror()
    # The WAL checkpoint is pure maintenance and the main first-run cost; defer it
    # to a background task so readiness is not gated on it.
    warmup_task = asyncio.create_task(_startup_warmup(), name="studio-startup-warmup")
    yield
    from .services.launches import shutdown_launches

    await _finalize_warmup(warmup_task)
    await _stop_claude_mirror(mirror_stop, mirror_task)
    await shutdown_launches()
    await scheduler.stop()


app = FastAPI(title="Lion Studio Server", lifespan=lifespan)


@app.exception_handler(LionError)
async def _lion_error_handler(request: Request, exc: LionError) -> JSONResponse:
    """Translate domain errors raised by service logic into HTTP responses."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.message},
    )


@app.middleware("http")
async def require_studio_bearer_token(request: Request, call_next):
    # CORS preflight requests arrive without an Authorization header by design.
    # Let them pass through so CORSMiddleware can respond with the correct
    # Allow-* headers; blocking them here would prevent browsers from ever
    # reaching authenticated endpoints from a separate frontend origin.
    if request.method == "OPTIONS":
        return await call_next(request)
    token = os.getenv("LIONAGI_STUDIO_AUTH_TOKEN")
    path = request.url.path
    if token and request.headers.get("authorization") != f"Bearer {token}":
        # All /api/* paths (any method) and the FastAPI schema/docs endpoints
        # are protected when a token is configured.  Non-API GET/HEAD — the
        # SPA shell, hashed assets, and liveness probes — stay public: browsers
        # navigate without an Authorization header, so gating the shell would
        # make the UI unloadable in authed mode.  Every byte behind those paths
        # is the static frontend bundle; all data lives under /api.
        is_api = path == "/api" or path.startswith("/api/")
        is_guarded_non_api = path in _GUARDED_NON_API_PATHS
        is_public_static = (
            request.method in ("GET", "HEAD") and not is_api and not is_guarded_non_api
        )
        if path not in _PUBLIC_PATHS and not is_public_static:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)


# Mount routes registered via the @studio_route decorator. Area modules listed
# in _STUDIO_ROUTE_MODULES are imported here so their @studio_route decorators
# fire and populate _ROUTES before the loop below adds each route to the app.
load_studio_route_modules()
for _route in iter_studio_routes():
    app.add_api_route(
        f"/api{_route.path}",
        _route.handler,
        methods=[_route.method],
        **({"response_model": _route.response_model} if _route.response_model is not None else {}),
        dependencies=list(_route.dependencies),
        status_code=_route.status_code,
        tags=list(_route.tags),
        name=_route.name,
        summary=_route.summary,
        description=_route.description,
        **({"response_class": _route.response_class} if _route.response_class is not None else {}),
        responses=dict(_route.responses) if _route.responses is not None else None,
        include_in_schema=_route.include_in_schema,
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok"}


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
        # /api/* paths that reach here (no route matched) stay 404 JSON —
        # browsers never navigate to /api/* directly, only JavaScript does,
        # so returning HTML there would surface a confusing error.
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


# Mount the SPA if a dist/ exists.  Assets mount must happen BEFORE
# CORSMiddleware is added so _collect_cors_methods sees the Mount entry.
# The 404 exception handler is registered on the app object and takes effect
# after all route resolution — CORSMiddleware position doesn't affect it.
_dist = _resolve_frontend_dist()
if _dist is not None:
    _mount_spa(app, _dist)

# CORS middleware is registered LAST — after every router and the one direct
# @app.get endpoint above and the optional SPA mount — so the method allowlist
# is derived from the complete route table (see _collect_cors_methods).  Added
# last, it sits outermost in the middleware stack, the correct position for
# CORS: preflight is answered before the bearer-token gate (which already lets
# OPTIONS through).
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=_collect_cors_methods(app),
    allow_headers=["*"],
)
