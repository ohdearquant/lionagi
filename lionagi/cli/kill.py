# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li kill` — terminate in-progress lionagi runs/sessions/plays/shows."""

from __future__ import annotations

import argparse
import os
import signal
import socket
import time
from typing import Any, Literal

import psutil

from lionagi._auto import CliDeclaration, auto_register
from lionagi.state.db import PLAY_ACTIVE_STATUSES as _PLAY_ACTIVE_STATUSES

from ._logging import log_error, warn
from ._util import _TABLE_TO_ENTITY_TYPE, AmbiguousIdError
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
    """Host-scoped process identity for liveness and kill verification."""
    return {
        "pid": os.getpid(),
        "pid_create_time": psutil.Process(os.getpid()).create_time(),
        "pid_host": socket.gethostname(),
        "pid_boot_time": psutil.boot_time(),
        "process_identity_mode": "local",
    }


# Clock-tick rounding tolerance for process start time comparison (CWE-362).
_CREATE_TIME_TOLERANCE = 0.1

#: Outcomes where nothing was stopped and no cancellation was written. A caller
#: that reports these as a kill is claiming a stop that did not happen.
_NOT_STOPPED_SIGNALS = frozenset({"identity_mismatch", "in_process"})


def _cmdline_is_lionagi(cmdline: list[str], expected_cmd: str) -> bool:
    """Exact-token match: is this cmdline a lionagi CLI invocation?"""
    if not cmdline:
        return False
    exe = os.path.basename(cmdline[0])
    if exe in ("li", expected_cmd):
        return True
    # Shebang-launched console scripts: argv[0] is the Python interpreter and
    # argv[1] is the script path (e.g. .venv/bin/li).
    if exe.lower().startswith("python") and len(cmdline) >= 2:
        if os.path.basename(cmdline[1]) == "li":
            return True
    for flag, mod in zip(cmdline, cmdline[1:], strict=False):
        if flag == "-m" and (mod == expected_cmd or mod.startswith(expected_cmd + ".")):
            return True
    return False


_IdentityVerdict = Literal["ours", "not_ours", "unverifiable", "zombie"]


