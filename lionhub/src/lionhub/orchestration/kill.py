# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Process identity, signalling and cleanup helpers for stopping runs.

The ``li kill`` command and the Studio cancel path share them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import time
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
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
from lionhub.orchestration.console import log_error, warn
from lionhub.orchestration.job import BOOT_TIME_TOLERANCE
from lionhub.orchestration.job import pid_alive as _pid_alive
from lionhub.orchestration.job import recorded_identity_mode as _recorded_identity_mode
from lionhub.orchestration.job import recorded_pid_is_foreign as _recorded_pid_is_foreign


@dataclass(frozen=True)
class CancelTargetResult:
    """Observed process and persistence effects for one cancellation target."""

    entity_type: str
    entity_id: str
    signal: str
    pid: int | None
    signals_sent: tuple[int, ...] = ()
    cancel_status: str | None = None
    cancel_error: str | None = None
    state_committed: bool = False

    @property
    def signal_sent(self) -> bool:
        return bool(self.signals_sent)


class CancelPersistenceError(RuntimeError):
    """A cancellation state write failed, so the outcome is indeterminate."""

    def __init__(
        self, *, entity_type: str, entity_id: str, results: tuple[CancelTargetResult, ...]
    ) -> None:
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.results = results
        self.signal_sent = any(result.signal_sent for result in results)
        super().__init__(
            f"Cancel-path state write failed for {entity_type} {entity_id}; "
            "termination outcome is indeterminate"
        )


_CancelPersistenceError = CancelPersistenceError


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
    # Membership here routes this signal through the early return in `kill_one`, so no
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

# The supported process APIs take a signed 32-bit PID.
_OS_PID_RANGE = range(-(2**31), 2**31)


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


# The CLI package. A background flow and the MCP server's fallback command start it
# with ``-m``, and its name does not begin with the expected command.
_CLI_PACKAGE = "lionhub.serve.cli"


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
        if flag != "-m":
            continue
        for package in (expected_cmd, _CLI_PACKAGE):
            if mod == package or mod.startswith(package + "."):
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


@dataclass(frozen=True)
class _TerminationTarget:
    pid: int
    expected_cmd: str | None
    expected_session_id: str | None = None
    expected_create_time: float | None = None


def _termination_identity(target: _TerminationTarget, *, after_term: bool) -> str | None:
    if target.expected_cmd is None:
        return None
    reasons: list[str] = []
    verdict = check_pid_identity(
        target.pid,
        target.expected_cmd,
        expected_session_id=target.expected_session_id,
        expected_create_time=target.expected_create_time,
        _reason_out=reasons,
    )
    if verdict == "zombie":
        return "sigterm" if after_term else "already_dead"
    if verdict == "unverifiable" and reasons == ["start_mismatch"]:
        return "identity_unverifiable_after_sigterm" if after_term else "identity_unverifiable"
    if verdict != "ours":
        return "identity_mismatch"
    return None


def _process_exited(pid: int) -> bool:
    # A zombie still answers kill -0, but has already exited.
    return not _pid_alive(pid) or _pid_is_zombie(pid)


def _begin_termination(target: _TerminationTarget, signals: list[int]) -> str | None:
    if _process_exited(target.pid):
        return "already_dead"
    refused = _termination_identity(target, after_term=False)
    if refused is not None:
        return refused
    try:
        os.kill(target.pid, signal.SIGTERM)
    except ProcessLookupError:
        return "already_dead"
    except PermissionError as exc:
        raise RuntimeError(
            f"cannot send SIGTERM to pid {target.pid}: {exc}. "
            "Try again as root, or mark the entity cancelled manually."
        ) from exc
    signals.append(signal.SIGTERM)
    return None


def _finish_termination(target: _TerminationTarget, signals: list[int]) -> str:
    if _process_exited(target.pid):
        return "sigterm"
    # The PID may have been reused during grace. Inspect it again just before escalating.
    refused = _termination_identity(target, after_term=True)
    if refused is not None:
        return refused
    try:
        os.kill(target.pid, signal.SIGKILL)
    except ProcessLookupError:
        return "already_dead"
    except PermissionError:
        return "permission_denied"
    signals.append(signal.SIGKILL)
    return "sigkill"


