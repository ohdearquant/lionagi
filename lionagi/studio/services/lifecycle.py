# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Studio self-healing lifecycle reapers — write through StateDB.update_status()."""

from __future__ import annotations

import logging
import os
import time

from lionagi.state.db import DEFAULT_DB_PATH, StateDB
from lionagi.state.reasons import RunReasons, SessionReasons

from . import admin as admin_svc
from .admin import _artifacts_path, _ps_snapshot, process_liveness

_log = logging.getLogger(__name__)

# Phantom PhantomReason → SessionReasons code (mirrors admin._PHANTOM_REASON_CODES).
_PHANTOM_REASON_CODES: dict[str, str] = {
    "process_dead": SessionReasons.HEALTH_PHANTOM_PROCESS_DEAD,
    "missing_artifacts": SessionReasons.HEALTH_PHANTOM_MISSING_ARTIFACTS,
    "stale_lock": SessionReasons.HEALTH_ZOMBIE_STALE_LOCKS,
}


# ── invocation deadline + zero-session reaper ────────────────────────────────


def _deadline_for_kind(action_kind: str | None, global_default: int) -> int:
    """Resolve the effective deadline for an invocation's action_kind.

    Checks ``LIONAGI_STUDIO_INVOCATION_DEADLINE_<KIND>_SECONDS`` first;
    falls back to *global_default* when the env var is absent or the kind
    is None.
    """
    if action_kind:
        env_key = f"LIONAGI_STUDIO_INVOCATION_DEADLINE_{action_kind.upper()}_SECONDS"
        raw = os.environ.get(env_key)
        if raw is not None:
            try:
                return int(raw)
            except ValueError:
                _log.warning("Ignoring non-integer env var %s=%r", env_key, raw)
    return global_default


async def reap_stale_invocations(
    *,
    deadline_seconds: int | None = None,
    zero_session_grace_seconds: int | None = None,
) -> int:
    """Transition stale running invocations to ``timed_out``.

    Two conditions: (1) wall-clock deadline exceeded (per-kind env override
    ``LIONAGI_STUDIO_INVOCATION_DEADLINE_<KIND>_SECONDS`` → global fallback);
    (2) zero sessions spawned past the grace period.
    Returns count transitioned.
    """
    from lionagi.studio.config import (
        INVOCATION_DEADLINE_SECONDS,
        ZERO_SESSION_GRACE_SECONDS,
    )

    if deadline_seconds is None:
        deadline_seconds = INVOCATION_DEADLINE_SECONDS
    if zero_session_grace_seconds is None:
        zero_session_grace_seconds = ZERO_SESSION_GRACE_SECONDS

    if not DEFAULT_DB_PATH.exists():
        return 0

    now = time.time()
    grace_cutoff = now - zero_session_grace_seconds
    reaped = 0

    try:
        async with StateDB() as db:
            invocations = await db.list_invocations(status="running", limit=1000)
            for inv in invocations:
                inv_id = inv["id"]
                started_at = inv.get("started_at") or now
                updated_at = inv.get("updated_at") or started_at
                session_count = inv.get("session_count") or 0
                action_kind = inv.get("action_kind")  # SELECT inv.* includes this column

                # Per-kind override: check env var before falling back to global.
                effective_deadline = _deadline_for_kind(action_kind, deadline_seconds)
                deadline_cutoff = now - effective_deadline

                # Condition 1: wall-clock deadline exceeded.
                if started_at < deadline_cutoff:
                    _log.info(
                        "Reaping invocation %s (kind=%s): deadline exceeded "
                        "(started_at=%s, deadline=%ss)",
                        inv_id,
                        action_kind,
                        started_at,
                        effective_deadline,
                    )
                    await db.update_invocation(inv_id, ended_at=now)
                    transitioned = await db.update_status(
                        "invocation",
                        inv_id,
                        new_status="timed_out",
                        reason_code=RunReasons.TIMED_OUT_DEADLINE,
                        reason_summary="invocation_deadline_exceeded",
                        evidence_refs=[{"kind": "invocation", "id": inv_id}],
                        source="system",
                        actor="studio_lifecycle_reaper",
                        metadata={
                            "deadline_seconds": effective_deadline,
                            "action_kind": action_kind,
                            "started_at": started_at,
                        },
                        expected_statuses={"running"},
                    )
                    if transitioned:
                        reaped += 1
                    else:
                        _log.debug("Invocation %s skipped (status changed before CAS lock)", inv_id)
                    continue

                # Condition 2: zero sessions and past grace period.
                if session_count == 0 and updated_at < grace_cutoff:
                    _log.info(
                        "Reaping invocation %s: zero sessions past grace period (%ss)",
                        inv_id,
                        zero_session_grace_seconds,
                    )
                    await db.update_invocation(inv_id, ended_at=now)
                    transitioned = await db.update_status(
                        "invocation",
                        inv_id,
                        new_status="timed_out",
                        reason_code=RunReasons.TIMED_OUT_DEADLINE,
                        reason_summary="zero_session_invocation_timeout",
                        evidence_refs=[{"kind": "invocation", "id": inv_id}],
                        source="system",
                        actor="studio_lifecycle_reaper",
                        metadata={
                            "zero_session_grace_seconds": zero_session_grace_seconds,
                            "updated_at": updated_at,
                        },
                        expected_statuses={"running"},
                    )
                    if transitioned:
                        reaped += 1
                    else:
                        _log.debug("Invocation %s skipped (status changed before CAS lock)", inv_id)
    except Exception:
        _log.exception("reap_stale_invocations error")

    return reaped


