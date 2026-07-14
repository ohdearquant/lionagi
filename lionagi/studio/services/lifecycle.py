# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Studio self-healing lifecycle reapers — write through StateDB.update_status()."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from lionagi.state.db import DEFAULT_DB_PATH, StateDB
from lionagi.state.reasons import RunReasons, SessionReasons, ShowReasons

from . import admin as admin_svc
from .admin import _artifacts_path, _ps_snapshot, process_liveness
from .shows import _SHOW_TERMINAL_STATUSES, _play_dirs
from .shows import _read_json as _read_show_json

_log = logging.getLogger(__name__)

# Phantom PhantomReason → SessionReasons code (mirrors admin._PHANTOM_REASON_CODES).
_PHANTOM_REASON_CODES: dict[str, str] = {
    "process_dead": SessionReasons.HEALTH_PHANTOM_PROCESS_DEAD,
    "missing_artifacts": SessionReasons.HEALTH_PHANTOM_MISSING_ARTIFACTS,
    "stale_lock": SessionReasons.HEALTH_ZOMBIE_STALE_LOCKS,
}

# Dead-runner-in-flight play statuses. Deliberately EXCLUDES:
#   gated   — a paused gate is legitimately long-lived, never reap
#   pending — queued, may be waiting on depends_on; not an in-flight crash
_REAPABLE_PLAY_STATUSES = frozenset({"running", "running_complete", "prepared", "redoing"})


# ── invocation deadline + zero-session reaper ────────────────────────────────


