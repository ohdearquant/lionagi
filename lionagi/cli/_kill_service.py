# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Process identity, signalling and cleanup helpers for stopping runs.

The ``li kill`` command and the Studio cancel path share them.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import psutil

from lionagi.ln._proc import (
    _admit_recorded_worker_group,
    _recorded_worker_group_matches,
    _recorded_worker_group_state,
    _RecordedGroupRefusal,
    _RecordedGroupWitness,
    _RecordedWorkerIdentity,
    raw_create_time,
)
from lionagi.ln._proc import start_time_matches as _start_time_matches
from lionagi.ln.concurrency import current_time, effective_deadline, fail_after, sleep
from lionagi.state.db import SESSION_TERMINAL_STATUSES

from ._logging import log_error, warn
from ._util import BOOT_TIME_TOLERANCE
from ._util import pid_alive as _pid_alive
from ._util import recorded_identity_mode as _recorded_identity_mode
from ._util import recorded_pid_is_foreign as _recorded_pid_is_foreign


def read_pid_from_entity(entity: dict[str, Any]) -> int | None:
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


#: Refusals to signal. A pid only names a process together with its host and boot; when the
#: record cannot show that, nothing is signalled or written, so the row's claim stays true.
_REFUSED_SIGNAL_REASONS: dict[str, str] = {
    "identity_mismatch": "did not match the expected lionagi process",
    "identity_unverifiable": "could not be identified reliably",
    "identity_unverifiable_after_sigterm": (
        "received SIGTERM but could not be identified reliably for escalation; "
        "the process may still be running"
    ),
    "host_mismatch": "was recorded on a different host",
    "boot_mismatch": "was recorded before this machine last booted",
    "boot_unverifiable": "has a Linux boot time that cannot be compared reliably",
    "in_process": (
        "runs inside a shared host process, so no signal reaches it alone — "
        "use the Studio cancel for this run"
    ),
    "foreign_mode": "records a process identity this CLI does not manage",
    # Membership here routes this signal through the early return in `_kill_one`, so no
    # cancellation is written for a process that is demonstrably still running. The kernel
    # refused the signal, so it arrives from a different direction than its neighbours --
    # they are refusals this CLI decides before signalling, this one is a refusal it is
    # handed after asking -- but the caller's question is the same either way: was it
    # stopped. It was not.
    "permission_denied": "could not be signalled: permission denied",
    "no_recorded_process": "names a run whose persistence setup failed and recorded no process",
    "no_recorded_identity": (
        "was recorded with no start time, so nothing shows which process it named"
    ),
    "imported_no_process": (
        "is a row `li state import` made from a run.json, which records no process: "
        "stop the run by its own session id (`li monitor` lists them)"
    ),
}

#: Outcomes where nothing was stopped and no cancellation was written: every refusal above.
#: A caller that reports these as a kill is claiming a stop that did not happen.
NOT_STOPPED_SIGNALS = frozenset(_REFUSED_SIGNAL_REASONS)

#: Re-exported so readers find it beside the check that uses it; defined once in ``_util``
#: because the Studio liveness probe compares the same recorded value.
_BOOT_TIME_TOLERANCE = BOOT_TIME_TOLERANCE


#: Reasons a row cannot be judged on this host, regardless of PID liveness.
NOT_JUDGEABLE_HERE = frozenset({"host_mismatch", "in_process", "foreign_mode"})

#: Identity modes this CLI can signal. ``in_process`` is excluded on purpose — the run shares
#: a host process, so no signal reaches it alone.
_LOCALLY_SIGNALLABLE_MODES = frozenset({"local"})


def unaddressable_pid_reason(meta: dict[str, Any]) -> str | None:
    """Why the recorded pid cannot be signalled from here, or None if it can — checked in order: identity mode, host, then boot time; absent markers return None, not a refusal."""
    mode = _recorded_identity_mode(meta)
    if mode is not None and mode not in _LOCALLY_SIGNALLABLE_MODES:
        return "in_process" if mode == "in_process" else "foreign_mode"

    if _recorded_pid_is_foreign(meta):
        return "host_mismatch"

    raw_boot = meta.get("pid_boot_time")
    if raw_boot is not None:
        try:
            recorded_boot = float(raw_boot)
        except (TypeError, ValueError):
            return None
        if abs(recorded_boot - psutil.boot_time()) > _BOOT_TIME_TOLERANCE:
            if getattr(psutil, "LINUX", False) is True:
                return "boot_unverifiable"
            return "boot_mismatch"
    return None


