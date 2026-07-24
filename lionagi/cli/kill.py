# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li kill` — terminate in-progress lionagi runs/sessions/plays/shows."""

from __future__ import annotations

import argparse
import os
import signal
import time
from collections import deque
from typing import Any, Literal

import psutil

from lionagi.state.db import PLAY_ACTIVE_STATUSES as _PLAY_ACTIVE_STATUSES

from ._logging import log_error, warn
from ._util import _TABLE_TO_ENTITY_TYPE
from ._util import pid_alive as _pid_alive
from ._util import resolve_entity as _resolve_entity


def _read_pid_from_entity(entity: dict[str, Any]) -> int | None:
    """Extract the OS PID from an entity row."""
    meta = entity.get("node_metadata") or {}
    if isinstance(meta, dict):
        raw_pid = meta.get("pid")
        if raw_pid is not None:
            try:
                return int(raw_pid)
            except (TypeError, ValueError):
                pass

    artifacts_path = entity.get("artifacts_path")
    if artifacts_path:
        pid_file = os.path.join(artifacts_path, ".pid")
        try:
            text = open(pid_file).read().strip()  # noqa: WPS515
            return int(text)
        except (OSError, ValueError):
            pass

    return None


def current_pid_markers() -> dict[str, Any]:
    """PID + create_time for the current process, for kill verification (CWE-362)."""
    return {
        "pid": os.getpid(),
        "pid_create_time": psutil.Process(os.getpid()).create_time(),
    }


# Clock-tick rounding tolerance for process start time comparison (CWE-362).
_CREATE_TIME_TOLERANCE = 0.1


def _cmdline_is_lionagi(cmdline: list[str], expected_cmd: str) -> bool:
    """Exact-token match: is this cmdline a lionagi CLI invocation?"""
    if not cmdline:
        return False
    exe = os.path.basename(cmdline[0])
    if exe in ("li", expected_cmd):
        return True
    # Shebang-launched console scripts: argv[0] is the Python interpreter and
    # argv[1] is the script path (e.g. .venv/bin/li).
    if exe.startswith("python") and len(cmdline) >= 2:
        if os.path.basename(cmdline[1]) == "li":
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
    """Return True iff the live process at *pid* is the lionagi run we recorded."""
    try:
        proc = psutil.Process(pid)
        create_time_ok: bool | None = None
        if expected_create_time is not None:
            create_time_ok = (
                abs(proc.create_time() - expected_create_time) <= _CREATE_TIME_TOLERANCE
            )
            if not create_time_ok:
                return False

        if expected_session_id is not None:
            try:
                marker = proc.environ().get("LIONAGI_SESSION_ID")
            except (psutil.AccessDenied, NotImplementedError):
                marker = None
            if marker is not None:
                return marker == expected_session_id
            # Without env marker, require BOTH create_time match AND lionagi cmdline.
            return create_time_ok is True and _cmdline_is_lionagi(proc.cmdline(), expected_cmd)

        cmdline = proc.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False

    return _cmdline_is_lionagi(cmdline, expected_cmd)


_IdentityVerdict = Literal["ours", "not_ours", "unverifiable"]


def _check_pid_identity_tristate(
    pid: int,
    expected_cmd: str,
    *,
    expected_session_id: str | None = None,
    expected_create_time: float | None = None,
) -> _IdentityVerdict:
    """Sweep-only identity check that separates "definitely not ours" from
    "cannot tell" (permission denied reading process details).

    The direct-kill path (`_check_pid_identity`) collapses AccessDenied to a
    refusal, which is the safe default when a human explicitly asked to kill
    one entity. The stale sweep runs unattended over many rows; if it reused
    that same collapse, a live worker we simply lack permission to inspect
    would be swept and its row marked cancelled while the process keeps
    running. Callers must treat "unverifiable" as still-alive, not as dead.
    """
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return "not_ours"
    except psutil.AccessDenied:
        return "unverifiable"

    if expected_create_time is not None:
        try:
            create_time_ok = (
                abs(proc.create_time() - expected_create_time) <= _CREATE_TIME_TOLERANCE
            )
        except psutil.NoSuchProcess:
            return "not_ours"
        except psutil.AccessDenied:
            return "unverifiable"
        if not create_time_ok:
            return "not_ours"

    if expected_session_id is not None:
        try:
            marker = proc.environ().get("LIONAGI_SESSION_ID")
        except psutil.NoSuchProcess:
            # The process died between the liveness check and here: the row
            # is genuinely stale, and letting this escape would abort the
            # whole sweep with later rows unprocessed.
            return "not_ours"
        except (psutil.AccessDenied, NotImplementedError):
            marker = None
        if marker is not None:
            return "ours" if marker == expected_session_id else "not_ours"
        if expected_create_time is None:
            # No create_time correlation and the env marker is unreadable:
            # cmdline alone cannot distinguish this run from a different
            # concurrent one that recycled the pid.
            return "unverifiable"

    try:
        cmdline = proc.cmdline()
    except psutil.NoSuchProcess:
        return "not_ours"
    except psutil.AccessDenied:
        return "unverifiable"

    return "ours" if _cmdline_is_lionagi(cmdline, expected_cmd) else "not_ours"


