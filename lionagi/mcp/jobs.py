# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Background job engine for the lionagi MCP server.

See docs/internals/mcp.md#jobs-engine for the run lifecycle, the three writers
of a terminal state, and the per-run locking/outcome contract.
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import math
import os
import shlex
import signal
import stat
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from lionagi.ln._json_dump import raise_if_non_finite
from lionagi.ln._proc import (
    CREATE_TIME_TOLERANCE,
    live_group_members,
    pinned_member,
    process_create_time,
    process_marker,
    start_time_matches,
)

from . import config

_log = logging.getLogger(__name__)

# Per-run mutation lock: platform's own advisory file lock.
if sys.platform == "win32":  # pragma: no cover - POSIX is what CI runs
    _fcntl = None
    try:
        import msvcrt as _msvcrt
    except ImportError:
        _msvcrt = None
else:
    import fcntl as _fcntl

    _msvcrt = None

# li subcommand for each job kind ("orchestrate" is the canonical parser name;
# the `o` alias also works). "play" spawns as `orchestrate flow -p NAME` (the
# expanded form of `li play`'s sugar) because the sugar's NAME-probing path
# rejects a playbook's own declared args, which every submit needs since it
# always prepends --notify.
_KIND_ARGV: dict[str, list[str]] = {
    "agent": ["agent"],
    "flow": ["orchestrate", "flow"],
    "fanout": ["orchestrate", "fanout"],
    "play": ["orchestrate", "flow"],
}

# Statuses that mean the work came out right. Deliberately narrow, and used ONLY
# to pick `outcome` for a run already established terminal by a recorded end —
# never to decide whether a run ended. A status this build has never heard of is
# reported verbatim and classified as a failure, because the failure mode of a
# stale success list is a timeout or an empty completion read back as a success.
_SUCCEEDED_STATUSES = frozenset({"completed"})

# Statuses that mean the run was stopped on purpose. Separated from failure
# because "someone cancelled this" and "this went wrong" call for different
# things from a caller, and reporting a cancellation as a failure invites a
# retry of work that was deliberately abandoned.
_CANCELLED_STATUSES = frozenset({"cancelled", "aborted"})

# Short advisory qualifiers for a terminal outcome. A caller may surface one; it
# never needs one to decide `outcome`.
_REASON_BY_STATUS = {
    "completed_empty": "no_artifacts",
}
_SPAWN_FAILED_REASON = "spawn_failed"

# How a run came out when its process is conclusively gone and nothing
# authoritative ever said what the work did. It is not a failure: the work may
# well have had its intended effect before the process died, and no producer
# survived to say either way. That is exactly why a caller may retry a `failed`
# run under its own policy and must not automatically retry this one — an
# external side effect may already have committed.
#
# The value is the one the closed outcome vocabulary already reserves for a run
# that ended and whose result cannot be established, rather than a new word for
# this producer. Widening a closed vocabulary without moving the contract
# version would be a silent contract change; what makes this transition
# recognisable is the reason code and the terminal source beside it, which is
# where the mechanism was always meant to live.
OUTCOME_INDETERMINATE = "indeterminate"
LOST_REASON = "process_gone_without_outcome"

# A narrower sibling of LOST_REASON: the run's own directory carries a
# notify_outcome.json recording that its terminal notice was never delivered
# (persistence was unavailable, or a direct-delivery attempt itself failed —
# see lionagi/cli/orchestrate/_notify.py). Unlike LOST_REASON this is not pure
# silence — the run said something about its own end before going quiet — but
# it is still not a recorded status, so it stays OUTCOME_INDETERMINATE. Never
# assigned from the *absence* of that file: retention prunes run directories,
# and an absent file is exactly what true silence also looks like.
LOST_REASON_NOTICE_RECORDED_UNDELIVERED = "process_gone_notice_recorded_undelivered"

# Outcome values valid to report back from a job record.
_OUTCOMES = frozenset({"succeeded", "failed", "cancelled", OUTCOME_INDETERMINATE})

# Which mechanism wrote a run's terminal state (separate from `status`, the
# open producer vocabulary, and `reason_code`, why it ended that way).
TERMINAL_SOURCE_HOOK = "cli_terminal_hook"
TERMINAL_SOURCE_LIFECYCLE = "lifecycle_cache"
TERMINAL_SOURCE_SPAWN_FAILURE = "spawn_failure"
TERMINAL_SOURCE_ORPHAN_REAPER = "mcp_orphan_reaper"
TERMINAL_SOURCE_KILL = "mcp_kill"

# How `finished_at` was arrived at, recorded beside it because the two answer
# different questions and only one of them is always knowable.
#
# OBSERVED means the run's own end was seen: the process was killed here, the
# terminal hook fired, the spawn failed at that moment, or the lifecycle record
# carried a real end time.
#
# UPPER_BOUND means nobody saw the end and the stored value is when the end was
# *noticed*. It is a real bound (the run had ended by then) and it is never the
# duration. Written by the paths that cannot know better, so that "we do not
# know when this ended" is representable instead of being replaced by a
# confident wrong value.
#
# A record written before this field existed carries neither. Absent is its own
# answer and must not be read as OBSERVED: that would restate the same guess one
# layer up.
FINISHED_AT_OBSERVED = "observed"
FINISHED_AT_UPPER_BOUND = "upper_bound"

# Why a guarded mutation has no record to work on: RECORD_ABSENT means the run
# is unknown; LOCK_UNAVAILABLE means the write was refused, so a caller may retry.
RECORD_ABSENT = "absent"
LOCK_UNAVAILABLE = "lock_unavailable"

# Orphan-observer evidence, deliberately bounded to kind + finding — no argv,
# environment, logs, or secrets belong on a record any caller may read back.
EVIDENCE_PROCESS_GONE = "process_identity_conclusively_gone"

# The only findings that positively establish a run's process is gone and admit
# a terminal transition; a finding added later isn't conclusive until listed here.
FINDING_PID_ABSENT = "pid_absent"
FINDING_DISAPPEARED_DURING_PROBE = "disappeared_during_probe"
FINDING_PID_RECYCLED = "pid_recycled"
CONCLUSIVE_FINDINGS = frozenset(
    {FINDING_PID_ABSENT, FINDING_DISAPPEARED_DURING_PROBE, FINDING_PID_RECYCLED}
)

# Name of the per-run mutation lock, kept beside the record it guards.
_LOCK_NAME = "job.lock"

# Lifecycle read is a control-plane query against a local store, consulted from
# inside a caller's own poll; anything slower is treated as unavailable.
LIFECYCLE_TIMEOUT_SECONDS = 20.0

# The most the lifecycle command may write on its result channel.
_LIFECYCLE_OUTPUT_LIMIT = 1_000_000
_PERSISTENCE_DEGRADED_REASON_FIELD = "persistence_degraded_reason"

# Bounds for wait(). The maximum sits below ordinary MCP client timeouts so a
# bounded observation returns partial results rather than being cut off mid-call.
WAIT_MAX_SECONDS = 600.0
WAIT_MIN_POLL_SECONDS = 0.05
WAIT_MAX_POLL_SECONDS = 60.0

# How long a spawn may sit unresolved before wait() stops holding its window
# open — a polling decision, not a verdict; it never terminalises a run. Chosen
# as a defensible default (see docs/internals/mcp.md#unresolved-spawn-window),
# not derived: a caller wanting a different line can compute one from the spawn
# phase and submission time already on every entry.
UNRESOLVED_SPAWN_AFTER_SECONDS = WAIT_MAX_SECONDS

# The terminal hook module, invoked by the CLI's --notify by absolute
# interpreter path so it runs regardless of PATH in the CLI's environment.
_NOTIFY_MODULE = "lionagi.mcp._notify_hook"

# Re-exported so this surface's own comparisons and any sweep that has to agree
# with them read the same number.
_CREATE_TIME_TOLERANCE = CREATE_TIME_TOLERANCE

