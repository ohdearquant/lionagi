# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import OperationalError as _SAOperationalError

from lionagi.cli._util import pid_alive as _pid_is_live
from lionagi.ln import now_utc
from lionagi.state.db import ADMIN_TRANSITION_TARGETS as _ADMIN_TRANSITION_TARGETS
from lionagi.state.db import DEFAULT_DB_PATH
from lionagi.state.reasons import RunReasons, SessionReasons, validate_reason_code

from ..registry import studio_route
from ._db import open_db as _open_db

_DB = str(DEFAULT_DB_PATH)
_log = logging.getLogger(__name__)

# Fallback mapping for deprecated 'reason' field without reason_code.
_LEGACY_ADMIN_REASON_CODES: dict[str, str] = {
    "failed": RunReasons.FAILED_EXCEPTION,
    "aborted": RunReasons.ABORTED_USER,
    "cancelled": RunReasons.CANCELLED_SYSTEM,
}

PhantomReason = Literal["process_dead", "missing_artifacts", "stale_lock"]


class MaintenanceBody(BaseModel):
    """Request body for POST /api/admin/maintenance."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["vacuum", "checkpoint", "prune"] = Field(
        ...,
        description="DB maintenance action: 'vacuum', 'checkpoint', or 'prune'.",
    )


class PruneBody(BaseModel):
    session_ids: list[str] | None = None
    all_phantom: bool = False


class PruneOldDataBody(BaseModel):
    keep_days: int | None = Field(
        default=None, ge=1, description="Retain sessions newer than this many days"
    )


class TransitionBody(BaseModel):
    """Admin session transition; reason_code is preferred over deprecated reason."""

    session_ids: list[str] = Field(..., min_length=1)
    target_status: Literal["failed", "aborted", "cancelled"]
    reason_code: str | None = None
    reason_summary: str = ""
    evidence_refs: list[dict] = Field(default_factory=list)
    # Deprecated; kept for backwards compatibility.
    reason: str | None = Field(default=None, max_length=500)
    actor: str = Field(default="admin", max_length=64)


def db_health() -> dict[str, int]:
    db_path = DEFAULT_DB_PATH
    size_bytes = db_path.stat().st_size if db_path.exists() else 0
    wal_path = db_path.parent / (db_path.name + "-wal")
    wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
    return {"size_bytes": size_bytes, "wal_bytes": wal_bytes, "wal_pending": wal_bytes}


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


def _ps_snapshot() -> str:
    """One ``ps`` capture, shareable across rows; empty string when unavailable."""
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,command="],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return result.stdout
    except Exception:
        return ""


# Process start-time comparison tolerance (clock-tick rounding).
_PID_CREATE_TIME_TOLERANCE = 1.0


def process_liveness(
    session: dict[str, Any],
    artifacts_path: Path | None,
    ps_snapshot: str | None = None,
) -> bool | None:
    """Tri-state process liveness: True = observed alive, False = confirmed
    dead, None = unknown (no recorded pid/no process match)."""
    pid: int | None = None
    create_time: float | None = None

    meta = session.get("node_metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = None
    if isinstance(meta, dict):
        raw_pid = meta.get("pid")
        if raw_pid is not None:
            try:
                pid = int(raw_pid)
            except (TypeError, ValueError):
                pid = None
        raw_ct = meta.get("pid_create_time")
        if isinstance(raw_ct, int | float):
            create_time = float(raw_ct)

    if pid is None and artifacts_path is not None and artifacts_path.exists():
        pid = _find_pid_file(artifacts_path)

    if pid is not None:
        if not _pid_is_live(pid):
            return False
        try:
            import psutil

            proc = psutil.Process(pid)
            # A zombie has exited but not been reaped; not a live worker
            # even though _pid_is_live() still reports it as present.
            if proc.status() == psutil.STATUS_ZOMBIE:
                return False
            if create_time is not None:
                actual = proc.create_time()
                if abs(actual - create_time) > _PID_CREATE_TIME_TOLERANCE:
                    return False  # pid recycled; the recorded process is gone
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return False
        except Exception:
            # Best-effort check: the pid-is-live test above already
            # passed, so an unreadable status/start time reads as alive.
            _log.debug("pid %s status/start-time check failed", pid, exc_info=True)
        return True

    session_id = session.get("id") or ""
    snapshot = ps_snapshot if ps_snapshot is not None else _ps_snapshot()
    if session_id and session_id in snapshot:
        return True
    return None


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
    row: Any,
    *,
    now: float,
    stale_seconds: float,
    ps_snapshot: str | None = None,
) -> PhantomReason | None:
    ap = _artifacts_path(row)
    node_metadata = row["node_metadata"] if "node_metadata" in row.keys() else None
    session = {"id": row["id"], "node_metadata": node_metadata}
    # A running session is never a phantom while its process is observably alive.
    if process_liveness(session, ap, ps_snapshot) is True:
        return None
    # Not yet stale: it may simply not have written artifacts yet, so give it
    # the benefit of the doubt rather than reap a fresh/quiet session.
    updated_at = row["updated_at"] or 0.0
    if now - updated_at < stale_seconds:
        return None
    if ap and not ap.exists():
        return "missing_artifacts"
    if ap and ap.exists() and _find_stale_lock(ap, cutoff=now - stale_seconds) is not None:
        return "stale_lock"
    return "process_dead"


async def list_phantom_sessions(*, stale_hours: float = 1.0) -> list[dict[str, Any]]:
    if not DEFAULT_DB_PATH.exists():
        return []
    now = time.time()
    stale_seconds = stale_hours * 3600
    phantoms: list[dict[str, Any]] = []
    async with _open_db(_DB) as db:
        cur = await db.execute(
            """
            SELECT id, name, playbook_name, started_at, updated_at, artifacts_path,
                   status, node_metadata
            FROM sessions
            WHERE status = 'running'
            ORDER BY updated_at DESC
            """
        )
        rows = await cur.fetchall()
    snapshot: str | None = None
    for row in rows:
        if snapshot is None:
            snapshot = _ps_snapshot()
        reason = _classify_phantom(row, now=now, stale_seconds=stale_seconds, ps_snapshot=snapshot)
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
        "diagnostic_run_at": now_utc().isoformat(),
    }


async def health_report() -> dict[str, Any]:
    """Composite session health snapshot for the admin console."""
    from collections import Counter

    from lionagi.state.health import (
        SessionHealth,
        classify_session_health,
    )

    if not DEFAULT_DB_PATH.exists():
        return {
            "sessions": {"total": 0, "by_status": {}, "by_health": {}, "unhealthy": []},
            "db": db_health(),
            "diagnostic_run_at": now_utc().isoformat(),
        }

    now = time.time()
    async with _open_db(_DB) as db:
        cur = await db.execute(
            """
            SELECT s.id, s.name, s.status, s.invocation_kind, s.agent_name,
                   s.playbook_name, s.started_at, s.ended_at, s.updated_at,
                   s.last_message_at, s.artifacts_path, s.node_metadata,
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
    snapshot: str | None = None

    for row in rows:
        sess = {k: row[k] for k in row.keys()}
        status = sess.get("status") or "completed"

        artifacts = _artifacts_path(row)
        has_artifacts = artifacts is not None and artifacts.exists()
        has_stale_locks = False
        if artifacts is not None and artifacts.exists():
            cutoff = now - 3600
            has_stale_locks = _find_stale_lock(artifacts, cutoff=cutoff) is not None

        if status == "running":
            if snapshot is None:
                snapshot = _ps_snapshot()
            process_alive = process_liveness(sess, artifacts, snapshot)
        else:
            process_alive = False

        health = classify_session_health(
            sess,
            now=now,
            process_alive=process_alive,
            has_artifacts=has_artifacts,
            has_stale_locks=has_stale_locks,
        )
        by_health[health.value] += 1

        # A "running" row whose process is confirmed dead isn't actually
        # running; bucket it under its health verdict instead.
        status_bucket = status
        if status == "running" and health in (
            SessionHealth.STALE,
            SessionHealth.ORPHANED,
            SessionHealth.ZOMBIE,
        ):
            status_bucket = health.value
        by_status[status_bucket] += 1

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
        "diagnostic_run_at": now_utc().isoformat(),
    }