# ── null-status session detector ─────────────────────────────────────────────


async def reap_null_status_sessions(*, stale_hours: float | None = None) -> int:
    """Transition null-status sessions whose process is dead to ``failed``.

    Sessions get ``status=NULL`` when the process crashes before writing a
    terminal status (crash, OOM, SIGKILL).  Guard is ``status IS NULL`` so
    already-terminal rows are never touched.  Liveness honors the recorded
    ``node_metadata.pid`` via ``process_liveness()``; when a row is not
    observably alive it still gets a staleness grace (mirroring
    ``_classify_phantom``) before it is reaped, so a fresh/quiet null-status
    session is not punished for a momentary window before it writes its own
    status.
    """
    from lionagi.studio.config import PHANTOM_STALE_HOURS

    if stale_hours is None:
        stale_hours = PHANTOM_STALE_HOURS
    stale_seconds = stale_hours * 3600

    if not DEFAULT_DB_PATH.exists():
        return 0

    now = time.time()
    reaped = 0

    try:
        async with StateDB() as db:
            rows = await db.fetch_all(
                "SELECT id, artifacts_path, started_at, ended_at, updated_at, node_metadata "
                "FROM sessions WHERE status IS NULL"
            )

        ps_snapshot: str | None = None
        for row in rows:
            sid = row["id"]
            artifacts = _artifacts_path(row)
            session = {"id": sid, "node_metadata": row.get("node_metadata")}
            if ps_snapshot is None:
                ps_snapshot = _ps_snapshot()
            if process_liveness(session, artifacts, ps_snapshot) is True:
                # Process still alive — skip, it may write its own status.
                continue

            updated_at = row.get("updated_at") or row.get("started_at") or 0.0
            if now - updated_at < stale_seconds:
                # Not confirmed alive, but too fresh to reap — give it the
                # benefit of the doubt, same as _classify_phantom does.
                continue

            _log.info("Reaping null-status session %s: process is dead", sid)
            try:
                async with StateDB() as db:
                    if row["ended_at"] is None:
                        await db.update_session(sid, ended_at=now)
                    transitioned = await db.update_status(
                        "session",
                        sid,
                        new_status="failed",
                        reason_code=RunReasons.FAILED_EXCEPTION,
                        reason_summary="process_exited_without_status",
                        evidence_refs=[{"kind": "session", "id": sid}],
                        source="system",
                        actor="studio_lifecycle_reaper",
                        metadata={"detector": "null_status_dead_process"},
                        expected_statuses={None},
                    )
                if transitioned:
                    reaped += 1
                else:
                    _log.debug("Session %s skipped (status changed before CAS lock)", sid)
            except LookupError:
                pass
            except Exception:
                _log.exception("Failed to transition null-status session %s", sid)
    except Exception:
        _log.exception("reap_null_status_sessions error")

    return reaped


# ── automatic phantom reaper ─────────────────────────────────────────────────