def _pid_addressability(reason: str | None, pid: int | None) -> tuple[str | None, bool | None]:
    """Resolve a boot disagreement using one liveness probe when needed.

    The second value is the observed liveness, or None if no probe was needed;
    callers can reuse it without observing a different process at the same PID.
    """
    if reason != "boot_unverifiable":
        return reason, None
    alive = pid is not None and _pid_alive(pid)
    return ("boot_unverifiable" if alive else "boot_mismatch"), alive


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


def check_pid_identity(
    pid: int,
    expected_cmd: str,
    *,
    expected_session_id: str | None = None,
    expected_create_time: float | None = None,
    _reason_out: list[str] | None = None,
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

    A recorded create_time is checked before the session marker, and is readable
    on a zombie. On non-Linux systems a mismatched birth excludes a recycled PID
    before it can be mistaken for the original process's corpse. On Linux, an
    epoch disagreement is ambiguous because independent readers can use different
    boot-time anchors; even an inherited matching marker cannot resolve it.
    """

    def unknown(reason: str) -> _IdentityVerdict:
        if _reason_out is not None:
            _reason_out.append(reason)
        return "unverifiable"

    try:
        proc = psutil.Process(pid)
    except psutil.ZombieProcess:
        return "zombie"
    except psutil.NoSuchProcess:
        return "not_ours"
    except psutil.AccessDenied:
        return unknown("permission_denied")

    if expected_create_time is not None:
        try:
            create_time_ok = _start_time_matches(raw_create_time(proc), expected_create_time)
        except psutil.ZombieProcess:
            return "zombie"
        except psutil.NoSuchProcess:
            return "not_ours"
        except psutil.AccessDenied:
            return unknown("permission_denied")
        if not create_time_ok:
            if getattr(psutil, "LINUX", False) is True:
                return unknown("start_mismatch")
            return "not_ours"

    if expected_session_id is not None:
        marker_denied = False
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
        except psutil.AccessDenied:
            marker_denied = True
            marker = None
        except NotImplementedError:
            marker = None
        if marker is not None:
            return "ours" if marker == expected_session_id else "not_ours"
        if expected_create_time is None:
            # No create_time correlation and the env marker is unreadable:
            # cmdline alone cannot distinguish this run from a different
            # concurrent one that recycled the pid.
            return unknown("permission_denied" if marker_denied else "identity_unknown")

    if expected_session_id is None and expected_create_time is None:
        # Nothing durable was ever recorded for this row (e.g. an invocation,
        # which carries no session id and may have no pid_create_time either).
        # A lionagi-looking cmdline is not proof of identity — any other
        # lionagi process satisfies it — so there is nothing left to check
        # this pid against, and cmdline shape alone must not authorize a
        # signal.
        return unknown("identity_unknown")

    try:
        cmdline = proc.cmdline()
    except psutil.ZombieProcess:
        return "zombie"
    except psutil.NoSuchProcess:
        return "not_ours"
    except psutil.AccessDenied:
        return unknown("permission_denied")

    return "ours" if _cmdline_is_lionagi(cmdline, expected_cmd) else "not_ours"


def _terminate_pid(
    pid: int,
    grace_seconds: float = 5.0,
    expected_cmd: str | None = None,
    *,
    expected_session_id: str | None = None,
    expected_create_time: float | None = None,
) -> str:
    """SIGTERM then SIGKILL, or return a refusal describing the observed identity."""
    if not _pid_alive(pid):
        return "already_dead"

    if _pid_is_zombie(pid):
        # Exited already, just not reaped by whoever started it. There is
        # nothing left to signal, and no other process can be holding this pid
        # until the reap happens, so this is a termination that is already
        # complete rather than a process we failed to identify.
        return "already_dead"

    if expected_cmd is not None:
        reasons: list[str] = []
        verdict = check_pid_identity(
            pid,
            expected_cmd,
            expected_session_id=expected_session_id,
            expected_create_time=expected_create_time,
            _reason_out=reasons,
        )
        if verdict == "zombie":
            return "already_dead"
        if verdict == "unverifiable" and reasons == ["start_mismatch"]:
            return "identity_unverifiable"
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
        reasons = []
        verdict = check_pid_identity(
            pid,
            expected_cmd,
            expected_session_id=expected_session_id,
            expected_create_time=expected_create_time,
            _reason_out=reasons,
        )
        if verdict == "zombie":
            # It exited between the last poll and this check: the SIGTERM
            # worked, and escalating would be aiming at a corpse.
            return "sigterm"
        if verdict == "unverifiable" and reasons == ["start_mismatch"]:
            return "identity_unverifiable_after_sigterm"
        if verdict != "ours":
            return "identity_mismatch"

    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass

    return "sigkill"


# Only sessions/invocations carry PIDs; plays/shows are orchestrators.
_STALE_SWEEP_ORDER = ("sessions", "invocations")


async def list_running_children(
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
            children.extend(await list_running_children(db, "session", session_row["id"]))
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

    Invocation end time records when cancellation was observed, including
    stale cleanup; it does not reconstruct when an unobserved process died.
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
            # Guard even a competing cancellation: a same-status write must
            # not replace the first recorded end time or its reason.
            expected_statuses={"running"} if entity_type == "invocation" else None,
            # A session's live phase ends with its terminal status, in this one
            # statement: a kill can land on a row a SIGKILLed flow left at its
            # last phase, and a separate clear would be a second write that can
            # be skipped. Invocations have no phase column; they record their
            # end time instead.
            extra_fields=(
                {"ended_at": time.time()}
                if entity_type == "invocation"
                else {"current_phase": None}
                if entity_type == "session"
                else None
            ),
        )
    except TransitionRejectedError:
        # The entity went terminal between the pre-check and this write —
        # nothing to cancel, same as the pre-check `return`s above.
        pass


async def kill_one(
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

    meta = row.get("node_metadata") if isinstance(row.get("node_metadata"), dict) else {}
    if entity_type == "session":
        from lionagi.state.lifecycle.service import _RUNNER_IDENTITY_FIELDS

        # Capture before signalling; missing keys and explicit nulls differ.
        identity = {key: meta[key] for key in _RUNNER_IDENTITY_FIELDS if key in meta}
        generation_present = "cli_worker_generation" in row
        generation = row.get("cli_worker_generation")
    # Asked before the pid is read, not only when one exists — a pid-less row from another
    # host or runtime would otherwise fall through and get cancelled for work still running.
    unaddressable = unaddressable_pid_reason(meta)
    pid: int | None = None
    pid_was_read = unaddressable == "boot_unverifiable"
    if pid_was_read:
        pid = read_pid_from_entity(row)
        unaddressable, _ = _pid_addressability(unaddressable, pid)
    if unaddressable in NOT_JUDGEABLE_HERE | {"boot_unverifiable"}:
        warn(f"  {entity_type} {entity_id[:12]}: {_REFUSED_SIGNAL_REASONS[unaddressable]}")
        return {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "signal": unaddressable,
            "pid": None,
        }

    if not pid_was_read:
        pid = read_pid_from_entity(row)
    signal_used = "no_pid"
    # A degraded run that `li state import` gave a row while it still ran: the row has no pid,
    # the run's own run.json does, and that is the process this kill stops (ADR-0104 Amendment 2).
    degraded = _degraded_run(entity_id) if pid is None and entity_type == "session" else None

    if degraded is not None:
        pid, signal_used = _signal_degraded_process(degraded[1], grace_seconds=grace_seconds)
    elif pid is not None:
        # Only boot_mismatch can still be pending here; it stays out of the early return
        # because the stale sweep treats it as evidence the process is gone, unlike a kill.
        if unaddressable is not None:
            signal_used = unaddressable
        else:
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
    elif entity_type == "session" and row.get("source_kind") == "imported_fs":
        # `li state import` names a row by the run id of every run.json it reads and puts no pid
        # on it. Unless the run is a degraded one (above), its process belongs to the session the
        # run wrote itself, so cancelling this row would stop nothing and say it had.
        signal_used = "imported_no_process"
    else:
        if verbose:
            warn(f"  {entity_type} {entity_id[:12]}: no PID found — skipping OS signal")

    if signal_used == "sigkill":
        reason_code = RunReasons.CANCELLED_FORCE_KILL
        reason_summary = f"Force-killed (SIGKILL after grace period). {user_reason}".strip()
    elif signal_used in _REFUSED_SIGNAL_REASONS:
        action = (
            "escalation skipped"
            if signal_used == "identity_unverifiable_after_sigterm"
            else "kill skipped"
        )
        warn(
            f"  {entity_type} {entity_id[:12]}: {f'pid {pid} ' if pid is not None else ''}"
            f"{_REFUSED_SIGNAL_REASONS[signal_used]} — {action}"
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

    cancel_status = None
    cancel_error = None
    if entity_type == "session":
        # The process we signalled may have exited and the session been resumed
        # during the grace period. Guard the write with the original snapshot,
        # including absent identity keys, rather than re-reading after signalling.
        if not generation_present:
            cancel_status = "unavailable"
            cancel_error = "session generation was not available in the snapshot"
        else:
            try:
                cancel_status = await db.cancel_session_if_worker_generation(
                    entity_id,
                    generation,
                    expected_runner_identity=identity,
                    reason_code=reason_code,
                    reason_summary=reason_summary,
                    evidence=evidence,
                )
            except TimeoutError:
                cancel_status = "unavailable"
                cancel_error = "session cancellation did not finish before the deadline"
            except Exception as exc:
                cancel_status = "unavailable"
                cancel_error = f"guarded cancellation unavailable: {exc}"
        # "already_terminal": the signalled execution recorded its own end during the
        # grace period, so the kill worked and that record is kept as written.
        if cancel_status not in ("applied", "already_terminal") and cancel_error is None:
            cancel_error = {
                "conflict": "session changed before cancellation was recorded",
                "missing": "session no longer exists",
                "rejected": "session cancellation was rejected",
            }.get(cancel_status, f"guarded cancellation returned {cancel_status!r}")
    else:
        await _persist_cancel(
            db,
            entity_type,
            entity_id,
            reason_code=reason_code,
            reason_summary=reason_summary,
            evidence=evidence,
        )
    if degraded is not None:
        _settle_degraded_run(*degraded)

    result = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "signal": signal_used,
        "pid": pid,
    }
    if cancel_status is not None:
        result["cancel_status"] = cancel_status
    if cancel_error is not None:
        result["cancel_error"] = cancel_error
    return result


def _degraded_run(run_id: str) -> tuple[Path, dict[str, Any]] | None:
    """Return a degraded run's readable manifest without following links."""
    from ._runs import PERSISTENCE_DEGRADED_REASON_FIELD, RUNS_ROOT

    # a run id names one directory under RUNS_ROOT: anything that is not a single path
    # component is no run id, and is never joined onto the path
    if not run_id or run_id in (".", "..") or Path(run_id).name != run_id:
        return None
    # and reached through no link, to another run's directory or out of the root: the run.json
    # read, and later written, is the one in the directory the id names
    path = RUNS_ROOT.resolve() / run_id / "run.json"
    if path.resolve() != path:
        return None
    try:
        manifest = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict) or not manifest.get(PERSISTENCE_DEGRADED_REASON_FIELD):
        return None
    return path, manifest