def _finish_terminations(
    targets: Sequence[tuple[_TerminationTarget, list[int]]], *, grace_seconds: float
) -> list[str]:
    if not targets:
        return []
    deadline = time.monotonic() + grace_seconds
    interval = 0.1
    survivors = set(range(len(targets)))
    while survivors:
        if time.monotonic() >= deadline:
            break
        survivors = {index for index in survivors if not _process_exited(targets[index][0].pid)}
        remaining = deadline - time.monotonic()
        if not survivors or remaining <= 0:
            break
        time.sleep(min(remaining, interval))
        interval = min(interval * 2, 0.5)
    return [
        _finish_termination(target, signals) if index in survivors else "sigterm"
        for index, (target, signals) in enumerate(targets)
    ]


def _terminate_pid(
    pid: int,
    grace_seconds: float = 5.0,
    expected_cmd: str | None = None,
    *,
    expected_session_id: str | None = None,
    expected_create_time: float | None = None,
    _signal_record: list[int] | None = None,
) -> str:
    """SIGTERM then SIGKILL, with the one-target command's existing result labels."""
    target = _TerminationTarget(pid, expected_cmd, expected_session_id, expected_create_time)
    signals = _signal_record if _signal_record is not None else []
    outcome = _begin_termination(target, signals)
    if outcome is not None:
        return outcome
    outcome = _finish_terminations(((target, signals),), grace_seconds=grace_seconds)[0]
    # The command has historically reported an attempted escalation in these races.
    return "sigkill" if outcome in {"already_dead", "permission_denied"} else outcome


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
) -> bool:
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
            return False
        if row["status"] in PLAY_TERMINAL_STATUSES:
            return False
        target_status = "blocked"
    elif entity_type == "show":
        row = await db.fetch_one("SELECT status FROM shows WHERE id = ?", (entity_id,))
        if row is None:
            return False
        if row["status"] in SHOW_TERMINAL_STATUSES:
            return False
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
            return False
        if row["status"] != "running":
            return False
        target_status = "cancelled"

    try:
        applied = await db.update_status(
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
        return bool(applied)
    except TransitionRejectedError:
        # The entity went terminal between the pre-check and this write —
        # nothing to cancel, same as the pre-check `return`s above.
        return False


@dataclass(frozen=True)
class _CancelSnapshot:
    entity_type: str
    entity_id: str
    row: dict[str, Any]
    runner_identity: dict[str, Any]
    generation_present: bool
    generation: str | None


def _snapshot_cancel_target(
    entity_type: str, entity_id: str, row: dict[str, Any]
) -> _CancelSnapshot:
    from lionagi.state.lifecycle.service import _RUNNER_IDENTITY_FIELDS

    row_metadata = row.get("node_metadata")
    meta = dict(row_metadata) if isinstance(row_metadata, dict) else {}
    # Persisted markers can contain malformed JSON values. Copy their values as well as
    # the mapping so a mutable marker cannot change the captured write guard.
    identity = deepcopy({key: meta[key] for key in _RUNNER_IDENTITY_FIELDS if key in meta})
    return _CancelSnapshot(
        entity_type,
        entity_id,
        {**row, "node_metadata": {**meta, **identity}},
        identity,
        "cli_worker_generation" in row,
        row.get("cli_worker_generation"),
    )


@dataclass
class _CancelAttempt:
    target: _CancelSnapshot
    result: CancelTargetResult
    process: _TerminationTarget | None = None
    degraded: tuple[Path, dict[str, Any]] | None = None


def _prepare_cancel_attempt(target: _CancelSnapshot, *, verbose: bool = False) -> _CancelAttempt:
    entity_type = target.entity_type
    entity_id = target.entity_id
    meta = target.row["node_metadata"]
    result = CancelTargetResult(entity_type, entity_id, "no_pid", None)
    # The mode and host matter even if the record has no PID.
    unaddressable = unaddressable_pid_reason(meta)
    if unaddressable in NOT_JUDGEABLE_HERE:
        return _CancelAttempt(target, replace(result, signal=unaddressable))

    pid = read_pid_from_entity(target.row)
    if pid is not None and pid not in _OS_PID_RANGE:
        return _CancelAttempt(target, replace(result, signal="identity_unverifiable", pid=pid))
    if unaddressable == "boot_unverifiable":
        unaddressable, _ = _pid_addressability(unaddressable, pid)
        if unaddressable == "boot_unverifiable":
            return _CancelAttempt(target, replace(result, signal=unaddressable))
    if pid is not None:
        result = replace(result, pid=pid)
        if unaddressable is not None:
            return _CancelAttempt(target, replace(result, signal=unaddressable))
        raw_ct = meta.get("pid_create_time")
        try:
            expected_create_time = float(raw_ct) if raw_ct is not None else None
        except (TypeError, ValueError):
            expected_create_time = None
        session_id = entity_id if entity_type == "session" else None
        process = _TerminationTarget(pid, "lionagi", session_id, expected_create_time)
        return _CancelAttempt(target, result, process=process)

    if entity_type == "session":
        # A degraded import has a process in its own run.json. A regular import has no
        # process of its own and must be stopped via its separately persisted session.
        if (degraded := _degraded_run(entity_id)) is not None:
            return _CancelAttempt(target, result, degraded=degraded)
        if target.row.get("source_kind") == "imported_fs":
            return _CancelAttempt(target, replace(result, signal="imported_no_process"))
    if verbose:
        warn(f"  {entity_type} {entity_id[:12]}: no PID found — skipping OS signal")
    return _CancelAttempt(target, result)


def _signal_cancel_targets(
    targets: Sequence[_CancelSnapshot], *, grace_seconds: float
) -> list[_CancelAttempt]:
    attempts = [_prepare_cancel_attempt(target) for target in targets]
    for attempt in attempts:
        if attempt.degraded is not None:
            pid, refusal, process = _degraded_termination_target(attempt.degraded[1])
            attempt.result = replace(attempt.result, pid=pid, signal=refusal or "no_pid")
            attempt.process = process

    pending: list[tuple[_TerminationTarget, list[int]]] = []
    waiting: list[_CancelAttempt] = []
    for attempt in attempts:
        if attempt.process is None:
            continue
        signals: list[int] = []
        try:
            outcome = _begin_termination(attempt.process, signals)
        except RuntimeError as exc:
            warn(str(exc))
            outcome = "permission_denied"
        if outcome is None:
            waiting.append(attempt)
            pending.append((attempt.process, signals))
        else:
            attempt.result = replace(attempt.result, signal=outcome, signals_sent=tuple(signals))

    outcomes = _finish_terminations(pending, grace_seconds=grace_seconds)
    for attempt, (_, signals), outcome in zip(waiting, pending, outcomes, strict=True):
        attempt.result = replace(attempt.result, signal=outcome, signals_sent=tuple(signals))
    return attempts


def _warn_cancel_refusal(result: CancelTargetResult) -> None:
    message = _REFUSED_SIGNAL_REASONS[result.signal]
    target = f"  {result.entity_type} {result.entity_id[:12]}: "
    if result.signal in NOT_JUDGEABLE_HERE | {"boot_unverifiable"} and result.pid is None:
        warn(f"{target}{message}")
        return
    action = (
        "escalation skipped"
        if result.signal == "identity_unverifiable_after_sigterm"
        else "kill skipped"
    )
    pid = f"pid {result.pid} " if result.pid is not None else ""
    warn(f"{target}{pid}{message} — {action}")


async def _write_cancellation(
    db: Any, target: _CancelSnapshot, result: CancelTargetResult, *, user_reason: str
) -> CancelTargetResult:
    from lionagi.state.reasons import RunReasons

    if result.signal == "sigkill":
        reason_code = RunReasons.CANCELLED_FORCE_KILL
        reason_summary = f"Force-killed (SIGKILL after grace period). {user_reason}".strip()
    else:
        reason_code = RunReasons.CANCELLED_MANUAL_KILL
        reason_summary = f"Manually cancelled via `li kill`. {user_reason}".strip()

    evidence: dict[str, Any] = {
        "kind": "kill_event",
        "signal": result.signal,
        "pid": result.pid,
        "killed_at": time.time(),
    }
    if user_reason:
        evidence["user_reason"] = user_reason

    if target.entity_type != "session":
        state_committed = await _persist_cancel(
            db,
            target.entity_type,
            target.entity_id,
            reason_code=reason_code,
            reason_summary=reason_summary,
            evidence=evidence,
        )
        return replace(result, state_committed=state_committed)
    if not target.generation_present:
        return replace(
            result,
            cancel_status="generation_unavailable",
            cancel_error="session generation was not available in the snapshot",
        )

    # Use the snapshot taken before signalling. A resume can replace the worker during grace.
    status = await db.cancel_session_if_worker_generation(
        target.entity_id,
        target.generation,
        expected_runner_identity=target.runner_identity,
        reason_code=reason_code,
        reason_summary=reason_summary,
        evidence=evidence,
    )
    error = None
    if status not in ("applied", "already_terminal"):
        error = {
            "conflict": "session changed before cancellation was recorded",
            "missing": "session no longer exists",
            "rejected": "session cancellation was rejected",
        }.get(status, f"guarded cancellation returned {status!r}")
    return replace(
        result, cancel_status=status, cancel_error=error, state_committed=status == "applied"
    )


async def _settle_cancel_attempt(
    db: Any, attempt: _CancelAttempt, *, user_reason: str
) -> tuple[CancelTargetResult, Exception | None, Exception | None]:
    result = attempt.result
    if result.signal in NOT_STOPPED_SIGNALS:
        _warn_cancel_refusal(result)
        return result, None, None

    database_error = None
    manifest_error = None
    try:
        result = await _write_cancellation(db, attempt.target, result, user_reason=user_reason)
    except Exception as exc:
        database_error = exc
        result = replace(result, cancel_status="write_failed")

    # The manifest has its own locked identity guard. A missing database generation
    # does not preclude settlement when the recorded process was confirmed stopped.
    if attempt.degraded is not None and result.signal in {"already_dead", "sigterm", "sigkill"}:
        try:
            if await asyncio.to_thread(_settle_degraded_run, *attempt.degraded):
                result = replace(result, state_committed=True)
        except Exception as exc:
            manifest_error = exc
    return result, database_error, manifest_error


async def cancel_targets(
    db: Any,
    targets: Sequence[tuple[str, str, dict[str, Any]]],
    *,
    user_reason: str,
    grace_seconds: float = 5.0,
) -> tuple[CancelTargetResult, ...]:
    """Cancel targets supplied deepest first with one process grace deadline.

    Finish eligible settlements in that order and include the observed effects in a
    typed error if any cancellation write fails. The caller owns request shielding.
    """
    snapshots = tuple(_snapshot_cancel_target(*target) for target in targets)
    attempts = await asyncio.to_thread(
        _signal_cancel_targets, snapshots, grace_seconds=grace_seconds
    )
    results: list[CancelTargetResult] = []
    first_failure: tuple[_CancelSnapshot, Exception] | None = None
    for attempt in attempts:
        result, database_error, manifest_error = await _settle_cancel_attempt(
            db, attempt, user_reason=user_reason
        )
        error = database_error if database_error is not None else manifest_error
        if error is not None and first_failure is None:
            first_failure = (attempt.target, error)
        results.append(result)

    report = tuple(results)
    if first_failure is not None:
        target, cause = first_failure
        raise CancelPersistenceError(
            entity_type=target.entity_type, entity_id=target.entity_id, results=report
        ) from cause
    return report


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
    """Kill one entity and return the command's dictionary result."""
    target = _snapshot_cancel_target(entity_type, entity_id, row)
    attempt = _prepare_cancel_attempt(target, verbose=verbose)
    if attempt.degraded is not None:
        pid, outcome = _signal_degraded_process(attempt.degraded[1], grace_seconds=grace_seconds)
        attempt.result = replace(attempt.result, pid=pid, signal=outcome)
    elif (process := attempt.process) is not None:
        try:
            outcome = _terminate_pid(
                process.pid,
                grace_seconds=grace_seconds,
                expected_cmd=process.expected_cmd,
                expected_session_id=process.expected_session_id,
                expected_create_time=process.expected_create_time,
            )
        except RuntimeError as exc:
            warn(str(exc))
            outcome = "permission_denied"
        attempt.result = replace(attempt.result, signal=outcome)

    result, database_error, manifest_error = await _settle_cancel_attempt(
        db, attempt, user_reason=user_reason
    )
    if manifest_error is not None:
        raise manifest_error
    if database_error is not None:
        if entity_type != "session":
            raise database_error
        error = (
            "session cancellation did not finish before the deadline"
            if isinstance(database_error, TimeoutError)
            else f"guarded cancellation unavailable: {database_error}"
        )
        result = replace(result, cancel_status="unavailable", cancel_error=error)

    wire = {
        "entity_type": result.entity_type,
        "entity_id": result.entity_id,
        "signal": result.signal,
        "pid": result.pid,
    }
    if result.cancel_status is not None:
        wire["cancel_status"] = (
            "unavailable"
            if result.cancel_status == "generation_unavailable"
            else result.cancel_status
        )
    if result.cancel_error is not None:
        wire["cancel_error"] = result.cancel_error
    return wire


def _run_manifest_path(run_id: str) -> Path | None:
    """Return the run.json path a run id names, or None when the id is not safe to join."""
    from lionhub.orchestration.runs import _resolved_run_state_dir

    # a run id names one directory under RUNS_ROOT: anything that is not a single path
    # component is no run id, and is never joined onto the path
    run_dir = _resolved_run_state_dir(run_id)
    if run_dir is None:
        return None
    # and reached through no link, to another run's directory or out of the root: the run.json
    # read, and later written, is the one in the directory the id names
    path = run_dir / "run.json"
    if path.resolve() != path:
        return None
    return path


def _degraded_run(run_id: str) -> tuple[Path, dict[str, Any]] | None:
    """Return a degraded run's readable manifest without following links."""
    from lionhub.orchestration.runs import PERSISTENCE_DEGRADED_REASON_FIELD

    path = _run_manifest_path(run_id)
    if path is None:
        return None
    try:
        manifest = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict) or not manifest.get(PERSISTENCE_DEGRADED_REASON_FIELD):
        return None
    return path, manifest


