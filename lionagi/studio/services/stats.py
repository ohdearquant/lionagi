from __future__ import annotations

import time
from typing import Any

from fastapi import HTTPException, Query

from lionagi.state.db import DEFAULT_DB_PATH, StateDB

from ..registry import studio_route
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

# ADR-0057 D1 defines the seven-value session status vocabulary; the Pulse
# sparkline folds it into four buckets (timed_out→failed, aborted→cancelled).
_ACTIVITY_WINDOWS: dict[str, tuple[int, int]] = {
    # window key -> (bucket_seconds, bucket_count)
    "24h": (3600, 24),
    "7d": (24 * 3600, 7),
}

_BUCKET_STATUS_MAP: dict[str, str] = {
    "completed": "completed",
    "completed_empty": "completed",
    "failed": "failed",
    "timed_out": "failed",
    "aborted": "cancelled",
    "cancelled": "cancelled",
    "running": "running",
}


async def get_activity_stats(window: str) -> dict[str, Any]:
    if window not in _ACTIVITY_WINDOWS:
        raise HTTPException(status_code=422, detail="window must be one of: 24h, 7d")
    bucket_seconds, bucket_count = _ACTIVITY_WINDOWS[window]
    now = time.time()
    now_bucket_start = int(now // bucket_seconds) * bucket_seconds
    oldest_bucket_start = now_bucket_start - (bucket_count - 1) * bucket_seconds

    # A dashboard read must not create/migrate the DB on a fresh workspace.
    if not DEFAULT_DB_PATH.exists():
        rows: list[dict[str, Any]] = []
    else:
        async with StateDB(DEFAULT_DB_PATH) as db:
            rows = await db.activity_stats(
                window_start=oldest_bucket_start, bucket_seconds=bucket_seconds
            )

    buckets = [
        {
            "t": oldest_bucket_start + i * bucket_seconds,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
            "running": 0,
        }
        for i in range(bucket_count)
    ]
    by_start = {b["t"]: b for b in buckets}

    completed = failed = cancelled = 0
    total = 0
    for row in rows:
        bucket_start = int(row["bucket_start"])
        n = int(row["n"])
        total += n
        # NULL/unknown statuses count in total only — no bucket, no rate.
        bucket_key = _BUCKET_STATUS_MAP.get(row["status"])
        if bucket_key is None:
            continue
        if bucket_key == "completed":
            completed += n
        elif bucket_key == "failed":
            failed += n
        elif bucket_key == "cancelled":
            cancelled += n
        bucket = by_start.get(bucket_start)
        if bucket is not None:
            bucket[bucket_key] += n

    denom = completed + failed + cancelled
    completion_rate = (completed / denom) if denom else None

    return {
        "window": window,
        "buckets": buckets,
        "completion_rate": completion_rate,
        "total": total,
    }


@studio_route("/stats/activity", method="GET", area="stats", tags=[], name="get_activity_stats")
async def get_activity_stats_route(
    window: str = Query(default="24h", description="Activity window: 24h or 7d"),
) -> dict[str, Any]:
    return await get_activity_stats(window)


async def _table_counts(db: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in (
        "messages",
        "progressions",
        "sessions",
        "branches",
        "definitions",
        "shows",
        "plays",
    ):
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
    for pragma in (
        "journal_mode",
        "wal_autocheckpoint",
        "busy_timeout",
        "synchronous",
        "foreign_keys",
    ):
        try:
            cur = await db.execute(f"PRAGMA {pragma}")
            row = await cur.fetchone()
            result[pragma] = row[0] if row else None
        except Exception:
            result[pragma] = None
    return result


async def get_db_stats() -> dict[str, Any]:
    from .db_maintenance import get_db_size_alert, get_last_checkpoint_at

    db_path = DEFAULT_DB_PATH
    size_bytes = db_path.stat().st_size if db_path.exists() else 0
    wal_path = db_path.parent / (db_path.name + "-wal")
    wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
    connections_active = get_active_connection_count()
    path_str = public_path(db_path)
    size_alert, size_threshold_bytes = get_db_size_alert(size_bytes)

    if not db_path.exists():
        return {
            "path": path_str,
            "size_bytes": 0,
            "wal_bytes": 0,
            "connections_active": connections_active,
            "last_checkpoint_at": None,
            "size_alert": False,
            "size_threshold_bytes": size_threshold_bytes,
            "tables": {
                t: 0
                for t in (
                    "messages",
                    "progressions",
                    "sessions",
                    "branches",
                    "definitions",
                    "shows",
                    "plays",
                )
            },
            "sessions_by_status": {},
            "pragmas": {},
            "slow_queries": None,
        }

    last_checkpoint_at = await get_last_checkpoint_at()

    async with _open_db(_DB) as db:
        tables = await _table_counts(db)
        by_status = await _sessions_by_status(db)
        pragmas = await _pragmas(db)

    return {
        "path": path_str,
        "size_bytes": size_bytes,
        "wal_bytes": wal_bytes,
        "connections_active": connections_active,
        "last_checkpoint_at": last_checkpoint_at,
        "size_alert": size_alert,
        "size_threshold_bytes": size_threshold_bytes,
        "tables": tables,
        "sessions_by_status": by_status,
        "pragmas": pragmas,
        "slow_queries": None,
    }


async def get_stats() -> dict[str, Any]:
    from lionagi.studio.services.lifecycle import get_phantom_count

    return {
        "playbooks": len(playbooks_svc.list_playbooks()),
        "agents": len(agents_svc.list_agents()),
        "runs": len(await sessions_svc.list_sessions()),
        "shows": len(await shows_svc.list_shows()),
        "skills": len(skills_svc.list_skills()),
        "plugins": len(plugins_svc.list_plugins()),
        "db": await get_db_stats(),
        "phantom_count": await get_phantom_count(),
    }


@studio_route("/stats", method="GET", area="stats", tags=[], name="get_stats")
async def get_stats_route() -> dict[str, Any]:
    # The runs count comes from SQLite sessions so the dashboard matches the Runs
    # list page — runs_svc.list_runs() reads filesystem dirs and returns a different count.
    return await get_stats()