def _pid_is_zombie(pid: int) -> bool:
    """True if *pid* has exited and is waiting for its parent to reap it.

    `pid_alive` answers "does the OS still know this pid", and a zombie
    satisfies that: it keeps its slot until it is reaped, so `kill -0` keeps
    succeeding and signals keep being accepted with no effect. Anything that
    reads "still there" as "still working" will wait out a grace period that
    can never end and then escalate onto a process that is already dead.
    """
    if pid <= 1:
        return False
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.ZombieProcess:
        return True
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _check_pid_identity(
    pid: int,
    expected_cmd: str,
    *,
    expected_session_id: str | None = None,
    expected_create_time: float | None = None,
) -> _IdentityVerdict:
    """Classify the process at *pid* against the run we recorded.

    Four answers, every caller must handle all of them:

    - "ours": positively identified as the run in the row.
    - "not_ours": pid is gone or held by a different process — killing it
      would hit a stranger.
    - "unverifiable": present but uninspectable (usually permission denied),
      or inspectable but with no durable identity recorded to check it
      against (no session id, no create_time) — a lionagi-looking cmdline
      alone cannot distinguish our run from any other lionagi process holding
      this pid. Callers must treat this as still-alive, never as dead, or an
      unattended sweep would cancel a worker it merely lacks permission to
      see — and a direct kill must refuse rather than signal a stranger.
    - "zombie": exited, not yet reaped. Not a recycled pid (the OS won't
      reissue one before reaping) and not killable again — a finished
      termination, so folding it into "not_ours" loses a cancellation that
      already happened.

    A recorded create_time still rules first, since it's readable on a
    zombie: a reaped-recycled-and-died-again pid reports "not_ours" rather
    than being mistaken for our own corpse.
    """
    try:
        proc = psutil.Process(pid)
    except psutil.ZombieProcess:
        return "zombie"
    except psutil.NoSuchProcess:
        return "not_ours"
    except psutil.AccessDenied:
        return "unverifiable"

    if expected_create_time is not None:
        try:
            create_time_ok = (
                abs(proc.create_time() - expected_create_time) <= _CREATE_TIME_TOLERANCE
            )
        except psutil.ZombieProcess:
            return "zombie"
        except psutil.NoSuchProcess:
            return "not_ours"
        except psutil.AccessDenied:
            return "unverifiable"
        if not create_time_ok:
            return "not_ours"

    if expected_session_id is not None:
        try:
            marker = proc.environ().get("LIONAGI_SESSION_ID")
        except psutil.ZombieProcess:
            # A zombie has no environment left to read, which is exactly how
            # this case used to disappear into "cannot identify it".
            return "zombie"
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

    if expected_session_id is None and expected_create_time is None:
        # Nothing durable was ever recorded for this row (e.g. an invocation,
        # which carries no session id and may have no pid_create_time either).
        # A lionagi-looking cmdline is not proof of identity — any other
        # lionagi process satisfies it — so there is nothing left to check
        # this pid against, and cmdline shape alone must not authorize a
        # signal.
        return "unverifiable"

    try:
        cmdline = proc.cmdline()
    except psutil.ZombieProcess:
        return "zombie"
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

    if _pid_is_zombie(pid):
        # Exited already, just not reaped by whoever started it. There is
        # nothing left to signal, and no other process can be holding this pid
        # until the reap happens, so this is a termination that is already
        # complete rather than a process we failed to identify.
        return "already_dead"

    if expected_cmd is not None:
        verdict = _check_pid_identity(
            pid,
            expected_cmd,
            expected_session_id=expected_session_id,
            expected_create_time=expected_create_time,
        )
        if verdict == "zombie":
            return "already_dead"
        # "unverifiable" refuses too: when a human named one entity to kill,
        # not being able to confirm the target is a reason to stop.
        if verdict != "ours":
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
        # A process whose parent never reaps it stays visible to `kill -0`
        # forever, so waiting on liveness alone would sit out the whole grace
        # window for a process that obeyed the SIGTERM immediately.
        if not _pid_alive(pid) or _pid_is_zombie(pid):
            return "sigterm"
        time.sleep(interval)
        interval = min(interval * 2, 0.5)

    if not _pid_alive(pid) or _pid_is_zombie(pid):
        return "sigterm"

    # Re-check identity before escalating: the pid may have been reused by an
    # unrelated process in the grace_seconds since SIGTERM, and SIGKILL is not
    # survivable, so this must not escalate onto a stranger. A pid that now belongs to a different process is
    # reported the same way a mismatch at entry is.
    if expected_cmd is not None:
        verdict = _check_pid_identity(
            pid,
            expected_cmd,
            expected_session_id=expected_session_id,
            expected_create_time=expected_create_time,
        )
        if verdict == "zombie":
            # It exited between the last poll and this check: the SIGTERM
            # worked, and escalating would be aiming at a corpse.
            return "sigterm"
        if verdict != "ours":
            return "identity_mismatch"

    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass

    return "sigkill"


# Only sessions/invocations carry PIDs; plays/shows are orchestrators.
_STALE_SWEEP_ORDER = ("sessions", "invocations")


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
        # `plays.session_id` is bound only by the Studio show importer; a play
        # created by a live run leaves it NULL, so this returns nothing for
        # those and the caller reports the gap rather than a false reap.
        rows = await db.fetch_all(
            "SELECT sessions.* FROM plays "
            "JOIN sessions ON sessions.id = plays.session_id "
            "WHERE plays.id = ? AND sessions.status = 'running'",
            (entity_id,),
        )
        for row in rows:
            session_row = db._row_to_dict(row)
            # Deepest first: whatever the session reaches is signalled before
            # the session, so no process is orphaned by its owner going
            # terminal ahead of it.
            children.extend(await _list_running_children(db, "session", session_row["id"]))
            children.append(("sessions", "session", session_row))

    if entity_type == "session":
        # `sessions.invocation_id` points at the invocation that OWNS this
        # session, so this walks up rather than down. It belongs here anyway:
        # stopping a session has to stop the process running it, and that
        # process is the owning invocation. Traversal order still holds, since
        # the owner is signalled before the session that named it.
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


