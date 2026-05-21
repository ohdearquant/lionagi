from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from lionagi.state.db import DEFAULT_DB_PATH

from ._db import open_db as _open_db

_DB = str(DEFAULT_DB_PATH)

PhantomReason = Literal["process_dead", "missing_artifacts", "stale_lock"]


def db_health() -> dict[str, int]:
    db_path = DEFAULT_DB_PATH
    size_bytes = db_path.stat().st_size if db_path.exists() else 0
    wal_path = db_path.parent / (db_path.name + "-wal")
    wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
    return {"size_bytes": size_bytes, "wal_bytes": wal_bytes, "wal_pending": wal_bytes}


def _pid_is_live(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _find_pid_file(root: Path) -> int | None:
    for name in ("session.pid", "run.pid", ".pid"):
        p = root / name
        if p.exists():
            try:
                return int(p.read_text().strip())
            except (OSError, ValueError):
                pass
    for p in root.glob("*.pid"):
        try:
            return int(p.read_text().strip())
        except (OSError, ValueError):
            pass
    return None


def _live_process_matches(session_id: str, artifacts_path: Path | None) -> bool:
    if artifacts_path and artifacts_path.exists():
        pid = _find_pid_file(artifacts_path)
        if pid is not None:
            return _pid_is_live(pid)
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,command="],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return session_id in result.stdout
    except Exception:
        return False


def _artifacts_path(row: Any) -> Path | None:
    ap = row["artifacts_path"] if "artifacts_path" in row.keys() else None
    if ap:
        return Path(ap)
    return None


def _find_stale_lock(root: Path, *, cutoff: float) -> Path | None:
    try:
        for lock in root.glob("**/*.lock"):
            try:
                if lock.stat().st_mtime < cutoff:
                    return lock
            except OSError:
                pass
    except OSError:
        pass
    return None


def _classify_phantom(
    row: Any, *, now: float, stale_seconds: float
) -> PhantomReason | None:
    ap = _artifacts_path(row)
    if ap and not ap.exists():
        return "missing_artifacts"
    if ap and ap.exists():
        cutoff = now - stale_seconds
        if _find_stale_lock(ap, cutoff=cutoff) is not None:
            return "stale_lock"
    updated_at = row["updated_at"] or 0.0
    age = now - updated_at
    if age >= stale_seconds and not _live_process_matches(row["id"], ap):
        return "process_dead"
    return None


async def list_phantom_sessions(*, stale_hours: float = 1.0) -> list[dict[str, Any]]:
    if not DEFAULT_DB_PATH.exists():
        return []
    now = time.time()
    stale_seconds = stale_hours * 3600
    phantoms: list[dict[str, Any]] = []
    async with _open_db(_DB) as db:
        cur = await db.execute(
            """
            SELECT id, name, playbook_name, started_at, updated_at, artifacts_path, status
            FROM sessions
            WHERE status = 'running'
            ORDER BY updated_at DESC
            """
        )
        rows = await cur.fetchall()
    for row in rows:
        reason = _classify_phantom(row, now=now, stale_seconds=stale_seconds)
        if reason is not None:
            phantoms.append(
                {
                    "session_id": row["id"],
                    "playbook": row["playbook_name"] or row["name"],
                    "started_at": row["started_at"],
                    "reason": reason,
                }
            )
    return phantoms


async def doctor(*, stale_hours: float = 1.0) -> dict[str, Any]:
    return {
        "phantom_sessions": await list_phantom_sessions(stale_hours=stale_hours),
        "db_health": db_health(),
        "diagnostic_run_at": datetime.now(timezone.utc).isoformat(),
    }


async def prune_sessions(session_ids: list[str]) -> int:
    """Delete sessions by explicit ID list (intentional admin action; unconditional)."""
    seen: dict[str, None] = {}
    for sid in session_ids:
        seen[sid] = None
    unique_ids = list(seen)
    if not unique_ids or not DEFAULT_DB_PATH.exists():
        return 0
    placeholders = ",".join("?" * len(unique_ids))
    async with _open_db(_DB) as db:
        cur = await db.execute(
            f"DELETE FROM sessions WHERE id IN ({placeholders})",  # noqa: S608
            unique_ids,
        )
        await db.commit()
        pruned = cur.rowcount or 0
        await db.execute(
            """
            DELETE FROM messages
            WHERE id NOT IN (
              SELECT value FROM progressions, json_each(progressions.collection)
            )
            """
        )
        await db.commit()
    return pruned


async def prune_phantom_sessions(*, stale_hours: float = 1.0) -> int:
    """Prune phantom sessions with a TOCTOU-safe guarded DELETE.

    Re-checks the phantom condition atomically in the WHERE clause so sessions
    that transitioned to a terminal state between classification and deletion are
    never removed.

    Guard strategy is per-reason:
    - ``process_dead`` / ``stale_lock``: require ``status = 'running' AND
      updated_at <= stale_cutoff`` (staleness confirms the process is gone).
    - ``missing_artifacts``: require ``status = 'running' AND updated_at <=
      stale_cutoff`` — same guard as stale reasons to prevent deleting a session
      that recovered (created its artifacts + heartbeated) between classification
      and deletion.
    """
    phantoms = await list_phantom_sessions(stale_hours=stale_hours)
    if not phantoms or not DEFAULT_DB_PATH.exists():
        return 0

    now = time.time()
    stale_cutoff = now - stale_hours * 3600

    # Split by reason so each group gets the appropriate WHERE guard.
    stale_ids = [
        p["session_id"]
        for p in phantoms
        if p.get("reason") in ("process_dead", "stale_lock")
    ]
    artifact_entries = [
        (p["session_id"], p.get("started_at", 0))
        for p in phantoms
        if p.get("reason") == "missing_artifacts"
    ]
    artifact_ids = [e[0] for e in artifact_entries]
    artifact_cutoff = max((e[1] for e in artifact_entries), default=0) if artifact_entries else 0

    pruned = 0
    async with _open_db(_DB) as db:
        if stale_ids:
            placeholders = ",".join("?" * len(stale_ids))
            cur = await db.execute(
                f"DELETE FROM sessions WHERE id IN ({placeholders})"  # noqa: S608
                " AND status = 'running' AND (updated_at IS NULL OR updated_at <= ?)",
                (*stale_ids, stale_cutoff),
            )
            await db.commit()
            pruned += cur.rowcount or 0

        if artifact_ids:
            placeholders = ",".join("?" * len(artifact_ids))
            cur = await db.execute(
                f"DELETE FROM sessions WHERE id IN ({placeholders})"  # noqa: S608
                " AND status = 'running'"
                " AND (updated_at IS NULL OR updated_at <= ?)",
                (*artifact_ids, stale_cutoff),
            )
            await db.commit()
            pruned += cur.rowcount or 0

        if pruned:
            await db.execute(
                """
                DELETE FROM messages
                WHERE id NOT IN (
                  SELECT value FROM progressions, json_each(progressions.collection)
                )
                """
            )
            await db.commit()
    return pruned
