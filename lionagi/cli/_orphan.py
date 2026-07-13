# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Orphan detection for CLI-launched sessions.

A session's launcher (a terminal, a harness, a parent CLI process) can die
while the session it started is still recorded as ``running`` — the child
process is killed or stranded, and the row never receives its own terminal
write. This module classifies a session's recorded launcher-process
liveness from the pid markers every `li agent` / orchestration session
already stamps into ``node_metadata`` (see ``cli/kill.py::current_pid_markers``),
and sweeps confirmed-dead rows to a terminal status with a canonical reason
code and recovery-capability evidence, instead of leaving them stuck at
``running`` forever.

No new persisted status is introduced. A confirmed-dead session moves
``running`` -> ``failed`` with ``reason_code=RunReasons.FAILED_ORPHANED_PARENT``;
callers that want a human-facing "orphaned" label read it back via
``lionagi.cli.monitor``'s read-time projection, not a stored column value —
raw SQL / other consumers see the honest persisted status plus reason code.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import psutil

from lionagi.state.reasons import RunReasons

from ._util import pid_alive as _pid_alive

_logger = logging.getLogger(__name__)

__all__ = (
    "ORPHAN_PID_CREATE_TIME_TOLERANCE",
    "extract_pid_identity",
    "recovery_capability",
    "session_process_liveness",
    "sweep_orphaned_sessions",
)

# Clock-tick rounding tolerance for process-creation-time comparison —
# guards against a recycled pid (same integer, different process) reading
# as "the same launcher" (CWE-362). Mirrors the tolerance already used by
# lionagi.studio.services.admin.process_liveness().
ORPHAN_PID_CREATE_TIME_TOLERANCE = 1.0

# Sessions are swept in one pass over a single upfront read rather than
# paginated: pagination against a `status = 'running'` filter that this
# same sweep is actively shrinking (by transitioning rows out of
# 'running') would skip rows as later pages shift under the mutation. A
# single bounded read avoids that hazard; this is a housekeeping sweep,
# not a hot path, so one large page is the simpler-and-correct choice.
_SWEEP_ROW_LIMIT = 10_000


def extract_pid_identity(node_metadata: Any) -> tuple[int | None, float | None]:
    """Pull ``(pid, pid_create_time)`` out of a session's ``node_metadata``.

    Tolerates a JSON-encoded string (raw ``text()`` query rows on SQLite)
    or a missing/malformed value; never raises.
    """
    meta = node_metadata
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError):
            meta = None
    if not isinstance(meta, dict):
        return None, None

    pid: int | None = None
    raw_pid = meta.get("pid")
    if raw_pid is not None:
        try:
            pid = int(raw_pid)
        except (TypeError, ValueError):
            pid = None

    create_time: float | None = None
    raw_ct = meta.get("pid_create_time")
    if isinstance(raw_ct, int | float):
        create_time = float(raw_ct)

    return pid, create_time


def session_process_liveness(node_metadata: Any) -> bool | None:
    """Tri-state liveness for a session's recorded launcher process.

    ``True`` = observed alive; ``False`` = confirmed dead (positive
    evidence: a recorded pid that no longer resolves, resolves to a
    zombie, or resolves to a different process per its recorded creation
    time); ``None`` = unknown (no recorded pid — a session that predates
    pid-marker recording, or one mirrored in from an externally-driven
    source that never carried a launcher pid). Unknown liveness is never
    treated as positive evidence of death; callers must never
    orphan-terminalize a ``None`` result.

    ``pid <= 1`` is never dereferenced with a real liveness probe — a
    corrupt or mocked ``node_metadata`` row must not drive a signal at
    init (pid 1) or an unowned process group.
    """
    pid, create_time = extract_pid_identity(node_metadata)
    if pid is None or pid <= 1:
        return None
    if not _pid_alive(pid):
        return False
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False
        if create_time is not None:
            actual = proc.create_time()
            if abs(actual - create_time) > ORPHAN_PID_CREATE_TIME_TOLERANCE:
                return False  # pid recycled; the recorded launcher is gone
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except Exception:  # noqa: BLE001
        # Best-effort: the pid_alive() check above already passed, so an
        # unreadable status/create-time reads as alive rather than
        # crashing the sweep on a transient psutil failure.
        _logger.debug(
            "psutil follow-up liveness check failed for pid=%s; treating as alive",
            pid,
            exc_info=True,
        )
    return True


