# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li kill` — terminate in-progress lionagi runs/sessions/plays/shows.

When an agent, play, or orchestrated flow is killed externally (Ctrl-C in
the wrong terminal, ``kill PID``, OS restart), the state DB is left with
``status=running`` rows whose underlying processes are dead.  Studio shows
them forever as "still running".  ``li kill`` is the operator-explicit
recovery path.

Usage:
    li kill <id>                    # one entity by id (prefix or full UUID)
    li kill <id> --reason "text"    # with a custom reason message
    li kill <id> --recursive        # kill entity + every child invocation
    li kill --all-stale             # sweep all running rows with dead PIDs
    li kill --all-stale --threshold 3600   # stale = started > 1h ago

Entity types resolved from id: session, invocation, play, show.
"""

from __future__ import annotations

import argparse
import os
import signal
import time
from typing import Any

from ._logging import log_error, warn

# ── PID probing ────────────────────────────────────────────────────────────────


def _pid_alive(pid: int) -> bool:
    """Return True iff *pid* is a live OS process.

    Uses ``os.kill(pid, 0)`` — sends no actual signal; raises
    ``ProcessLookupError`` (no such process) or ``PermissionError``
    (process exists but not ours to signal).  Both mean the pid is
    either dead or owned by another user; callers treat ``PermissionError``
    as "alive" because the process IS running, just not killable by us.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists; we can't signal it, but it is alive.
        return True


def _read_pid_from_entity(entity: dict[str, Any]) -> int | None:
    """Extract the OS PID from an entity row.

    Check ``node_metadata.pid`` (written by the CLI executor at session-open
    time) and the artifact dir ``.pid`` file (written by some play runners).
    """
    meta = entity.get("node_metadata") or {}
    if isinstance(meta, dict):
        raw_pid = meta.get("pid")
        if raw_pid is not None:
            try:
                return int(raw_pid)
            except (TypeError, ValueError):
                pass

    # Fall back to the artifacts-dir .pid file.
    artifacts_path = entity.get("artifacts_path")
    if artifacts_path:
        pid_file = os.path.join(artifacts_path, ".pid")
        try:
            text = open(pid_file).read().strip()  # noqa: WPS515
            return int(text)
        except (OSError, ValueError):
            pass

    return None


# ── Signal / terminate ─────────────────────────────────────────────────────────


# Tolerance (seconds) when comparing a recorded process start time against the
# live process's create_time. Clocks and rounding differ slightly between the
# launcher and psutil; 2s is comfortably tighter than any PID-recycle window.
_CREATE_TIME_TOLERANCE = 2.0


def _cmdline_is_lionagi(cmdline: list[str], expected_cmd: str) -> bool:
    """Return True iff *cmdline* is genuinely a lionagi CLI invocation.

    Exact-token match, NOT substring. A broad ``expected_cmd in part`` check
    matches any unrelated process whose path or arguments merely *mention*
    lionagi — e.g. ``vim /Users/lion/projects/lionagi/README.md`` — which would
    let ``li kill`` signal a recycled, unrelated PID (CWE-362).

    A process counts as lionagi only when:
      * its executable basename is the console entrypoint (``li``) or
        ``expected_cmd`` itself, or
      * it is run as ``python -m lionagi`` / ``python -m lionagi.<submodule>``.

    A path component (``.../lionagi/README.md``) is deliberately not a match.
    """
    if not cmdline:
        return False
    exe = os.path.basename(cmdline[0])
    if exe in ("li", expected_cmd):
        return True
    for flag, mod in zip(cmdline, cmdline[1:], strict=False):
        if flag == "-m" and (mod == expected_cmd or mod.startswith(expected_cmd + ".")):
            return True
    return False