def _degraded_termination_target(
    manifest: dict[str, Any],
) -> tuple[int | None, str | None, _TerminationTarget | None]:
    try:
        pid = int(manifest["pid"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, "no_recorded_process", None
    if pid not in _OS_PID_RANGE:
        return pid, "identity_unverifiable", None
    if pid <= 0:
        return None, "no_recorded_process", None

    refusal, _ = _pid_addressability(unaddressable_pid_reason(manifest), pid)
    if refusal is not None:
        return pid, refusal, None
    try:
        expected_create_time = float(manifest["pid_create_time"])
    except (KeyError, TypeError, ValueError):
        # A degraded run never recorded a session ID. The start time is its only
        # process identity, even when the recorded PID is already gone.
        return pid, "no_recorded_identity", None
    return pid, None, _TerminationTarget(pid, "lionagi", expected_create_time=expected_create_time)


def _signal_degraded_process(
    manifest: dict[str, Any],
    *,
    grace_seconds: float,
    _signal_record: list[int] | None = None,
) -> tuple[int | None, str]:
    """Signal the process a degraded run recorded on its run.json."""
    pid, refusal, process = _degraded_termination_target(manifest)
    if process is None:
        assert refusal is not None
        return pid, refusal
    try:
        kwargs = {
            "grace_seconds": grace_seconds,
            "expected_cmd": process.expected_cmd,
            "expected_create_time": process.expected_create_time,
        }
        if _signal_record is not None:
            kwargs["_signal_record"] = _signal_record
        signal_used = _terminate_pid(process.pid, **kwargs)
    except RuntimeError as exc:
        warn(str(exc))
        signal_used = "permission_denied"
    return pid, signal_used


def _imported_degraded_process_identity(run_id: str) -> tuple[_IdentityVerdict, str | None]:
    """Classify the process an imported run's manifest recorded, degraded or not.

    The manifest is the run's own record, so it is read whether or not it carries the degraded
    marker. An id that names no run directory, no manifest, a run that is not ``running`` and a
    recorded process that is gone (one from an earlier boot included) are `not_ours`. Whatever
    leaves it unknown whether the run still owns a live process is `unverifiable` with its reason,
    never `not_ours`: a pid recorded on another host, in an identity mode this CLI cannot signal,
    or as a shared in-process host that is still alive (the `NOT_JUDGEABLE_HERE` reasons); a
    manifest that is present but unreadable (``manifest_unreadable``); no recorded pid
    (``no_recorded_process``); a Linux boot epoch disagreement (``boot_disagreement``); no
    recorded start time (``no_recorded_identity``); or the identity check's own reason for a live
    process it cannot confirm (start-time mismatch, permission denied). Such a run is kept, not
    swept as a stranger.
    """
    path = _run_manifest_path(run_id)
    if path is None:
        return "not_ours", None
    try:
        manifest = json.loads(path.read_text())
    except (FileNotFoundError, NotADirectoryError):
        return "not_ours", None
    except (OSError, ValueError):
        return "unverifiable", "manifest_unreadable"
    if not isinstance(manifest, dict):
        return "unverifiable", "manifest_unreadable"
    if manifest.get("status") != "running":
        return "not_ours", None
    try:
        pid = int(manifest["pid"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return "unverifiable", "no_recorded_process"
    if pid <= 0:
        return "unverifiable", "no_recorded_process"
    unaddressable, alive = _pid_addressability(unaddressable_pid_reason(manifest), pid)
    if unaddressable in NOT_JUDGEABLE_HERE:
        # The recorded pid cannot say from here whether the run still holds a process: it is
        # another host's, it uses an identity mode this CLI cannot signal, or it is the host
        # process the run shares. Only a shared host process on this machine that is gone
        # settles it.
        if (
            unaddressable == "in_process"
            and not _recorded_pid_is_foreign(manifest)
            and not _pid_alive(pid)
        ):
            return "not_ours", None
        return "unverifiable", unaddressable
    if unaddressable is not None and unaddressable != "boot_unverifiable":
        return "not_ours", None
    if alive is None:
        alive = _pid_alive(pid)
    if not alive:
        return "not_ours", None
    if unaddressable == "boot_unverifiable":
        return "unverifiable", "boot_disagreement"
    try:
        expected_create_time = float(manifest["pid_create_time"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return "unverifiable", "no_recorded_identity"
    reason: list[str] = []
    verdict = check_pid_identity(
        pid, "lionagi", expected_create_time=expected_create_time, _reason_out=reason
    )
    return verdict, reason[0] if reason else None


def imported_degraded_process_is_live(run_id: str) -> bool:
    """Whether an imported run's recorded process may still be alive, for the boolean reapers.

    True for a process that is ours and for a run whose process cannot be ruled out
    (`unverifiable`); False when the run is `not_ours` or its recorded process is a zombie.
    """
    verdict, _ = _imported_degraded_process_identity(run_id)
    return verdict in ("ours", "unverifiable")


def _settle_degraded_run(path: Path, manifest: dict[str, Any]) -> bool:
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
    from lionhub.orchestration.runs import _atomic_write_json, _run_manifest_lock

    with contextlib.ExitStack() as held:
        try:
            held.enter_context(_run_manifest_lock(path.parent))
        except OSError as exc:
            warn(
                f"  run {path.parent.name}: run.json lock could not be taken ({exc}), left as it is"
            )
            return False
        try:
            current = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            warn(f"  run {path.parent.name}: run.json could not be read ({exc}), left as it is")
            return False
        if not isinstance(current, dict) or current.get("status") != "running":
            return False
        stopped = (manifest.get("pid"), manifest.get("pid_create_time"))
        if (current.get("pid"), current.get("pid_create_time")) != stopped:
            warn(
                f"  run {path.parent.name}: run.json now records another process "
                f"(pid {current.get('pid')}), left as it is"
            )
            return False
        current["status"] = "cancelled"
        current["ended_at"] = time.time()
        _atomic_write_json(path, current)
        return True


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
            # A truncated float could name a different process.
            if (
                (isinstance(raw_pid, float) and raw_pid != pid)
                or pid <= 1
                or pid not in _OS_PID_RANGE
            ):
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
