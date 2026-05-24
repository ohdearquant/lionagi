from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from lionagi.state.db import DEFAULT_DB_PATH
from lionagi.state.reasons import SessionReasons

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


def _classify_phantom(row: Any, *, now: float, stale_seconds: float) -> PhantomReason | None:
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
                    "updated_at": row["updated_at"] or 0.0,
                    "artifacts_path": row["artifacts_path"],
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


# ─── ADR-0024: graduated health + transition (additive) ──────────────────────


async def health_report() -> dict[str, Any]:
    """Composite session health snapshot for the admin console.

    Layers ADR-0024's ``classify_session_health`` over the existing
    phantom checks: process liveness comes from the same PID/ps scan
    that ``doctor`` uses; artifacts/locks come from the run directory.
    Terminal sessions are classified too (so we can spot zombies left
    behind by past crashes).
    """
    from collections import Counter

    from lionagi.state.health import (
        SessionHealth,
        classify_session_health,
    )

    if not DEFAULT_DB_PATH.exists():
        return {
            "sessions": {"total": 0, "by_status": {}, "by_health": {}, "unhealthy": []},
            "db": db_health(),
            "diagnostic_run_at": datetime.now(timezone.utc).isoformat(),
        }

    now = time.time()
    async with _open_db(_DB) as db:
        cur = await db.execute(
            """
            SELECT s.id, s.name, s.status, s.invocation_kind, s.agent_name,
                   s.playbook_name, s.started_at, s.ended_at, s.updated_at,
                   s.last_message_at, s.artifacts_path,
                   COALESCE(SUM(json_array_length(p.collection)), 0) AS message_count
            FROM sessions s
            LEFT JOIN branches b ON b.session_id = s.id
            LEFT JOIN progressions p ON p.id = b.progression_id
            GROUP BY s.id
            ORDER BY s.updated_at DESC
            """
        )
        rows = await cur.fetchall()

    by_status: Counter[str] = Counter()
    by_health: Counter[str] = Counter()
    unhealthy: list[dict[str, Any]] = []

    for row in rows:
        # Convert sqlite3.Row to dict for the pure classifier.
        sess = {k: row[k] for k in row.keys()}
        status = sess.get("status") or "completed"
        by_status[status] += 1

        artifacts = _artifacts_path(row)
        has_artifacts = artifacts is not None and artifacts.exists()
        has_stale_locks = False
        if artifacts is not None and artifacts.exists():
            # Lock check is cheap (one glob) but only matters for
            # candidate-zombie terminal sessions and stale-process
            # running ones. Skip for clearly-healthy active ones.
            cutoff = now - 3600
            has_stale_locks = _find_stale_lock(artifacts, cutoff=cutoff) is not None

        if status == "running":
            process_alive = _live_process_matches(row["id"], artifacts)
        else:
            # Terminal session — process_alive is moot; classifier only
            # uses it on the running branch.
            process_alive = False

        health = classify_session_health(
            sess,
            now=now,
            process_alive=process_alive,
            has_artifacts=has_artifacts,
            has_stale_locks=has_stale_locks,
        )
        by_health[health.value] += 1

        # "Unhealthy" = anything that warrants operator attention.
        if health not in (SessionHealth.HEALTHY, SessionHealth.IDLE):
            last_activity = (
                sess.get("last_message_at") or sess.get("updated_at") or sess.get("started_at") or 0
            )
            unhealthy.append(
                {
                    "session_id": row["id"],
                    "name": sess.get("name")
                    or sess.get("playbook_name")
                    or sess.get("agent_name")
                    or "",
                    "health": health.value,
                    "status": status,
                    "invocation_kind": sess.get("invocation_kind"),
                    "agent_name": sess.get("agent_name"),
                    "playbook_name": sess.get("playbook_name"),
                    "last_message_at": sess.get("last_message_at"),
                    "idle_seconds": now - last_activity if last_activity else None,
                    "process_alive": process_alive,
                    "message_count": sess.get("message_count") or 0,
                }
            )

    return {
        "sessions": {
            "total": sum(by_status.values()),
            "by_status": dict(by_status),
            "by_health": dict(by_health),
            "unhealthy": unhealthy,
        },
        "db": db_health(),
        "diagnostic_run_at": datetime.now(timezone.utc).isoformat(),
    }