_PHANTOM_REASON_CODES: dict[str, str] = {
    "process_dead": SessionReasons.HEALTH_PHANTOM_PROCESS_DEAD,
    "missing_artifacts": SessionReasons.HEALTH_PHANTOM_MISSING_ARTIFACTS,
    "stale_lock": SessionReasons.HEALTH_ZOMBIE_STALE_LOCKS,
}


def _resolve_session_health_reason_code(
    *,
    phantom_reason: str | None,
    health,  # SessionHealth enum from lionagi.state.health
) -> str | None:
    """Return the most-specific health-derived reason code, or None."""
    if phantom_reason is not None:
        return _PHANTOM_REASON_CODES.get(phantom_reason)
    from lionagi.state.health import SessionHealth

    if health == SessionHealth.STALE:
        return SessionReasons.HEALTH_STALE_NO_HEARTBEAT
    if health == SessionHealth.ORPHANED:
        return SessionReasons.HEALTH_ORPHANED_NO_PROCESS
    if health == SessionHealth.ZOMBIE:
        return SessionReasons.HEALTH_ZOMBIE_STALE_LOCKS
    return None


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
    """Transition running sessions to a terminal status with an audit-log entry."""
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
    txn_snapshot: str | None = None

    async with StateDB() as db:
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
            _snap_last_msg = current.get("last_message_at")
            _snap_updated = current.get("updated_at")
            artifacts = _artifacts_path(current)
            has_artifacts = artifacts is not None and artifacts.exists()
            has_stale_locks = (
                _find_stale_lock(artifacts, cutoff=now - 3600) is not None
                if artifacts is not None and artifacts.exists()
                else False
            )
            if txn_snapshot is None:
                txn_snapshot = _ps_snapshot()
            process_alive = process_liveness(current, artifacts, txn_snapshot)
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

            phantom_reason = _classify_phantom(
                current, now=now, stale_seconds=3600, ps_snapshot=txn_snapshot
            )
            classifier_code = _resolve_session_health_reason_code(
                phantom_reason=phantom_reason,
                health=health,
            )
            effective_reason_code = reason_code
            effective_reason_summary = reason_summary
            effective_evidence_refs: list[dict[str, Any]] = list(evidence_refs)
            if classifier_code is not None:
                effective_reason_code = classifier_code
                if not reason_summary:
                    cause = phantom_reason or health.value
                    effective_reason_summary = (
                        f"Operator transitioned session after classifier: {cause}."
                    )
                if phantom_reason is not None:
                    effective_evidence_refs.append(
                        {
                            "kind": "phantom_classification",
                            "reason": phantom_reason,
                            "session_id": sid,
                        }
                    )
                else:
                    effective_evidence_refs.append(
                        {
                            "kind": "session_health",
                            "health": health.value,
                            "session_id": sid,
                        }
                    )

            # Intentional specialized CAS (not a bypass of update_status()):
            # WHERE status='running' only allows a legal forward transition,
            # and the last_message_at/updated_at equality guards stop this
            # from clobbering a session that went active again mid-check.
            async with db.transaction() as conn:
                result = await conn.execute(
                    text(
                        "UPDATE sessions SET status=:status, ended_at=:now, updated_at=:now, "
                        "  status_reason_code=:rcode, status_reason_summary=:rsummary, "
                        "  status_evidence_refs=:erefs "
                        "WHERE id=:sid AND status='running'"
                        "  AND (last_message_at IS :slast OR last_message_at = :slast)"
                        "  AND (updated_at      IS :supd  OR updated_at      = :supd)"
                    ),
                    {
                        "status": target_status,
                        "now": now,
                        "rcode": effective_reason_code,
                        "rsummary": effective_reason_summary,
                        "erefs": json.dumps(effective_evidence_refs),
                        "sid": sid,
                        "slast": _snap_last_msg,
                        "supd": _snap_updated,
                    },
                )
                cas_hit = result.rowcount != 0
                if cas_hit:
                    await conn.execute(
                        text(
                            "INSERT INTO status_transitions "
                            "(id, entity_type, entity_id, previous_status, status, "
                            " reason_code, reason_summary, evidence_refs, "
                            " source, actor, created_at, metadata) "
                            "VALUES (:id, :etype, :eid, :prev, :status, "
                            " :rcode, :rsummary, :erefs, :source, :actor, :now, :meta)"
                        ),
                        {
                            "id": uuid.uuid4().hex,
                            "etype": "session",
                            "eid": sid,
                            "prev": "running",
                            "status": target_status,
                            "rcode": effective_reason_code,
                            "rsummary": effective_reason_summary,
                            "erefs": json.dumps(effective_evidence_refs),
                            "source": "admin",
                            "actor": actor,
                            "now": now,
                            "meta": json.dumps(
                                {
                                    "legacy_reason": legacy_reason,
                                    "health": health.value,
                                    "process_alive": process_alive,
                                }
                            ),
                        },
                    )
            if not cas_hit:
                existing = await db.get_session(sid)
                if existing is None:
                    skipped.append({"session_id": sid, "reason": "not_found"})
                elif existing.get("status") == "running":
                    skipped.append({"session_id": sid, "reason": "changed_since_snapshot"})
                else:
                    skipped.append(
                        {"session_id": sid, "reason": f"not_running:{existing.get('status')}"}
                    )
                continue
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
    """Delete sessions by explicit ID list."""
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
    """Transition phantom sessions to 'failed' via the sanctioned status path;
    rows are preserved so reason history and artifacts stay inspectable."""
    from lionagi.studio.services.lifecycle import reap_phantom_sessions

    return await reap_phantom_sessions(stale_hours=stale_hours, actor="admin_prune")


