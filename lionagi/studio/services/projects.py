# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import time
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel

from lionagi.state.db import DEFAULT_DB_PATH

from ..registry import studio_route
from ._db import open_db as _open_db

_DB = str(DEFAULT_DB_PATH)

_ENSURE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    name         TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    path         TEXT,
    github       TEXT,
    description  TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    last_seen_at REAL
);
CREATE INDEX IF NOT EXISTS idx_projects_source ON projects(source);
CREATE INDEX IF NOT EXISTS idx_projects_updated ON projects(updated_at DESC);
"""


async def _ensure_table(db) -> None:
    await db.executescript(_ENSURE_TABLE_SQL)


async def list_projects() -> dict[str, Any]:
    """Return all known projects with session counts and an unassigned count."""
    if not DEFAULT_DB_PATH.exists():
        return {"projects": [], "unassigned_count": 0}

    async with _open_db(_DB) as db:
        await _ensure_table(db)
        cur = await db.execute(
            """SELECT p.name, p.source, p.path, p.github, p.description,
                      p.created_at, p.updated_at, p.last_seen_at,
                      COUNT(s.id) AS session_count,
                      SUM(CASE WHEN s.status = 'running' THEN 1 ELSE 0 END)
                          AS running_count
               FROM projects p
               LEFT JOIN sessions s ON s.project = p.name
               GROUP BY p.name
               ORDER BY COALESCE(p.last_seen_at, p.updated_at) DESC"""
        )
        rows = await cur.fetchall()

        unassigned_cur = await db.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE project IS NULL"
        )
        unassigned_row = await unassigned_cur.fetchone()
        unassigned_count = unassigned_row["n"] if unassigned_row else 0

    return {
        "projects": [
            {
                "name": r["name"],
                "source": r["source"],
                "path": r["path"],
                "github": r["github"],
                "description": r["description"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "last_seen_at": r["last_seen_at"],
                "session_count": r["session_count"] or 0,
                "running_count": r["running_count"] or 0,
                "editable": r["source"] in ("studio", "global_override"),
            }
            for r in rows
        ],
        "unassigned_count": unassigned_count,
    }


async def get_project(name: str) -> dict[str, Any] | None:
    """Return a single project with session counts and usage summaries."""
    if not DEFAULT_DB_PATH.exists():
        return None

    async with _open_db(_DB) as db:
        await _ensure_table(db)
        cur = await db.execute(
            """SELECT p.name, p.source, p.path, p.github, p.description,
                      p.created_at, p.updated_at, p.last_seen_at,
                      COUNT(s.id) AS session_count,
                      SUM(CASE WHEN s.status = 'running' THEN 1 ELSE 0 END)
                          AS running_count
               FROM projects p
               LEFT JOIN sessions s ON s.project = p.name
               WHERE p.name = ?
               GROUP BY p.name""",
            (name,),
        )
        row = await cur.fetchone()
        if not row:
            return None

        agents_cur = await db.execute(
            """SELECT agent_name, COUNT(*) AS run_count
               FROM sessions
               WHERE project = ? AND agent_name IS NOT NULL
               GROUP BY agent_name
               ORDER BY run_count DESC
               LIMIT 20""",
            (name,),
        )
        agents_used = [
            {"agent_name": r["agent_name"], "run_count": r["run_count"]}
            for r in await agents_cur.fetchall()
        ]

        playbooks_cur = await db.execute(
            """SELECT playbook_name, COUNT(*) AS run_count
               FROM sessions
               WHERE project = ? AND playbook_name IS NOT NULL
               GROUP BY playbook_name
               ORDER BY run_count DESC
               LIMIT 20""",
            (name,),
        )
        playbooks_used = [
            {"playbook_name": r["playbook_name"], "run_count": r["run_count"]}
            for r in await playbooks_cur.fetchall()
        ]

    return {
        "name": row["name"],
        "source": row["source"],
        "path": row["path"],
        "github": row["github"],
        "description": row["description"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_seen_at": row["last_seen_at"],
        "session_count": row["session_count"] or 0,
        "running_count": row["running_count"] or 0,
        "editable": row["source"] in ("studio", "global_override"),
        "agents_used": agents_used,
        "playbooks_used": playbooks_used,
    }


async def create_project(
    name: str,
    *,
    github: str | None = None,
    description: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Create a Studio-managed project. Raises ValueError on bad input."""
    if not name or not name.strip():
        raise ValueError("Project name is required")

    clean_name = name.strip()
    now = time.time()
    async with _open_db(_DB) as db:
        await _ensure_table(db)
        await db.execute(
            """INSERT INTO projects
                   (name, source, path, github, description,
                    created_at, updated_at, last_seen_at)
               VALUES (?, 'studio', ?, ?, ?, ?, ?, ?)""",
            (clean_name, path, github, description, now, now, now),
        )
        await db.commit()

    return {"name": clean_name, "source": "studio", "created_at": now}