# ADR-0025: admin operators cannot mark sessions completed/timed_out —
# those are system-determined. Mirror the Python guard from db.py here
# so the API rejects the request before touching the DB.
_ADMIN_TRANSITION_TARGETS: frozenset[str] = frozenset({"failed", "aborted", "cancelled"})

# ADR-0028 §5: phantom classification → reason code mapping.
_PHANTOM_REASON_CODES: dict[str, str] = {
    "process_dead": SessionReasons.HEALTH_PHANTOM_PROCESS_DEAD,
    "missing_artifacts": SessionReasons.HEALTH_PHANTOM_MISSING_ARTIFACTS,
    "stale_lock": SessionReasons.HEALTH_ZOMBIE_STALE_LOCKS,
}


async def transition_sessions(
    session_ids: list[str],
    *,
    target_status: str,
    reason_code: str,
    reason_summary: str = "",
    evidence_refs: list[dict[str, Any]] | None = None,
    actor: str = "admin",
    legacy_reason: str | None = None,
) -> dict[str, Any]:
    """Mark running sessions terminal with an audit-log entry.

    ADR-0024 §B replaces the blunt "prune" with "transition": the session
    row + messages + artifacts are preserved for debugging. Guards:

    * ``target_status`` must be in ``ADMIN_TRANSITION_TARGETS``.
    * Only ``running`` sessions are touched; already-terminal ones are
      reported in ``skipped`` so the caller can warn the operator.
    * HEALTHY and IDLE sessions are refused with ValueError (→ 422) to
      prevent accidental termination of active sessions (ADR-0024 health
      guard).
    * The DB update is conditional on ``status='running'`` in the WHERE
      clause to close the TOCTOU window between the pre-check read and the
      write.
    """
    from lionagi.state.reasons import validate_reason_code

    if target_status not in _ADMIN_TRANSITION_TARGETS:
        raise ValueError(
            f"target_status must be one of {sorted(_ADMIN_TRANSITION_TARGETS)}; "
            f"got {target_status!r}"
        )
    validate_reason_code(reason_code)
    if reason_summary is None:
        reason_summary = ""
    evidence_refs = list(evidence_refs or [])
    if not session_ids:
        return {"transitioned": [], "skipped": [], "event_id": None}
    if not DEFAULT_DB_PATH.exists():
        return {"transitioned": [], "skipped": session_ids, "event_id": None}

    from lionagi.state.db import StateDB
    from lionagi.state.health import SessionHealth, classify_session_health

    transitioned: list[str] = []
    skipped: list[dict[str, str]] = []
    now = time.time()

    async with StateDB() as db:
        # Health guard + UPDATE merged into one loop per session.
        # Re-classify each session immediately before the UPDATE to minimize
        # the TOCTOU window between the guard read and the destructive write.
        for sid in session_ids:
            current = await db.get_session(sid)
            if current is None:
                skipped.append({"session_id": sid, "reason": "not_found"})
                continue
            if current.get("status") != "running":
                skipped.append(
                    {"session_id": sid, "reason": f"not_running:{current.get('status')}"}
                )
                continue
            # Snapshot health-relevant timestamps for the atomic WHERE below.
            _snap_last_msg = current.get("last_message_at")
            _snap_updated = current.get("updated_at")
            artifacts = _artifacts_path(current)
            has_artifacts = artifacts is not None and artifacts.exists()
            has_stale_locks = (
                _find_stale_lock(artifacts, cutoff=now - 3600) is not None
                if artifacts is not None and artifacts.exists()
                else False
            )
            process_alive = _live_process_matches(current["id"], artifacts)
            health = classify_session_health(
                current,
                now=now,
                process_alive=process_alive,
                has_artifacts=has_artifacts,
                has_stale_locks=has_stale_locks,
            )
            if health in (SessionHealth.HEALTHY, SessionHealth.IDLE):
                raise ValueError(
                    f"Session {sid!r} is {health.value} — transition refused. "
                    "Only unhealthy sessions may be force-transitioned."
                )

            # Phantom classification overrides the operator reason code when a
            # concrete health cause is available (ADR-0028 §5).
            phantom_reason = _classify_phantom(current, now=now, stale_seconds=3600)
            effective_reason_code = reason_code
            effective_reason_summary = reason_summary
            effective_evidence_refs: list[dict[str, Any]] = list(evidence_refs)
            if phantom_reason is not None:
                effective_reason_code = _PHANTOM_REASON_CODES[phantom_reason]
                effective_reason_summary = reason_summary or (
                    f"Operator transitioned session after phantom classification: {phantom_reason}."
                )
                effective_evidence_refs.append(
                    {"kind": "phantom_classification", "reason": phantom_reason, "session_id": sid}
                )

            # UPDATE immediately after the health check to minimize the race
            # window. Reason columns are written in the same conditional UPDATE
            # so they're atomic with the TOCTOU guard. The WHERE status='running'
            # guard closes the residual TOCTOU: rowcount==0 means a concurrent
            # transition already won.
            cur = await db.db.execute(
                "UPDATE sessions SET status=?, ended_at=?, updated_at=?, "
                "  status_reason_code=?, status_reason_summary=?, status_evidence_refs=? "
                "WHERE id=? AND status='running'"
                "  AND (last_message_at IS ? OR last_message_at = ?)"
                "  AND (updated_at      IS ? OR updated_at      = ?)",
                (
                    target_status,
                    now,
                    now,
                    effective_reason_code,
                    effective_reason_summary,
                    json.dumps(effective_evidence_refs),
                    sid,
                    _snap_last_msg,
                    _snap_last_msg,
                    _snap_updated,
                    _snap_updated,
                ),
            )
            await db.db.commit()
            if cur.rowcount == 0:
                existing = await db.get_session(sid)
                if existing is None:
                    skipped.append({"session_id": sid, "reason": "not_found"})
                elif existing.get("status") == "running":
                    # Status is still running but timestamps changed between
                    # snapshot and UPDATE — a concurrent heartbeat raced us.
                    skipped.append(
                        {
                            "session_id": sid,
                            "reason": "changed_since_snapshot",
                        }
                    )
                else:
                    skipped.append(
                        {
                            "session_id": sid,
                            "reason": f"not_running:{existing.get('status')}",
                        }
                    )
                continue
            # Log the transition to history. Separate commit is acceptable:
            # status is already correct; this is append-only audit data.
            await db.db.execute(
                "INSERT INTO status_transitions "
                "(id, entity_type, entity_id, previous_status, status, "
                " reason_code, reason_summary, evidence_refs, "
                " source, actor, created_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex,
                    "session",
                    sid,
                    "running",
                    target_status,
                    effective_reason_code,
                    effective_reason_summary,
                    json.dumps(effective_evidence_refs),
                    "admin",
                    actor,
                    now,
                    json.dumps(
                        {
                            "legacy_reason": legacy_reason,
                            "health": health.value,
                            "process_alive": process_alive,
                        }
                    ),
                ),
            )
            await db.db.commit()
            transitioned.append(sid)

        event_id = await db.insert_admin_event(
            action="transition",
            target_id=None,
            actor=actor,
            details={
                "target_status": target_status,
                "reason_code": reason_code,
                "reason_summary": reason_summary,
                "evidence_refs": evidence_refs,
                "reason": legacy_reason,
                "transitioned": transitioned,
                "skipped": skipped,
            },
        )

    return {
        "transitioned": transitioned,
        "skipped": skipped,
        "event_id": event_id,
    }


async def list_admin_events(
    *,
    action: str | None = None,
    target_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if not DEFAULT_DB_PATH.exists():
        return []
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        return await db.list_admin_events(action=action, target_id=target_id, limit=limit)


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
        p["session_id"] for p in phantoms if p.get("reason") in ("process_dead", "stale_lock")
    ]
    artifact_entries = [
        {
            "id": p["session_id"],
            "classified_updated_at": p.get("updated_at", 0),
            "artifacts_path": p.get("artifacts_path"),
        }
        for p in phantoms
        if p.get("reason") == "missing_artifacts"
    ]

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

        for entry in artifact_entries:
            ap = Path(entry["artifacts_path"]) if entry["artifacts_path"] else None
            if ap and ap.exists():
                continue
            cur = await db.execute(
                "DELETE FROM sessions WHERE id = ?"
                " AND status = 'running'"
                " AND (updated_at IS NULL OR updated_at <= ?)",
                (entry["id"], entry["classified_updated_at"]),
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