# kill() reason codes: the human `reason` explains the case; the code is what a
# caller can branch on without matching prose. Distinctions that matter for a
# caller's next move (retry vs. settled, read-failure vs. damaged-but-parsed) are
# split into separate codes; see docs/internals/mcp.md#kill-reason-codes.
KILL_NO_SUCH_JOB = "no_such_job"
KILL_RECORD_UNREADABLE = "job_record_unreadable"
KILL_RECORD_WRONG_SHAPE = "job_record_wrong_shape"
KILL_RECORD_FOREIGN_RUN = "job_record_names_another_run"
KILL_NO_PID = "no_pid_on_record"
KILL_SIGNALLED = "signalled"
KILL_NOT_RECORDED = "kill_not_recorded"
KILL_PROCESS_GONE = "process_gone"
KILL_PERMISSION_DENIED = "permission_denied"
KILL_NO_RECORDED_IDENTITY = "no_recorded_process_identity"
KILL_IDENTITY_UNUSABLE = "recorded_identity_unusable"
KILL_PID_RECYCLED = "pid_recycled"
KILL_LEADER_UNVERIFIABLE = "leader_identity_unreadable"
KILL_LEADER_IDENTITY_CHANGED = "leader_identity_changed"
KILL_LEADER_GROUP_MISMATCH = "leader_group_mismatch"
KILL_LEADER_GROUP_UNREADABLE = "leader_group_unreadable"
KILL_GROUP_GONE = "group_gone"
KILL_GROUP_FOREIGN = "group_belongs_to_another_run"
KILL_GROUP_MARKERS_CONFLICT = "group_markers_conflict"
KILL_GROUP_PREDATES_RUN = "group_predates_run"
KILL_GROUP_SCAN_INCOMPLETE = "group_scan_incomplete"
KILL_GROUP_OWNERSHIP_UNPROVEN = "group_ownership_unproven"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id() -> str:
    """Mint a run_id in the CLI's own format: ``YYYYMMDDTHHMMSS-<6hex>``."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{ts}-{uuid4().hex[:6]}"


# A run of taken ids this large is the shape of something else being wrong (a
# pinned clock, a directory reporting every name as taken) — retry without a
# bound would hang the submission instead of saying so.
_RUN_ID_ATTEMPTS = 8

# Fixed list of what a submission writes into its own reserved directory, so the
# writes and the cleanup that removes them can't drift apart.
_PROMPT_FILENAME = "prompt.txt"
_MCP_SNAPSHOT_FILENAME = "mcp-servers.json"
_RESERVATION_CONTENTS = (_PROMPT_FILENAME, _MCP_SNAPSHOT_FILENAME)
# Written into a reservation directory only when its giveback could not remove
# it, never as part of a submission's own state — kept out of
# _RESERVATION_CONTENTS so a later giveback attempt doesn't try to unlink it
# before the directory itself is gone.
_RESERVATION_STRANDED_MARKER = "RESERVATION_ROLLBACK_INCOMPLETE"


def _reserve_run_dir() -> tuple[str, Path]:
    """Mint a run_id nobody else holds, and return it with its directory.

    Minting and directory creation are one step: ``mkdir`` without ``exist_ok``
    atomically creates-or-rejects, closing the check-then-create race window
    (two submissions in the same second can mint the same six hex digits).
    """
    for _ in range(_RUN_ID_ATTEMPTS):
        run_id = new_run_id()
        d = config.job_dir(run_id)
        try:
            d.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        return run_id, d
    raise RuntimeError(
        f"could not reserve a run directory under {config.JOBS_DIR}: "
        f"{_RUN_ID_ATTEMPTS} freshly minted ids were all already taken"
    )


def _discard_reservation(d: Path) -> bool:
    """Give a reserved directory back, along with what a submission put in it.

    A submission that fails partway through writing has already left files
    behind, so removing only an empty directory would give the reservation back
    for some failures and not others. The files a submission writes into its own
    reservation are named here, and only those: they are addressed as fixed
    names under *d*, never through a path a caller handed in. A caller may name
    an MCP config that lives anywhere at all, and that file is theirs — it is not
    part of this reservation whatever it points at, and nothing here can be
    talked into deleting it.

    ``rmdir`` refuses a directory with anything in it, and that refusal stays the
    safety here rather than becoming a check taken beforehand: whatever this is
    asked to remove, a directory holding a run's state survives it — anything not
    on the short list above stops the removal. A removal that fails for any other
    reason leaves a directory nobody claimed, which is worth less than the error
    that sent us here.

    Returns whether the directory is actually gone afterward. When it is not —
    the one case this function suppresses rather than raises for — a marker is
    left in what remains of it, so a directory found later under the jobs root
    with no job record reads as a giveback that could not run rather than as one
    that succeeded; both leave the same absence of a job otherwise, and nothing
    else here tells them apart.
    """
    for name in _RESERVATION_CONTENTS:
        try:
            (d / name).unlink()
        except OSError:
            pass
    try:
        d.rmdir()
    except OSError:
        pass
    given_back = not d.exists()
    if not given_back:
        with contextlib.suppress(OSError):
            (d / _RESERVATION_STRANDED_MARKER).write_text(
                "this reservation's giveback could not fully run: the "
                "directory below the jobs root was left behind rather than "
                "removed or claimed by a job.\n"
            )
    return given_back


def _discard_reservation_and_warn(d: Path, run_id: str) -> None:
    """Give a reservation back, and say so when the giveback itself fails.

    ``_discard_reservation``'s own marker only helps an operator who later
    goes looking under the jobs root. The boolean it returns is the only
    signal available at the moment the failure actually happens, so every
    caller that discards a reservation on an error path must act on it here
    rather than let a `False` disappear along with the exception it rode in on.

    The boolean says nothing about whether the marker write itself landed —
    that write is best-effort and suppresses its own ``OSError`` — so this
    checks the marker's actual presence afterward rather than assuming it from
    the directory surviving. An operator reading the warning must not be sent
    looking for a file that was never written.
    """
    if _discard_reservation(d):
        return
    marker = d / _RESERVATION_STRANDED_MARKER
    if marker.exists():
        _log.warning(
            "reservation rollback for run %s could not remove %s; marked %s",
            run_id,
            d,
            marker,
        )
    else:
        _log.warning(
            "reservation rollback for run %s could not remove %s; the "
            "stranding marker could not be written either",
            run_id,
            d,
        )


# --- record I/O ----------------------------------------------------------------


def _write_job(record: dict[str, Any]) -> None:
    # Publish atomically: write a unique temp file in the same directory, then
    # os.replace() it into place. os.replace is atomic on the same filesystem, so
    # a concurrent reader (status / list_jobs) never observes a torn file — and a
    # failed write leaves the previous record intact instead of a partial one. The
    # temp name is per-write-unique so two writers to the same run (the pid-attach
    # write in submit() and the terminal hook) never collide on the temp itself.
    # This makes each publish all-or-nothing; it does not serialize two writers,
    # so a read-modify-write pair can still lose an update (last replace wins).
    # Checked before the temp file is opened, so a refused record leaves neither a
    # staging file nor a published one. json.dumps would write a non-finite float
    # as the bare token NaN or Infinity, which only Python reads back: every
    # reader of this record that is not Python — and every strict parser — would
    # fail on it long after the run that wrote it. The start time already has a
    # representation for "unreadable" and it is null, so nothing here encodes a
    # sentinel that this refuses.
    raise_if_non_finite(record)
    d = config.job_dir(record["run_id"])
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".job.json.{os.getpid()}.{uuid4().hex[:8]}.tmp"
    try:
        tmp.write_text(json.dumps(record, indent=2))
        os.replace(tmp, d / "job.json")
    except BaseException:
        # Catch every exception (not just OSError) so a staging file left by an
        # interrupt doesn't block _discard_reservation's rmdir cleanup later;
        # only suppress OSError from the cleanup itself so a stop request still
        # propagates rather than being swallowed by a failed unlink.
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def _write_mcp_server_snapshot(path: Path, servers: dict[str, Any]) -> None:
    """Write the ``{"mcpServers": ...}`` file the spawned child is pointed at."""
    raise_if_non_finite({"mcpServers": servers})
    path.write_text(json.dumps({"mcpServers": servers}, indent=2))


def _lock_fd(fd: int) -> None:
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_EX)
        return
    if _msvcrt is not None:  # pragma: no cover - POSIX is what CI runs
        os.lseek(fd, 0, os.SEEK_SET)
        _msvcrt.locking(fd, _msvcrt.LK_LOCK, 1)


def _unlock_fd(fd: int) -> None:
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_UN)
        return
    if _msvcrt is not None:  # pragma: no cover - POSIX is what CI runs
        os.lseek(fd, 0, os.SEEK_SET)
        _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)


@dataclass
class _GuardedJob:
    """The record a mutation holds while inside the per-run lock.

    *record* is reread inside the lock (never a caller-supplied snapshot).
    *state* distinguishes "no run submitted" from "record on disk and damaged".
    """

    record: dict[str, Any] | None
    state: str


@dataclass(frozen=True)
class WriteResult:
    """Outcome of one guarded mutation.

    *record* is None either when nothing was ever recorded, or when the write
    was refused because its critical section couldn't be entered — ``refused``
    (below) is what tells those two apart, so a caller never mistakes a refused
    write for a completed one and announces an end that isn't on disk.
    """

    record: dict[str, Any] | None
    state: str

    @property
    def refused(self) -> bool:
        """The mutation was not attempted: the record could not be serialized."""
        return self.state == LOCK_UNAVAILABLE


@contextlib.contextmanager
def _locked_job(run_id: str) -> Iterator[_GuardedJob]:
    """Read-modify-write one run's record inside one per-run critical section.

    See docs/internals/mcp.md#locked-job-contract for why the whole
    reread-merge-publish cycle (not just the final ``os.replace``) must be
    exclusive across processes, and why RECORD_ABSENT/LOCK_UNAVAILABLE must stay
    distinguishable states rather than one collapsing into the other.
    """
    try:
        fd = os.open(config.job_dir(run_id) / _LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    except FileNotFoundError:
        yield _GuardedJob(None, RECORD_ABSENT)
        return
    except OSError:
        yield _GuardedJob(None, LOCK_UNAVAILABLE)
        return
    try:
        _lock_fd(fd)
    except OSError:
        # This context manager's contract is to yield a state, never raise; a
        # close failure while tidying up an untaken lock must not escape instead.
        with contextlib.suppress(OSError):
            os.close(fd)
        yield _GuardedJob(None, LOCK_UNAVAILABLE)
        return
    section_succeeded = False
    try:
        record, state = _read_job_state(run_id)
        guard = _GuardedJob(record, state)
        before = copy.deepcopy(record)
        yield guard
        if guard.record is not None and guard.record != before:
            _write_job(guard.record)
        section_succeeded = True
    finally:
        # Both unlock and close are attempted even if one fails, since a lock
        # nobody released is worse than either failing alone; a close that fails
        # must not be retried (the fd number may have been reassigned by then) —
        # process exit is what bounds a leaked lock.
        if section_succeeded:
            try:
                _unlock_fd(fd)
            finally:
                os.close(fd)
        else:
            try:
                with contextlib.suppress(OSError):
                    _unlock_fd(fd)
            finally:
                with contextlib.suppress(OSError):
                    os.close(fd)


def _short_repr(value: Any, limit: int = 60) -> str:
    """A recorded value, shown as written (not coerced) and bounded in length —
    it came off disk, so the record itself must not choose how long the answer is."""
    shown = repr(value)
    return shown if len(shown) <= limit else f"{shown[:limit]}… ({len(shown)} characters)"


def _read_job_state(run_id: str) -> tuple[dict[str, Any] | None, str]:
    """The job record for *run_id*, and why there isn't one when there isn't.

    ``"absent"`` (no file — run unknown), ``"unreadable"`` (bytes present but
    unreadable/unparseable), and ``"wrong_shape"`` (parses to non-object JSON)
    are distinct: only the first means the run is unknown, the other two mean a
    record is present but damaged. The record is returned only for ``"ok"``.
    Each guard below wraps one read-or-parse expression with no branching, so a
    broad except there classifies damage rather than hiding a bug — do not widen
    a guard to cover more than that if this function grows.
    """
    p = config.job_dir(run_id) / "job.json"
    try:
        raw = p.read_text()
    except FileNotFoundError:
        return None, "absent"
    except Exception:
        return None, "unreadable"
    try:
        record = json.loads(raw)
    except Exception:
        return None, "unreadable"
    if not isinstance(record, dict):
        return None, "wrong_shape"
    return record, "ok"


def _read_job(run_id: str) -> dict[str, Any] | None:
    """The job record, or None when there is no usable one.

    Callers that must tell an unknown run from a damaged file should read
    ``_read_job_state`` directly instead.
    """
    return _read_job_state(run_id)[0]


def _read_lifecycle(run_id: str) -> dict[str, Any] | None:
    """Ask the CLI what the lifecycle store records about *run_id*.

    Spawned as ``li lifecycle <run_id> --machine`` rather than opening the
    database directly, so this package stays the only non-owning reader of a
    schema it doesn't own. Returns None for any failure at all (missing command,
    refusal, timeout, unreadable store) — never treat None as "no record"; treat
    it as "learned nothing" and fall back to what was already known, since the
    alternative is calling a run finished on a read that never happened.
    """
    argv = [*config.li_command(), "lifecycle", run_id, "--machine"]
    try:
        completed = subprocess.run(  # noqa: S603 — resolved li command plus one run id, no shell
            argv,
            capture_output=True,
            timeout=LIFECYCLE_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception:
        return None

    if len(completed.stdout) > _LIFECYCLE_OUTPUT_LIMIT:
        return None
    text = completed.stdout.decode("utf-8", "replace").strip()
    if not text:
        return None
    try:
        envelope = json.loads(text)
    except Exception:
        return None
    if not isinstance(envelope, dict) or not envelope.get("ok"):
        return None
    data = envelope.get("data")
    if not isinstance(data, dict):
        return None
    state = data.get("lifecycle")
    if not isinstance(state, dict) or not state.get("available"):
        return None
    value = state.get("value")
    return value if isinstance(value, dict) else None


def _read_run_manifest(run_id: str) -> dict[str, Any] | None:
    """The run manifest, or None when there is not one to be had."""
    try:
        return json.loads(config.run_manifest(run_id).read_text())
    except Exception:
        return None


# --- process + log helpers -----------------------------------------------------


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 1:
        return False
    # Reap first: an unreaped exited child is a zombie and `kill -0` would still
    # report it alive. waitpid: (pid, _) just exited, (0, 0) still running,
    # ChildProcessError if not our child (e.g. after an MCP-server restart).
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
        if reaped == 0:
            return True
    except ChildProcessError:
        pass
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _askable_pid(value: object) -> int | None:
    """The recorded pid if the OS can be asked about it at all, otherwise None.

    A record is JSON off disk, so *value* may be a bool, string, or anything
    else that survives a parse — bools are excluded even though ``isinstance``
    accepts them, since 0/1 mean something else to a group signal. Only
    OverflowError is treated as "unusable"; any other failure is the probe's own.
    """
    if not isinstance(value, int) or isinstance(value, bool) or value <= 1:
        return None
    try:
        os.kill(value, 0)
    except OverflowError:
        return None
    except OSError:
        pass
    return value


# The identity-verified process reads live in lionagi.ln._proc, which owns the
# process-group primitives, and are bound here to this surface's marker. A
# second copy would be a second thing to keep correct, and these are exactly the
# reads a quiescence sweep elsewhere has to agree with.
#
# Written as wrappers rather than assigned aliases so the shared name is
# resolved per call. An alias would freeze the original function object here,
# and a probe substituted in lionagi.ln._proc would then reach the shared scan
# below while missing this module's own direct calls — two targets with
# different reach, which is how a substitution silently covers half of what its
# author believes it covers.
def _process_create_time(pid: int) -> tuple[str, float | None]:
    return process_create_time(pid)


def _start_time_matches(observed: float, recorded: float) -> bool:
    return start_time_matches(observed, recorded)


def _process_marker(pid: int) -> tuple[str, str | None]:
    return process_marker(pid, config.JOB_MARKER_ENV_VAR)


def _pinned_member(pid: int, pgid: int) -> tuple[str, tuple[int, float, str | None, bool] | None]:
    return pinned_member(pid, pgid, marker_var=config.JOB_MARKER_ENV_VAR)


def _live_group_members(pgid: int) -> tuple[list[tuple[int, float, str | None, bool]], bool]:
    return live_group_members(pgid, marker_var=config.JOB_MARKER_ENV_VAR)


def _spawned_pgid(pid: int) -> int:
    """The process group of a just-spawned child.

    Falls back to the child's own pid (started via ``start_new_session``, so it
    leads its own group by construction). Recorded at spawn, not derived at kill
    time, since a reused pid would otherwise resolve to a stranger's group.
    """
    try:
        return os.getpgid(pid)
    except OSError:
        return pid


def _group_identity(pgid: int, spawned_at: float, run_id: str) -> tuple[str, str]:
    """Whether the live group *pgid* can be the group this run spawned.

    Returns the verdict and the rule that reached it (see
    docs/internals/mcp.md#group-identity-rules for the two-rule ordering and
    why each verdict means what it does).
    """
    members, complete = _live_group_members(pgid)

    markers = {marker for _, _, marker, _ in members if marker is not None}
    if len(markers) > 1:
        return "conflict", "marker"
    if markers:
        return ("ours" if markers == {run_id} else "not_ours"), "marker"

    if not complete:
        return "unknown", "scan"
    if not members:
        return "gone", "scan"
    floor = spawned_at - _CREATE_TIME_TOLERANCE
    if any(created < floor for _, created, _, _ in members):
        return "not_ours", "start_time"
    if any(not marker_read for _, _, _, marker_read in members):
        return "unknown", "marker"
    return "unproven", "start_time"


def _tail(path: str | None, limit: int = 4000) -> str | None:
    """The last *limit* characters of the log, or None when there is no tail to
    show. Unlike the job record, the tail is advisory — a read failure reports
    as no tail rather than as an error, since it's not worth failing the call.
    """
    if not path:
        return None
    try:
        data = Path(path).read_text(errors="replace")
    except OSError:
        return None
    return data[-limit:] if len(data) > limit else data


def _list_artifacts(run_id: str) -> tuple[list[str], str]:
    """The persisted artifacts of *run_id*, and whether the traversal completed.

    A failed traversal answers ``"unreadable"``, never a bare empty list — "no
    artifacts" is a claim about the run, "couldn't list them" a claim about the
    read, and conflating them tells the caller something false. A missing
    artifacts directory is not a failed read: nothing creates one until a run
    persists something, so absence just means an empty list. Metadata is read
    directly (`.stat()`) rather than via the `is_file()`-style predicates, since
    those report `False` both for "not a file" and "couldn't be checked" —
    indistinguishably, and inconsistently across interpreter versions — which
    would hide exactly the shortfall this function's `"unreadable"` state exists
    to report.
    """
    adir = config.run_dir(run_id) / "artifacts"
    unreadable = False

    def _note(exc: OSError) -> None:
        nonlocal unreadable
        if not isinstance(exc, FileNotFoundError):
            unreadable = True

    found: list[str] = []
    for root, _dirs, names in os.walk(adir, onerror=_note):
        for name in names:
            path = Path(root) / name
            try:
                mode = path.stat().st_mode
            except FileNotFoundError:
                continue  # gone between the walk naming it and this stat
            except OSError:
                unreadable = True  # listable dir, unreadable entry metadata
                continue
            if stat.S_ISREG(mode):
                found.append(str(path.relative_to(adir)))
    return sorted(found), "unreadable" if unreadable else "ok"


def _split_at_sentinel(flags: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split rendered tokens into the option side and the positional side.

    The sentinel stays with the positionals, so re-joining the two halves is the
    identity when nothing is added between them.
    """
    tokens = list(flags)
    try:
        cut = tokens.index("--")
    except ValueError:
        return tokens, []
    return tokens[:cut], tokens[cut:]