async def update_project(name: str, fields: dict[str, Any]) -> bool:
    """Patch mutable project fields. Returns True when a row was updated."""
    allowed = {"description", "github", "path"}
    clean = {k: v for k, v in fields.items() if k in allowed}
    if not clean:
        return False

    async with _open_db(_DB) as db:
        await _ensure_table(db)
        clean["updated_at"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in clean)
        vals = list(clean.values()) + [name]
        cur = await db.execute(
            f"UPDATE projects SET {sets} WHERE name = ?",  # noqa: S608
            vals,
        )
        await db.commit()
        return cur.rowcount > 0


async def assign_sessions_to_project(
    project_name: str,
    *,
    session_ids: list[str] | None = None,
    all_unassigned: bool = False,
) -> int:
    """Assign sessions to a project. Returns count of updated rows."""
    async with _open_db(_DB) as db:
        await _ensure_table(db)
        if session_ids:
            placeholders = ",".join("?" for _ in session_ids)
            cur = await db.execute(
                f"UPDATE sessions SET project = ?, project_source = 'manual' "  # noqa: S608
                f"WHERE id IN ({placeholders})",
                [project_name, *session_ids],
            )
        elif all_unassigned:
            cur = await db.execute(
                "UPDATE sessions SET project = ?, project_source = 'manual' WHERE project IS NULL",
                (project_name,),
            )
        else:
            return 0
        await db.commit()
        count = cur.rowcount

        now = time.time()
        await db.execute(
            """INSERT INTO projects (name, source, created_at, updated_at, last_seen_at)
               VALUES (?, 'studio', ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   last_seen_at = excluded.last_seen_at,
                   updated_at = excluded.updated_at""",
            (project_name, now, now, now),
        )
        await db.commit()
        return count


async def delete_project(name: str) -> bool:
    """Delete a Studio-managed project. Returns True when deleted."""
    async with _open_db(_DB) as db:
        await _ensure_table(db)
        cur = await db.execute(
            "DELETE FROM projects WHERE name = ? AND source = 'studio'",
            (name,),
        )
        await db.commit()
        return cur.rowcount > 0


class CreateProjectRequest(BaseModel):
    name: str
    github: str | None = None
    description: str | None = None
    path: str | None = None


class UpdateProjectRequest(BaseModel):
    github: str | None = None
    description: str | None = None
    path: str | None = None


class AssignProjectRequest(BaseModel):
    session_ids: list[str] | None = None
    all_unassigned: bool = False


@studio_route("/projects/", method="GET", area="projects", name="list_projects")
async def list_projects_route() -> dict[str, Any]:
    return await list_projects()


@studio_route("/projects/{name}", method="GET", area="projects", name="get_project")
async def get_project_route(name: str) -> dict[str, Any]:
    project = await get_project(name)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found")
    return project


@studio_route("/projects/", method="POST", area="projects", status_code=201, name="create_project")
async def create_project_route(body: CreateProjectRequest) -> dict[str, Any]:
    try:
        return await create_project(
            body.name,
            github=body.github,
            description=body.description,
            path=body.path,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@studio_route("/projects/{name}", method="PUT", area="projects", name="update_project")
async def update_project_route(name: str, body: UpdateProjectRequest) -> dict[str, Any]:
    fields = body.model_dump(exclude_none=True)
    ok = await update_project(name, fields)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail=f"Project '{name}' not found or no changes",
        )
    return {"ok": True}


@studio_route("/projects/{name}/assign", method="POST", area="projects")
async def assign_project(name: str, body: AssignProjectRequest) -> dict[str, Any]:
    """Assign sessions to a project. Use session_ids for specific ones, or all_unassigned=true."""
    count = await assign_sessions_to_project(
        name,
        session_ids=body.session_ids,
        all_unassigned=body.all_unassigned,
    )
    return {"assigned": count, "project": name}


@studio_route("/projects/{name}", method="DELETE", area="projects", name="delete_project")
async def delete_project_route(name: str) -> dict[str, Any]:
    ok = await delete_project(name)
    if not ok:
        raise HTTPException(
            status_code=403,
            detail="Only Studio-managed projects can be deleted",
        )
    return {"ok": True}
