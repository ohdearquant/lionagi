from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from .config import CORS_ORIGINS
from .routers import (
    admin,
    agents,
    artifacts,
    definitions,
    invocations,
    playbooks,
    plugins,
    projects,
    runs,
    schedules,
    sessions,
    shows,
    skills,
    teams,
)
from .services import stats as stats_svc

_log = logging.getLogger(__name__)

# Paths that remain reachable without a bearer token regardless of whether
# LIONAGI_STUDIO_AUTH_TOKEN is set.  This is intentionally a very small set:
# only pure liveness probes that carry no application state belong here.
_PUBLIC_PATHS = frozenset({"/health"})


@asynccontextmanager
async def lifespan(app_instance):
    from .scheduler.engine import scheduler
    from .services.db_maintenance import checkpoint_state_db
    from .services.lifecycle import run_startup_reconciliation

    await scheduler.start()
    await run_startup_reconciliation()
    try:
        await checkpoint_state_db(actor="startup")
    except Exception:  # noqa: BLE001
        _log.warning("Startup WAL checkpoint failed (non-fatal)", exc_info=True)
    yield
    await scheduler.stop()


app = FastAPI(title="Lion Studio Server", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
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
        # Allow only explicit liveness probes without a token.  Every other
        # route — including all /api/* paths regardless of HTTP method — is
        # protected when a token is configured.
        if path not in _PUBLIC_PATHS:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)


app.include_router(runs.router, prefix="/api")
app.include_router(sessions.router, prefix="/api")
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