async def recovery_capability(session_id: str) -> tuple[str, str | None]:
    """Return ``(capability, resume_command)`` for a terminalized session.

    ``"checkpoint_resume"`` when the session's own flow run recorded a
    checkpoint (its ``node_metadata.run_id`` resolves to a
    ``checkpoint.json``); ``"rerun_only"`` otherwise — there is no resume
    frontier for a bare agent/fanout/play run today. Never raises; a
    missing or unreadable checkpoint degrades to ``rerun_only``.
    """
    from .orchestrate._checkpoint import FlowResumeError, resolve_checkpoint_target

    try:
        await resolve_checkpoint_target(session_id)
    except FlowResumeError:
        return "rerun_only", None
    return "checkpoint_resume", f"li o flow --resume {session_id}"


_ORPHAN_REASON_SUMMARY = (
    "Session orphaned: recorded launcher process (pid={pid}) is confirmed dead."
)


async def sweep_orphaned_sessions(db: Any, *, now: float | None = None) -> dict[str, int]:
    """Terminalize sessions whose recorded launcher pid is confirmed dead.

    Scans ``status = 'running'`` sessions, classifies each recorded pid's
    liveness (tri-state; ``True``/``None`` rows are never touched), and
    transitions a confirmed-dead row to ``failed`` with
    ``reason_code=RunReasons.FAILED_ORPHANED_PARENT``, recording the
    classification evidence (pid, pid_create_time, recovery capability)
    on the transition.

    Every write is CAS-guarded (``expected_statuses={"running"}`` plus
    ``expected_updated_at`` pinned to the row's own snapshot) so a session
    that reaches its own terminal status between the read and this write
    — including one that legitimately finishes in the race window — is
    never overwritten; the guarded write loses that race silently, the
    same shape every other reaper in this codebase already uses. Because
    the underlying pid check is *positive* dead-evidence (a confirmed
    absent process, not a staleness inference), no additional wait/grace
    window is applied before acting — the conservative element here is
    the per-write CAS guard, not a delay.

    Returns counts: ``scanned``, ``orphaned``, ``skipped_alive``,
    ``skipped_unknown``, ``skipped_race``.
    """
    from lionagi.state.db import TransitionRejectedError

    ts = now if now is not None else time.time()
    counts = {
        "scanned": 0,
        "orphaned": 0,
        "skipped_alive": 0,
        "skipped_unknown": 0,
        "skipped_race": 0,
    }

    rows = await db.list_sessions(status="running", limit=_SWEEP_ROW_LIMIT)
    for row in rows:
        counts["scanned"] += 1
        liveness = session_process_liveness(row.get("node_metadata"))
        if liveness is not False:
            if liveness is None:
                counts["skipped_unknown"] += 1
            else:
                counts["skipped_alive"] += 1
            continue

        pid, create_time = extract_pid_identity(row.get("node_metadata"))
        capability, resume_command = await recovery_capability(row["id"])
        evidence = {
            "kind": "orphan_evidence",
            "label": f"recovery={capability}" + (f" ({resume_command})" if resume_command else ""),
            "pid": pid,
            "pid_create_time": create_time,
            "recovery_capability": capability,
            "resume_command": resume_command,
            "classified_at": ts,
        }
        try:
            applied = await db.update_status(
                "session",
                row["id"],
                new_status="failed",
                reason_code=RunReasons.FAILED_ORPHANED_PARENT,
                reason_summary=_ORPHAN_REASON_SUMMARY.format(pid=pid),
                evidence_refs=[evidence],
                source="system",
                actor="orphan_sweep",
                expected_statuses={"running"},
                expected_updated_at=row.get("updated_at"),
            )
        except TransitionRejectedError:
            # The row went terminal by some other path between our read
            # and this write (e.g. its own teardown raced us) — nothing
            # left to do; not an error.
            counts["skipped_race"] += 1
            continue
        if applied:
            counts["orphaned"] += 1
        else:
            # CAS lost: the row changed under us (most likely finishing
            # legitimately) — never counted as orphaned.
            counts["skipped_race"] += 1

    return counts