def _notify_template(
    run_id: str,
    notify_target: str | None,
    notify_command: str | None,
    notify_sender: str | None = None,
) -> str:
    """Command the CLI runs on terminal status (records finished_at + delivery).

    Invokes the terminal hook module by absolute interpreter path with a
    ``{status}`` placeholder the CLI substitutes (a bareword, so it survives
    the CLI's own shlex-split before being replaced). ``--target`` carries the
    ``{target}`` value; ``--command`` carries an optional per-submit delivery
    override.
    """
    parts = [
        shlex.quote(sys.executable),
        "-m",
        _NOTIFY_MODULE,
        "--run-id",
        shlex.quote(run_id),
        "--status",
        "{status}",
    ]
    if notify_target:
        parts += ["--target", shlex.quote(notify_target)]
    if notify_command:
        parts += ["--command", shlex.quote(notify_command)]
    if notify_sender:
        parts += ["--sender", shlex.quote(notify_sender)]
    return " ".join(parts)


# argv and envp are pointer arrays, so every entry costs a slot as well as its bytes.
_POINTER_BYTES = 8
# Small allowance for the aux vector and alignment the kernel adds on top.
_EXEC_RESERVE_BYTES = 4096


def _max_single_arg_bytes() -> int | None:
    """The per-argument exec limit, or None where the platform imposes none.

    Linux caps one argument at ``MAX_ARG_STRLEN`` (32 pages), independently of
    the aggregate limit and with no ``sysconf`` knob for it, so it's derived
    from the page size. Other platforms (macOS included) bound only the total.
    """
    if not sys.platform.startswith("linux"):
        return None
    try:
        page = os.sysconf("SC_PAGESIZE")
    except (ValueError, OSError):  # pragma: no cover — platform without the knob
        page = 4096
    if not isinstance(page, int) or page <= 0:  # pragma: no cover — unset knob
        page = 4096
    return 32 * page


def _reject_oversized_argv(argv: list[str], env: dict[str, str], *, kind: str) -> None:
    """Refuse a command line the OS will not accept, before anything is spawned
    — ``exec``'s own ``Argument list too long`` arrives too late, after a run id
    already exists for a process that never started.

    Two independent limits, both must hold: the *aggregate* (`SC_ARG_MAX`,
    argv+env together, counting each entry's terminator+pointer so a long list
    of short arguments can't defeat a flat byte reserve) and the *per-argument*
    (:func:`_max_single_arg_bytes`, checked separately since an argument can be
    under the aggregate limit and still be refused on its own).
    """
    try:
        limit = os.sysconf("SC_ARG_MAX")
    except (ValueError, OSError):  # pragma: no cover — platform without the knob
        return
    if not isinstance(limit, int) or limit <= 0:  # pragma: no cover — unset knob
        return

    advice = (
        "Shorten the instruction, or use agent.submit, which hands the instruction "
        "to the run in a file instead of on the command line."
    )

    per_arg = _max_single_arg_bytes()
    if per_arg is not None:
        for arg in argv:
            n = len(arg.encode())
            if n > per_arg:
                raise ValueError(
                    f"cannot submit this {kind} run: one argument is {n} bytes, over the "
                    f"{per_arg}-byte limit this platform places on a single argument "
                    f"regardless of the {limit}-byte total. {advice}"
                )

    used = sum(len(a.encode()) + 1 + _POINTER_BYTES for a in argv)
    used += sum(len(k.encode()) + len(v.encode()) + 2 + _POINTER_BYTES for k, v in env.items())
    if used + _EXEC_RESERVE_BYTES <= limit:
        return

    detail = (
        "the instruction is passed on the command line for this kind of run"
        if kind != "agent"
        else "the command line is too long"
    )
    raise ValueError(
        f"cannot submit this {kind} run: {detail}, and it needs {used} bytes of "
        f"argument vector plus environment against an OS limit of {limit}. {advice}"
    )


# --- lifecycle derivation ------------------------------------------------------


class SpawnError(RuntimeError):
    """Raised when the child could not be started after the job record existed.

    Carries ``run_id`` and the terminal ``record`` written for it, so a caller
    still learns which run failed instead of having to parse the message.
    """

    def __init__(self, run_id: str, record: dict[str, Any], message: str) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.record = record


def _outcome_for(status: Any) -> str:
    """How a terminal run came out, from the status the CLI recorded.

    Used only once a run is already established terminal, never to decide
    whether it ended. An unrecognized status classifies as failure — a stale
    success list would otherwise read a timeout back as a success.
    """
    if status in _SUCCEEDED_STATUSES:
        return "succeeded"
    if status in _CANCELLED_STATUSES:
        return "cancelled"
    return "failed"


def _recorded_outcome(job: dict[str, Any]) -> str | None:
    """The outcome a writer recorded, validated against `_OUTCOMES` rather than
    passed through — a damaged or hand-edited record must not be able to put a
    value in the field callers branch on that no producer would ever write."""
    value = job.get("outcome")
    return value if isinstance(value, str) and value in _OUTCOMES else None