def _check_pid_identity(
    pid: int,
    expected_cmd: str,
    *,
    expected_session_id: str | None = None,
    expected_create_time: float | None = None,
) -> bool:
    """Return True iff the live process at *pid* is the lionagi run we recorded.

    Uses psutil when available.  If psutil is not installed, logs a warning
    and returns False so the caller skips the kill rather than risk signalling
    a recycled PID (CWE-362).

    Identity is established in order of decreasing certainty:

    1. ``create_time`` — when *expected_create_time* was recorded at launch, a
       mismatch means the PID was recycled to a different process; reject
       outright. This is the decisive defense against PID reuse.
    2. ``LIONAGI_SESSION_ID`` env marker — a lionagi run started by the CLI
       carries its session id in the environment (see the orchestrate
       background launcher). When *expected_session_id* is given and the live
       process exposes a matching marker, that is a definitive match; a
       *different* marker means another lionagi run now holds this PID — reject.
       An absent marker is inconclusive, so fall through.
    3. cmdline — exact-token match that the process really is a lionagi CLI
       invocation (see ``_cmdline_is_lionagi``).
    """
    try:
        import psutil  # optional dependency
    except ImportError:
        import logging

        logging.getLogger("lionagi.cli").warning(
            "psutil unavailable — skipping kill of pid %d to avoid PID-reuse race; "
            "install psutil (pip install psutil) to enable identity checks",
            pid,
        )
        return False

    try:
        proc = psutil.Process(pid)
        # create_time gate — recycled PID has a different start time.
        if expected_create_time is not None:
            if abs(proc.create_time() - expected_create_time) > _CREATE_TIME_TOLERANCE:
                return False
        # session-marker gate — exact per-run identity when accessible.
        if expected_session_id is not None:
            try:
                marker = proc.environ().get("LIONAGI_SESSION_ID")
            except (psutil.AccessDenied, NotImplementedError):
                marker = None  # can't read env; fall through to cmdline
            if marker is not None:
                return marker == expected_session_id
        cmdline = proc.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False

    return _cmdline_is_lionagi(cmdline, expected_cmd)


def _terminate_pid(
    pid: int,
    grace_seconds: float = 5.0,
    expected_cmd: str | None = None,
    *,
    expected_session_id: str | None = None,
    expected_create_time: float | None = None,
) -> str:
    """SIGTERM → wait → SIGKILL.  Returns "sigterm", "sigkill", "already_dead", or "identity_mismatch".

    If the process doesn't exist at all, returns "already_dead".
    If *expected_cmd* is given, verifies the live process is the lionagi run we
    recorded (see ``_check_pid_identity``) before signalling; returns
    "identity_mismatch" (and skips the kill) if the check fails or psutil is
    unavailable — prevents PID-reuse races (CWE-362).
    If SIGTERM is enough, returns "sigterm".  If the grace period expires
    and the process is still alive, escalates to SIGKILL and returns "sigkill".
    """
    if not _pid_alive(pid):
        return "already_dead"

    if expected_cmd is not None and not _check_pid_identity(
        pid,
        expected_cmd,
        expected_session_id=expected_session_id,
        expected_create_time=expected_create_time,
    ):
        return "identity_mismatch"

    # Phase 1: SIGTERM
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "already_dead"
    except PermissionError as exc:
        raise RuntimeError(
            f"cannot send SIGTERM to pid {pid}: {exc}. "
            "Try again as root, or mark the entity cancelled manually."
        ) from exc

    # Wait up to grace_seconds for graceful exit.
    deadline = time.monotonic() + grace_seconds
    interval = 0.1
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return "sigterm"
        time.sleep(interval)
        interval = min(interval * 2, 0.5)

    if not _pid_alive(pid):
        return "sigterm"

    # Phase 2: SIGKILL
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # Died in the gap between the last check and the SIGKILL.
        pass

    return "sigkill"


# ── DB entity resolution ───────────────────────────────────────────────────────

_SEARCH_ORDER = ("sessions", "invocations", "plays", "shows")

# Only sessions and invocations carry direct PID semantics. Plays and
# shows are orchestrators that spawn child sessions/invocations — they
# don't carry their own PID. Sweeping them by PID-absence would abort
# legitimate long-running orchestrations whose child processes are still
# alive. Use `li kill <play_or_show_id> --recursive` for explicit cleanup.
_STALE_SWEEP_ORDER = ("sessions", "invocations")

# Play statuses that are NOT terminal — the play is still in-progress.
_PLAY_ACTIVE_STATUSES = frozenset(
    {"pending", "prepared", "running", "running_complete", "gated", "redoing"}
)

