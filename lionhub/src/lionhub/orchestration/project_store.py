from __future__ import annotations

from typing import Any

from lionhub.orchestration.state_store import open_db as _open_db
from lionhub.orchestration.state_store import require_file_store, store_exists, store_path

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


async def get_project(name: str) -> dict[str, Any] | None:
    """Return a single project with session counts and usage summaries."""
    require_file_store()
    if not store_exists():
        return None

    async with _open_db(store_path()) as db:
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