async def _persist_cancel(
    db: Any,
    entity_type: str,
    entity_id: str,
    *,
    reason_code: str,
    reason_summary: str,
    evidence: dict[str, Any],
) -> None:
    """Write the entity's terminal status + status_transition row.

    The status is per entity kind, not one word: a session or invocation goes
    ``cancelled``, a play goes ``blocked``, a show goes ``aborted``. Anything
    reported to an operator has to name the one that was actually written.
    """
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

    row_meta = row.get("node_metadata") if isinstance(row.get("node_metadata"), dict) else {}
    if row_meta.get("process_identity_mode") == "in_process":
        # This run has no OS process of its own: it executes inside a
        # long-lived host. There is nothing here to signal, and signalling the
        # host would stop every other run it carries. Persisting a
        # cancellation would report a stop that did not happen, so this path
        # reports that it did not stop anything. Studio's operator cancel
        # reaches these by cancelling the driving task.
        warn(
            f"  {entity_type} {entity_id[:12]}: runs inside a host process, "
            "not cancellable from here — use the Studio cancel for this run"
        )
        return {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "signal": "in_process",
            "pid": None,
        }

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

    async with StateDB() as db:
        try:
            resolved = await _resolve_entity(db, id_or_short)
        except AmbiguousIdError as exc:
            # Killing "whichever row matched first" would signal a process the
            # caller never named — refuse and show the candidates instead.
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

        play_workers_unreachable = False

        if entity_type == "show":
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
        elif entity_type == "play":
            # A play row carries no PID of its own, so its whole effect on the
            # running system is through the sessions it started. Resolving them
            # is the kill, not an extra step behind --recursive.
            children = await _list_running_children(db, entity_type, row["id"])
            play_workers_unreachable = not children
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
                if r["signal"] in _NOT_STOPPED_SIGNALS:
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
        if r["signal"] in _NOT_STOPPED_SIGNALS:
            blocked.append(r)
        else:
            print(f"killed {entity_type} {row['id'][:12]} (signal={r['signal']}, pid={r['pid']})")

        if play_workers_unreachable:
            log_error(
                f"play {row['id'][:12]} is marked blocked, but no worker "
                "process was stopped: the row records no running session, so "
                "any workers it started cannot be resolved from the play id. "
                "Find the running sessions with `li monitor` and kill those "
                "ids directly."
            )
            return 1

    return 1 if blocked else 0