# Map DB table name → canonical entity type for update_status().
_TABLE_TO_ENTITY_TYPE = {
    "sessions": "session",
    "invocations": "invocation",
    "plays": "play",
    "shows": "show",
}


async def _resolve_entity(db: Any, id_or_short: str) -> tuple[str, str, dict[str, Any]] | None:
    """Resolve an id (full UUID or prefix) to (table, entity_type, row).

    Searches sessions, invocations, plays, shows in that order.  Accepts
    prefix matches (at least 6 characters) so operators can use short IDs.
    Returns None if nothing matches.

    Uses ``db._row_to_dict`` so JSON columns (e.g. node_metadata) are
    already decoded in the returned dict.
    """
    id_or_short = id_or_short.strip()
    is_prefix = len(id_or_short) < 36

    for table in _SEARCH_ORDER:
        if is_prefix:
            cur = await db.db.execute(
                f"SELECT * FROM {table} WHERE id LIKE ?",  # noqa: S608
                (id_or_short + "%",),
            )
        else:
            cur = await db.db.execute(
                f"SELECT * FROM {table} WHERE id = ?",  # noqa: S608
                (id_or_short,),
            )
        row = await cur.fetchone()
        if row is not None:
            entity_type = _TABLE_TO_ENTITY_TYPE[table]
            return table, entity_type, db._row_to_dict(row)

    return None


async def _list_child_invocations(db: Any, session_id: str) -> list[dict[str, Any]]:
    """Return invocations linked to *session_id* that are still running."""
    cur = await db.db.execute("SELECT * FROM invocations WHERE status = 'running'")
    rows = await cur.fetchall()
    result = []
    for row in rows:
        d = dict(row)
        # invocations reference sessions via sessions.invocation_id FK
        # (a session may reference the invocation that spawned it).
        # We look at sessions that claim this invocation as their parent.
        child_cur = await db.db.execute(
            "SELECT 1 FROM sessions WHERE invocation_id = ? LIMIT 1",
            (d["id"],),
        )
        if await child_cur.fetchone() is not None:
            result.append(d)
        elif d.get("id") == session_id:
            result.append(d)
    return result


async def _list_running_children(
    db: Any, entity_type: str, entity_id: str
) -> list[tuple[str, str, dict[str, Any]]]:
    """Return list of (table, entity_type, row) for running children.

    For shows: running plays.
    For sessions: the invocation that spawned this session (linked via
    sessions.invocation_id) if it is still running.
    For invocations: sessions spawned by this invocation.
    """
    children: list[tuple[str, str, dict[str, Any]]] = []

    if entity_type == "show":
        cur = await db.db.execute(
            "SELECT * FROM plays WHERE show_id = ? AND status = 'running'",
            (entity_id,),
        )
        for row in await cur.fetchall():
            children.append(("plays", "play", db._row_to_dict(row)))

    if entity_type == "session":
        # The session may be linked to a parent invocation.
        cur = await db.db.execute(
            "SELECT * FROM invocations "
            "WHERE status = 'running' AND id IN ("
            "  SELECT invocation_id FROM sessions "
            "  WHERE invocation_id IS NOT NULL AND id = ?"
            ")",
            (entity_id,),
        )
        for row in await cur.fetchall():
            children.append(("invocations", "invocation", db._row_to_dict(row)))

    if entity_type == "invocation":
        # sessions spawned by this invocation
        cur = await db.db.execute(
            "SELECT * FROM sessions WHERE invocation_id = ? AND status = 'running'",
            (entity_id,),
        )
        for row in await cur.fetchall():
            children.append(("sessions", "session", db._row_to_dict(row)))

    return children


# ── DB status write ────────────────────────────────────────────────────────────