def _deadline_for_kind(action_kind: str | None, global_default: int) -> int:
    """Resolve the effective deadline: checks
    ``LIONAGI_STUDIO_INVOCATION_DEADLINE_<KIND>_SECONDS`` first, falling back
    to *global_default* when absent or *action_kind* is None.
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


# ── play-level staleness reaper ──────────────────────────────────────────────


async def reap_stale_plays(*, stale_hours: float | None = None) -> int:
    """Transition dead-runner in-flight plays to ``blocked``.

    Keys on the plays table's own ``status`` + ``updated_at`` rather than a
    session join — ``db_maintenance`` severs ``session_id`` to NULL on
    session prune, so an orphaned play row must still be reachable by its own
    status. Liveness honors the child session's recorded process (when one is
    still linked) before falling back to a staleness backstop, mirroring
    ``reap_null_status_sessions``.
    """
    from lionagi.studio.config import PLAY_STALE_HOURS

    if stale_hours is None:
        stale_hours = PLAY_STALE_HOURS
    stale_seconds = stale_hours * 3600

    if not DEFAULT_DB_PATH.exists():
        return 0

    now = time.time()
    reaped = 0

    try:
        async with StateDB() as db:
            placeholders = ",".join("?" * len(_REAPABLE_PLAY_STATUSES))
            candidates = await db.fetch_all(
                f"SELECT id FROM plays WHERE status IN ({placeholders})",  # noqa: S608
                tuple(sorted(_REAPABLE_PLAY_STATUSES)),
            )

        ps_snapshot: str | None = None
        for cand in candidates:
            play_id = cand["id"]
            try:
                async with StateDB() as db:
                    # Re-read fresh immediately before deciding. A play can be
                    # legitimately claimed between the scan and here — set to
                    # running, given a live child session, and refreshed to
                    # updated_at=now. The status-membership CAS below would
                    # still overwrite it (running is a reapable status), so the
                    # full stale predicate is revalidated against current state.
                    row = await db.fetch_one(
                        "SELECT status, session_id, started_at, updated_at, ended_at "
                        "FROM plays WHERE id = ?",
                        (play_id,),
                    )
                    if row is None or row["status"] not in _REAPABLE_PLAY_STATUSES:
                        continue

                    session_id = row.get("session_id")
                    # Liveness FIRST: a play whose child session process is
                    # still alive is never reaped on staleness alone.
                    if session_id:
                        srow = await db.fetch_one(
                            "SELECT id, artifacts_path, node_metadata FROM sessions WHERE id = ?",
                            (session_id,),
                        )
                        if srow is not None:
                            if ps_snapshot is None:
                                ps_snapshot = _ps_snapshot()
                            session = {"id": srow["id"], "node_metadata": srow.get("node_metadata")}
                            if (
                                process_liveness(session, _artifacts_path(srow), ps_snapshot)
                                is True
                            ):
                                continue

                    updated_at_raw = row.get("updated_at")
                    updated_at = updated_at_raw or row.get("started_at") or 0.0
                    if now - updated_at < stale_seconds:
                        # Not confirmed alive, but too fresh to reap — benefit of the doubt.
                        continue

                    _log.info(
                        "Reaping stale play %s: status=%s, session_id=%s",
                        play_id,
                        row["status"],
                        session_id,
                    )
                    # expected_updated_at pins the transition to the exact row
                    # version we validated: a claim landing between this read
                    # and the write bumps updated_at, so the guarded write loses
                    # the race and we skip rather than block a live play.
                    transitioned = await db.update_status(
                        "play",
                        play_id,
                        new_status="blocked",
                        reason_code=RunReasons.CANCELLED_STALE_AUTO,
                        reason_summary="play_runner_dead_or_orphaned",
                        evidence_refs=[{"kind": "play", "id": play_id}],
                        source="system",
                        actor="studio_lifecycle_reaper",
                        metadata={
                            "detector": "stale_play_reaper",
                            "prior_status": row["status"],
                            "session_id": session_id,
                            "updated_at": updated_at,
                        },
                        expected_statuses=_REAPABLE_PLAY_STATUSES,
                        expected_updated_at=updated_at_raw,
                    )
                    if transitioned:
                        # Stamp ended_at only after the guarded transition wins,
                        # so a lost CAS never mutates a row we did not reap.
                        if row.get("ended_at") is None:
                            await db.update_play(play_id, ended_at=now)
                        reaped += 1
                    else:
                        _log.debug("Play %s skipped (status changed before CAS lock)", play_id)
            except LookupError:
                pass
            except Exception:
                _log.exception("Failed to reap stale play %s", play_id)
    except Exception:
        _log.exception("reap_stale_plays error")

    return reaped


# ── schedule_run staleness reaper ─────────────────────────────────────────────


async def reap_stale_schedule_runs(*, stale_hours: float | None = None) -> int:
    """Transition ``schedule_runs`` rows stuck at ``status="running"`` to
    ``timed_out``.

    A schedule_run row is created and its owning schedule's cursor
    (``next_fire_at``/``github_cursor``) advanced atomically in the same
    transaction (``StateDB.create_schedule_run_and_advance``), so by the
    time this row is durable the scheduler has already committed to firing
    it -- but the process can still die anywhere between that commit and
    the run's own terminal write (mid-spawn, or before
    ``update_schedule_run``'s exit-code write lands), leaving the row
    orphaned at ``running`` forever. ``count_schedule_runs()`` already
    excludes ``running`` from budget bookkeeping, so these rows are
    harmless for max_runs, but they are audit-trail zombies that (unlike
    stale sessions/plays) have no process-liveness signal to check against
    -- the "process" here is the scheduler daemon itself, and its own
    restart is what triggers reaping. This is a pure wall-clock deadline
    against the row's own ``updated_at`` (falling back to ``fired_at`` for
    a row that was never otherwise touched), mirroring
    ``reap_stale_invocations``'s deadline condition, with the version guard
    (``expected_updated_at``) revalidating the row hasn't moved between the
    scan and the write -- the same optimistic-lock pattern
    ``reap_stale_plays`` uses.

    Scoped to ``schedule_id IS NOT NULL`` -- scheduler-fired occurrence
    rows only. schedule_runs also backs the ad-hoc task queue (schedule_id
    IS NULL, claimed via a lease: ``leased_by``/``lease_expires_at``/
    ``lease_attempts``), which has its own recovery loop and policy
    (``worker.reap_expired_leases``, run every ``worker_tick``): a task
    whose lease is still live but has been running longer than this
    reaper's stale window would otherwise get marked ``timed_out`` here
    before the lease even expires, bypassing the lease's own
    requeue/retry-budget semantics entirely. Excluding those rows leaves
    them exclusively to the lease reaper.
    """
    from lionagi.studio.config import SCHEDULE_RUN_STALE_HOURS

    if stale_hours is None:
        stale_hours = SCHEDULE_RUN_STALE_HOURS
    stale_seconds = stale_hours * 3600

    if not DEFAULT_DB_PATH.exists():
        return 0

    now = time.time()
    deadline_cutoff = now - stale_seconds
    reaped = 0

    try:
        async with StateDB() as db:
            rows = await db.fetch_all(
                "SELECT id, fired_at, updated_at FROM schedule_runs "
                "WHERE status = 'running' AND schedule_id IS NOT NULL"
            )
            for row in rows:
                run_id = row["id"]
                fired_at = row.get("fired_at") or now
                updated_at_raw = row.get("updated_at")
                reference = updated_at_raw if updated_at_raw is not None else fired_at
                if reference >= deadline_cutoff:
                    continue

                _log.info(
                    "Reaping stale schedule_run %s: running past deadline (%.1fh)",
                    run_id,
                    stale_hours,
                )
                try:
                    transitioned = await db.update_status(
                        "schedule_run",
                        run_id,
                        new_status="timed_out",
                        reason_code=RunReasons.TIMED_OUT_DEADLINE,
                        reason_summary="schedule_run_stale_running_reaped",
                        evidence_refs=[{"kind": "schedule_run", "id": run_id}],
                        source="system",
                        actor="studio_lifecycle_reaper",
                        metadata={
                            "stale_hours": stale_hours,
                            "fired_at": fired_at,
                            "reference": reference,
                        },
                        expected_statuses={"running"},
                        expected_updated_at=updated_at_raw,
                    )
                    if transitioned:
                        reaped += 1
                    else:
                        _log.debug(
                            "schedule_run %s skipped (status changed before CAS lock)", run_id
                        )
                except LookupError:
                    pass
    except Exception:
        _log.exception("reap_stale_schedule_runs error")

    return reaped


# ── show-level staleness reaper ──────────────────────────────────────────────

# Non-terminal show statuses (the complement of `_SHOW_TERMINAL_STATUSES`).
# Kept as its own curated set — like `_REAPABLE_PLAY_STATUSES` — so the CAS
# `expected_statuses` guard below only matches the exact statuses this
# reaper is willing to move out of, rather than importing the full status
# vocabulary just to subtract the terminal set at call time.
_REAPABLE_SHOW_STATUSES = frozenset({"active", "imported"})


def _recompute_show_status_from_disk(show_dir: Path) -> tuple[str, str, str] | None:
    """Re-derive a show's terminal status from on-disk play/verdict evidence.

    Mirrors the rules ``shows.import_shows()`` applies once, at mirror-row
    creation time: an ``_ABORT`` marker means aborted; a passing
    ``_final_verdict.json`` means completed; every child play reaching
    ``merged`` (with at least one play) also means completed. Any other
    on-disk state is still genuinely in flight, so this returns ``None`` and
    the caller skips the show.

    Returns ``(new_status, reason_code, reason_summary)`` or ``None``.
    """
    if (show_dir / "_ABORT").exists():
        return (
            "aborted",
            ShowReasons.ABORTED_OPERATOR,
            "Show directory carries an operator abort marker.",
        )

    final_verdict = _read_show_json(show_dir / "_final_verdict.json")
    if final_verdict and final_verdict.get("show_passed"):
        return (
            "completed",
            ShowReasons.COMPLETED_FINAL_GATE,
            "Show has a passing final gate verdict.",
        )

    metas = [_read_show_json(p / "_meta.json") or {} for p in _play_dirs(show_dir)]
    statuses = [m.get("status", "pending") for m in metas]
    if statuses and all(s == "merged" for s in statuses):
        return (
            "completed",
            ShowReasons.COMPLETED_ALL_PLAYS_MERGED,
            "All child plays reached merged status.",
        )

    return None


async def reap_stale_shows(*, stale_hours: float | None = None) -> int:
    """Recompute a stale non-terminal show's status from its plays' state.

    ``shows.py`` computes ``show_status`` only once, at mirror-row creation
    time (``import_shows()``): a show mirrored while its plays are still
    in flight gets ``status="active"`` and the row is never re-evaluated
    once those plays later merge or abort on disk — there is no periodic
    re-derivation, unlike sessions/plays/invocations/schedule_runs, which
    all have their own reapers. This fills that gap using the exact same
    on-disk rules ``import_shows()`` already applies (see
    ``_recompute_show_status_from_disk``).

    Liveness-first, like ``reap_stale_plays``: a show with any child play
    whose session process is still observably alive is never reaped,
    regardless of the on-disk snapshot or how stale the row looks.
    """
    from lionagi.studio.config import SHOW_STALE_HOURS

    if stale_hours is None:
        stale_hours = SHOW_STALE_HOURS
    stale_seconds = stale_hours * 3600

    if not DEFAULT_DB_PATH.exists():
        return 0

    now = time.time()
    reaped = 0

    try:
        async with StateDB() as db:
            placeholders = ",".join("?" * len(_SHOW_TERMINAL_STATUSES))
            candidates = await db.fetch_all(
                f"SELECT id FROM shows WHERE status NOT IN ({placeholders})",  # noqa: S608
                tuple(sorted(_SHOW_TERMINAL_STATUSES)),
            )

        ps_snapshot: str | None = None
        for cand in candidates:
            show_id = cand["id"]
            try:
                async with StateDB() as db:
                    # Re-read fresh immediately before deciding — a show can
                    # be legitimately re-touched between the scan and here.
                    row = await db.fetch_one(
                        "SELECT id, status, show_dir, updated_at FROM shows WHERE id = ?",
                        (show_id,),
                    )
                    if row is None or row["status"] not in _REAPABLE_SHOW_STATUSES:
                        continue

                    updated_at_raw = row.get("updated_at")
                    updated_at = updated_at_raw or 0.0
                    if now - updated_at < stale_seconds:
                        # Not confirmed alive, but too fresh to reap.
                        continue

                    play_rows = await db.fetch_all(
                        "SELECT id, session_id FROM plays WHERE show_id = ?",
                        (show_id,),
                    )
                    live = False
                    for prow in play_rows:
                        session_id = prow.get("session_id")
                        if not session_id:
                            continue
                        srow = await db.fetch_one(
                            "SELECT id, artifacts_path, node_metadata FROM sessions WHERE id = ?",
                            (session_id,),
                        )
                        if srow is None:
                            continue
                        if ps_snapshot is None:
                            ps_snapshot = _ps_snapshot()
                        session = {"id": srow["id"], "node_metadata": srow.get("node_metadata")}
                        if process_liveness(session, _artifacts_path(srow), ps_snapshot) is True:
                            live = True
                            break
                    if live:
                        # A live child play process is never reaped on
                        # staleness alone.
                        continue

                    show_dir_raw = row.get("show_dir")
                    if not show_dir_raw:
                        continue
                    show_dir = Path(show_dir_raw)
                    for play_dir in _play_dirs(show_dir):
                        try:
                            play_pid = int((play_dir / ".pid").read_text().strip())
                        except (OSError, ValueError):
                            continue
                        if play_pid <= 1:
                            continue
                        session = {"id": "", "node_metadata": {"pid": play_pid}}
                        if process_liveness(session, None, ps_snapshot) is True:
                            live = True
                            break
                    if live:
                        # The play PID is written before its session is linked.
                        continue

                    recomputed = _recompute_show_status_from_disk(show_dir)
                    if recomputed is None:
                        # Still genuinely in flight on disk — nothing to reap.
                        continue
                    new_status, reason_code, reason_summary = recomputed

                    _log.info(
                        "Reaping stale show %s: status=%s -> %s",
                        show_id,
                        row["status"],
                        new_status,
                    )
                    # expected_updated_at pins the transition to the exact row
                    # version validated above: a claim/re-touch landing
                    # between this read and the write bumps updated_at, so
                    # the guarded write loses the race and we skip rather
                    # than clobber a show that moved.
                    transitioned = await db.update_status(
                        "show",
                        show_id,
                        new_status=new_status,
                        reason_code=reason_code,
                        reason_summary=reason_summary,
                        evidence_refs=[{"kind": "show", "id": show_id}],
                        source="system",
                        actor="studio_lifecycle_reaper",
                        metadata={
                            "detector": "stale_show_reaper",
                            "prior_status": row["status"],
                            "updated_at": updated_at,
                        },
                        expected_statuses=_REAPABLE_SHOW_STATUSES,
                        expected_updated_at=updated_at_raw,
                    )
                    if transitioned:
                        reaped += 1
                    else:
                        _log.debug("Show %s skipped (status changed before CAS lock)", show_id)
            except LookupError:
                pass
            except Exception:
                _log.exception("Failed to reap stale show %s", show_id)
    except Exception:
        _log.exception("reap_stale_shows error")

    return reaped


# ── Startup + periodic entry points ──────────────────────────────────────────


async def run_startup_reconciliation() -> dict[str, int]:
    """One-shot reconciliation called on Studio startup.

    Runs all reapers so stale rows left from an unclean shutdown are
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
    try:
        results["stale_plays"] = await reap_stale_plays()
    except Exception:
        _log.exception("Startup play reaper failed")
        results["stale_plays"] = 0
    try:
        results["stale_shows"] = await reap_stale_shows()
    except Exception:
        _log.exception("Startup show reaper failed")
        results["stale_shows"] = 0
    try:
        results["stale_schedule_runs"] = await reap_stale_schedule_runs()
    except Exception:
        _log.exception("Startup schedule_run reaper failed")
        results["stale_schedule_runs"] = 0
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
    try:
        results["stale_plays"] = await reap_stale_plays()
    except Exception:
        _log.exception("Periodic play reaper failed")
        results["stale_plays"] = 0
    try:
        results["stale_shows"] = await reap_stale_shows()
    except Exception:
        _log.exception("Periodic show reaper failed")
        results["stale_shows"] = 0
    try:
        results["stale_schedule_runs"] = await reap_stale_schedule_runs()
    except Exception:
        _log.exception("Periodic schedule_run reaper failed")
        results["stale_schedule_runs"] = 0
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