def _terminate_pid(
    pid: int,
    grace_seconds: float = 5.0,
    expected_cmd: str | None = None,
    *,
    expected_session_id: str | None = None,
    expected_create_time: float | None = None,
) -> str:
    """SIGTERM then SIGKILL. Returns "sigterm"/"sigkill"/"already_dead"/"identity_mismatch"."""
    if not _pid_alive(pid):
        return "already_dead"

    if expected_cmd is not None and not _check_pid_identity(
        pid,
        expected_cmd,
        expected_session_id=expected_session_id,
        expected_create_time=expected_create_time,
    ):
        return "identity_mismatch"

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "already_dead"
    except PermissionError as exc:
        raise RuntimeError(
            f"cannot send SIGTERM to pid {pid}: {exc}. "
            "Try again as root, or mark the entity cancelled manually."
        ) from exc

    deadline = time.monotonic() + grace_seconds
    interval = 0.1
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return "sigterm"
        time.sleep(interval)
        interval = min(interval * 2, 0.5)

    if not _pid_alive(pid):
        return "sigterm"

    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass

    return "sigkill"


# Only sessions/invocations carry PIDs; plays/shows are orchestrators.
_STALE_SWEEP_ORDER = ("sessions", "invocations")
_MAX_RECURSIVE_CHILDREN = 100


async def _list_running_children(
    db: Any, entity_type: str, entity_id: str
) -> list[tuple[str, str, dict[str, Any]]]:
    children: list[tuple[str, str, dict[str, Any]]] = []

    if entity_type == "show":
        rows = await db.fetch_all(
            "SELECT * FROM plays WHERE show_id = ? AND status = 'running'",
            (entity_id,),
        )
        for row in rows:
            children.append(("plays", "play", db._row_to_dict(row)))

    if entity_type == "play":
        rows = await db.fetch_all(
            "SELECT sessions.* FROM plays "
            "JOIN sessions ON sessions.id = plays.session_id "
            "WHERE plays.id = ? AND sessions.status = 'running'",
            (entity_id,),
        )
        if not rows:
            warn(f"play {entity_id[:12]} has no running worker session to reap")
        for row in rows:
            session_row = db._row_to_dict(row)
            children.append(("sessions", "session", session_row))
            children.extend(await _list_running_children(db, "session", session_row["id"]))

    if entity_type == "session":
        rows = await db.fetch_all(
            "SELECT * FROM invocations "
            "WHERE status = 'running' AND id IN ("
            "  SELECT invocation_id FROM sessions "
            "  WHERE invocation_id IS NOT NULL AND id = ?"
            ")",
            (entity_id,),
        )
        for row in rows:
            children.append(("invocations", "invocation", db._row_to_dict(row)))

    if entity_type == "invocation":
        rows = await db.fetch_all(
            "SELECT * FROM sessions WHERE invocation_id = ? AND status = 'running'",
            (entity_id,),
        )
        for row in rows:
            children.append(("sessions", "session", db._row_to_dict(row)))

    return children


async def _walk_running_children(
    db: Any, entity_type: str, entity_id: str
) -> list[tuple[str, str, dict[str, Any]]]:
    """Discover running descendants breadth-first and return them deepest-first."""
    frontier = deque(await _list_running_children(db, entity_type, entity_id))
    seen = {(entity_type, entity_id)}
    children: list[tuple[str, str, dict[str, Any]]] = []

    while frontier:
        table, child_type, child_row = frontier.popleft()
        child_id = child_row["id"]
        child_key = (child_type, child_id)
        if child_key in seen:
            continue
        if len(children) >= _MAX_RECURSIVE_CHILDREN:
            warn(
                f"recursive kill stopped after {_MAX_RECURSIVE_CHILDREN} children; "
                "remaining descendants were not reaped"
            )
            break

        seen.add(child_key)
        children.append((table, child_type, child_row))
        frontier.extend(await _list_running_children(db, child_type, child_id))

    children.reverse()
    return children