def _signal_degraded_process(
    manifest: dict[str, Any],
    *,
    grace_seconds: float,
) -> tuple[int | None, str]:
    """Signal the process a degraded run recorded on its run.json; ``(pid, signal)``.

    The checks are a session pid's, less the session id such a run never set: host, boot and
    identity mode, then the recorded start time and a lionagi command line, refusing when the
    start time is absent (``no_recorded_identity``, whether or not the pid is alive). A run that
    recorded no pid answers ``(None, "no_recorded_process")``.
    """
    try:
        pid = int(manifest["pid"])
    except (KeyError, TypeError, ValueError):
        return None, "no_recorded_process"
    if pid <= 0:
        return None, "no_recorded_process"

    signal_used, _ = _pid_addressability(unaddressable_pid_reason(manifest), pid)
    if signal_used is None:
        try:
            expected_create_time = float(manifest["pid_create_time"])
        except (KeyError, TypeError, ValueError):
            # The start time is the run's only recorded identity: it set no session id to read
            # off the process. Without it the record cannot show which process the pid named, so
            # the kill cannot say it stopped one, alive or dead: nothing is signalled or written.
            return pid, "no_recorded_identity"
        try:
            signal_used = _terminate_pid(
                pid,
                grace_seconds=grace_seconds,
                expected_cmd="lionagi",
                expected_create_time=expected_create_time,
            )
        except RuntimeError as exc:
            warn(str(exc))
            signal_used = "permission_denied"
    return pid, signal_used