def _derive(
    job: dict[str, Any] | None,
    alive: bool,
    lifecycle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify a job record into the fields a caller is allowed to branch on.

    See docs/internals/mcp.md#derive-contract for what `terminal` and `outcome`
    may and may not be derived from.
    """
    if job is None:
        return {
            "status": "unknown",
            "terminal": False,
            "outcome": None,
            "reason_code": None,
            "spawn_state": None,
            "possibly_orphaned": False,
        }

    recorded = job.get("status", "unknown")
    spawn_state = job.get("spawn_state")

    if spawn_state == "failed":
        return {
            "status": recorded,
            "terminal": True,
            "outcome": "failed",
            "reason_code": _SPAWN_FAILED_REASON,
            "spawn_state": spawn_state,
            "possibly_orphaned": False,
        }

    if job.get("finished_at") is not None:
        return {
            "status": recorded,
            "terminal": True,
            # A recorded outcome wins over one derived from the status string.
            "outcome": _recorded_outcome(job) or _outcome_for(recorded),
            # A recorded reason wins: it came from the lifecycle store.
            "reason_code": job.get("reason_code") or _REASON_BY_STATUS.get(recorded),
            "spawn_state": spawn_state,
            "possibly_orphaned": False,
        }

    if alive:
        return {
            "status": "running",
            "terminal": False,
            "outcome": None,
            "reason_code": None,
            "spawn_state": spawn_state,
            "possibly_orphaned": False,
        }

    # Process gone (or never there), sidecar records no end. The lifecycle
    # store is the only place `li kill` records an end (it writes nothing here).
    if lifecycle is not None and lifecycle.get("terminal"):
        lifecycle_status = lifecycle.get("status", recorded)
        return {
            "status": lifecycle_status,
            "terminal": True,
            "outcome": _outcome_for(lifecycle_status),
            "reason_code": lifecycle.get("reason_code"),
            "spawn_state": spawn_state,
            "possibly_orphaned": False,
        }

    if spawn_state == "preparing":
        # Spawn not yet attempted or its result not yet written; a stale one
        # stays non-terminal rather than guessed at by a timeout.
        return {
            "status": recorded,
            "terminal": False,
            "outcome": None,
            "reason_code": None,
            "spawn_state": spawn_state,
            "possibly_orphaned": False,
        }

    # A recorded pid that is gone, with no end recorded anywhere: an orphan.
    # Advisory only. Nothing here terminalises it — liveness is an observation
    # about a pid, which can be reused or denied, and two readers of one
    # unchanged record may see it differently. It stays non-terminal.
    #
    # A conclusively gone process does not reach this branch: its end is
    # published before the record is classified, so it arrives here carrying a
    # `finished_at` and is answered above. What is left is the observation that
    # established nothing — an unaskable pid — which is precisely the case that
    # must stay advisory.
    return {
        "status": "exited",
        "terminal": False,
        "outcome": None,
        "reason_code": None,
        "spawn_state": spawn_state,
        "possibly_orphaned": True,
    }


# --- public API ----------------------------------------------------------------


def _submit_cwd() -> str | None:
    """This process's own directory, or None when it no longer has one.

    Valid as "the submitter's directory" only because the server is served over
    stdio (one client spawns one server, inheriting its cwd) — a future
    multi-caller transport must carry the anchor on the request instead. Errors
    are swallowed to None rather than raised, since a removed cwd must not
    strand the run being submitted.
    """
    try:
        return os.getcwd()
    except OSError:
        return None


def submit(
    kind: str,
    flags: list[str],
    *,
    prompt: str | None = None,
    cwd: str | None = None,
    label: str | None = None,
    notify_command: str | None = None,
    notify_target: str | None = None,
    notify_sender: str | None = None,
    mcp_config: str | None = None,
    no_mcp_config: bool = False,
) -> dict[str, Any]:
    """Spawn a ``li`` run in the background and return its handle immediately.

    *flags* are the already-built CLI flags (everything except the prompt).
    *prompt*, when given, is handed to an agent via ``--prompt-file`` (robust for
    long text) or appended as the flow/fanout positional.

    On terminal, the run records its status and — if a delivery command is
    configured — sends a terminal notice. *notify_command* is an optional
    per-submit delivery-argv override (JSON list); *notify_target* fills the
    ``{target}`` placeholder in the configured command. With neither and no
    configured default, the run simply records its status and delivers nothing.

    *mcp_config* and *no_mcp_config* are the caller's own answer to where the
    child's MCP servers come from, passed as values (not re-parsed out of
    *flags*, which are already rendered in `--flag=value` form) since this
    function must decide whether to resolve its own server set.
    """
    if kind not in _KIND_ARGV:
        raise ValueError(f"unknown job kind {kind!r}; expected one of {sorted(_KIND_ARGV)}")

    run_id, d = _reserve_run_dir()
    log_path = d / "console.log"

    # Any failure before the record exists (below) gives the reservation back
    # via _discard_reservation — every directory under the jobs root is listed
    # as a job, so an abandoned one must not read back as one with no kind.
    try:
        # `flags` may carry a `--` sentinel, after which every token is a
        # positional; options this function adds must go in front or they
        # arrive as text instead of being parsed.
        options, positionals = _split_at_sentinel(flags)
        prompt_path = None
        if prompt is not None:
            if kind == "agent":
                prompt_path = d / _PROMPT_FILENAME
                options += ["--prompt-file", str(prompt_path)]
            else:
                # flow/fanout take the prompt as a positional; it may begin with a
                # dash, so it goes behind a sentinel either way.
                if not positionals:
                    positionals = ["--"]
                positionals.append(prompt)

        # Resolve MCP servers here (submitting directory still in effect — a
        # detached run's cwd is a checkout, not this server's own directory) and
        # snapshot them into this run's own directory, so the child's tool set
        # can't drift if the discovered file changes or the run resumes later.
        # A config that exists but can't be used fails the submission now
        # instead of surfacing minutes later, only in the child's own log.
        mcp_config_path: str | None = None
        mcp_config_source: str | None = None
        mcp_config_reason: str | None = None
        mcp_servers: dict[str, Any] | None = None
        # Servers resolved for the run's workers, by name. `[]` means this run
        # settled the question (caller disabled MCP, or a found config declares
        # no servers) — `None` means it never resolved a set. This reports what
        # was RESOLVED, not what the child's provider then managed to start.
        mcp_config_servers: list[str] | None = None
        if no_mcp_config:
            mcp_config_reason = "mcp_disabled_by_caller"
            mcp_config_servers = []
        elif mcp_config is not None:
            # Caller named the file; their flag is already on the line, so no
            # snapshot is taken or prepended (a second --mcp-config would let the
            # parser pick between them, misreporting what the child actually read).
            mcp_config_path = mcp_config
            mcp_config_source = mcp_config
            mcp_config_reason = "mcp_config_named_by_caller"
        else:
            from lionagi.cli._mcp_resolve import McpConfigError, resolve_spawn_mcp_servers

            launch_dir = os.getcwd()
            resolution = resolve_spawn_mcp_servers(launch_dir=launch_dir)
            if resolution.servers is None:
                if resolution.reason and resolution.reason.startswith("mcp_config_unusable:"):
                    raise McpConfigError(
                        f"cannot submit this agent run: the MCP config found at "
                        f"{resolution.source} cannot be used "
                        f"({resolution.reason.split(':', 1)[1].strip()})"
                    )
                mcp_config_reason = (
                    f"{resolution.reason}_at_or_above:{launch_dir}"
                    if resolution.reason == "no_mcp_config_found"
                    else resolution.reason
                )
                if resolution.reason == "mcp_config_declares_no_servers":
                    # A settled "none" (as opposed to "no config found", which
                    # the resolver reports with the same null server map — only
                    # the reason string tells the two apart).
                    mcp_config_servers = []
                    mcp_config_source = str(resolution.source) if resolution.source else None
            else:
                mcp_servers = resolution.servers
                mcp_config_source = str(resolution.source) if resolution.source else None
                mcp_config_path = str(d / _MCP_SNAPSHOT_FILENAME)
                mcp_config_servers = sorted(
                    mcp_servers
                )  # readable order; child reads the snapshot file, not this list
                options = ["--mcp-config", mcp_config_path, *options]

        # Wire the CLI's terminal hook back to the MCP server so we record a reliable
        # finished_at/status (and fire the configured delivery) even across a restart.
        options = [
            "--notify",
            _notify_template(run_id, notify_target, notify_command, notify_sender),
            *options,
        ]

        argv = [*config.li_command(), *_KIND_ARGV[kind], *options, *positionals]

        # Drop the parent harness marker so the detached child does not inherit an
        # environment that claims it is running under an interactive harness.
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        env[config.RUN_ID_ENV_VAR] = run_id
        # Inherited by every process the child spawns, so a live group member
        # can later be asked what it belongs to instead of guessed from timing.
        env[config.JOB_MARKER_ENV_VAR] = run_id
        # Where the run drops the typed class of whatever exception ended it, so
        # the terminal hook can record a cause instead of leaving the reason to
        # be read out of console prose.
        env[config.CAUSE_FILE_ENV_VAR] = str(d / config.CAUSE_FILENAME)

        # Checked before anything is written: Popen raising this late would
        # leave a job recorded "running" for a run that never started.
        _reject_oversized_argv(argv, env, kind=kind)

        if prompt_path is not None:
            prompt_path.write_text(prompt)
        if mcp_servers is not None and mcp_config_path is not None:
            _write_mcp_server_snapshot(Path(mcp_config_path), mcp_servers)
    except BaseException:
        _discard_reservation_and_warn(d, run_id)
        raise

    # Persist the record BEFORE spawning, so the child's terminal --notify hook
    # always finds a record to mark. mark_terminal no-ops on a missing record, so
    # a child that reaches a terminal in the window between spawn and this write
    # would otherwise lose its status and delivery outcome. pid is filled in right
    # after the spawn; that follow-up write only attaches the pid and never
    # rewrites status, so a terminal the hook may already have recorded survives.
    #
    # The write also records which phase of the spawn the record was written in,
    # so the phase is a recorded fact rather than something a reader guesses from
    # the pid being absent. It rides writes that have to happen anyway, so it adds
    # no failure mode of its own.
    record = {
        "run_id": run_id,
        "pid": None,
        "pid_create_time": None,
        "pgid": None,
        "kind": kind,
        "argv": argv,
        "cwd": cwd,
        # Both working directories, because they answer different questions and a
        # notice signed by the wrong one is the failure this pair exists to close.
        # `cwd` is where the run executes. `submit_cwd` is this process's own
        # directory, which is the submitting seat's: it is what the server was
        # started in, and therefore what any directory-anchored identity lookup
        # resolves the submitter from. A notifier that resolves who it is from its
        # working directory reports the run's directory owner unless it is run in
        # the submitter's, so the delivery uses this one — see
        # _notify_hook.deliver_terminal_notice. Recorded even when the two agree,
        # so a record read afterwards can always say which identity a notice
        # carried rather than leaving it to be inferred from a path that has since
        # been reused.
        "submit_cwd": _submit_cwd(),
        "label": label,
        "notify_command": notify_command,
        "notify_target": notify_target,
        "notify_sender": notify_sender,
        "mcp_config": mcp_config_path,
        "mcp_config_source": mcp_config_source,
        "mcp_config_reason": mcp_config_reason,
        "mcp_config_servers": mcp_config_servers,
        "submitted_at": _now_iso(),
        "finished_at": None,
        "status": "running",
        "spawn_state": "preparing",
        "log": str(log_path),
    }
    try:
        _write_job(record)
    except BaseException:
        # The record is what makes a reservation a job, so a publication that
        # never landed leaves the prepared files behind with nothing claiming
        # them — the same stranded directory every earlier failure here gives
        # back, reached one step later. This is the last point where giving it
        # back is the right answer: past this line the run exists, and a failure
        # is marked on the record rather than erased along with it.
        _discard_reservation_and_warn(d, run_id)
        raise

    try:
        # Append mode, not truncate: the terminal hook appends to this same log
        # while the child is still alive, so a fixed-offset descriptor here
        # would let the child's own trailing writes overwrite it.
        log_f = open(log_path, "ab")
        try:
            proc = subprocess.Popen(  # noqa: S603 — argv is the resolved li_command + CLI flags, no shell
                argv,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=cwd or None,
                env=env,
                start_new_session=True,  # own session/pgid: survives restart, killable as a group
            )
        finally:
            log_f.close()  # child holds its own fd; parent drops its copy
    except Exception as exc:
        # Catch every exception, not just OSError — an argv the exec can't carry
        # raises ValueError with no errno — so the record is always marked
        # rather than claiming "running" forever for an unenumerated failure mode.
        raise _record_spawn_failure(run_id, exc) from exc

    # Attach the pid without rewriting status (preserves a terminal the hook
    # already recorded in the spawn window). Alongside it: start time and pgid,
    # since a bare pid isn't an identity (the OS recycles it once reaped) — a
    # failed start-time read leaves it null, which kill() reads as "no identity
    # captured" rather than as any claim about the process. Probed before the
    # lock is taken so a slow process table never holds up another mutation.
    _state, created = _process_create_time(proc.pid)
    pgid = _spawned_pgid(proc.pid)
    latest = record
    with _locked_job(run_id) as guard:
        if guard.record is None:
            guard.record = latest = {**record}
        else:
            latest = guard.record
        latest["pid"] = proc.pid
        latest["pid_create_time"] = created
        latest["pgid"] = pgid
        latest["spawn_state"] = "started"

    # No liveness probe here — Popen returning means the child exists; probing
    # would only race an instant exit into reading back as an orphan.
    derived = _derive(latest, alive=True)
    return {
        "run_id": run_id,
        "pid": proc.pid,
        "status": derived["status"],
        "terminal": derived["terminal"],
        "outcome": derived["outcome"],
        "reason_code": derived["reason_code"],
        "spawn_state": latest["spawn_state"],
        "log": str(log_path),
        "mcp_config": mcp_config_path,
        "mcp_config_source": mcp_config_source,
        "mcp_config_reason": mcp_config_reason,
        "mcp_config_servers": mcp_config_servers,
        "notify_sender": notify_sender,
    }


def _record_spawn_failure(run_id: str, exc: Exception) -> SpawnError:
    """Write the terminal record for a spawn that failed, and build the error.

    Records `spawn_state="failed"` and, in the same write, the terminal end
    itself — otherwise the phase would say failed while status still said running.
    """
    reason = f"spawn failed: {exc}"
    record: dict[str, Any] = {
        "run_id": run_id,
        "spawn_state": "failed",
        "status": "failed",
        "finished_at": _now_iso(),
        # The spawn failed here, so this is the end as it happened.
        "finished_at_precision": FINISHED_AT_OBSERVED,
        "reason": reason,
        "terminal_source": TERMINAL_SOURCE_SPAWN_FAILURE,
    }
    try:
        with _locked_job(run_id) as guard:
            current = guard.record
            if current is None:
                current = {"run_id": run_id}
                guard.record = current
            current["spawn_state"] = "failed"
            current["reason"] = reason
            # First-writer-wins, same rule as every other mutation here.
            if current.get("finished_at") is None:
                current["status"] = "failed"
                current["finished_at"] = _now_iso()
                current["finished_at_precision"] = FINISHED_AT_OBSERVED
                current["terminal_source"] = TERMINAL_SOURCE_SPAWN_FAILURE
            record = current
    except OSError:
        pass  # the write can fail on the same disk that refused the spawn
    return SpawnError(run_id, record, f"could not spawn run {run_id}: {exc}")


def _record_is_terminal(job: dict[str, Any]) -> bool:
    """Whether the record itself already says the run ended.

    Deliberately not a membership test against terminal status strings (status
    is an open vocabulary passed through verbatim) — an end is marked by
    ``finished_at`` or a failed spawn, never inferred from the status value.
    """
    return job.get("finished_at") is not None or job.get("spawn_state") == "failed"


def _needs_lifecycle_read(job: dict[str, Any] | None, alive: bool) -> bool:
    """Whether this observation has to go and ask the lifecycle store: only when
    there's a job, its process isn't running, and nothing has recorded an end —
    so a healthy run's poll spawns nothing, and a run asks the store once."""
    if job is None or alive:
        return False
    return not _record_is_terminal(job)