async def _persist_cancel(
    db: Any,
    entity_type: str,
    entity_id: str,
    *,
    reason_code: str,
    reason_summary: str,
    evidence: dict[str, Any],
) -> None:
    """Write cancelled status + status_transition row."""
    from lionagi.state.db import (
        PLAY_TERMINAL_STATUSES,
        SHOW_TERMINAL_STATUSES,
        TransitionRejectedError,
    )

    if entity_type == "play":
        row = await db.fetch_one("SELECT status FROM plays WHERE id = ?", (entity_id,))
        if row is None:
            return
        if row["status"] in PLAY_TERMINAL_STATUSES:
            return
        target_status = "blocked"
    elif entity_type == "show":
        row = await db.fetch_one("SELECT status FROM shows WHERE id = ?", (entity_id,))
        if row is None:
            return
        if row["status"] in SHOW_TERMINAL_STATUSES:
            return
        target_status = "aborted"
    else:
        table = {
            "session": "sessions",
            "invocation": "invocations",
        }.get(entity_type, "sessions")
        row = await db.fetch_one(
            f"SELECT status FROM {table} WHERE id = ?",  # noqa: S608
            (entity_id,),
        )
        if row is None:
            return
        if row["status"] != "running":
            return
        target_status = "cancelled"

    try:
        await db.update_status(
            entity_type,
            entity_id,
            new_status=target_status,
            reason_code=reason_code,
            reason_summary=reason_summary,
            evidence_refs=[evidence],
            source="admin",
            actor="user",
        )
    except TransitionRejectedError:
        # The entity went terminal between the pre-check and this write —
        # nothing to cancel, same as the pre-check `return`s above.
        pass


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
    """Kill one entity: terminate process, persist cancellation."""
    from lionagi.state.reasons import RunReasons

    pid = _read_pid_from_entity(row)
    signal_used = "no_pid"

    if pid is not None:
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

    if signal_used == "sigkill":
        reason_code = RunReasons.CANCELLED_FORCE_KILL
        reason_summary = f"Force-killed (SIGKILL after grace period). {user_reason}".strip()
    elif signal_used == "identity_mismatch":
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
    """Resolve entity, kill process, persist cancellation."""
    from lionagi.state.db import StateDB

    from ._util import AmbiguousIdError

    async with StateDB() as db:
        try:
            resolved = await _resolve_entity(db, id_or_short)
        except AmbiguousIdError as exc:
            log_error(str(exc))
            return 1
        if resolved is None:
            log_error(f"entity not found for id: {id_or_short!r}")
            return 1

        table, entity_type, row = resolved
        current_status = row.get("status")
        # Shows never reach "running" (they persist as 'active' per the
        # shows.status vocabulary in state/db.py); every other entity type
        # uses "running" as its only killable status.
        killable_status = "active" if entity_type == "show" else "running"

        if current_status != killable_status:
            log_error(
                f"{entity_type} {row['id'][:12]} is already in terminal state: "
                f"{current_status!r} — nothing to kill"
            )
            return 1

        results = []
        blocked = []

        if entity_type == "play":
            children = await _walk_running_children(db, entity_type, row["id"])
        elif entity_type == "show":
            # ADR-0104 explicitly defers show-level reaping: a show kill only
            # marks the show row terminal. --recursive is a documented no-op
            # here rather than a partial reap of the show's plays/workers.
            children = []
            if recursive:
                warn(
                    f"show {row['id'][:12]}: --recursive does not reap a show's "
                    "plays or their workers (deferred per ADR-0104) — kill the "
                    "play or session ids directly to stop a show's workers"
                )
        elif recursive:
            children = await _list_running_children(db, entity_type, row["id"])
        else:
            children = []

        if children:
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
                if r["signal"] == "identity_mismatch":
                    blocked.append(r)
                else:
                    print(
                        f"  killed child {child_type} {child_row['id'][:12]} (signal={r['signal']})"
                    )

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
        if r["signal"] == "identity_mismatch":
            blocked.append(r)
        else:
            print(f"killed {entity_type} {row['id'][:12]} (signal={r['signal']}, pid={r['pid']})")

    return 1 if blocked else 0


async def _play_child_stale(db: Any, play_row: dict[str, Any]) -> bool:
    """True if the play's linked session has terminated."""
    session_id = play_row.get("session_id")
    if not session_id:
        return False
    row = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (session_id,))
    if row is None:
        return False
    return row["status"] != "running"