async def reap_phantom_sessions(
    *,
    stale_hours: float | None = None,
    actor: str = "studio_lifecycle_reaper",
) -> int:
    """Transition phantom sessions to ``failed`` via StateDB.update_status() — no DELETE.

    Uses ``admin_svc.list_phantom_sessions()`` for detection.
    Returns count transitioned.
    """
    from lionagi.studio.config import PHANTOM_STALE_HOURS

    if stale_hours is None:
        stale_hours = PHANTOM_STALE_HOURS

    if not DEFAULT_DB_PATH.exists():
        return 0

    phantoms = await admin_svc.list_phantom_sessions(stale_hours=stale_hours)
    if not phantoms:
        return 0

    now = time.time()
    reaped = 0

    for phantom in phantoms:
        sid = phantom["session_id"]
        phantom_reason = phantom.get("reason", "process_dead")
        reason_code = _PHANTOM_REASON_CODES.get(
            phantom_reason, SessionReasons.HEALTH_ORPHANED_NO_PROCESS
        )
        try:
            async with StateDB() as db:
                current = await db.get_session(sid)
                if current is None:
                    continue
                if current.get("status") != "running":
                    # Already transitioned by another path.
                    continue
                if current.get("ended_at") is None:
                    await db.update_session(sid, ended_at=now)
                if current.get("agent_name") == "claude-code":
                    # A mirrored external session has no lionagi process, so the
                    # phantom model misfires: an idle transcript is a normal
                    # completion, not a crash. Reap it to completed (the same
                    # reason its own idle-reconcile uses), never to failed.
                    transitioned = await db.update_status(
                        "session",
                        sid,
                        new_status="completed",
                        reason_code=RunReasons.COMPLETED_OK,
                        reason_summary="mirror_idle_reaped",
                        source="system",
                        actor=actor,
                        metadata={"mirror_idle_reaped": True},
                        expected_statuses={"running"},
                    )
                else:
                    transitioned = await db.update_status(
                        "session",
                        sid,
                        new_status="failed",
                        reason_code=reason_code,
                        reason_summary="phantom_reaped",
                        evidence_refs=[
                            {
                                "kind": "phantom_classification",
                                "reason": phantom_reason,
                                "session_id": sid,
                            }
                        ],
                        source="system",
                        actor=actor,
                        metadata={"phantom_reaped": True, "phantom_reason": phantom_reason},
                        expected_statuses={"running"},
                    )
            if transitioned:
                _log.info("Phantom session %s reaped (reason=%s)", sid, phantom_reason)
                reaped += 1
            else:
                _log.debug("Session %s skipped (status changed before CAS lock)", sid)
        except LookupError:
            pass
        except Exception:
            _log.exception("Failed to reap phantom session %s", sid)

    return reaped


# ── Startup + periodic entry points ──────────────────────────────────────────


async def run_startup_reconciliation() -> dict[str, int]:
    """One-shot reconciliation called on Studio startup.

    Runs all three reapers so stale rows left from an unclean shutdown are
    cleaned up before the scheduler begins firing new invocations.
    """
    results: dict[str, int] = {}
    try:
        results["phantom_sessions"] = await reap_phantom_sessions()
    except Exception:
        _log.exception("Startup phantom reaper failed")
        results["phantom_sessions"] = 0
    try:
        results["null_status_sessions"] = await reap_null_status_sessions()
    except Exception:
        _log.exception("Startup null-status reaper failed")
        results["null_status_sessions"] = 0
    try:
        results["stale_invocations"] = await reap_stale_invocations()
    except Exception:
        _log.exception("Startup invocation reaper failed")
        results["stale_invocations"] = 0
    if any(v for v in results.values()):
        _log.info("Startup reconciliation: %s", results)
    return results


async def run_periodic_reapers(now: float | None = None) -> dict[str, int]:
    """Periodic lifecycle maintenance; called from the scheduler tick.

    Identical to ``run_startup_reconciliation()`` — the throttling is
    handled by the caller (``SchedulerEngine._tick``).
    """
    _ = now  # reserved for future rate-limiting based on wall clock
    results: dict[str, int] = {}
    try:
        results["phantom_sessions"] = await reap_phantom_sessions()
    except Exception:
        _log.exception("Periodic phantom reaper failed")
        results["phantom_sessions"] = 0
    try:
        results["null_status_sessions"] = await reap_null_status_sessions()
    except Exception:
        _log.exception("Periodic null-status reaper failed")
        results["null_status_sessions"] = 0
    try:
        results["stale_invocations"] = await reap_stale_invocations()
    except Exception:
        _log.exception("Periodic invocation reaper failed")
        results["stale_invocations"] = 0
    return results


async def get_phantom_count(*, stale_hours: float | None = None) -> int:
    """Return current phantom session count for dashboard health data."""
    from lionagi.studio.config import PHANTOM_STALE_HOURS

    if stale_hours is None:
        stale_hours = PHANTOM_STALE_HOURS
    try:
        phantoms = await admin_svc.list_phantom_sessions(stale_hours=stale_hours)
        return len(phantoms)
    except Exception:
        _log.exception("get_phantom_count error")
        return 0