async def _persist_cancel(
    db: Any,
    entity_type: str,
    entity_id: str,
    *,
    reason_code: str,
    reason_summary: str,
    evidence: dict[str, Any],
) -> None:
    """Write cancelled status + status_transition row via update_status()."""
    # Determine valid target status for this entity type.
    # Sessions/invocations accept "cancelled"; plays and shows have their
    # own terminal vocabularies.
    play_terminal = {"merged", "escalated", "gate_failed", "blocked", "aborted_after_finish"}
    if entity_type == "play":
        cur = await db.db.execute("SELECT status FROM plays WHERE id = ?", (entity_id,))
        row = await cur.fetchone()
        if row is None:
            return
        if row["status"] in play_terminal:
            return  # already terminal
        target_status = "blocked"  # plays don't have "cancelled"
    elif entity_type == "show":
        cur = await db.db.execute("SELECT status FROM shows WHERE id = ?", (entity_id,))
        row = await cur.fetchone()
        if row is None:
            return
        if row["status"] in ("completed", "aborted"):
            return
        target_status = "aborted"
    else:
        # session / invocation
        table = {
            "session": "sessions",
            "invocation": "invocations",
        }.get(entity_type, "sessions")
        cur = await db.db.execute(
            f"SELECT status FROM {table} WHERE id = ?",  # noqa: S608
            (entity_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return
        if row["status"] != "running":
            return  # already terminal
        target_status = "cancelled"

    await db.update_status(
        entity_type,
        entity_id,
        new_status=target_status,
        reason_code=reason_code,
        reason_summary=reason_summary,
        evidence_refs=[evidence],
        source="admin",  # CLI kill is an operator/admin action (ADR-0028 source vocabulary)
        actor="user",
    )


# ── Core kill logic ────────────────────────────────────────────────────────────


async def _kill_one(
    db: Any,
    entity_type: str,
    entity_id: str,
    row: dict[str, Any],
    *,
    user_reason: str,
    grace_seconds: float = 5.0,
    verbose: bool = False,
) -> dict[str, Any]:
    """Kill one entity: terminate process, persist cancellation.

    Returns a result dict: {entity_type, entity_id, signal, status_written}.
    """
    from lionagi.state.reasons import RunReasons

    pid = _read_pid_from_entity(row)
    signal_used = "no_pid"

    if pid is not None:
        # Strong identity markers recorded at launch (when available). A session
        # carries its own id as the LIONAGI_SESSION_ID env marker on the running
        # process; node_metadata may also carry the process start time.
        meta = row.get("node_metadata") if isinstance(row.get("node_metadata"), dict) else {}
        expected_session_id = entity_id if entity_type == "session" else None
        raw_ct = meta.get("pid_create_time")
        try:
            expected_create_time = float(raw_ct) if raw_ct is not None else None
        except (TypeError, ValueError):
            expected_create_time = None
        try:
            signal_used = _terminate_pid(
                pid,
                grace_seconds=grace_seconds,
                # Verify the live process is the lionagi run we recorded before
                # signalling, to prevent PID-reuse races (CWE-362).
                expected_cmd="lionagi",
                expected_session_id=expected_session_id,
                expected_create_time=expected_create_time,
            )
        except RuntimeError as exc:
            warn(str(exc))
            signal_used = "permission_denied"
    else:
        if verbose:
            warn(f"  {entity_type} {entity_id[:12]}: no PID found — skipping OS signal")

    # Decide reason code from signal result.
    if signal_used == "sigkill":
        reason_code = RunReasons.CANCELLED_FORCE_KILL
        reason_summary = f"Force-killed (SIGKILL after grace period). {user_reason}".strip()
    elif signal_used == "identity_mismatch":
        # PID belongs to a different process — log and skip state update to
        # avoid recording a false cancellation (CWE-362, issue #1126).
        warn(
            f"  {entity_type} {entity_id[:12]}: pid {pid} did not match "
            "expected lionagi process — kill skipped"
        )
        return {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "signal": signal_used,
            "pid": pid,
        }
    else:
        reason_code = RunReasons.CANCELLED_MANUAL_KILL
        reason_summary = f"Manually cancelled via `li kill`. {user_reason}".strip()

    evidence: dict[str, Any] = {
        "kind": "kill_event",
        "signal": signal_used,
        "pid": pid,
        "killed_at": time.time(),
    }
    if user_reason:
        evidence["user_reason"] = user_reason

    await _persist_cancel(
        db,
        entity_type,
        entity_id,
        reason_code=reason_code,
        reason_summary=reason_summary,
        evidence=evidence,
    )

    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "signal": signal_used,
        "pid": pid,
    }


async def _do_kill(
    id_or_short: str,
    *,
    user_reason: str = "",
    recursive: bool = False,
    grace_seconds: float = 5.0,
    verbose: bool = False,
) -> int:
    """Resolve entity, kill process, persist cancellation.  Returns exit code."""
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        resolved = await _resolve_entity(db, id_or_short)
        if resolved is None:
            log_error(f"entity not found for id: {id_or_short!r}")
            return 1

        table, entity_type, row = resolved
        current_status = row.get("status")

        if current_status != "running":
            log_error(
                f"{entity_type} {row['id'][:12]} is already in terminal state: "
                f"{current_status!r} — nothing to kill"
            )
            return 1

        results = []

        if recursive:
            children = await _list_running_children(db, entity_type, row["id"])
            for _child_table, child_type, child_row in children:
                r = await _kill_one(
                    db,
                    child_type,
                    child_row["id"],
                    child_row,
                    user_reason=user_reason,
                    grace_seconds=grace_seconds,
                    verbose=verbose,
                )
                results.append(r)
                print(f"  killed child {child_type} {child_row['id'][:12]} (signal={r['signal']})")

        r = await _kill_one(
            db,
            entity_type,
            row["id"],
            row,
            user_reason=user_reason,
            grace_seconds=grace_seconds,
            verbose=verbose,
        )
        results.append(r)
        print(f"killed {entity_type} {row['id'][:12]} (signal={r['signal']}, pid={r['pid']})")

    return 0


async def _play_child_stale(db: Any, play_row: dict[str, Any]) -> bool:
    """Return True if a play's linked session has terminated (child-derived staleness).

    A play is child-stale only when ``session_id`` is set and the linked
    session is no longer running. Plays with no session_id are not yet
    started and must not be swept.
    """
    session_id = play_row.get("session_id")
    if not session_id:
        return False
    cur = await db.db.execute("SELECT status FROM sessions WHERE id = ?", (session_id,))
    row = await cur.fetchone()
    if row is None:
        return False
    return row["status"] != "running"


async def _show_children_all_terminal(db: Any, show_id: str) -> bool:
    """Return True if a show has >= 1 child play and all are in terminal states.

    A show with no plays is not yet active and must not be swept.
    """
    cur = await db.db.execute("SELECT status FROM plays WHERE show_id = ?", (show_id,))
    rows = await cur.fetchall()
    if not rows:
        return False
    return all(row["status"] not in _PLAY_ACTIVE_STATUSES for row in rows)


async def _do_kill_all_stale(
    *,
    threshold_seconds: int,
    user_reason: str = "",
    grace_seconds: float = 5.0,
    dry_run: bool = False,
    verbose: bool = False,
) -> int:
    """Sweep stale sessions and invocations whose PIDs are dead.

    ``threshold_seconds`` is the minimum age (since started_at or updated_at)
    required before a running row is considered stale.  Rows newer than this
    threshold may be legitimately in-progress and are skipped.

    Plays and shows are NOT swept.  They are orchestrators with no direct
    PID — their child sessions/invocations carry the actual OS process.
    Sweeping them by PID-absence would silently abort legitimate long-running
    orchestrations.  Use ``li kill <play_or_show_id> --recursive`` for
    explicit play/show cleanup.
    """
    from lionagi.state.db import StateDB
    from lionagi.state.reasons import RunReasons

    cutoff = time.time() - threshold_seconds
    killed = 0
    skipped_live = 0
    skipped_recent = 0

    live_status_for: dict[str, str] = {
        "sessions": "running",
        "invocations": "running",
    }

    async with StateDB() as db:
        for table in _STALE_SWEEP_ORDER:
            entity_type = _TABLE_TO_ENTITY_TYPE[table]
            live_status = live_status_for[table]
            cur = await db.db.execute(
                f"SELECT * FROM {table} WHERE status = ?",  # noqa: S608
                (live_status,),
            )
            rows = await cur.fetchall()

            for row in rows:
                row_dict = db._row_to_dict(row)
                entity_id = row_dict["id"]

                # Age check: skip entities started/updated recently.
                started = (
                    row_dict.get("started_at")
                    or row_dict.get("updated_at")
                    or row_dict.get("created_at")
                    or 0
                )
                if started > cutoff:
                    skipped_recent += 1
                    if verbose:
                        print(
                            f"  skip {entity_type} {entity_id[:12]}: "
                            f"started recently (< {threshold_seconds}s ago)"
                        )
                    continue

                pid = _read_pid_from_entity(row_dict)
                if pid is not None and _pid_alive(pid):
                    skipped_live += 1
                    if verbose:
                        print(
                            f"  skip {entity_type} {entity_id[:12]}: process {pid} is still alive"
                        )
                    continue

                # Stale: process is dead or no PID recorded.
                if dry_run:
                    print(
                        f"  (dry-run) would cancel {entity_type} {entity_id[:12]} "
                        f"(pid={pid}, started_at={started:.0f})"
                    )
                    killed += 1
                    continue

                evidence: dict[str, Any] = {
                    "kind": "stale_kill",
                    "pid": pid,
                    "pid_alive": False,
                    "killed_at": time.time(),
                    "threshold_seconds": threshold_seconds,
                }
                if user_reason:
                    evidence["user_reason"] = user_reason

                reason_summary = f"Stale auto-cancel: process dead or no PID. {user_reason}".strip()

                await _persist_cancel(
                    db,
                    entity_type,
                    entity_id,
                    reason_code=RunReasons.CANCELLED_STALE_AUTO,
                    reason_summary=reason_summary,
                    evidence=evidence,
                )
                killed += 1
                print(f"  cancelled stale {entity_type} {entity_id[:12]} (pid={pid})")

        # ── Child-derived staleness sweep for plays (#1144) ───────────
        # A play is child-stale when its linked session has terminated.
        # This is separate from the PID sweep above: plays never carry a
        # direct PID, so they can't be detected by process absence alone.
        play_cur = await db.db.execute("SELECT * FROM plays WHERE status = 'running'")
        play_rows = await play_cur.fetchall()
        for row in play_rows:
            row_dict = db._row_to_dict(row)
            play_id = row_dict["id"]

            started = row_dict.get("started_at") or row_dict.get("created_at") or 0
            if started > cutoff:
                skipped_recent += 1
                if verbose:
                    print(
                        f"  skip play {play_id[:12]}: started recently (< {threshold_seconds}s ago)"
                    )
                continue

            if not await _play_child_stale(db, row_dict):
                if verbose:
                    print(f"  skip play {play_id[:12]}: child session still running or absent")
                continue

            if dry_run:
                print(f"  (dry-run) would cancel stale play {play_id[:12]} (child-derived)")
                killed += 1
                continue

            evidence = {
                "kind": "child_stale_kill",
                "reason": "child_session_terminal",
                "killed_at": time.time(),
                "threshold_seconds": threshold_seconds,
            }
            if user_reason:
                evidence["user_reason"] = user_reason
            await _persist_cancel(
                db,
                "play",
                play_id,
                reason_code=RunReasons.CANCELLED_STALE_AUTO,
                reason_summary=(
                    f"Stale auto-cancel: child session terminated. {user_reason}".strip()
                ),
                evidence=evidence,
            )
            killed += 1
            print(f"  cancelled stale play {play_id[:12]} (child-derived)")

        # ── Child-derived staleness sweep for shows (#1144) ───────────
        # A show is child-stale when all child plays have terminated.
        show_cur = await db.db.execute("SELECT * FROM shows WHERE status = 'active'")
        show_rows = await show_cur.fetchall()
        for row in show_rows:
            row_dict = db._row_to_dict(row)
            show_id = row_dict["id"]

            started = (
                row_dict.get("started_at")
                or row_dict.get("updated_at")
                or row_dict.get("created_at")
                or 0
            )
            if started > cutoff:
                skipped_recent += 1
                if verbose:
                    print(
                        f"  skip show {show_id[:12]}: started recently (< {threshold_seconds}s ago)"
                    )
                continue

            if not await _show_children_all_terminal(db, show_id):
                if verbose:
                    print(f"  skip show {show_id[:12]}: has active child plays or no plays")
                continue

            if dry_run:
                print(f"  (dry-run) would cancel stale show {show_id[:12]} (child-derived)")
                killed += 1
                continue

            evidence = {
                "kind": "child_stale_kill",
                "reason": "all_child_plays_terminal",
                "killed_at": time.time(),
                "threshold_seconds": threshold_seconds,
            }
            if user_reason:
                evidence["user_reason"] = user_reason
            await _persist_cancel(
                db,
                "show",
                show_id,
                reason_code=RunReasons.CANCELLED_STALE_AUTO,
                reason_summary=(
                    f"Stale auto-cancel: all child plays terminated. {user_reason}".strip()
                ),
                evidence=evidence,
            )
            killed += 1
            print(f"  cancelled stale show {show_id[:12]} (child-derived)")

    prefix = "(dry-run) would cancel" if dry_run else "cancelled"
    print(
        f"\n{prefix} {killed} stale entities "
        f"[skipped_recent={skipped_recent}, skipped_live_pid={skipped_live}]"
    )
    return 0


# ── CLI wiring ─────────────────────────────────────────────────────────────────


def add_kill_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register `li kill` subcommand."""
    kill = subparsers.add_parser(
        "kill",
        help="Terminate a running entity (run/session/play/show).",
        description=(
            "Kill a running lionagi entity by id, or sweep all stale running "
            "entities whose underlying OS process is dead.\n\n"
            "The entity's status is set to 'cancelled' (sessions/invocations) "
            "or 'aborted' (shows) with reason tracking per ADR-0028.\n\n"
            "Examples:\n"
            "  li kill abc123                        # kill by id prefix\n"
            "  li kill abc123 --reason 'stuck'\n"
            "  li kill abc123 --recursive            # kill + child invocations\n"
            "  li kill --all-stale                   # sweep dead-PID rows\n"
            "  li kill --all-stale --threshold 3600  # only rows older than 1h\n"
            "  li kill --all-stale --dry-run\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    kill.add_argument(
        "id",
        nargs="?",
        help=(
            "Entity id to kill: run_id / session_id / invocation_id / play_id / "
            "show_id. Accepts full UUID or a unique prefix (≥6 chars)."
        ),
    )
    kill.add_argument(
        "--reason",
        default="",
        help="Optional human-readable reason recorded in status_transitions.",
    )
    kill.add_argument(
        "--recursive",
        action="store_true",
        help="Also kill child entities (e.g. invocations spawned by a session).",
    )
    kill.add_argument(
        "--all-stale",
        action="store_true",
        help=(
            "Sweep stale sessions and invocations with dead PIDs older than --threshold. "
            "Plays and shows are not swept (they are orchestrators without direct PIDs; "
            "use --recursive with an explicit ID instead)."
        ),
    )
    kill.add_argument(
        "--threshold",
        type=int,
        default=3600,
        help=(
            "Stale threshold in seconds (default 3600 = 1h). Only entities "
            "started more than this many seconds ago are swept."
        ),
    )
    kill.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be killed/cancelled without making any changes.",
    )
    kill.add_argument(
        "--grace",
        type=float,
        default=5.0,
        help="Seconds to wait after SIGTERM before escalating to SIGKILL (default 5).",
    )


def run_kill(args: argparse.Namespace) -> int:
    """Dispatch `li kill` subcommand."""
    from lionagi.ln.concurrency import run_async

    verbose = getattr(args, "verbose", False)

    if args.all_stale:
        return run_async(
            _do_kill_all_stale(
                threshold_seconds=args.threshold,
                user_reason=args.reason,
                grace_seconds=args.grace,
                dry_run=args.dry_run,
                verbose=verbose,
            )
        )

    if not args.id:
        log_error("specify an entity id or use --all-stale")
        return 1

    if args.dry_run:
        log_error("--dry-run is only meaningful with --all-stale")
        return 1

    return run_async(
        _do_kill(
            args.id,
            user_reason=args.reason,
            recursive=args.recursive,
            grace_seconds=args.grace,
            verbose=verbose,
        )
    )