async def _play_child_stale(db: Any, play_row: dict[str, Any]) -> bool | None:
    """Whether the play's linked session has terminated.

    ``None`` means the question could not be asked: the row records no session
    to look up, or it points at one that is no longer in the table. That is a
    different answer from "the session is still running", and the sweep reports
    it differently.
    """
    session_id = play_row.get("session_id")
    if not session_id:
        return None
    row = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (session_id,))
    if row is None:
        return None
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
    skipped_unlinked_plays = 0
    unverifiable_tracked = 0

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
                # A live pid alone isn't enough — the OS may have reused it.
                # Correlate against the row's own session id/create_time (same
                # fields the direct-kill path uses) so a recycled pid occupied
                # by a different lionagi process doesn't pass as "still alive".
                pid_alive_now = pid is not None and _pid_alive(pid)
                verdict: str | None = None
                if pid_alive_now:
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

                    verdict = _check_pid_identity(
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
                        # we simply can't inspect. That decision must not be
                        # only a per-sweep counter: persist when this was
                        # first observed so a permanently-uninspectable pid
                        # leaves durable evidence instead of being silently
                        # skipped forever with nothing to show for it.
                        skipped_unverifiable += 1
                        if not dry_run:
                            since = meta.get("unverifiable_since")
                            if not isinstance(since, (int, float)):
                                since = time.time()
                            count = meta.get("unverifiable_count")
                            count = (count + 1) if isinstance(count, int) else 1
                            # Merge only the two marker fields through the
                            # atomic UPDATE. A whole-column snapshot built
                            # from `meta` (read at the top of this loop) and
                            # written back with update_session()/
                            # update_invocation() can land after a concurrent
                            # writer's own atomic merge (e.g. the flow
                            # segment/control-log writers) and silently
                            # overwrite whatever they just added — the same
                            # clobber this patch closes on the flow side.
                            marker_patch = {
                                "unverifiable_since": since,
                                "unverifiable_count": count,
                            }
                            if entity_type == "session":
                                await db.merge_session_node_metadata(entity_id, marker_patch)
                            else:
                                await db.merge_invocation_node_metadata(entity_id, marker_patch)
                            unverifiable_tracked += 1
                        if verbose:
                            print(
                                f"  skip {entity_type} {entity_id[:12]}: process {pid} "
                                "identity unverifiable (permission denied) — treated as live"
                            )
                        continue
                    # "not_ours" (the pid was recycled by an unrelated process)
                    # and "zombie" (our process exited and nobody reaped it)
                    # both mean the recorded run is gone: fall through and
                    # sweep the row.

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
                    # Numeric liveness and identity are two different questions
                    # (issue: a live-but-recycled pid used to hardcode False
                    # here, indistinguishable from a genuinely dead pid).
                    "pid_alive": pid_alive_now,
                    "identity_verdict": verdict,
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

            child_stale = await _play_child_stale(db, row_dict)
            if child_stale is None:
                # No session to check. Age alone would not distinguish a play
                # whose workers are gone from one still doing hours of work, so
                # the row is left alone and counted for the closing summary.
                skipped_unlinked_plays += 1
                if verbose:
                    print(
                        f"  skip play {play_id[:12]}: row records no worker session, "
                        "so staleness cannot be determined"
                    )
                continue
            if not child_stale:
                if verbose:
                    print(f"  skip play {play_id[:12]}: worker session still running")
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
        f"skipped_unverifiable_pid={skipped_unverifiable}, "
        f"skipped_unlinked_plays={skipped_unlinked_plays}]"
    )
    if unverifiable_tracked:
        # Durable evidence, not just this sweep's counter: a row that stays
        # uninspectable forever (e.g. permission denied) never reaches any
        # other outcome, so its unverifiable_since/count are now visible on
        # the row itself rather than only inferred from repeated sweeps.
        warn(
            f"{unverifiable_tracked} running row(s) recorded first-observed "
            "unverifiable-pid evidence this sweep (node_metadata.unverifiable_since / "
            "unverifiable_count) — no automatic disposition is applied; inspect "
            "with `li status` and resolve manually."
        )
    if skipped_unlinked_plays:
        # One line per sweep, not one per row: a play created by a live run
        # never records the sessions it started, so this is a property of how
        # plays are written rather than an observation about these rows.
        warn(
            f"{skipped_unlinked_plays} running play row(s) were not swept: "
            "plays created by live runs record no link to the sessions they started, "
            "so their worker state cannot be determined. Sweep the worker session ids instead "
            "(`li monitor` lists them)."
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
            "Recursion boundary: --recursive kills a session's or invocation's "
            "direct children, which are the PID-bearing workers. A PLAY kill "
            "reaches the play's worker chain only when the row records the "
            "session it started; a play created by a live run does not, so that "
            "kill marks the row terminal and exits non-zero to say no worker was "
            "stopped. A SHOW kill only marks the show row terminal. To stop work "
            "either one cannot reach, kill the session ids directly; --all-stale "
            "marks a play 'blocked' only when the row records a worker session "
            "that has gone terminal, and leaves an unlinked play running.\n\n"
            "Examples:\n"
            "  li kill abc123                        # kill by id prefix\n"
            "  li kill <session-id>                  # stop a worker process\n"
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
            "Has no effect on a play kill, which already reaps whatever worker chain "
            "the row records, or on a show kill, which cannot reach one -- kill the "
            "session ids directly to stop a show's workers."
        ),
    )
    kill.add_argument(
        "--all-stale",
        action="store_true",
        help=(
            "Sweep stale sessions and invocations with dead PIDs older than --threshold. "
            "A play older than --threshold whose recorded worker session has gone "
            "terminal is marked 'blocked'; a play that records no worker session is "
            "left running, because age alone cannot tell it apart from one still "
            "working. A show is marked 'aborted' only once it is older than --threshold "
            "and ALL of its plays are terminal."
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


@auto_register(area="kill", cli=CliDeclaration(seed="kill", parser_factory=add_kill_subparser))
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
