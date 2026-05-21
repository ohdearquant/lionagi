from __future__ import annotations

from typing import Any

from lionagi.state.db import DEFAULT_DB_PATH

from . import agents as agents_svc
from . import playbooks as playbooks_svc
from . import plugins as plugins_svc
from . import sessions as sessions_svc
from . import shows as shows_svc
from . import skills as skills_svc
from ._db import get_active_connection_count
from ._db import open_db as _open_db
from ._path_safety import public_path

_DB = str(DEFAULT_DB_PATH)


async def _table_counts(db: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in ("messages", "progressions", "sessions", "branches", "definitions", "shows", "plays"):
        try:
            cur = await db.execute(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608
            row = await cur.fetchone()
            counts[table] = row["n"] if row else 0
        except Exception:
            counts[table] = 0
    return counts


async def _sessions_by_status(db: Any) -> dict[str, int]:
    try:
        cur = await db.execute(
            "SELECT COALESCE(status, '(null)') AS status, COUNT(*) AS n FROM sessions GROUP BY status"
        )
        rows = await cur.fetchall()
        return {row["status"]: row["n"] for row in rows}
    except Exception:
        return {}


async def _pragmas(db: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for pragma in ("journal_mode", "wal_autocheckpoint", "busy_timeout", "synchronous", "foreign_keys"):
        try:
            cur = await db.execute(f"PRAGMA {pragma}")
            row = await cur.fetchone()
            result[pragma] = row[0] if row else None
        except Exception:
            result[pragma] = None
    return result


async def get_db_stats() -> dict[str, Any]:
    db_path = DEFAULT_DB_PATH
    size_bytes = db_path.stat().st_size if db_path.exists() else 0
    wal_path = db_path.parent / (db_path.name + "-wal")
    wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
    connections_active = get_active_connection_count()
    path_str = public_path(db_path)

    if not db_path.exists():
        return {
            "path": path_str,
            "size_bytes": 0,
            "wal_bytes": 0,
            "connections_active": connections_active,
            "last_checkpoint_at": None,
            "tables": {t: 0 for t in ("messages", "progressions", "sessions", "branches", "definitions", "shows", "plays")},
            "sessions_by_status": {},
            "pragmas": {},
            "slow_queries": None,
        }

    async with _open_db(_DB) as db:
        tables = await _table_counts(db)
        by_status = await _sessions_by_status(db)
        pragmas = await _pragmas(db)

    return {
        "path": path_str,
        "size_bytes": size_bytes,
        "wal_bytes": wal_bytes,
        "connections_active": connections_active,
        "last_checkpoint_at": None,
        "tables": tables,
        "sessions_by_status": by_status,
        "pragmas": pragmas,
        "slow_queries": None,
    }


async def get_stats() -> dict[str, Any]:
    return {
        "playbooks": len(playbooks_svc.list_playbooks()),
        "agents": len(agents_svc.list_agents()),
        "runs": len(await sessions_svc.list_sessions()),
        "shows": len(await shows_svc.list_shows()),
        "skills": len(skills_svc.list_skills()),
        "plugins": len(plugins_svc.list_plugins()),
        "db": await get_db_stats(),
    }