async def _show_children_all_terminal(db: Any, show_id: str) -> bool:
    """True if the show has >= 1 child play and all are terminal."""
    rows = await db.fetch_all("SELECT status FROM plays WHERE show_id = ?", (show_id,))
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
    """Sweep stale sessions/invocations whose PIDs are dead."""
    from lionagi.state.db import StateDB
    from lionagi.state.reasons import RunReasons

    cutoff = time.time() - threshold_seconds
    killed = 0
    skipped_live = 0
    skipped_recent = 0
    skipped_unverifiable = 0

    live_status_for: dict[str, str] = {
        "sessions": "running",
        "invocations": "running",
    }

    async with StateDB() as db:
        for table in _STALE_SWEEP_ORDER:
            entity_type = _TABLE_TO_ENTITY_TYPE[table]
            live_status = live_status_for[table]
            rows = await db.fetch_all(
                f"SELECT * FROM {table} WHERE status = ?",  # noqa: S608
                (live_status,),
            )

            for row_dict in (db._row_to_dict(r) for r in rows):
                entity_id = row_dict["id"]

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
                # A live pid alone isn't enough: if the original process died
                # and the OS reused its pid, `_pid_alive` still reports True.
                # Correlate against the row's own session id / recorded
                # create_time — the same fields the direct-kill path uses —
                # so a recycled pid occupied by a DIFFERENT lionagi process
                # doesn't pass as "still alive".
                if pid is not None and _pid_alive(pid):
                    meta = (
                        row_dict.get("node_metadata")
                        if isinstance(row_dict.get("node_metadata"), dict)
                        else {}
                    )
                    expected_session_id = entity_id if entity_type == "session" else None
                    raw_ct = meta.get("pid_create_time")
                    try:
                        expected_create_time = float(raw_ct) if raw_ct is not None else None
                    except (TypeError, ValueError):
                        expected_create_time = None

                    verdict = _check_pid_identity_tristate(
                        pid,
                        "lionagi",
                        expected_session_id=expected_session_id,
                        expected_create_time=expected_create_time,
                    )
                    if verdict == "ours":
                        skipped_live += 1
                        if verbose:
                            print(
                                f"  skip {entity_type} {entity_id[:12]}: "
                                f"process {pid} is still alive"
                            )
                        continue
                    if verdict == "unverifiable":
                        # We couldn't read enough of the process to confirm
                        # identity either way (e.g. AccessDenied). Treat as
                        # live rather than sweep it out from under a worker
                        # we simply can't inspect.
                        skipped_unverifiable += 1
                        if verbose:
                            print(
                                f"  skip {entity_type} {entity_id[:12]}: process {pid} "
                                "identity unverifiable (permission denied) — treated as live"
                            )
                        continue
                    # verdict == "not_ours": pid was recycled by an unrelated
                    # process, fall through and sweep the row.

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

        play_rows = await db.fetch_all("SELECT * FROM plays WHERE status = 'running'", ())
        for row_dict in (db._row_to_dict(r) for r in play_rows):
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

        show_rows = await db.fetch_all("SELECT * FROM shows WHERE status = 'active'", ())
        for row_dict in (db._row_to_dict(r) for r in show_rows):
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
        f"[skipped_recent={skipped_recent}, skipped_live_pid={skipped_live}, "
        f"skipped_unverifiable_pid={skipped_unverifiable}]"
    )
    return 0


def add_kill_subparser(subparsers: argparse._SubParsersAction) -> None:
    kill = subparsers.add_parser(
        "kill",
        help="Terminate a running entity (run/session/play/show).",
        description=(
            "Kill a running lionagi entity by id, or sweep all stale running "
            "entities whose underlying OS process is dead.\n\n"
            "The entity's status is set to 'cancelled' (sessions/invocations), "
            "'blocked' (plays), or 'aborted' (shows) with reason tracking per "
            "ADR-0028.\n\n"
            "Recursion boundary: --recursive walks play -> session -> invocation "
            "and always reaches the PID-bearing workers. A SHOW kill ('active' "
            "-> 'aborted') only marks the show row terminal: --recursive has no "
            "effect on shows, since reaping a show's plays/workers is deferred "
            "per ADR-0104. To stop a show's work, kill its play ids or session "
            "ids directly; --all-stale cancels play and show rows once their "
            "workers are gone.\n\n"
            "Examples:\n"
            "  li kill abc123                        # kill by id prefix\n"
            "  li kill <play-id>                     # also reap linked workers\n"
            "  li kill abc123 --reason 'stuck'\n"
            "  li kill abc123 --recursive            # kill + direct children\n"
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
            "show_id. Accepts a full UUID, or an id prefix (resolved to the "
            "first matching row)."
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
        help=(
            "Also kill direct child entities (e.g. invocations spawned by a session). "
            "Play kills always reap their linked workers. Has no effect on show kills: "
            "reaping a show's plays/workers is deferred per ADR-0104 -- kill the play "
            "or session id directly to stop a show's workers."
        ),
    )
    kill.add_argument(
        "--all-stale",
        action="store_true",
        help=(
            "Sweep stale sessions and invocations with dead PIDs older than --threshold. "
            "A play whose swept session was its worker is cancelled with it; a show is "
            "cancelled only once it is older than --threshold and ALL of its plays are "
            "terminal."
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
