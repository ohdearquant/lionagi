from __future__ import annotations

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

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# API route prefixes whose GET responses contain agent-produced content that
# must be protected when a bearer token is configured.  The /api/admin/* and
# /api/artifacts/* surfaces are the two concrete cases identified in
# LIONAGI-AUDIT-001 (studio-standards 2026-06-06).
_PROTECTED_GET_PREFIXES = ("/api/admin/", "/api/artifacts")


@asynccontextmanager
async def lifespan(app_instance):
    from .scheduler.engine import scheduler

    await scheduler.start()
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
    token = os.getenv("LIONAGI_STUDIO_AUTH_TOKEN")
    path = request.url.path
    if token and request.headers.get("authorization") != f"Bearer {token}":
        # Gate all methods on protected GET prefixes (admin, artifacts).
        if any(path.startswith(pfx) for pfx in _PROTECTED_GET_PREFIXES):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        # Gate mutating methods on all other /api/* routes.
        if path.startswith("/api") and request.method in _MUTATING_METHODS:
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