def _imported_degraded_process_identity(run_id: str) -> tuple[_IdentityVerdict, str | None]:
    """Classify a degraded imported run's recorded process.

    A live process whose identity cannot be checked (Linux epoch disagreement, start-time
    mismatch, permission denied, unknown identity) is `unverifiable` with its reason, never
    `not_ours`: it is kept, not swept as a stranger.
    """
    run = _degraded_run(run_id)
    if run is None:
        return "not_ours", None
    manifest = run[1]
    status = manifest.get("status")
    if status != "running":
        return "not_ours", None
    try:
        pid = int(manifest["pid"])
        expected_create_time = float(manifest["pid_create_time"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return "not_ours", None
    if pid <= 0:
        return "not_ours", None
    unaddressable, alive = _pid_addressability(unaddressable_pid_reason(manifest), pid)
    if unaddressable is not None and unaddressable != "boot_unverifiable":
        return "not_ours", None
    if alive is None:
        alive = _pid_alive(pid)
    if not alive:
        return "not_ours", None
    if unaddressable == "boot_unverifiable":
        return "unverifiable", "boot_disagreement"
    reason: list[str] = []
    verdict = check_pid_identity(
        pid, "lionagi", expected_create_time=expected_create_time, _reason_out=reason
    )
    return verdict, reason[0] if reason else None


def imported_degraded_process_is_live(run_id: str) -> bool:
    """Keep unknown imported identities out of legacy boolean reapers."""
    verdict, _ = _imported_degraded_process_identity(run_id)
    return verdict in ("ours", "unverifiable")


def _settle_degraded_run(path: Path, manifest: dict[str, Any]) -> None:
    """Mark a stopped degraded run cancelled on its run.json, unless it wrote its own end.

    A process that took the SIGTERM wrote its own terminal status on the way out; one that was
    force-killed, or had already died, could not, and its manifest still says running. Invocations
    sharing a run id (`LIONAGI_RUN_ID`) share this file, so it is settled only if, read here, it
    still records the process this kill stopped: one that names another, by pid or start time, is
    that invocation's record, and is left as it is. So is one this kill cannot read, since what it
    records cannot be checked. On POSIX the read and the write are made under the run's manifest lock
    (`flock` on a file beside run.json), which every writer of a run.json takes when it can, so no
    other locked record lands between them. A lock this kill cannot take (opening or locking the file
    fails) leaves the file as it is, with a warning; a writer that cannot take it writes without it,
    with a warning, and where `fcntl` does not exist (Windows) nothing is held and the record is settled
    unlocked, last writer wins (ADR-0104 Amendment 2, Limits).
    """
    from ._runs import _atomic_write_json, _run_manifest_lock

    with contextlib.ExitStack() as held:
        try:
            held.enter_context(_run_manifest_lock(path.parent))
        except OSError as exc:
            warn(
                f"  run {path.parent.name}: run.json lock could not be taken ({exc}), left as it is"
            )
            return
        try:
            current = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            warn(f"  run {path.parent.name}: run.json could not be read ({exc}), left as it is")
            return
        if not isinstance(current, dict) or current.get("status") != "running":
            return
        stopped = (manifest.get("pid"), manifest.get("pid_create_time"))
        if (current.get("pid"), current.get("pid_create_time")) != stopped:
            warn(
                f"  run {path.parent.name}: run.json now records another process "
                f"(pid {current.get('pid')}), left as it is"
            )
            return
        current["status"] = "cancelled"
        current["ended_at"] = time.time()
        _atomic_write_json(path, current)


def _kill_degraded_run(path: Path, manifest: dict[str, Any], *, grace_seconds: float) -> int:
    """Stop the process a degraded run recorded, by the same identity checks as a session's."""
    run_id = path.parent.name
    status = manifest.get("status")
    if status != "running":
        log_error(f"run {run_id} is already in terminal state: {status!r} — nothing to kill")
        return 1
    pid, signal_used = _signal_degraded_process(manifest, grace_seconds=grace_seconds)
    if pid is None:
        log_error(f"run {run_id} recorded no process to stop")
        return 1
    if signal_used in _REFUSED_SIGNAL_REASONS:
        action = (
            "escalation skipped"
            if signal_used == "identity_unverifiable_after_sigterm"
            else "kill skipped"
        )
        warn(f"  run {run_id}: pid {pid} {_REFUSED_SIGNAL_REASONS[signal_used]} — {action}")
        return 1
    _settle_degraded_run(path, manifest)
    print(f"killed run {run_id} (signal={signal_used}, pid={pid})")
    return 0


@dataclass
class _WorkerCleanup:
    record: dict[str, Any]
    identity: _RecordedWorkerIdentity | None
    witness: _RecordedGroupWitness | None = None
    refusal: str | None = None
    signalled: str | None = None
    empty: bool = False
    settled: bool = False


def _same_cleanup_session(row: dict[str, Any], session: dict[str, Any]) -> bool:
    """Compare resolved session identity to the worker snapshot, including absence."""
    from lionagi.state.lifecycle.service import _RUNNER_IDENTITY_FIELDS

    absent = object()
    for field in ("id", "status", "cli_worker_generation"):
        if field not in row or field not in session or row[field] != session[field]:
            return False
    for field in ("artifacts_path", "source_kind"):
        if row.get(field, absent) != session.get(field, absent):
            return False
    left = row.get("node_metadata", absent)
    right = session.get("node_metadata", absent)
    if isinstance(left, dict) and isinstance(right, dict):
        for field in _RUNNER_IDENTITY_FIELDS:
            if left.get(field, absent) != right.get(field, absent):
                return False
    elif left != right:
        return False
    return True


def _runner_process_create_time(pid: int) -> tuple[str, float | None]:
    """Read only the session runner identity, independently of other probes."""
    from lionagi.ln._proc import process_create_time

    return process_create_time(pid)


async def _try_recorded_worker_cleanup(
    db: Any,
    row: dict[str, Any],
    *,
    user_reason: str,
    grace_seconds: float,
    snapshot: dict[str, Any] | None = None,
) -> int | None:
    """Handle recorded workers, or leave existing running-session handling intact."""
    from lionagi.state.compat import StateCompatibilityRefused
    from lionagi.state.lifecycle.service import _RUNNER_IDENTITY_FIELDS
    from lionagi.state.reasons import RunReasons

    sid = row["id"]
    if not callable(getattr(db, "snapshot_cli_worker_spawns", None)):
        return None
    if snapshot is None:
        try:
            snapshot = await db.snapshot_cli_worker_spawns(sid)
        except StateCompatibilityRefused:
            return None
        except Exception as exc:
            warn(f"  session {sid[:12]}: worker records could not be read ({exc})")
            return 1
    if snapshot is None:
        log_error(f"session {sid[:12]} changed during cleanup")
        return 1
    session = snapshot["session"]
    status = session["status"]
    generation = session["cli_worker_generation"]
    records = snapshot["spawns"]
    if not _same_cleanup_session(row, session):
        log_error(f"session {sid[:12]} changed during cleanup")
        return 1

    running = status == "running"
    if running:
        meta = session.get("node_metadata")
        meta = meta if isinstance(meta, dict) else {}
        unjudgeable = unaddressable_pid_reason(meta)
        if unjudgeable in NOT_JUDGEABLE_HERE | {"boot_mismatch"}:
            # A non-Linux boot mismatch retains its existing meaning. Other
            # reasons leave this host unable to stop the recorded workers.
            if records and unjudgeable in NOT_JUDGEABLE_HERE:
                warn(
                    f"  session {sid[:12]}: {len(records)} recorded worker group(s) not "
                    f"handled: the runner {_REFUSED_SIGNAL_REASONS[unjudgeable]}"
                )
            return None
        try:
            raw_pid = meta["pid"]
            if isinstance(raw_pid, bool) or not isinstance(raw_pid, (int, float, str)):
                raise ValueError("invalid runner pid")
            pid = int(raw_pid)
            # A float must denote the same integer, not be truncated to a
            # different process. The supported POSIX pid_t is a signed 32-bit
            # value; larger values cannot be passed safely to psutil's OS API.
            if (isinstance(raw_pid, float) and raw_pid != pid) or not 1 < pid <= 2**31 - 1:
                raise ValueError("invalid runner pid")
        except (KeyError, ValueError, TypeError, OverflowError):
            if unjudgeable == "boot_unverifiable":
                # Preserve the existing boot-mismatch refusal for an invalid runner PID.
                return None
            if records:
                warn(f"worker group for session {sid[:12]} could not be confirmed stopped")
                return 1
            return None
        if unjudgeable == "boot_unverifiable":
            unjudgeable, _ = _pid_addressability(unjudgeable, pid)
            if unjudgeable == "boot_unverifiable" and records:
                warn(
                    f"  session {sid[:12]}: {len(records)} recorded worker group(s) not "
                    f"handled: the runner {_REFUSED_SIGNAL_REASONS[unjudgeable]}"
                )
            return None
        state, _ = _runner_process_create_time(pid)
        if state == "unknown" and records:
            log_error(f"session {sid[:12]}: recorded runner could not be verified")
            return 1
        if state != "gone":
            return None
        evidence = {
            "kind": "kill_event",
            "signal": "already_dead",
            "pid": pid,
            "killed_at": time.time(),
        }
        if user_reason:
            evidence["user_reason"] = user_reason
        try:
            outcome = await db.cancel_session_if_worker_generation(
                sid,
                generation,
                reason_code=RunReasons.CANCELLED_MANUAL_KILL,
                reason_summary=f"Manually cancelled via `li kill`. {user_reason}".strip(),
                evidence=evidence,
                expected_runner_identity={
                    key: meta[key] for key in _RUNNER_IDENTITY_FIELDS if key in meta
                },
            )
        except TimeoutError:
            log_error(f"session {sid[:12]} cancellation did not finish before the deadline")
            return 1
        except Exception as exc:
            warn(f"  session {sid[:12]}: cancellation could not be confirmed ({exc})")
            return 1
        if outcome == "already_terminal":
            # The runner's own end was recorded after the snapshot and is kept as written;
            # the session is handled from here as one that had already ended.
            try:
                current = await db.get_session(sid)
            except Exception as exc:
                warn(f"  session {sid[:12]}: session could not be re-read ({exc})")
                return 1
            if (
                current is None
                or current.get("status") not in SESSION_TERMINAL_STATUSES
                or current.get("cli_worker_generation") != generation
            ):
                log_error(f"session {sid[:12]} changed during cleanup")
                return 1
            running = False
            status = current["status"]
            if not records:
                log_error(
                    f"session {sid[:12]} is already in terminal state: {status!r} — nothing to kill"
                )
                return 1
        elif outcome != "applied":
            log_error(f"session {sid[:12]} changed during cleanup")
            return 1
        else:
            status = "cancelled"
            if not records:
                print(f"killed session {sid[:12]} (signal=already_dead, pid={pid})")
                return 0
    elif not records:
        return None

    workers = []
    for record in records:
        try:
            identity = _RecordedWorkerIdentity(
                pid=record["pid"],
                pgid=record["pgid"],
                pid_create_time=record["pid_create_time"],
                pid_host=record["pid_host"],
                pid_boot_time=record["pid_boot_time"],
                process_identity_mode=record["process_identity_mode"],
            )
            admission = _admit_recorded_worker_group(
                identity, boot_time_tolerance=BOOT_TIME_TOLERANCE
            )
            worker = _WorkerCleanup(record, identity)
            if isinstance(admission, _RecordedGroupWitness):
                worker.witness = admission
            elif isinstance(admission, _RecordedGroupRefusal):
                worker.refusal = admission.reason
                if admission.reason == "leader_not_live":
                    worker.empty = _recorded_worker_group_state(identity) == "empty"
            else:
                worker.refusal = "identity_unverifiable"
            workers.append(worker)
        except (KeyError, TypeError, ValueError, OSError):
            workers.append(_WorkerCleanup(record, None, refusal="identity_unverifiable"))

    changed = False
    guard_failed = False

    async def signal_one(worker: _WorkerCleanup, sig: signal.Signals) -> None:
        nonlocal changed, guard_failed
        try:
            async with db.guard_cli_worker_signal(
                sid, status, generation, worker.record["spawn_id"]
            ) as verdict:
                if verdict != "unchanged":
                    changed = True
                else:
                    await sleep(0)
                    if _recorded_worker_group_matches(worker.witness) == "matches":
                        # The synchronous identity read cannot deliver a cooperative
                        # timeout; refuse a signal if the guard budget elapsed in it.
                        deadline = effective_deadline()
                        if deadline is not None and current_time() >= deadline:
                            raise TimeoutError("worker signal guard deadline expired")
                        try:
                            os.killpg(worker.identity.pgid, sig)
                        except OSError:
                            pass
                        else:
                            worker.signalled = "sigterm" if sig == signal.SIGTERM else "sigkill"
        except Exception as exc:
            guard_failed = True
            warn(f"  session {sid[:12]}: worker signal guard failed ({exc})")

    for worker in workers:
        if worker.witness is not None:
            await signal_one(worker, signal.SIGTERM)
            if changed or guard_failed:
                break
    if not changed and not guard_failed and any(w.signalled for w in workers):
        await sleep(max(0.0, grace_seconds))
        for worker in workers:
            if worker.witness is not None and worker.signalled:
                # Only a matching frozen witness can authorize a KILL; a missing
                # witness does not prove the group empty. signal_one repeats the
                # identity check inside the guard before sending any signal.
                if _recorded_worker_group_matches(worker.witness) == "matches":
                    await signal_one(worker, signal.SIGKILL)
                if changed or guard_failed:
                    break
    if not changed and not guard_failed:
        deadline = time.monotonic() + max(0.0, grace_seconds)
        while True:
            pending = False
            for worker in workers:
                if worker.witness is not None and not worker.empty:
                    worker.empty = _recorded_worker_group_state(worker.identity) == "empty"
                    pending |= not worker.empty
            if not pending or time.monotonic() >= deadline:
                break
            await sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        for worker in workers:
            if worker.empty:
                try:
                    worker.settled = await db.settle_cli_worker_spawn(
                        worker.record["spawn_id"], confirmed_empty_at=time.time()
                    )
                except Exception as exc:
                    warn(f"  session {sid[:12]}: worker settlement failed ({exc})")
        try:
            from lionagi.state.db import _CLI_WORKER_OPERATION_TIMEOUT_SECONDS

            with fail_after(_CLI_WORKER_OPERATION_TIMEOUT_SECONDS):
                current = await db.get_session(sid)
            changed = current is None or (
                current.get("status") != status
                or "cli_worker_generation" not in current
                or current["cli_worker_generation"] != generation
            )
        except Exception as exc:
            guard_failed = True
            warn(f"  session {sid[:12]}: worker session recheck failed ({exc})")

    # The last session read can establish a new status or fail altogether.
    # Never assert that the previous terminal status remains true in either case.
    if changed:
        for worker in workers:
            if worker.signalled and worker.settled:
                print(
                    f"stopped worker for session {sid[:12]} (pid={worker.identity.pid}, "
                    f"pgid={worker.identity.pgid}, signal={worker.signalled})"
                )
        log_error(f"session {sid[:12]} changed during cleanup")
        return 1
    if guard_failed:
        for _worker in workers:
            warn(f"worker group for session {sid[:12]} could not be confirmed stopped")
        return 1

    if not running and any(w.refusal and not w.empty for w in workers):
        log_error(f"session {sid[:12]} is already in terminal state: {status!r} — nothing to kill")
    for worker in workers:
        if worker.signalled and worker.settled:
            print(
                f"stopped worker for session {sid[:12]} (pid={worker.identity.pid}, "
                f"pgid={worker.identity.pgid}, signal={worker.signalled}); "
                f"terminal status remains {status!r}"
            )
        elif worker.refusal and not worker.empty:
            detail = (
                "did not match"
                if worker.refusal == "identity_mismatch"
                else "could not be verified"
            )
            warn(f"  session {sid[:12]}: recorded worker identity {detail} — kill skipped")
        elif not worker.settled or guard_failed:
            warn(
                f"worker group for session {sid[:12]} could not be confirmed stopped; "
                f"terminal status remains {status!r}"
            )
    if not running and workers and all(w.settled and not w.signalled for w in workers):
        log_error(f"session {sid[:12]} is already in terminal state: {status!r} — nothing to kill")
    if running:
        print(f"killed session {sid[:12]} (signal=already_dead, pid={pid})")
    signalled = any(w.signalled for w in workers)
    return (
        0
        if not changed
        and not guard_failed
        and all(w.settled for w in workers)
        and (running or signalled)
        else 1
    )