def _cache_lifecycle_end(
    job: dict[str, Any] | None, lifecycle: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Copy a lifecycle-recorded end onto the sidecar record, once.

    The sidecar is a cache of the end, not a second opinion — first writer
    between the lifecycle store and the terminal hook wins. A failed write here
    is not an error (it's a cache; the next observation just asks again); the
    in-memory record is returned either way so this call is what classifies.
    """
    if job is None or lifecycle is None or not lifecycle.get("terminal"):
        return job
    ended = lifecycle.get("ended_at")
    # A lifecycle record that carries its own end time is an observation. One
    # that does not leaves the same gap the reaper has, and the same substituted
    # value: now, standing in for a moment nobody recorded.
    observed_end = _iso_from_epoch(ended)
    fields = {
        "status": lifecycle.get("status", job.get("status")),
        "finished_at": observed_end or _now_iso(),
        "finished_at_precision": (
            FINISHED_AT_OBSERVED if observed_end else FINISHED_AT_UPPER_BOUND
        ),
        "reason_code": lifecycle.get("reason_code"),
        "terminal_source": TERMINAL_SOURCE_LIFECYCLE,
    }
    updated = {**job, **fields}
    run_id = job.get("run_id")
    if not isinstance(run_id, str):
        return updated
    try:
        with _locked_job(run_id) as guard:
            current = guard.record
            if current is None:
                return updated
            if current.get("finished_at") is not None:
                return current
            current.update(fields)
            return current
    except OSError:
        return updated


def _iso_from_epoch(value: Any) -> str | None:
    """The store keeps epoch seconds; the sidecar keeps ISO-8601 strings."""
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _server_identity() -> dict[str, str]:
    """Which implementation answered this call: version + directory actually
    imported, resolved per call (not cached at import) so it reflects the
    module genuinely loaded rather than the file currently on disk."""
    try:
        from lionagi.version import __version__ as version
    except Exception:  # noqa: BLE001 — identity is diagnostic; never fail a status read
        version = "unknown"
    return {"version": version, "module": str(Path(__file__).resolve().parent)}


LivenessConclusion = Literal["alive", "process_gone", "unknown"]


@dataclass(frozen=True)
class ProcessLiveness:
    """What one observation of a run's recorded process established.

    ``alive`` decides whether waiting can still help. ``conclusion`` is the
    decision surface: ``"process_gone"`` is a *positive* finding and the only
    value that may end a run; ``"unknown"`` (an unaskable pid, a denied read)
    can never end one — a case added later is inconclusive until explicitly
    added to `CONCLUSIVE_FINDINGS`, never by exclusion. ``finding`` names which
    observation produced the conclusion.
    """

    alive: bool
    conclusion: LivenessConclusion
    finding: str


# Internal finding -> public `pid_identity` value; keeps the two vocabularies
# independently readable rather than one inferred from the other.
_PID_IDENTITY_BY_FINDING: dict[str, str | None] = {
    "unusable_pid": "unusable_pid",
    FINDING_PID_ABSENT: None,
    FINDING_DISAPPEARED_DURING_PROBE: "gone",
    FINDING_PID_RECYCLED: "recycled",
    "identity_confirmed": "confirmed",
    "identity_not_recorded": "not_recorded",
    "identity_unusable": "unusable",
    "identity_unreadable": "unreadable",
    "no_record": None,
}


def _run_process_liveness(job: dict[str, Any] | None, pid: int | None) -> ProcessLiveness:
    """Whether the process *this run* spawned is alive, and what settled it.

    A pid number is not an identity — once the run's process exits and the OS
    reassigns it, a probe answers about a stranger, and an ended run would
    report as running for as long as the stranger lives. Where the record
    captured a start time, identity is confirmed here before liveness is
    reported; a number now held by a different process reports this run's
    process as not alive, raising ``possibly_orphaned``.

    See docs/internals/mcp.md#liveness-findings for the full finding taxonomy
    and the two-question evaluation order (pid-alive, then whose-process).
    """
    asked = _askable_pid(pid)
    if asked is None:
        return ProcessLiveness(False, "unknown", "unusable_pid")
    if not _pid_alive(asked):
        return ProcessLiveness(False, "process_gone", FINDING_PID_ABSENT)

    state, live_created = _process_create_time(asked)
    if state == "gone":
        return ProcessLiveness(False, "process_gone", FINDING_DISAPPEARED_DURING_PROBE)

    if job is None:
        return ProcessLiveness(True, "alive", "no_record")
    recorded = job.get("pid_create_time")
    if recorded is None:
        return ProcessLiveness(True, "alive", "identity_not_recorded")
    try:
        spawned_at = float(recorded)
        usable = not isinstance(recorded, bool) and math.isfinite(spawned_at)
    except (TypeError, ValueError, OverflowError):
        usable = False
    if not usable:
        return ProcessLiveness(True, "alive", "identity_unusable")

    if state != "found" or live_created is None:
        return ProcessLiveness(True, "unknown", "identity_unreadable")
    if _start_time_matches(live_created, spawned_at):
        return ProcessLiveness(True, "alive", "identity_confirmed")
    return ProcessLiveness(False, "process_gone", FINDING_PID_RECYCLED)


@dataclass(frozen=True)
class ReapResult:
    """What one attempt to end a conclusively gone run came to.

    ``won_transition`` is true for exactly one caller per run — the one whose
    guarded write published the end, and therefore owns the terminal notice.
    ``record`` reflects the durable fact (this call's write, or another's) even
    for a loser. ``reason`` is diagnostic only, never something to branch on.
    """

    won_transition: bool
    record: dict[str, Any] | None
    reason: str


def _notice_recorded_undelivered(run_id: str) -> bool:
    """Whether *run_id*'s own run directory recorded its terminal notice as
    never delivered (``notify_outcome.json`` with ``ok: false``).

    Best-effort and evidence-only: a missing or unreadable file returns
    False, the same as a run that never wrote one — see LOST_REASON_
    NOTICE_RECORDED_UNDELIVERED for why absence must never be read as this.
    """
    try:
        text = config.run_dir(run_id).joinpath("notify_outcome.json").read_text()
        outcome = json.loads(text)
    except (OSError, ValueError):
        return False
    return isinstance(outcome, dict) and outcome.get("ok") is False


def reap_orphan(run_id: str, *, finding: str, observed_at: str) -> ReapResult:
    """Publish the end of a run whose process is conclusively gone.

    Idempotent, safe to call from every observer at once. The whole check runs
    inside the per-run lock against a record reread there — the caller's
    observation was taken before the lock, and by now the terminal hook, a
    kill, or another observer may have already written the end.

    All admission checks (record exists and matches, spawn reached "started",
    no end recorded yet, *finding* is in `CONCLUSIVE_FINDINGS`) hold under the
    lock. ``finished_at`` is *observed_at* — when the loss was established, not
    the unknowable moment the process actually exited. Notification runs after
    this returns, outside the lock, and is the winner's to attempt.
    """
    if finding not in CONCLUSIVE_FINDINGS:
        return ReapResult(False, None, "finding_is_not_conclusive")

    with _locked_job(run_id) as guard:
        job = guard.record
        if job is None:
            return ReapResult(False, None, f"no_usable_record:{guard.state}")
        if job.get("run_id") != run_id:
            return ReapResult(False, job, "record_names_another_run")
        if job.get("spawn_state") != "started":
            return ReapResult(False, job, "spawn_state_is_not_started")
        if job.get("finished_at") is not None:
            return ReapResult(False, job, "already_ended")
        reason_code = (
            LOST_REASON_NOTICE_RECORDED_UNDELIVERED
            if _notice_recorded_undelivered(run_id)
            else LOST_REASON
        )
        job.update(
            {
                "status": "exited",
                "outcome": OUTCOME_INDETERMINATE,
                "reason_code": reason_code,
                "finished_at": observed_at,
                "finished_at_precision": FINISHED_AT_UPPER_BOUND,
                "terminal_source": TERMINAL_SOURCE_ORPHAN_REAPER,
                "terminal_evidence": {"kind": EVIDENCE_PROCESS_GONE, "finding": finding},
            }
        )
        return ReapResult(True, job, "reaped")


def _admits_orphan_reap(job: dict[str, Any] | None, liveness: ProcessLiveness) -> bool:
    """Whether this observation is one that may end the run: a positive
    ``process_gone`` conclusion for a started run with no end recorded. Cheap
    gate only — the checks that count are re-made under the lock in
    :func:`reap_orphan`; this keeps an ordinary healthy/ended poll from opening
    a lock file at all.
    """
    if job is None or liveness.conclusion != "process_gone":
        return False
    return job.get("spawn_state") == "started" and not _record_is_terminal(job)


def _deliver_reap_notice(run_id: str, record: dict[str, Any]) -> dict[str, Any] | None:
    """Attempt the terminal notice the run's own process never got to send.

    The dead child was the owner of both the end and its delivery, so an
    observer that publishes the end and stops there leaves a notice-only caller
    asleep forever — the terminality would be repaired and the wake-up would
    not. The winner of the transition therefore attempts the same configured
    delivery the hook would have, through the hook's own resolution, so a
    per-run override and the project/global settings mean here exactly what they
    mean there and there is only one place a notifier is configured.

    Best-effort and after the fact. The end is already durable when this runs,
    so nothing here can change how the run came out: a refusal, a non-zero exit
    or a timeout is recorded as a delivery failure, and a delivery that never
    gets to record anything leaves ``notify_delivery`` absent, which is what a
    crash between the two writes looks like from outside.

    The guard is total for the same reason: this is called from a read path, and
    a notifier that comes apart in a way the hook does not classify must not
    turn a status read of an already-ended run into a failed call. What is lost
    is the delivery result, which is the same thing the crash gap loses.
    """
    from ._notify_hook import deliver_terminal_notice

    try:
        outcome = deliver_terminal_notice(
            run_id,
            record,
            record.get("status") or "exited",
            target=record.get("notify_target"),
            command=record.get("notify_command"),
            sender=record.get("notify_sender"),
        )
    except Exception:  # noqa: BLE001 — the end is published; delivery may not undo it
        return None
    # A result that could not be recorded reads back the same way a crash
    # between the two writes does: the end is durable and the delivery outcome
    # is absent. Nothing here can be failed by that — this is a read path, and
    # the end it reports is already published.
    return record_notify_delivery(run_id, outcome).record


def _reap_if_conclusively_gone(
    run_id: str, job: dict[str, Any] | None, liveness: ProcessLiveness
) -> dict[str, Any] | None:
    """Turn a conclusive observation into a durable end, then report the record.

    The one place a read is allowed to write. Always returns a record read back
    from the transition (or an unchanged one), never the raw observation.
    """
    if not _admits_orphan_reap(job, liveness):
        return job
    try:
        result = reap_orphan(run_id, finding=liveness.finding, observed_at=_now_iso())
    except OSError:
        return job  # transition unpublishable; next observation retries
    if result.record is None:
        return job
    if not result.won_transition:
        return result.record
    return _deliver_reap_notice(run_id, result.record) or result.record


def status(run_id: str) -> dict[str, Any]:
    """Current state of *run_id*.

    See docs/internals/mcp.md#status-response-contract for the full field
    reference (``status`` vs ``terminal``/``outcome``, ``run["status"]`` vs the
    top-level ``status``, ``pid_identity``/``liveness_conclusion``,
    ``mcp_config*``, and ``record_state``).
    """
    job, record_state = _read_job_state(run_id)
    manifest = _read_run_manifest(run_id)
    pid = job.get("pid") if job else None
    liveness = _run_process_liveness(job, pid)
    alive = liveness.alive

    lifecycle = None
    if _needs_lifecycle_read(job, alive):
        lifecycle = _read_lifecycle(run_id)
        job = _cache_lifecycle_end(job, lifecycle)

    # Lifecycle store asked first — a reported end is the better answer; reaping
    # is only for a run no writer survived to speak for.
    job = _reap_if_conclusively_gone(run_id, job, liveness)

    derived = _derive(job, alive, lifecycle)
    persistence_degraded_reason = (
        manifest.get(_PERSISTENCE_DEGRADED_REASON_FIELD) if isinstance(manifest, dict) else None
    )
    if not isinstance(persistence_degraded_reason, str) or not persistence_degraded_reason:
        persistence_degraded_reason = None

    return {
        "run_id": run_id,
        "kind": (job or {}).get("kind"),
        "label": (job or {}).get("label"),
        "status": derived["status"],
        "terminal": derived["terminal"],
        "outcome": derived["outcome"],
        "reason_code": derived["reason_code"],
        "spawn_state": derived["spawn_state"],
        "possibly_orphaned": derived["possibly_orphaned"],
        "terminal_source": (job or {}).get("terminal_source"),
        "terminal_evidence": (job or {}).get("terminal_evidence"),
        "alive": alive,
        "pid_identity": _PID_IDENTITY_BY_FINDING.get(liveness.finding),
        "liveness_conclusion": liveness.conclusion,
        "pid": pid,
        "submitted_at": (job or {}).get("submitted_at"),
        "finished_at": (job or {}).get("finished_at"),
        # Whether finished_at is the end or only a bound on it. Null on records
        # written before the field existed, which is not the same as "observed".
        "finished_at_precision": (job or {}).get("finished_at_precision"),
        "notify_delivery": (job or {}).get("notify_delivery"),
        "mcp_config": (job or {}).get("mcp_config"),
        "mcp_config_source": (job or {}).get("mcp_config_source"),
        "mcp_config_reason": (job or {}).get("mcp_config_reason"),
        "mcp_config_servers": (job or {}).get("mcp_config_servers"),
        _PERSISTENCE_DEGRADED_REASON_FIELD: persistence_degraded_reason,
        "run": manifest,
        "log_tail": _tail((job or {}).get("log")),
        "known": job is not None,
        "record_state": record_state,
        "server": _server_identity(),
    }


# One message per way a record read can fail — "no such job" is only true of
# "absent"; reusing it for a damaged file sends an operator away from a
# file that's actually on disk.
_NO_RECORD_ERROR = {
    "absent": "no such job",
    "unreadable": "the record for this job is on disk and could not be read or parsed",
    "wrong_shape": "the record for this job holds valid JSON that is not an object",
}


def output(run_id: str, tail_chars: int = 20000) -> dict[str, Any]:
    """Terminal output of *run_id*: the console (an agent's final response prints
    here) plus any persisted artifacts. ``record_state``/``artifacts_state``
    distinguish "wrote none" from "listing failed", same as ``status()``.
    """
    job, record_state = _read_job_state(run_id)
    if job is None:
        return {
            "run_id": run_id,
            "known": False,
            "record_state": record_state,
            "error": _NO_RECORD_ERROR.get(record_state, "no such job"),
        }
    st = status(run_id)
    artifacts, artifacts_state = _list_artifacts(run_id)
    return {
        "run_id": run_id,
        "known": True,
        "record_state": record_state,
        "status": st["status"],
        "terminal": st["terminal"],
        "outcome": st["outcome"],
        "reason_code": st["reason_code"],
        "console": _tail(job.get("log"), limit=tail_chars),
        "artifacts": artifacts,
        "artifacts_state": artifacts_state,
        "run_dir": str(config.run_dir(run_id)),
    }


def _kill_result(
    run_id: str,
    *,
    killed: bool,
    reason: str | None,
    reason_code: str,
    pid: Any = None,
    pgid: int | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "killed": killed,
        "reason": reason,
        "reason_code": reason_code,
        "pid": pid,
        "pgid": pgid,
    }


def _mark_killed(job: dict[str, Any]) -> WriteResult:
    """Record the kill on the job record.

    A record that already carries an end keeps it — overwriting `completed`
    with `killed` would replace how the run came out with how its stragglers
    were cleaned up. Decided under the per-run lock against the record as it
    stands there (not the caller's copy), since an end can be recorded while
    the signal is still in flight; the kill happened either way, this write is
    only its record.
    """
    run_id = job.get("run_id")
    if not isinstance(run_id, str):
        return WriteResult(None, "no_run_id_on_record")
    with _locked_job(run_id) as guard:
        current = guard.record
        if current is None:
            return WriteResult(None, guard.state)
        if _record_is_terminal(current):
            current["group_reaped_at"] = _now_iso()
        else:
            current["status"] = "killed"
            current["finished_at"] = _now_iso()
            # The kill happened here, so the end is observed rather than noticed.
            current["finished_at_precision"] = FINISHED_AT_OBSERVED
            current["terminal_source"] = TERMINAL_SOURCE_KILL
        return WriteResult(current, guard.state)


def _signal_group(
    run_id: str,
    job: dict[str, Any],
    pid: int,
    pgid: int,
    sig: int,
    reason_code: str,
) -> dict[str, Any]:
    """Signal *pgid* and record the outcome on the job record."""
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return _kill_result(
            run_id,
            killed=False,
            reason="process gone",
            reason_code=KILL_PROCESS_GONE,
            pid=pid,
            pgid=pgid,
        )
    except PermissionError as e:
        return _kill_result(
            run_id,
            killed=False,
            reason=f"permission denied: {e}",
            reason_code=KILL_PERMISSION_DENIED,
            pid=pid,
            pgid=pgid,
        )
    written = _mark_killed(job)
    if written.refused:
        # Signal went out but nothing durable says so — its own code, not a
        # plain success, since `killed=True` beside a still-"running" record
        # would be telling the caller two things and only one is on disk.
        return _kill_result(
            run_id,
            killed=True,
            reason="signalled, but the kill could not be recorded: the run record could not be locked",
            reason_code=KILL_NOT_RECORDED,
            pid=pid,
            pgid=pgid,
        )
    return _kill_result(
        run_id, killed=True, reason=None, reason_code=reason_code, pid=pid, pgid=pgid
    )


def _signal_leader_group(
    run_id: str, job: dict[str, Any], pid: int, pgid: int, sig: int, observed_at: float
) -> dict[str, Any]:
    """Signal *pgid*, once the confirmed leader at *pid* is shown to be in it.

    See docs/internals/mcp.md#signal-leader-group-safety for why group
    equality, the run-id marker, and the exact (not tolerance-bounded) start
    time re-read are all required before this signals.
    """
    try:
        live_pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError) as e:
        return _kill_result(
            run_id,
            killed=False,
            reason=(
                f"pid {pid} is this run's leader but its process group could not be "
                f"read ({e}), so group {pgid} could not be confirmed to be its group; "
                "nothing was signalled"
            ),
            reason_code=KILL_LEADER_GROUP_UNREADABLE,
            pid=pid,
            pgid=pgid,
        )
    if live_pgid != pgid:
        return _kill_result(
            run_id,
            killed=False,
            reason=(
                f"pid {pid} is this run's leader but is in group {live_pgid}, not the "
                f"recorded group {pgid}; neither group was signalled because the "
                "record disagrees with the running process"
            ),
            reason_code=KILL_LEADER_GROUP_MISMATCH,
            pid=pid,
            pgid=pgid,
        )
    state, marker = _process_marker(pid)
    again, created_again = _process_create_time(pid)
    if again != "found" or created_again != observed_at:
        return _kill_result(
            run_id,
            killed=False,
            reason=(
                f"pid {pid} matched this run's leader, but reading its start time again "
                f"after its group and environment answered {created_again!r} rather than "
                f"{observed_at!r}, so those answers do not describe the process that "
                f"matched and group {pgid} was not confirmed to be this run's; nothing "
                "was signalled"
            ),
            reason_code=KILL_LEADER_IDENTITY_CHANGED,
            pid=pid,
            pgid=pgid,
        )
    if state == "found" and marker is not None and marker != run_id:
        return _kill_result(
            run_id,
            killed=False,
            reason=(
                f"pid {pid} matches this record but carries a different run's id in "
                f"its environment, so group {pgid} is that run's work and not this "
                "one's; nothing was signalled"
            ),
            reason_code=KILL_GROUP_FOREIGN,
            pid=pid,
            pgid=pgid,
        )
    return _signal_group(run_id, job, pid, pgid, sig, KILL_SIGNALLED)


def _refuse_record_without_identity(run_id: str, pid: int) -> dict[str, Any]:
    """Refuse a record that carries no process identity at all.

    Such a record's pid can't be told apart from one the OS has since handed to
    an unrelated process — deriving a group from it now is exactly the step
    that resolves a reused pid to a stranger's group, so nothing is signalled.
    The pid rides along on the refusal as the only handle an operator has left
    for reaping the group by hand.
    """
    return _kill_result(
        run_id,
        killed=False,
        reason=(
            f"this record carries neither a start time nor a process group, so pid {pid} "
            "cannot be distinguished from a reused one and no group was signalled; reap "
            "the group by hand after confirming the process is this run's"
        ),
        reason_code=KILL_NO_RECORDED_IDENTITY,
        pid=pid,
    )


def kill(run_id: str, sig: int = signal.SIGTERM) -> dict[str, Any]:
    """Signal the process group *run_id* was spawned into.

    See docs/internals/mcp.md#kill-safety-contract for the exact guarantee
    (positive identification always precedes a signal; provenance of the
    record itself is out of scope; a TOCTOU window between identification and
    the actual `killpg` call is inherent and unclosable with process groups
    alone).
    """
    job, state = _read_job_state(run_id)
    if state == "absent":
        return _kill_result(
            run_id, killed=False, reason="no such job", reason_code=KILL_NO_SUCH_JOB
        )
    if state == "unreadable":
        return _kill_result(
            run_id,
            killed=False,
            reason=(
                f"the record for {run_id} is on disk but could not be read or parsed, so "
                "nothing is known about the run it describes and nothing was signalled; "
                "the file itself is what has to be looked at"
            ),
            reason_code=KILL_RECORD_UNREADABLE,
        )
    if state == "wrong_shape" or job is None:
        return _kill_result(
            run_id,
            killed=False,
            reason=(
                f"the record for {run_id} holds valid JSON that is not an object, so it "
                "carries no fields to identify a process with and nothing was signalled; "
                "the file itself is what has to be looked at"
            ),
            reason_code=KILL_RECORD_WRONG_SHAPE,
        )

    # A record found under one run that names another wasn't written for the
    # run being killed. Checked before the pid is even looked at.
    recorded_run_id = job.get("run_id")
    if recorded_run_id != run_id:
        return _kill_result(
            run_id,
            killed=False,
            reason=(
                f"the record stored for {run_id} names run "
                f"{_short_repr(recorded_run_id)} instead, so the process it describes "
                f"is not this run's and nothing was signalled; kill that run by its own "
                "id if it is the one meant to stop"
            ),
            reason_code=KILL_RECORD_FOREIGN_RUN,
        )

    # pid 0 means the caller's own process group to killpg, and 1 is init;
    # either must never reach a group signal.
    recorded_pid = job.get("pid")
    pid = _askable_pid(recorded_pid)
    if pid is None:
        return _kill_result(
            run_id,
            killed=False,
            reason=(
                "no pid on record"
                if recorded_pid is None
                else (
                    "no pid on record that can identify a process to signal; the "
                    f"record carries {_short_repr(recorded_pid)}"
                )
            ),
            reason_code=KILL_NO_PID,
            pid=recorded_pid,
        )

    # Neither key on the record at all. What that establishes is that this record
    # cannot identify its process, not how it came to be that way — an absent key
    # says nothing about when or by what it was written. A key that is present and
    # holds the wrong type is a different observation: something that knows about
    # these fields wrote a value nothing can be compared against. The two get
    # different answers because they leave an operator with different things to
    # look at, not because one of them dates the record.
    if "pid_create_time" not in job and "pgid" not in job:
        return _refuse_record_without_identity(run_id, pid)
    created = job.get("pid_create_time")
    pgid = job.get("pgid")
    if not isinstance(created, int | float) or not isinstance(pgid, int) or pgid <= 1:
        return _kill_result(
            run_id,
            killed=False,
            reason=(
                f"the identity recorded for pid {pid} is not usable — start time "
                f"{_short_repr(created)}, process group {_short_repr(pgid)} — so this "
                "record cannot identify its own process and nothing was signalled; reap "
                "the group by hand after confirming the process is this run's"
            ),
            reason_code=KILL_IDENTITY_UNUSABLE,
            pid=pid,
        )
    # Three values reach here that look like numbers and cannot act as one. A NaN or
    # an infinity passes every type and range check above and then loses silently to
    # every comparison below, so the leader would be reported as a recycled pid. A
    # boolean is an int as far as isinstance is concerned, so a start time of `true`
    # arrives as 1.0 — a moment in 1970 — and mismatches the same way. And a JSON
    # integer is unbounded, so a record can carry one too large to be a float at all;
    # converting it is the only way to compare it, and the conversion is what fails,
    # so that refusal has to be decided from the failure rather than after it. All
    # three name the wrong fact if they are allowed through: nothing was established
    # about the pid, only that this record cannot say anything about it.
    try:
        spawned_at = float(created)
        unusable = isinstance(created, bool) or not math.isfinite(spawned_at)
    except OverflowError:
        unusable = True
    if unusable:
        shown = _short_repr(created)
        return _kill_result(
            run_id,
            killed=False,
            reason=(
                f"the start time recorded for pid {pid} is {shown}, which no start "
                "time can be compared against, so this record cannot identify its own "
                "process and nothing was signalled; reap the group by hand after "
                "confirming the process is this run's"
            ),
            reason_code=KILL_IDENTITY_UNUSABLE,
            pid=pid,
            pgid=pgid,
        )

    if _pid_alive(pid):
        state, live_created = _process_create_time(pid)
        if state == "unknown":
            return _kill_result(
                run_id,
                killed=False,
                reason=(
                    f"pid {pid} is alive but its start time could not be read, so it "
                    "cannot be confirmed to be this run; nothing was signalled"
                ),
                reason_code=KILL_LEADER_UNVERIFIABLE,
                pid=pid,
                pgid=pgid,
            )
        if state == "found" and live_created is not None:
            if _start_time_matches(live_created, spawned_at):
                return _signal_leader_group(run_id, job, pid, pgid, sig, live_created)
            return _kill_result(
                run_id,
                killed=False,
                reason=(
                    f"pid {pid} now belongs to a different process (started "
                    f"{live_created:.3f}, this run started {spawned_at:.3f}); "
                    "nothing was signalled"
                ),
                reason_code=KILL_PID_RECYCLED,
                pid=pid,
                pgid=pgid,
            )
        # "gone": exited between the liveness probe and this read; fall
        # through — its group may still be running.

    # Leader gone; its group may outlive it and is reapable once identified —
    # from the group's own live members, never by re-reading the leader's pid
    # (now free for the OS to hand to an unrelated process).
    verdict, rule = _group_identity(pgid, spawned_at, run_id)
    if verdict == "ours":
        return _signal_group(run_id, job, pid, pgid, sig, KILL_SIGNALLED)
    if verdict == "gone":
        return _kill_result(
            run_id,
            killed=False,
            reason=f"already exited; no live process remains in group {pgid}",
            reason_code=KILL_GROUP_GONE,
            pid=pid,
            pgid=pgid,
        )
    # Each refusal gets its own code: settled verdicts (foreign, conflict,
    # predates-run, unproven) read the same on every retry; an incomplete scan
    # is a failed measurement that may succeed next call.
    if verdict == "conflict":
        detail = f"live members of group {pgid} carry different run ids in their environment"
        code = KILL_GROUP_MARKERS_CONFLICT
    elif verdict == "not_ours" and rule == "marker":
        detail = f"a live member of group {pgid} carries a different run's id in its environment"
        code = KILL_GROUP_FOREIGN
    elif verdict == "not_ours":
        detail = f"a live member of group {pgid} started before this run did"
        code = KILL_GROUP_PREDATES_RUN
    elif verdict == "unproven":
        detail = (
            f"no live member of group {pgid} carries a readable run id, and starting "
            "after this run did is not evidence of belonging to it"
        )
        code = KILL_GROUP_OWNERSHIP_UNPROVEN
    else:
        detail = f"group {pgid} could not be fully inspected"
        code = KILL_GROUP_SCAN_INCOMPLETE
    return _kill_result(
        run_id,
        killed=False,
        reason=(
            f"the leader has exited and {detail}; the group could not be confirmed "
            "to be this run's, so nothing was signalled"
        ),
        reason_code=code,
        pid=pid,
        pgid=pgid,
    )


def _notify_delivery_state(outcome: Any) -> str:
    """One word for what became of a run's terminal notice, for the listing view
    (``status`` reports the full ``notify_delivery`` object for one-run detail).

    ``"none"``: not terminal yet, or terminal with no notifier configured —
    silence is the documented default, never a failure. ``"delivered"``: went
    out. ``"failed"``: every way a *configured* notifier came to nothing
    (refused, couldn't start, timed out, non-zero exit) — one fact to a caller
    waiting on it. ``"delivered_unverified"``: ran and exited zero, but for that
    command shape a zero exit doesn't mean the message was sent — collapsing it
    into either neighbor would report a claim this can't support.
    """
    if not isinstance(outcome, dict):
        return "none"
    if outcome.get("ok"):
        if outcome.get("delivery_verified") is False:
            return "delivered_unverified"
        return "delivered"
    if not outcome.get("attempted") and not outcome.get("error"):
        return "none"
    return "failed"


def list_jobs(limit: int = 50, status_filter: str | None = None) -> list[dict[str, Any]]:
    """Recent jobs, newest first (run_id sorts by timestamp).

    Every entry resolves through ``status``, so a conclusively gone run is
    ended here exactly as a direct status read would. ``notify_delivery_state``,
    ``terminal_source``, ``record_state``, and ``spawn_state`` all ride along
    for the same reason: this listing is the surface a caller polls while
    waiting on several runs, and collapsing any of them into the outcome alone
    would hide a fact this listing exists to surface (a failed notice, a
    self-ended orphan, a damaged-but-present record, a spawn stuck vs. genuinely
    running). A directory without a job record is not listed — submissions
    reserve their directory before `job.json` publishes it, the boundary that
    makes a reservation a job. Only a missing (not unreadable) jobs directory
    reports as an empty list; an unreadable one raises, since a listing has no
    field to say "could not be read" and reporting empty would falsely mean
    "no jobs at all".
    """
    try:
        entries = sorted(config.JOBS_DIR.iterdir(), reverse=True)
    except FileNotFoundError:
        return []
    out: list[dict[str, Any]] = []
    for d in entries:
        if not d.is_dir():
            continue
        try:
            (d / "job.json").stat()
        except FileNotFoundError:
            continue
        except OSError:
            pass  # let status() classify an inaccessible record
        st = status(d.name)
        if status_filter and st["status"] != status_filter:
            continue
        out.append(
            {
                "run_id": st["run_id"],
                "kind": st["kind"],
                "label": st["label"],
                "status": st["status"],
                "terminal": st["terminal"],
                "outcome": st["outcome"],
                "reason_code": st["reason_code"],
                "spawn_state": st["spawn_state"],
                "submitted_at": st["submitted_at"],
                "finished_at": st["finished_at"],
                "finished_at_precision": st["finished_at_precision"],
                "terminal_source": st["terminal_source"],
                "record_state": st["record_state"],
                _PERSISTENCE_DEGRADED_REASON_FIELD: st[_PERSISTENCE_DEGRADED_REASON_FIELD],
                "notify_delivery_state": _notify_delivery_state(st["notify_delivery"]),
            }
        )
        if len(out) >= limit:
            break
    return out


def _unresolved_spawn_age(submitted_at: Any) -> float | None:
    """Seconds since *submitted_at*, or ``None`` when that cannot be established
    (missing or unparseable) — never read as an old row, since an unreadable
    timestamp is no evidence that waiting has stopped being useful.
    """
    if not isinstance(submitted_at, str) or not submitted_at.strip():
        return None
    try:
        stamped = datetime.fromisoformat(submitted_at)
    except ValueError:
        return None
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=timezone.utc)  # this writer's own UTC
    return (datetime.now(timezone.utc) - stamped).total_seconds()


def _wait_entry(run_id: Any) -> dict[str, Any]:
    """One observation of *run_id*, resolved through the same path ``status`` uses.

    An id that cannot be observed comes back as an entry carrying an ``error``
    rather than raising, so one bad id never costs the caller the ids beside it
    — every entry carries the full lifecycle shape either way. ``not_found``
    (no record) and ``record_unusable`` (present but damaged) are different
    news and get different codes — collapsing them would send an operator away
    from a file that's actually on disk.
    """
    entry: dict[str, Any] = {
        "run_id": run_id,
        "kind": None,
        "label": None,
        "status": "unknown",
        "terminal": False,
        "outcome": None,
        "reason_code": None,
        "possibly_orphaned": False,
        "spawn_state": None,
        "submitted_at": None,
        "error": None,
    }
    if not isinstance(run_id, str) or not run_id.strip():
        entry["error"] = {"kind": "invalid_input", "message": "run id must be a non-empty string"}
        return entry

    st = status(run_id)
    if not st["known"]:
        if st["record_state"] == "absent":
            entry["error"] = {"kind": "not_found", "message": f"no job with id {run_id}"}
        else:
            entry["error"] = {
                "kind": "record_unusable",
                "message": f"{_NO_RECORD_ERROR[st['record_state']]}: {run_id}",
            }
        return entry

    entry.update(
        {
            "kind": st["kind"],
            "label": st["label"],
            "status": st["status"],
            "terminal": st["terminal"],
            "outcome": st["outcome"],
            "reason_code": st["reason_code"],
            "possibly_orphaned": st["possibly_orphaned"],
            "spawn_state": st["spawn_state"],
            "submitted_at": st["submitted_at"],
        }
    )
    return entry


def _clamp(value: float, low: float, high: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return low
    if v != v:  # NaN: no ordering, so no clamp can be meaningful
        return low
    return max(low, min(high, v))


async def wait(
    run_ids: list[str],
    max_wait: float = 60.0,
    poll_interval: float = 1.0,
) -> dict[str, Any]:
    """Observe *run_ids* until they are all terminal or the window closes.

    A bounded observation, not a subscription: mixed outcomes are the normal
    case, so the result is never a bare boolean — see
    docs/internals/mcp.md#wait-result-buckets for the full ``pending`` /
    ``stopped_without_end`` / ``unresolved_spawn`` / ``all_terminal`` /
    ``timed_out`` contract, including why ``unresolved_spawn`` exists and the
    back-off floor paid while any id sits in either special bucket.

    Observing leaves the run as it was, with one fenced exception: resolving
    a status may durably reap a conclusively-gone started orphan (see
    ``_reap_if_conclusively_gone``). A wait that expires, or whose caller
    cancels or disconnects, leaves the durable record untouched (cancelling
    an observation is not cancelling the work).
    """
    import anyio  # deferred: the CLI's terminal hook also imports this module and stays import-light

    ordered = list(run_ids)
    eff_max = _clamp(max_wait, 0.0, WAIT_MAX_SECONDS)
    eff_poll = _clamp(poll_interval, WAIT_MIN_POLL_SECONDS, WAIT_MAX_POLL_SECONDS)
    deadline = anyio.current_time() + eff_max

    entries: list[dict[str, Any]] = []
    pending: list[str] = []
    stopped: list[str] = []
    unresolved: list[str] = []
    waited = False
    while True:
        entries = [_wait_entry(rid) for rid in ordered]
        observed = [e for e in entries if e["error"] is None]
        stopped = [e["run_id"] for e in observed if e["possibly_orphaned"]]
        # Keyed on spawn_state == "preparing" (never != "started"): a pre-field
        # record carries null, meaning "no phase this can vouch for", not
        # "never attempted".
        unresolved = [
            e["run_id"]
            for e in observed
            if not e["terminal"]
            and not e["possibly_orphaned"]
            and e["spawn_state"] == "preparing"
            and (age := _unresolved_spawn_age(e["submitted_at"])) is not None
            and age >= UNRESOLVED_SPAWN_AFTER_SECONDS
        ]
        unresolved_ids = set(unresolved)
        pending = [
            e["run_id"]
            for e in observed
            if not e["terminal"]
            and not e["possibly_orphaned"]
            and e["run_id"] not in unresolved_ids
        ]
        remaining = deadline - anyio.current_time()
        if remaining <= 0:
            break
        # The stopped_without_end/unresolved_spawn back-off floor (see docs).
        if not pending and (waited or not (stopped or unresolved)):
            break
        waited = True
        await anyio.sleep(min(eff_poll, remaining))

    errored = any(e["error"] is not None for e in entries)
    return {
        "runs": entries,
        "all_terminal": not pending and not errored and not stopped and not unresolved,
        "timed_out": bool(pending),
        "pending": pending,
        "stopped_without_end": stopped,
        "unresolved_spawn": unresolved,
        "unresolved_spawn_after": UNRESOLVED_SPAWN_AFTER_SECONDS,
        "max_wait": eff_max,
        "poll_interval": eff_poll,
        "requested_max_wait": max_wait,
        "requested_poll_interval": poll_interval,
    }


def mark_terminal(run_id: str, cli_status: str) -> WriteResult:
    """Record a terminal status for *run_id* (called by the CLI notify hook).

    The CLI's terminal status is trusted and recorded verbatim, never matched
    against a local set (an earlier version's fallback-to-`"completed"` on any
    miss silently turned `timed_out`/`cancelled`/`aborted`/`completed_empty`
    into a false success). First-writer-wins: a record that already carries an
    end (a kill, a lifecycle-cached end, an orphan transition published while
    this hook was starting) keeps it — this call reports what's there rather
    than replacing it, though its own delivery attempt still goes ahead.
    """
    with _locked_job(run_id) as guard:
        job = guard.record
        if job is None or job.get("finished_at") is not None:
            return WriteResult(job, guard.state)
        job["status"] = cli_status
        job["cli_status"] = cli_status
        job["finished_at"] = _now_iso()
        # The hook fires as the run ends, so this is the end as it happened.
        job["finished_at_precision"] = FINISHED_AT_OBSERVED
        job["terminal_source"] = TERMINAL_SOURCE_HOOK
        return WriteResult(job, guard.state)


def record_notify_delivery(run_id: str, outcome: dict[str, Any]) -> WriteResult:
    """Record whether the terminal notice was delivered (called by the notify hook).

    Surfaced by ``status`` so a failed completion notice is visible rather than
    silently lost. Merges only the delivery result, under the same per-run
    lock as every other mutation, so it never carries a stale copy of the
    lifecycle fields back over an end recorded while the notice was in flight —
    a delivery outcome never changes how the run came out.
    """
    with _locked_job(run_id) as guard:
        job = guard.record
        if job is None:
            return WriteResult(None, guard.state)
        job["notify_delivery"] = outcome
        return WriteResult(job, guard.state)


def record_failure_cause(run_id: str, cause: dict[str, Any]) -> WriteResult:
    """Record the typed class of the exception that ended *run_id*.

    Merged under the same per-run lock as every other mutation, and merged
    alone: a cause describes how the run ended and must never carry a stale copy
    of the lifecycle fields back over an end recorded while the hook was
    starting, for the same reason ``record_notify_delivery`` does not.

    Only called with a cause that was actually read. A run whose cause file is
    absent leaves this field off the record entirely, so a caller can tell a run
    that reported no typed cause from one that predates the field.
    """
    with _locked_job(run_id) as guard:
        job = guard.record
        if job is None:
            return WriteResult(None, guard.state)
        job["failure_cause"] = cause
        return WriteResult(job, guard.state)


def failure_cause_path(run_id: str) -> Path | None:
    """Where *run_id*'s cause file would be, or None if it has no job directory.

    Resolved through ``config.job_dir`` — the same function the submit that set
    the child's environment used — so the two cannot drift onto different
    layouts. Reading the path back off the record instead would be checking the
    value against itself.
    """
    d = config.job_dir(run_id)
    return d / config.CAUSE_FILENAME if d.is_dir() else None
