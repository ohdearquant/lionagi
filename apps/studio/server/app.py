from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from .config import CORS_ORIGINS
from .routers import agents, definitions, playbooks, runs, sessions, shows, skills
from .services import agents as agents_svc
from .services import playbooks as playbooks_svc
from .services import runs as runs_svc
from .services import shows as shows_svc
from .services import skills as skills_svc

app = FastAPI(title="Lion Studio Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(runs.router, prefix="/api")
app.include_router(sessions.router, prefix="/api")
app.include_router(definitions.router, prefix="/api")
app.include_router(agents.router, prefix="/api")
app.include_router(playbooks.router, prefix="/api")
app.include_router(shows.router, prefix="/api")
app.include_router(skills.router, prefix="/api")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok"}


@app.get("/api/stats")
async def get_stats() -> dict[str, Any]:
    return {
        "playbooks": len(playbooks_svc.list_playbooks()),
        "agents": len(agents_svc.list_agents()),
        "runs": len(runs_svc.list_runs()),
        "shows": len(shows_svc.list_shows()),
        "skills": len(skills_svc.list_skills()),
    }