# ---------------------------------------------------------------------------
# Route handlers — admin area
# ---------------------------------------------------------------------------


@studio_route("/admin/doctor", method="GET", area="admin", name="doctor")
async def doctor_route(
    stale_hours: float = Query(default=1.0, gt=0),
) -> dict[str, Any]:
    return await doctor(stale_hours=stale_hours)


@studio_route("/admin/health", method="GET", area="admin", name="health")
async def health_route() -> dict[str, Any]:
    """ADR-0057 D6: composite session health report."""
    return await health_report()


@studio_route("/admin/transition", method="POST", area="admin", name="transition")
async def transition_route(body: TransitionBody) -> dict[str, Any]:
    """Mark running sessions terminal with a reason code."""
    reason_code = body.reason_code
    reason_summary = body.reason_summary

    if reason_code is not None:
        try:
            reason_code = validate_reason_code(reason_code)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    elif body.reason:
        reason_code = _LEGACY_ADMIN_REASON_CODES[body.target_status]
        reason_summary = body.reason
        _log.warning(
            "Deprecated admin transition field 'reason' used without reason_code; "
            "mapped target_status=%s to reason_code=%s",
            body.target_status,
            reason_code,
        )
    else:
        raise HTTPException(status_code=400, detail="reason_code is required")

    try:
        return await transition_sessions(
            body.session_ids,
            target_status=body.target_status,
            reason_code=reason_code,
            reason_summary=reason_summary,
            evidence_refs=body.evidence_refs,
            actor=body.actor,
            legacy_reason=body.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@studio_route("/admin/events", method="GET", area="admin", name="admin_events")
async def admin_events_route(
    action: str | None = Query(default=None),
    target_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, Any]:
    events = await list_admin_events(action=action, target_id=target_id, limit=limit)
    return {"events": events}


@studio_route(
    "/admin/prune-old-data",
    method="POST",
    area="admin",
    name="prune_old_data",
)
async def prune_old_data_route(body: PruneOldDataBody) -> dict[str, int]:
    """Remove terminal sessions/runs older than keep_days (default from config)."""
    from ..services.db_maintenance import prune_old_data as _prune

    return await _prune(keep_days=body.keep_days, actor="admin")


@studio_route(
    "/admin/maintenance",
    method="POST",
    area="admin",
    name="run_maintenance",
)
async def run_maintenance_route(body: MaintenanceBody) -> dict[str, Any]:
    """Run a DB maintenance action (vacuum | checkpoint | prune). Returns 409,
    not 500, when SQLite can't acquire the write lock — a retryable signal."""
    from ..services.db_maintenance import (
        checkpoint_state_db,
        prune_old_data,
        vacuum_state_db,
    )

    try:
        if body.action == "vacuum":
            result = await vacuum_state_db(actor="admin")
            return {"action": "vacuum", **result}

        if body.action == "checkpoint":
            result = await checkpoint_state_db(actor="admin")
            return {"action": "checkpoint", **result}

        # action == "prune"
        result = await prune_old_data(actor="admin")
        return {"action": "prune", **result}

    except (sqlite3.OperationalError, _SAOperationalError) as exc:
        # Only genuine lock/busy contention is retryable; open/path failures
        # should surface as 500. Inspect .orig since SQLAlchemy's wrapper can omit it.
        msg = str(exc).lower()
        orig = getattr(exc, "orig", None)
        if orig is not None:
            msg = f"{msg} {str(orig).lower()}"
        if "locked" in msg or "in progress" in msg:
            raise HTTPException(
                status_code=409,
                detail="State database is busy — another writer holds the lock. Try again shortly.",
            ) from exc
        raise


@studio_route("/admin/prune", method="POST", area="admin", name="prune")
async def prune_route(body: PruneBody) -> dict[str, int]:
    has_ids = bool(body.session_ids)
    has_all = body.all_phantom
    if not has_ids and not has_all:
        raise HTTPException(status_code=422, detail="Provide session_ids or all_phantom")
    if has_ids and has_all:
        raise HTTPException(
            status_code=422,
            detail="Provide either session_ids or all_phantom, not both",
        )
    if has_all:
        count = await prune_phantom_sessions()
    else:
        count = await prune_sessions(body.session_ids or [])
    return {"pruned": count}
