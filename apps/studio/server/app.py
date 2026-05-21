from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Request
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from .config import CORS_ORIGINS
from .routers import (
    admin,
    agents,
    definitions,
    playbooks,
    plugins,
    runs,
    sessions,
    shows,
    skills,
    teams,
)
from .services import stats as stats_svc

app = FastAPI(title="Lion Studio Server")

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
    # Gate ALL methods on /api/admin/* — GET endpoints must not bypass auth.
    # Other /api/* paths remain open for read-only access (stats, health, etc.).
    if (
        token
        and path.startswith("/api/admin/")
        and request.headers.get("authorization") != f"Bearer {token}"
    ):
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
