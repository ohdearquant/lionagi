from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles

from .config import CORS_ORIGINS, HOST
from .routers import (
    admin,
    agents,
    artifacts,
    definitions,
    engine_runs,
    invocations,
    playbooks,
    plugins,
    projects,
    runs,
    schedules,
    sessions,
    shows,
    signals,
    skills,
    teams,
)
from .services import stats as stats_svc

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

    Hardcoding the list is brittle: FastAPI auto-generates ``HEAD`` for every
    ``GET`` route and serves docs/OpenAPI endpoints, so a manual list silently
    omits methods that are really served (CORS preflight for them then 400s).
    Walking ``application.routes`` after all routers are mounted keeps the
    allowlist exactly in sync with what is served.  ``OPTIONS`` is always
    included so CORSMiddleware can answer preflight requests.
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


@asynccontextmanager
async def lifespan(app_instance):
    from .scheduler.engine import scheduler
    from .services.db_maintenance import checkpoint_state_db
    from .services.lifecycle import run_startup_reconciliation

    _emit_startup_warnings()
    await scheduler.start()
    await run_startup_reconciliation()
    try:
        await checkpoint_state_db(actor="startup")
    except Exception:  # noqa: BLE001
        _log.warning("Startup WAL checkpoint failed (non-fatal)", exc_info=True)
    yield
    await scheduler.stop()


app = FastAPI(title="Lion Studio Server", lifespan=lifespan)


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


app.include_router(runs.router, prefix="/api")
app.include_router(engine_runs.router, prefix="/api")
app.include_router(sessions.router, prefix="/api")
app.include_router(signals.router, prefix="/api")
app.include_router(definitions.router, prefix="/api")
app.include_router(agents.router, prefix="/api")
app.include_router(playbooks.router, prefix="/api")
app.include_router(shows.router, prefix="/api")
app.include_router(skills.router, prefix="/api")
app.include_router(plugins.router, prefix="/api")
app.include_router(admin.router, prefix="/api")
app.include_router(teams.router, prefix="/api")
app.include_router(invocations.router, prefix="/api")
app.include_router(artifacts.router, prefix="/api")
app.include_router(projects.router, prefix="/api")
app.include_router(schedules.router, prefix="/api")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok"}


@app.get("/api/stats")
async def get_stats() -> dict[str, Any]:
    # F-A2-1 (ADR-0012 §10): "runs" count must come from SQLite sessions so
    # the dashboard shows the same number as the Runs list page.  Previously
    # called runs_svc.list_runs() which read filesystem dirs and returned a
    # different count than the sessions-backed list endpoint.
    return await stats_svc.get_stats()


def _resolve_frontend_dist() -> Path | None:
    """Return the dist/ directory to serve, or None if absent.

    Reads the LIONAGI_STUDIO_FRONTEND_DIST env var.  The CLI (studio.py) sets
    this before starting uvicorn; the Dockerfile sets it at image build time.
    When the var is unset (e.g. raw ``uvicorn lionagi.studio.app:app`` without
    the CLI), the app starts in API-only mode.
    """
    env_override = os.environ.get("LIONAGI_STUDIO_FRONTEND_DIST")
    if not env_override:
        return None
    p = Path(env_override)
    return p if (p / "index.html").exists() else None


def _mount_spa(application: FastAPI, dist: Path) -> None:
    """Mount static assets and register an SPA 404 fallback.

    Assets (/assets/*) are served directly by StaticFiles with long-lived
    cache headers (the filenames are content-hashed by Vite).  Every other
    GET/HEAD path that does NOT start with /api and has no registered route
    returns index.html so client-side deep-links work.

    Implementation: the fallback is installed as a custom HTTP 404 exception
    handler rather than a catch-all route.  A catch-all ``/{full_path:path}``
    route intercepts ``/api/shows`` before FastAPI's trailing-slash redirect
    runs (router registers ``/api/shows/`` but the redirect from ``/api/shows``
    is emitted by Starlette's routing layer after route lookup fails — the
    catch-all grabs it first).  An exception handler runs AFTER all routes
    have been tried and none matched, so it never competes with real routes.
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

# CORS middleware is registered LAST — after every router and the two direct
# @app.get endpoints above and the optional SPA mount — so the method allowlist
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
