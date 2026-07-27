# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Background job engine for the lionagi MCP server.

``submit()`` spawns a ``li`` command as a detached process and returns immediately
with the run_id. The id is pre-assigned via ``LIONAGI_RUN_ID`` so it is known
before the child starts (no polling to discover it). ``status()`` / ``output()`` /
``kill()`` / ``list_jobs()`` / ``wait()`` then operate on that id by reading the
run state the CLI persists plus the MCP server's own small per-job record.

The detached child gets its own session/pgid (``start_new_session``), so it
survives an MCP-server restart and can still be signalled as a group. That is why
job state lives on disk rather than in server memory.

Every response that carries a run's ``status`` carries ``terminal`` and
``outcome`` with it, derived here from the durable record. ``status`` itself is an
open vocabulary passed through verbatim, so a caller never needs — and must never
keep — a copy of lionagi's status names to tell a finished run from a running one
or a success from a failure. All of these resolve through one path, ``status()``,
so no two calls can disagree about the same run at the same moment.

A run's end reaches that path from two writers. The terminal hook the CLI runs
on ``--notify`` writes it into this package's own job record. A run stopped by
``li kill`` never reaches that hook — the kill transitions the lifecycle row and
signals the process, and writes nothing here — so when the process is gone and
the job record shows no end, the state is read from the CLI itself, via
``li lifecycle <run_id> --machine``, and cached back onto the job record. A read
that cannot be made concludes nothing: the run is classified exactly as it would
have been without it.
"""

from __future__ import annotations

import json
import math
import os
import shlex
import signal
import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import config

# li subcommand for each job kind. "orchestrate" is the canonical parser name
# (the `o` alias also works); flow and fanout live under it.
#
# "play" is spawned as `orchestrate flow -p NAME`, which is exactly what the CLI's
# own `li play` sugar rewrites itself into. Going through the expanded form rather
# than the sugar is deliberate: the sugar has to locate NAME by probing the flow
# parser when a flag precedes it, and it rejects a playbook's own declared args in
# that path — while `o flow -p NAME` injects the playbook's arg schema into the
# parser and accepts them. Since every submit prepends --notify, the sugar would
# always take the probing path.
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

# The lifecycle read is a control-plane query against a local store; anything
# slower than this is treated as unavailable rather than waited on, because it
# is consulted from inside a caller's own poll.
LIFECYCLE_TIMEOUT_SECONDS = 20.0

# The most the lifecycle command may write on its result channel.
_LIFECYCLE_OUTPUT_LIMIT = 1_000_000

# Bounds for wait(). The maximum sits below ordinary MCP client timeouts so a
# bounded observation returns partial results rather than being cut off mid-call.
WAIT_MAX_SECONDS = 600.0
WAIT_MIN_POLL_SECONDS = 0.05
WAIT_MAX_POLL_SECONDS = 60.0

# The terminal hook module, invoked by the CLI's --notify by absolute
# interpreter path so it runs regardless of PATH in the CLI's environment.
_NOTIFY_MODULE = "lionagi.mcp._notify_hook"

# A process start time is read from the kernel in clock ticks, so two reads of
# the same process can differ in the last decimal. Compared within this
# tolerance, the same way the CLI's own kill path compares it.
_CREATE_TIME_TOLERANCE = 0.1

# Reason codes carried by kill(). The human `reason` explains the particular
# case; the code is what a caller can branch on without matching prose.
KILL_NO_SUCH_JOB = "no_such_job"
# The record is on disk and cannot be used. Two codes rather than one, on the same
# axis as everything else here: bytes that could not be read or parsed may read
# differently on the next call, whereas a record that parsed cleanly into something
# other than an object will answer identically every time and only a person can
# resolve it.
KILL_RECORD_UNREADABLE = "job_record_unreadable"
KILL_RECORD_WRONG_SHAPE = "job_record_wrong_shape"
# The record parsed into an object and names a run other than the one asked about.
# Its own code rather than the shape ones above: those say a file has to be looked
# at by a person, while this one names the run the record does describe, and acting
# on it is a call this caller can make on its own.
KILL_RECORD_FOREIGN_RUN = "job_record_names_another_run"
KILL_NO_PID = "no_pid_on_record"
KILL_SIGNALLED = "signalled"
KILL_PROCESS_GONE = "process_gone"
KILL_PERMISSION_DENIED = "permission_denied"
# The record was written before a run's process identity was recorded, so the
# pid on it cannot be told apart from a reused one and nothing can be signalled
# for it. Its own code, so a reader can tell an old record from a refusal
# decided about a record that does carry an identity.
KILL_LEGACY_NO_IDENTITY = "legacy_record_no_process_identity"
# The identity fields are present and of the right type but hold a value nothing
# can be compared against where a start time belongs. Its own code rather than the
# one above: that one says a record is old and expected, this one says a record is
# damaged, and only the second is worth investigating.
KILL_IDENTITY_UNUSABLE = "recorded_identity_unusable"
# Identity-bearing records. Split by what a caller would do next: a mismatch or
# a foreign group is settled and will not change on a retry, while an unreadable
# probe or an incomplete scan is a measurement that failed and may succeed later.
KILL_PID_RECYCLED = "pid_recycled"
KILL_LEADER_UNVERIFIABLE = "leader_identity_unreadable"
# The leader's start time was read twice around the reads that describe its
# group, and the two readings are not the same value. Separate from the code
# above, which says the start time could not be read at all: here it was read,
# twice, and what came back does not describe one process.
KILL_LEADER_IDENTITY_CHANGED = "leader_identity_changed"
KILL_LEADER_GROUP_MISMATCH = "leader_group_mismatch"
KILL_LEADER_GROUP_UNREADABLE = "leader_group_unreadable"
KILL_GROUP_GONE = "group_gone"
KILL_GROUP_FOREIGN = "group_belongs_to_another_run"
KILL_GROUP_MARKERS_CONFLICT = "group_markers_conflict"
KILL_GROUP_PREDATES_RUN = "group_predates_run"
KILL_GROUP_SCAN_INCOMPLETE = "group_scan_incomplete"
# The group was inspected end to end and simply yielded no evidence of ownership:
# every member's environment was read and none of them carries a marker. Separate
# from an incomplete scan, which covers a member whose environment would not open
# at all, because that
# one is a measurement that failed and may answer on the next call, while this one
# is the measurement succeeding and returning nothing — the same call will keep
# returning it, and only an operator can settle the group.
KILL_GROUP_OWNERSHIP_UNPROVEN = "group_ownership_unproven"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id() -> str:
    """Mint a run_id in the CLI's own format: ``YYYYMMDDTHHMMSS-<6hex>``."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{ts}-{uuid4().hex[:6]}"


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
    d = config.job_dir(record["run_id"])
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".job.json.{os.getpid()}.{uuid4().hex[:8]}.tmp"
    try:
        tmp.write_text(json.dumps(record, indent=2))
        os.replace(tmp, d / "job.json")
    except OSError:
        # Do not leave the staging file behind: a run whose writes keep failing
        # would otherwise accumulate orphans in its job dir. The original error
        # still propagates.
        tmp.unlink(missing_ok=True)
        raise


def _short_repr(value: Any, limit: int = 60) -> str:
    """A recorded value, shown as written and bounded in length.

    Reporting the value as written rather than as coerced lets a reader see the
    damage instead of a plausible-looking substitute. It came off disk, though, and
    a JSON number or string has no length limit, so the record must not get to
    choose how long an answer is.
    """
    shown = repr(value)
    return shown if len(shown) <= limit else f"{shown[:limit]}… ({len(shown)} characters)"


def _read_job_state(run_id: str) -> tuple[dict[str, Any] | None, str]:
    """The job record for *run_id*, and why there isn't one when there isn't.

    ``("absent", "unreadable", "wrong_shape")`` are three different facts and only
    the first means the run is unknown. A record whose bytes cannot be read or
    parsed is present and damaged; a record that parses to a JSON value that is not
    an object — an array, a string, ``null`` — is present, intact and unusable.
    Reporting either as "no such job" tells an operator to stop looking for a run
    whose file is sitting on disk.

    The record is returned only in the ``"ok"`` case, so nothing downstream can
    reach a value this did not admit.

    Absence is established by the read itself rather than by a separate question
    about whether the path is there. Asking first and reading second answers a
    question nobody asked — the path may become unreadable between the two — and,
    more plainly, a path whose directory cannot be searched is not a path that was
    found to be missing. Only "the file is not there" is absence; every other way
    the read can fail is a record that is present and could not be got at.
    """
    p = config.job_dir(run_id) / "job.json"
    try:
        raw = p.read_text()
    except FileNotFoundError:
        return None, "absent"
    except OSError:
        return None, "unreadable"
    try:
        record = json.loads(raw)
    except json.JSONDecodeError:
        return None, "unreadable"
    if not isinstance(record, dict):
        return None, "wrong_shape"
    return record, "ok"


def _read_job(run_id: str) -> dict[str, Any] | None:
    """The job record, or None when there is no usable one.

    Every reader that only needs the record goes through here and gets what it
    always got, including a falsy answer to fall back on. A caller that has to tell
    an unknown run from a damaged file reads the state alongside it instead.
    """
    return _read_job_state(run_id)[0]


def _read_lifecycle(run_id: str) -> dict[str, Any] | None:
    """Ask the CLI what the lifecycle store records about *run_id*.

    Spawned as ``li lifecycle <run_id> --machine``, the same way every other
    non-job verb reaches the CLI. Going through the command rather than opening
    the database here keeps one reader of a schema this package does not own.

    Returns the summary the command established, or None when it could not be
    established for any reason at all — the command missing, refusing, timing
    out, or answering that the store was unreadable. None is not "no record":
    a caller must treat it as "did not learn anything" and fall back to what it
    already knew, because the alternative is calling a run finished on the
    strength of a read that never happened.
    """
    argv = [*config.li_command(), "lifecycle", run_id, "--machine"]
    try:
        completed = subprocess.run(  # noqa: S603 — resolved li command plus one run id, no shell
            argv,
            capture_output=True,
            timeout=LIFECYCLE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if len(completed.stdout) > _LIFECYCLE_OUTPUT_LIMIT:
        return None
    text = completed.stdout.decode("utf-8", "replace").strip()
    if not text:
        return None
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError:
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
    try:
        return json.loads(config.run_manifest(run_id).read_text())
    except (OSError, json.JSONDecodeError):
        return None


# --- process + log helpers -----------------------------------------------------


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 1:
        return False
    # A detached child is still OUR child, so once it exits unreaped it lingers
    # as a zombie and `kill -0` would report it alive. Reap it first: waitpid
    # returns (pid, _) if it just exited, (0, 0) if still running, and raises
    # ChildProcessError when it is not our child (e.g. after an MCP-server
    # restart, where init reaps it and a direct probe is authoritative).
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


def _process_create_time(pid: int) -> tuple[str, float | None]:
    """When the process at *pid* started: ``("found", t)``, ``("gone", None)``
    or ``("unknown", None)``.

    Three answers, not two. "unknown" is a probe that errored — the process may
    well be there — and a caller must never read it as death or as licence to
    signal. A zombie answers "gone": it has exited, holds its pid until it is
    reaped, and cannot be a recycled pid while it does.
    """
    import psutil

    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return "gone", None
        return "found", proc.create_time()
    except psutil.NoSuchProcess:
        return "gone", None
    except (psutil.Error, OSError):
        return "unknown", None


def _start_time_matches(observed: float, recorded: float) -> bool:
    """Whether a start time read now is the one recorded for this run at spawn.

    Within a tolerance, because the two are separate reads of a clock the kernel
    keeps in ticks. Only for a recorded value against a live one: two live reads
    of the same process must be equal, and letting those drift would weaken the
    check that tells a recycled pid from the process that held it.
    """
    return abs(observed - recorded) <= _CREATE_TIME_TOLERANCE


def _spawned_pgid(pid: int) -> int:
    """The process group of a just-spawned child.

    Read from the OS, with the child's pid as the fallback: it was started with
    ``start_new_session``, so it leads its own group and the two are equal by
    construction. Recorded at spawn because deriving it at kill time is what
    lets a reused pid resolve to a stranger's group.
    """
    try:
        return os.getpgid(pid)
    except OSError:
        return pid


def _pinned_member(pid: int, pgid: int) -> tuple[str, tuple[int, float, str | None, bool] | None]:
    """Everything *pid* has to say as a member of *pgid*, read as one observation.

    ``("found", (pid, create_time, marker, marker_read))`` when a single process
    answered all of it, ``("gone", None)`` when the pid holds no live member of
    this group, and ``("unknown", None)`` when the reads could not be tied to one
    process.

    Group, start time and marker are three facts, each read by pid, and a pid
    the OS reassigns between two of those reads answers the later ones as the
    replacement process. A verdict assembled from those answers would describe
    no process that ever existed, so the reads are bracketed by the start time:
    read before, read again after, required to be unchanged. That is the value
    that tells a recycled pid from the process that held it, and it is what
    binds the other two to the same process. Failing the bracket is "unknown" —
    a measurement that did not come off, never evidence about the group.

    *marker_read* travels with the marker because a None marker alone does not
    say which of two things happened. The environment read and held no marker,
    and the environment could not be read at all, are the same None; only this
    flag tells a member that was inspected from one that refused inspection.
    """
    state, created = _process_create_time(pid)
    if state == "gone":
        return "gone", None
    if state != "found" or created is None:
        return "unknown", None
    try:
        in_group = os.getpgid(pid) == pgid
    except ProcessLookupError:
        return "gone", None
    except OSError:
        return "unknown", None
    marker_state, marker = _process_marker(pid)
    again, created_again = _process_create_time(pid)
    if again != "found" or created_again != created:
        return "unknown", None
    if not in_group:
        return "gone", None
    return "found", (pid, created, marker, marker_state == "found")


def _live_group_members(pgid: int) -> tuple[list[tuple[int, float, str | None, bool]], bool]:
    """Live members of process group *pgid*, and whether the scan was complete.

    Returns ``(members, complete)`` where each member is ``(pid, create_time,
    marker, marker_read)``. All of it arrives together, from
    :func:`_pinned_member`, so that a caller weighing a member's marker and a
    member's age is weighing one process. The group read in the loop below only
    narrows the process table to candidates; the membership that counts is the
    one read inside the bracket.

    A process that vanishes mid-scan is simply not a live member; a process
    whose group or identity could not be read leaves *complete* false, because
    the group may then hold a member this scan never saw. A member that cannot
    be pinned is never quietly dropped instead: a scan that reported itself
    complete while a member went unread would let a live group be answered for
    as gone. Zombies are excluded: an unreaped corpse still counts as a group
    member to the kernel, so counting it would report a group that is empty of
    running work as live.

    *complete* is about membership coverage and nothing else. A member whose
    marker could not be read was still seen — its pid, group and start time all
    answered — so it opens no gap in the membership, and it is reported as a
    member carrying *marker_read* false rather than as a member the scan missed.
    """
    import psutil

    members: list[tuple[int, float, str | None, bool]] = []
    complete = True
    try:
        pids = psutil.pids()
    except (psutil.Error, OSError):
        return [], False

    for pid in pids:
        if pid <= 1:
            continue
        try:
            if os.getpgid(pid) != pgid:
                continue
        except ProcessLookupError:
            continue
        except OSError:
            complete = False
            continue
        state, member = _pinned_member(pid, pgid)
        if state == "found" and member is not None:
            members.append(member)
        elif state == "unknown":
            complete = False
    return members, complete


def _process_marker(pid: int) -> tuple[str, str | None]:
    """The run marker carried by the process at *pid*.

    ``("found", value_or_None)`` when the environment was read, ``("unknown",
    None)`` when it raised — a probe that failed, and never evidence about the
    process.

    A None value is weaker than it looks and must not be read as "this process
    does not carry the marker". macOS returns an *empty* environment, without
    raising, for a process running a protected system binary, so a declined
    disclosure and a genuinely absent marker arrive identically. That is safe
    only in one direction: an unreadable environment contributes no marker, and
    no marker can only ever withhold ownership, never assert it. Nothing here
    may be inverted into evidence that a group is not this run's.
    """
    import psutil

    try:
        return "found", psutil.Process(pid).environ().get(config.JOB_MARKER_ENV_VAR)
    except (psutil.Error, OSError, UnicodeDecodeError):
        return "unknown", None


def _group_identity(pgid: int, spawned_at: float, run_id: str) -> tuple[str, str]:
    """Whether the live group *pgid* can be the group this run spawned.

    Returns the verdict and the rule that reached it. Two rules, tried in that
    order:

    The marker decides positively. Every process the run spawned carries the
    run id in its environment, so a live member that reads back this run's id
    identifies the group outright — members share a pgid, so one confirmed
    member makes the group this run's. A member carrying a *different* run's id
    is the same evidence pointing the other way: the group number has been
    reused.

    Every readable marker is collected before the rule is applied, because
    deciding on the first one read would make the verdict depend on the order
    the process table happened to be enumerated in — the same group could then
    be accepted or refused between two calls. Each marker arrives already tied
    to the member that carries it and to that member's age, so no rule here
    reasons about a pid, only about processes the scan pinned. Markers that disagree are
    ``"conflict"``: two runs cannot both own a group, so whatever produced the
    disagreement is unexplained, and an unexplained group is not signalled.

    The start time can only ever exclude. A member that started before this run
    did cannot be work this run spawned, so the group number has been handed on
    and the answer is ``"not_ours"``. The converse does not follow: every member
    being younger than the run is consistent with the group being ours and
    equally consistent with an unrelated group that simply started later, so it
    is not an identification and is never treated as one. A dead leader whose
    group yields no marker, every member's environment having been read, is
    ``"unproven"`` — inspected in full, and found to carry no evidence either
    way.

    ``"gone"`` when nothing live is left in the group, and ``"unknown"`` when
    the scan itself could not be completed. Two things leave it incomplete: a
    member it could not read at all, which leaves a group that may hold work the
    scan never saw, and a member it saw whose environment refused to be read,
    which leaves the marker rule undecided on a process it did see. Neither is a
    finding about the group, and both may answer on the next call, so they are
    the same news and share an answer.
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
    show.

    A log that cannot be read reports as no tail rather than as an error. Unlike
    the job record, the tail is advisory — the caller has already been told the
    run exists and what state it is in — so there is nothing an operator would do
    differently on "no log yet" versus "the log could not be read", and neither is
    worth failing the surrounding call for.
    """
    if not path:
        return None
    try:
        data = Path(path).read_text(errors="replace")
    except OSError:
        return None
    return data[-limit:] if len(data) > limit else data


def _list_artifacts(run_id: str) -> list[str]:
    adir = config.run_dir(run_id) / "artifacts"
    try:
        return sorted(str(p.relative_to(adir)) for p in adir.rglob("*") if p.is_file())
    except OSError:
        return []


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

    Linux caps one argument at ``MAX_ARG_STRLEN`` (32 pages) independently of the
    aggregate limit and exposes no ``sysconf`` for it, so it is derived from the
    running page size. Other platforms — macOS among them — bound only the total,
    and happily exec a single argument far larger than this; applying the Linux
    number there would refuse work the OS would have accepted.
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
    """Refuse a command line the OS will not accept, before anything is spawned.

    ``exec`` rejects an oversized invocation with ``OSError: [Errno 7] Argument
    list too long``, which arrives too late to be useful: by then the caller has a
    run id for a process that never started. There are two independent limits and
    both have to hold.

    The *aggregate* limit covers argv and the environment together and is readable
    as ``SC_ARG_MAX``. Alongside the strings themselves the kernel stores a
    terminator and a pointer per entry, so entries are counted, not just bytes —
    a flat reserve would be defeated by a long list of short arguments.

    The *per-argument* limit applies to one string on its own, and only where the
    platform imposes one — see :func:`_max_single_arg_bytes`. It is checked
    separately because an argument can be under the aggregate limit and still be
    refused on its own.
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

    Used only once a run has been established terminal by a recorded end, never
    to decide whether it ended. A status this build has never heard of is a
    failure, because the failure mode of a stale success list is a timeout or an
    empty completion read back as a success.
    """
    if status in _SUCCEEDED_STATUSES:
        return "succeeded"
    if status in _CANCELLED_STATUSES:
        return "cancelled"
    return "failed"


def _derive(
    job: dict[str, Any] | None,
    alive: bool,
    lifecycle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify a job record into the fields a caller is allowed to branch on.

    ``status`` is an open vocabulary: whatever the CLI recorded is passed through
    verbatim and is never matched against a local set to decide anything.

    ``terminal`` answers "stop waiting" and comes only from a recorded end — a
    ``finished_at`` written by the terminal hook or by ``kill``, a spawn failure
    the producer caught and wrote down, or an end recorded in the lifecycle
    store, which is where a run stopped by ``li kill`` leaves its only trace. It
    is never inferred from the status string and never from a missing pid:
    between the pre-spawn write and the write that attaches the pid, a perfectly
    healthy child has no pid yet.

    *lifecycle* is the summary ``li lifecycle`` established for this run, or None
    when nothing could be established. None never terminalises anything: a read
    that failed leaves the classification exactly as it was before this argument
    existed.

    ``outcome`` answers "did the work come out right" and is null whenever
    ``terminal`` is false — including for a run whose process is gone with no end
    recorded, which has stopped and is still not terminal.
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
            "outcome": _outcome_for(recorded),
            # A reason carried on the record wins: it came from the lifecycle
            # store, which knows why the run ended, while the status-derived one
            # is only what the status alone can say.
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

    # The process is gone (or was never there) and the sidecar records no end.
    # The lifecycle store is the other place an end gets written, and the only
    # place a cancellation does: `li kill` transitions the row and signals the
    # pid, and writes nothing here.
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
        # The spawn has not been attempted yet, or its result has not been
        # written. Report the record as it stands and make no claim about the
        # spawn's fate; a stale one stays non-terminal rather than being resolved
        # by a bound that cannot tell a loaded machine from a dead spawn.
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
    return {
        "status": "exited",
        "terminal": False,
        "outcome": None,
        "reason_code": None,
        "spawn_state": spawn_state,
        "possibly_orphaned": True,
    }


# --- public API ----------------------------------------------------------------


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
    child's MCP servers come from, handed over as values rather than left to be
    read back out of *flags*. Both are already rendered into *flags* by the
    surface; they are repeated here because this function has to decide whether
    to resolve a set of its own, and that is a decision about intent, not about
    tokens.
    """
    if kind not in _KIND_ARGV:
        raise ValueError(f"unknown job kind {kind!r}; expected one of {sorted(_KIND_ARGV)}")

    run_id = new_run_id()
    d = config.job_dir(run_id)
    log_path = d / "console.log"

    # The whole command line is assembled before anything is created on disk, so a
    # run that cannot be spawned leaves no trace. Creating the directory first
    # would leave an empty one behind on a rejection, and that reads back as a job
    # with no kind that never finishes.
    # `flags` may already carry a `--` sentinel, after which every token is a
    # positional. Options this function adds have to go in front of it, or they
    # arrive as text: appending `--prompt-file` past the sentinel would hand the
    # agent two words of prompt instead of a file to read.
    options, positionals = _split_at_sentinel(flags)
    prompt_path = None
    if prompt is not None:
        if kind == "agent":
            prompt_path = d / "prompt.txt"
            options += ["--prompt-file", str(prompt_path)]
        else:
            # flow/fanout take the prompt as a positional, and a prompt may well
            # begin with a dash, so it goes behind a sentinel whether or not the
            # rendered flags already opened one.
            if not positionals:
                positionals = ["--"]
            positionals.append(prompt)

    # A run discovers MCP servers from the directory it is told to work in,
    # which for a detached run is a checkout and not the directory holding this
    # server's config. Resolve it here, where the submitting directory is still
    # the one in effect, and hand the resolved set to the child.
    # Both outcomes are reported on the handle: a run that starts without the
    # tools its brief assumes should be visible at submit, not deduced later
    # from its own confused output. An orchestration builds many workers from
    # the one set its process holds, so leaving the choice to whatever each
    # provider CLI finds for itself scatters the same question across every
    # worker and answers it where nobody is looking.
    #
    # The servers are read here and written into this run's own directory, and
    # that copy is what the child is pointed at. Naming the discovered file
    # instead would leave the run's tool surface tied to a file anyone may edit
    # between submission and execution — and a run that resumes hours later
    # would re-read it again, so the same submission could start with a
    # different set of tools every time. A file only this run writes cannot
    # change under it, and staying a path keeps the child's existing flag
    # working. A config that exists but cannot be used fails the submission,
    # because a child that discovers the problem reports it minutes later and
    # only in its own log, while the submitter was told the run started.
    #
    # A snapshot is taken only when the caller left the choice open. Whether
    # they did is answered from the values they passed, never by looking through
    # the tokens for a flag: those tokens are built by the same surface, in a
    # form (`--flag=value`) chosen so that nothing downstream can take them
    # apart, so a scan of them reports on spelling rather than on intent.
    mcp_config_path: str | None = None
    mcp_config_source: str | None = None
    mcp_config_reason: str | None = None
    mcp_servers: dict[str, Any] | None = None
    if no_mcp_config:
        # The caller asked for no servers. That is an answer, not an absence, so
        # nothing is resolved and the handle says whose decision it was.
        mcp_config_reason = "mcp_disabled_by_caller"
    elif mcp_config is not None:
        # The caller named the file, and their flag is already on the line. No
        # snapshot is taken and none is prepended: a second --mcp-config would
        # let the parser pick between them, and the handle would go on naming
        # the one the child did not read. What the child reads is what the
        # handle reports, and its source is the caller's own path, which this
        # run does not own and cannot promise will hold still.
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
        else:
            mcp_servers = resolution.servers
            mcp_config_source = str(resolution.source) if resolution.source else None
            mcp_config_path = str(d / "mcp-servers.json")
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
    # The child carries the run that started it. Every process it goes on to
    # spawn inherits this, so a live member of the group can later be asked
    # what it belongs to instead of being guessed at from when it started.
    env[config.JOB_MARKER_ENV_VAR] = run_id

    # Only "agent" hands the instruction over in a file; flow and fanout take it
    # as a positional, so a long one has to fit in the process argument vector.
    # Checked before anything is written, because Popen raising this late would
    # leave a job recorded as "running" for a run that never started.
    _reject_oversized_argv(argv, env, kind=kind)

    d.mkdir(parents=True, exist_ok=True)
    if prompt_path is not None:
        prompt_path.write_text(prompt)
    if mcp_servers is not None and mcp_config_path is not None:
        Path(mcp_config_path).write_text(json.dumps({"mcpServers": mcp_servers}, indent=2))

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
        "label": label,
        "notify_command": notify_command,
        "notify_target": notify_target,
        "notify_sender": notify_sender,
        "mcp_config": mcp_config_path,
        "mcp_config_source": mcp_config_source,
        "mcp_config_reason": mcp_config_reason,
        "submitted_at": _now_iso(),
        "finished_at": None,
        "status": "running",
        "spawn_state": "preparing",
        "log": str(log_path),
    }
    _write_job(record)

    try:
        log_f = open(log_path, "wb")
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
        # The record already exists and no process will ever mark it, so the
        # producer that caught the failure marks it here: without this write the
        # run claims "running" forever, and nothing in the system can correct it.
        #
        # Every exception, not the errno family alone. What a spawn refuses is the
        # platform's business and does not arrive by one route: an argument the
        # exec cannot carry raises ValueError, with no errno anywhere in it. The
        # invariant being kept is about the record — written, therefore marked —
        # so making it depend on having enumerated the ways a spawn can fail would
        # leave the next unenumerated one stranding a run exactly as this one did.
        raise _record_spawn_failure(run_id, exc) from exc

    # Attach the pid without rewriting status: if the hook already recorded a
    # terminal in the (tiny) spawn window, re-reading here preserves it.
    #
    # The pid goes down with the two things that say WHICH process it was: when
    # that process started, and the group it leads. A pid number on its own is
    # not an identity — the OS hands it out again once the process is reaped —
    # so a kill holding only a number cannot tell the run it started from
    # whatever occupies that number later. The start time is read here, while
    # the child is certainly the one just spawned; a read that fails leaves it
    # null, which kill() reads as "no identity was captured" rather than as any
    # claim about the process.
    latest = _read_job(run_id) or record
    latest["pid"] = proc.pid
    _state, created = _process_create_time(proc.pid)
    latest["pid_create_time"] = created
    latest["pgid"] = _spawned_pgid(proc.pid)
    latest["spawn_state"] = "started"
    _write_job(latest)

    # The handle carries the same three lifecycle fields every other
    # status-bearing response does, so a caller never has to classify the status
    # string itself — including in the narrow case where the child reached a
    # terminal before this line ran.
    # Popen returned, so the child exists; no liveness probe is taken here, which
    # would only add a race in which an instant exit reads back as an orphan.
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
        "notify_sender": notify_sender,
    }


def _record_spawn_failure(run_id: str, exc: Exception) -> SpawnError:
    """Write the terminal record for a spawn that failed, and build the error.

    Records the spawn phase as ``failed`` and, in the same write, the end itself:
    a terminal record with a reason naming the spawn failure. Without the second
    part the phase would say the spawn failed while the lifecycle still said the
    run was going, which is two answers to one question.
    """
    reason = f"spawn failed: {exc}"
    record = _read_job(run_id) or {"run_id": run_id}
    record.update(
        {
            "spawn_state": "failed",
            "status": "failed",
            "finished_at": _now_iso(),
            "reason": reason,
        }
    )
    try:
        _write_job(record)
    except OSError:
        # The corrective write can fail on exactly the disk that refused the
        # spawn. The caller still gets the failure and the run_id; what is lost
        # is the durable mark, which is why the raise below carries the record.
        pass
    return SpawnError(run_id, record, f"could not spawn run {run_id}: {exc}")


def _record_is_terminal(job: dict[str, Any]) -> bool:
    """Whether the record itself already says the run ended.

    The same notion `_derive` classifies on, and deliberately not a membership
    test against a set of terminal status strings: the status is whatever the CLI
    reported, recorded verbatim, so any such set would silently read the statuses
    it did not happen to list as still running. What marks an end is the presence
    of ``finished_at``, or a spawn that failed — a run that never started is over
    however its status reads.
    """
    return job.get("finished_at") is not None or job.get("spawn_state") == "failed"


def _needs_lifecycle_read(job: dict[str, Any] | None, alive: bool) -> bool:
    """Whether this observation has to go and ask the lifecycle store.

    Only when the sidecar cannot already answer: there is a job, its process is
    not running, and nothing has recorded an end for it. Those are exactly the
    records `_derive` would otherwise classify from a dead pid alone. A run
    whose process is alive, or whose end is already on the record, is answered
    from the record — so the ordinary poll of a healthy run spawns nothing, and
    a run observed repeatedly after it ended asks once.
    """
    if job is None or alive:
        return False
    return not _record_is_terminal(job)


def _cache_lifecycle_end(
    job: dict[str, Any] | None, lifecycle: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Copy a lifecycle-recorded end onto the sidecar record, once.

    The sidecar is a cache of the end, not a second opinion about it: the
    lifecycle store and the terminal hook are the two writers, and whichever
    gets there first is what the record then says. Writing it back is what keeps
    the next observation from spawning the read again, and what keeps two
    observations of one unchanged run from answering differently.

    A failed write is not an error here — the record is a cache, so the next
    observation simply asks again — and the in-memory record is returned either
    way so this call is what the caller classifies.
    """
    if job is None or lifecycle is None or not lifecycle.get("terminal"):
        return job
    ended = lifecycle.get("ended_at")
    updated = {
        **job,
        "status": lifecycle.get("status", job.get("status")),
        "finished_at": _iso_from_epoch(ended) or _now_iso(),
        "reason_code": lifecycle.get("reason_code"),
        "terminal_source": "lifecycle",
    }
    try:
        _write_job(updated)
    except OSError:
        pass
    return updated


def _iso_from_epoch(value: Any) -> str | None:
    """The store keeps epoch seconds; the sidecar keeps ISO-8601 strings."""
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _server_identity() -> dict[str, str]:
    """Which implementation answered this call.

    A server imports its code once, at startup, so a caller cannot tell which
    build is answering from the file on disk: the process may predate it. The
    tool list does not help either, because two separate implementations can
    expose the same tool names and differ only in parameters and behaviour. That
    combination makes a wrong answer look authoritative — a field described from
    a newer source reads as missing rather than as unsupported, and a caller who
    trusts the description writes down a rule the running server does not
    implement.

    Reporting the version and the directory actually imported turns that from an
    inference into a readable fact. Resolved per call rather than cached at
    import so it reflects the module that is genuinely loaded.
    """
    try:
        from lionagi.version import __version__ as version
    except Exception:  # noqa: BLE001 — identity is diagnostic; never fail a status read
        version = "unknown"
    return {"version": version, "module": str(Path(__file__).resolve().parent)}


def _run_process_liveness(job: dict[str, Any] | None, pid: int | None) -> tuple[bool, str | None]:
    """Whether the process *this run* spawned is alive, and what settled it.

    A pid number is not an identity. Once the run's process exits and the OS
    hands its number to something else, a probe of that number answers about a
    stranger, and a run that ended would report as running for as long as the
    stranger lives. So where the record captured the start time of the process
    it spawned, that identity is confirmed here before liveness is reported, and
    a number now held by a different process reports this run's process as not
    alive — which is the truth about the run, and what raises
    ``possibly_orphaned``, the field that exists for a process gone with no end
    recorded.

    The second element names what was established, so a caller can tell the
    readings apart rather than infer them: ``"confirmed"``, ``"recycled"``,
    ``"gone"`` (the pid held no live process by the time its identity was read),
    ``"unreadable"`` (the identity probe errored, so nothing was established and
    the liveness probe stands), ``"not_recorded"`` (the record captured no start
    time) and ``"unusable"`` (it captured one that no start time can be compared
    against). None when there was no live pid to identify.

    Where no identity was captured the answer is the liveness probe alone,
    exactly as before: a pid probe is all such a record ever had, and calling
    those runs orphaned would be a claim their data does not support. A probe
    that errored is treated the same way, because a failed read is not evidence
    of death.
    """
    alive = _pid_alive(pid)
    if not alive or job is None or not isinstance(pid, int):
        return alive, None

    recorded = job.get("pid_create_time")
    if recorded is None:
        return alive, "not_recorded"
    # The same three values kill() refuses: a bool is an int to isinstance and
    # arrives as a moment in 1970, a NaN loses every comparison silently, and an
    # unbounded JSON integer fails the conversion that any comparison needs.
    try:
        spawned_at = float(recorded)
        usable = not isinstance(recorded, bool) and math.isfinite(spawned_at)
    except (TypeError, ValueError, OverflowError):
        usable = False
    if not usable:
        return alive, "unusable"

    state, live_created = _process_create_time(pid)
    if state == "gone":
        return False, "gone"
    if state != "found" or live_created is None:
        return alive, "unreadable"
    if _start_time_matches(live_created, spawned_at):
        return True, "confirmed"
    return False, "recycled"


def status(run_id: str) -> dict[str, Any]:
    """Current state of *run_id*.

    ``status`` is the recorded status, verbatim and in an open vocabulary — read
    it, display it, do not match it against a list. Branch on ``terminal`` ("stop
    waiting") and ``outcome`` ("did the work come out right", null while
    ``terminal`` is false) instead; both are derived here so a caller never has to
    keep a copy of the status vocabulary. ``run`` is the raw CLI manifest. Its
    ``status`` is not advisory in the sense of being unreliable — for a run that
    reaches its own teardown, the manifest is rewritten with the terminal status
    and an ``ended_at``, and that write happens after the CLI has finalized the
    run in the StateDB, so a manifest that says a run ended is telling the truth.
    What it cannot do is say a run ended when the run's own process did not live
    to write it: a killed or crashed run leaves a manifest still reading
    ``running`` forever. It is one-directional evidence, so read ``status`` here,
    not ``run["status"]``.
    ``alive`` is about the process this run spawned, not about whatever holds its
    pid number now: where the record carries the start time captured at spawn, a
    number the OS has handed on reports as not alive. ``pid_identity`` says how
    that was settled — ``"confirmed"``, ``"recycled"``, ``"gone"``,
    ``"unreadable"``, ``"not_recorded"``, ``"unusable"``, or null when there was
    no live pid to identify — so a caller can tell a process that vanished from a
    number that now belongs to someone else, which are different things to do
    next. A record written without a start time is answered by the pid probe
    alone, as it always was.
    ``possibly_orphaned`` flags a run whose process is gone with no end recorded;
    it is advisory and never makes the run terminal.
    ``notify_delivery`` reports whether the terminal notice was delivered.
    ``server`` identifies the implementation that answered, so a caller can tell
    which build it is talking to rather than inferring it from behaviour.

    ``known`` says whether a usable record was obtained, and ``record_state`` says
    what was read to answer that: ``"ok"``, or ``"absent"``, ``"unreadable"`` or
    ``"wrong_shape"`` when it was not. Only ``"absent"`` means the run is unknown.
    A record whose bytes cannot be read, or that parses to something other than an
    object, is a file sitting on disk, and reporting it as an unknown run tells an
    operator to stop looking for the run it describes.
    """
    job, record_state = _read_job_state(run_id)
    manifest = _read_run_manifest(run_id)
    pid = job.get("pid") if job else None
    alive, pid_identity = _run_process_liveness(job, pid)

    lifecycle = None
    if _needs_lifecycle_read(job, alive):
        lifecycle = _read_lifecycle(run_id)
        job = _cache_lifecycle_end(job, lifecycle)

    derived = _derive(job, alive, lifecycle)

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
        "alive": alive,
        "pid_identity": pid_identity,
        "pid": pid,
        "submitted_at": (job or {}).get("submitted_at"),
        "finished_at": (job or {}).get("finished_at"),
        "notify_delivery": (job or {}).get("notify_delivery"),
        "run": manifest,
        "log_tail": _tail((job or {}).get("log")),
        "known": job is not None,
        "record_state": record_state,
        "server": _server_identity(),
    }


# What to tell a caller that asked about a run whose record could not be used.
# One message per way the read can fail, because "no such job" is true of exactly
# one of them and sends an operator away from a file that is on disk.
_NO_RECORD_ERROR = {
    "absent": "no such job",
    "unreadable": "the record for this job is on disk and could not be read or parsed",
    "wrong_shape": "the record for this job holds valid JSON that is not an object",
}


def output(run_id: str, tail_chars: int = 20000) -> dict[str, Any]:
    """Terminal output of *run_id*: the console (an agent's final response prints
    here) plus any persisted artifacts.

    ``record_state`` carries what was read, the same way ``status`` reports it, and
    ``error`` names that state rather than reporting every failed read as an
    unknown run.
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
    return {
        "run_id": run_id,
        "known": True,
        "record_state": record_state,
        "status": st["status"],
        "terminal": st["terminal"],
        "outcome": st["outcome"],
        "reason_code": st["reason_code"],
        "console": _tail(job.get("log"), limit=tail_chars),
        "artifacts": _list_artifacts(run_id),
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


def _mark_killed(job: dict[str, Any]) -> None:
    """Record the kill on the job record.

    A record that already carries an end keeps it. The run really did finish
    the way it says, and what was signalled here is work that outlived that end
    — overwriting ``completed`` with ``killed`` would replace how the run came
    out with how its stragglers were cleaned up.
    """
    if _record_is_terminal(job):
        job["group_reaped_at"] = _now_iso()
    else:
        job["status"] = "killed"
        job["finished_at"] = _now_iso()
    _write_job(job)


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
    _mark_killed(job)
    return _kill_result(
        run_id, killed=True, reason=None, reason_code=reason_code, pid=pid, pgid=pgid
    )


def _signal_leader_group(
    run_id: str, job: dict[str, Any], pid: int, pgid: int, sig: int, observed_at: float
) -> dict[str, Any]:
    """Signal *pgid*, once the confirmed leader at *pid* is shown to be in it.

    The caller has established that *pid* is the process this run spawned, and
    *observed_at* is the start time it read to establish it. What is still open is
    whether the *pgid* on the record is that process's group: the two numbers are
    stored separately, and a record whose pgid is wrong would otherwise direct a
    signal at an unrelated group. Read the leader's group and require equality. If
    they differ, or the group cannot be read, refuse — with different codes,
    because a mismatch is a settled fact about the record while an unreadable
    group is a probe that may answer on a later call.

    Then ask the leader itself. Every process a run spawns carries the run id in
    its environment, so a leader that reads back a *different* run's id says the
    record does not describe it, whatever its numbers matched — the same evidence
    the group route acts on when the leader is gone, read here from the one
    process that has already been identified rather than from the group at large.
    The group's other rules stay where they are: they answer a different question,
    which member of a group can speak for it, and that question is settled here.

    The marker only ever withholds a signal. An absent or unreadable one leaves
    the decision exactly where the numbers left it, because those two arrive
    identically — a process whose environment is not disclosed reads as carrying
    no marker — and requiring one to permit a signal would turn every process
    that cannot be read into a job that can never be reaped.

    Group and marker are read by pid, after the start time that identified the pid
    was read, so neither is bound to that identification on its own. A run's leader
    leads its own group, which means the number it holds is at once its pid and its
    pgid: when it exits and the whole group drains, the OS is free to hand that one
    number to a new session leader whose group number is the same value — so the
    equality above can hold of a process this run never spawned, and the marker
    cannot make up the difference, being able to withhold a signal and never to
    permit one. So the two reads are bracketed by the start time, exactly as a
    group member's are: read again afterwards and required to equal *observed_at*
    exactly. Exactly, not within the tolerance the record is compared under — that
    tolerance is for a value written to disk at spawn against one read now, while
    these are two reads of the same kernel value, and allowing them to drift would
    weaken the one check that tells a recycled number from the process that held
    it. A re-read that fails, or that answers with a different process, is a
    measurement that did not come off, and it refuses.
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


def _refuse_legacy_record(run_id: str, pid: int) -> dict[str, Any]:
    """Refuse a record written before a run's process identity was recorded.

    Such a record carries a pid and nothing that distinguishes it from a pid the
    OS has since handed to an unrelated process. The missing fields cannot be
    filled in after the fact — they describe the process that was spawned, and
    nothing observable now recovers when it started — and deriving a group from
    the pid at this point is exactly the step that resolves a reused pid to a
    stranger's group. So nothing is signalled.

    The refusal leads with what was read off the record — both fields absent —
    and offers the age of the record as the explanation rather than as the
    finding. What is observed is that the fields are missing; that they are
    missing because the record predates them follows from every write since
    having set them, which is a fact about this code and not a measurement of
    this file. Stating the inference alone would hand an operator a history the
    refusal never established.

    The pid rides along on the refusal, because it is the only handle an operator
    has for reaping the group by hand, and this is the last place it is reported.
    """
    return _kill_result(
        run_id,
        killed=False,
        reason=(
            f"this record carries neither a start time nor a process group, so pid {pid} "
            "cannot be distinguished from a reused one and no group was signalled; every "
            "write since those fields existed sets them, so a record missing both was "
            "written before they were captured; reap the group by hand after confirming "
            "the process is this run's"
        ),
        reason_code=KILL_LEGACY_NO_IDENTITY,
        pid=pid,
    )


def kill(run_id: str, sig: int = signal.SIGTERM) -> dict[str, Any]:
    """Signal the process group *run_id* was spawned into.

    The record carries what the pid alone cannot: when the leader started, and
    the group it was given at spawn. Those turn the two cases a bare pid
    confuses into decidable ones — a group still running after its leader
    exited, which is the case worth reaping, and a pid the OS handed to an
    unrelated process, which must never be signalled.

    Nothing is signalled that the record does not identify — a statement about
    what is decided here, not about where the signal lands. Identification and
    the signal are separate system calls and the interval between them belongs
    to the OS, so the delivered guarantee is best-effort identification followed
    by a signal, never an atomic one; the last paragraph of this contract says
    what that costs, and it is not a caveat on the sentence so much as the
    sentence's actual scope. A probe that errors is unknown, and unknown
    refuses: the refusal says which fact was missing, and a refusal with an
    accurate reason is the outcome being aimed at, not the largest possible
    number of processes stopped. That holds without exception, including for a
    record written before these fields existed — such a record cannot confirm
    anything, so it is refused and its group is left for an operator to reap by
    hand.

    What that buys, stated exactly, because the difference matters to anyone
    relying on it. Every signal is preceded by a positive identification: either
    the live leader's start time matches the record and its current group is the
    recorded one, or a live member of the recorded group carries this run's id in
    its environment. A group is never signalled because it merely looks young
    enough. Where a process can be asked which run it belongs to, an answer
    naming another run refuses on either route; where it cannot be asked, the
    silence decides nothing, since an environment that is withheld and one that
    is empty come back the same and treating either as a denial would strand
    every job whose processes cannot be read.

    What no check here establishes is who wrote the record. The fields are
    compared against the running process, so they identify a process that is
    still the one described; they cannot show that this run described it. A
    record that has been rewritten with a live stranger's numbers, by something
    already able to write into this user's job store, is refused only when that
    stranger names a different run of its own. The store's own integrity is the
    boundary that would settle it, and it is not a boundary a field comparison
    can draw.

    So the store is a trusted input, and that is a premise of this function
    rather than an oversight in it. It is also not a weakness worth engineering
    against: the store lives in the invoking user's own directory, and anything
    able to rewrite a record there can call ``killpg`` on that user's processes
    directly, without going through here. Nothing kept beside the record helps,
    since the same writer reaches it too, and an identifier held only in this
    process would break the one property the recorded identity exists to
    provide, which is that a run stays reapable after the server that spawned it
    restarts. What is claimed, then, is the guarantee relative to a record this
    run wrote: given that, no signal is sent without a positive identification.
    Provenance of the record itself is out of scope and is not implied anywhere
    in the result.

    Identification and the signal are two separate system calls,
    and there is no way to make them one: ``killpg`` takes a group number, not a
    reference to the group that was inspected, and there is no "signal this group
    only if it still holds the process I verified" operation to reach for. So in
    the window between the two, the identified group can empty and its number be
    handed to an unrelated group, which would then receive the signal. The window
    is small and closing it is not possible with process groups alone; it is
    stated here rather than papered over, because the guarantee is "never signalled
    without an identification", not "never signals the wrong group".
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

    # The record names the run it describes, and every write of one puts this
    # run's own id there. So a record found under one run that names another was
    # not written for the run being killed — copied, restored over, or edited —
    # and its numbers describe some other run's process. Unlike every probe below,
    # this costs nothing when it is wrong about a healthy record: the field is
    # written here rather than measured, so a disagreement is never a reading that
    # failed. Checked before the pid is even looked at, because no probe of a
    # number from a record that does not belong here is worth making.
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

    # First, and before any number on the record is probed or dereferenced. A
    # pid of 0 means the caller's own process group to killpg, and 1 is init;
    # a record carrying either — a placeholder, a truncated write, a test
    # double — must never reach a group signal.
    pid = job.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        return _kill_result(
            run_id, killed=False, reason="no pid on record", reason_code=KILL_NO_PID, pid=pid
        )

    # A record written before a run's process identity was captured does not carry
    # these keys at all. That is the only thing that means the record is old: a key
    # that is present and holds the wrong type says the opposite — it was written by
    # code that knows about these fields, and what it wrote is damaged. The two get
    # different answers, because one is expected and ages out while the other is
    # worth looking into.
    if "pid_create_time" not in job and "pgid" not in job:
        return _refuse_legacy_record(run_id, pid)
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
                # The leader is confirmed to be the process this run spawned, so
                # its group can be read now and required to be the recorded one.
                # Without that equality the recorded pgid is only a number that
                # passed a range check, and a damaged or hand-edited record would
                # aim the signal at whatever group holds that number. The start
                # time just read goes over with it, because that group read and
                # the environment read beside it are made by pid and are not
                # otherwise tied to this comparison; there they are bracketed by
                # it.
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
        # "gone": it exited between the liveness probe and this read. Fall
        # through — its group may well still be running.

    # The leader is gone. Its group can outlive it, and that group is what the
    # run's work is actually in, so it is reapable — but only once the group
    # itself is identified, since a pgid is a pid number and is reused like one.
    #
    # Identified from the group's own live members, never by looking at the
    # leader's pid again: the liveness probe reaps an exited child, after which
    # that pid is free for the OS to hand to an unrelated process, and a second
    # read of it would describe whoever holds it now.
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
    # Each refusal gets its own code, because they are not the same news, and each
    # reason reports what the probe saw rather than the history that would explain
    # it. A foreign marker, a conflict, an older member and a group whose members
    # were all read and carry no marker are settled and will read the same on every
    # retry; an incomplete scan is a measurement that failed and may succeed on the
    # next call, and a member whose environment would not open is such a failure —
    # the marker it withheld is one the next call may well read.
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


def list_jobs(limit: int = 50, status_filter: str | None = None) -> list[dict[str, Any]]:
    """Recent jobs, newest first (run_id sorts by timestamp).

    An entry whose record could not be used is listed with the state of that read
    in ``record_state``, the same field ``status`` reports, so a damaged record is
    visible here as a damaged record rather than as a job with no kind and an
    unknown status.
    """
    try:
        entries = sorted(config.JOBS_DIR.iterdir(), reverse=True)
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for d in entries:
        if not d.is_dir():
            continue
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
                "submitted_at": st["submitted_at"],
                "finished_at": st["finished_at"],
                "record_state": st["record_state"],
            }
        )
        if len(out) >= limit:
            break
    return out


def _wait_entry(run_id: Any) -> dict[str, Any]:
    """One observation of *run_id*, resolved through the same path ``status`` uses.

    An id that cannot be observed comes back as an entry carrying an ``error``
    rather than raising, so one bad id never costs the caller the ids beside it.
    Every entry carries the full lifecycle shape, error or not, so a caller reads
    the same keys in both cases.

    An id with no record is ``not_found``; an id whose record is present and
    unusable is ``record_unusable``, and says which way it is unusable. They are
    different news — the first is a run nobody submitted, the second is a file to
    go and look at — and calling both of them not found sends an operator away
    from the second.
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

    A bounded observation, not a subscription. It returns one entry per requested
    id, in the order they were requested, plus ``all_terminal``, ``timed_out``,
    the ids still ``pending`` and the ids ``stopped_without_end`` — never a bare
    boolean, because mixed outcomes are the normal case and collapsing them forces
    the follow-up poll this call exists to replace.

    ``max_wait`` is clamped to ``[0, WAIT_MAX_SECONDS]`` and ``poll_interval`` to
    ``[WAIT_MIN_POLL_SECONDS, WAIT_MAX_POLL_SECONDS]``; the effective values are
    echoed back beside the requested ones, so a caller can see it was clamped
    rather than infer it from the elapsed time. ``max_wait=0`` is a legal snapshot
    request: it observes once and returns.

    Expiry is not an error. A window that closes with ids still running returns
    what was learned with ``timed_out`` set, so completed ids are not discarded
    and calling again is safe. Unknown or malformed ids are per-id errors inside
    the result and never stop the other ids being observed; they are not listed
    as pending, because waiting longer cannot resolve them.

    A run whose process is gone with no end recorded meets that same criterion:
    it has stopped, and both writers of an end are past it, so the window is not
    held open for it either. Such ids are named in ``stopped_without_end`` — not
    in ``pending``, which is what is still worth waiting for, and not as a per-id
    ``error``, because observing them succeeded. Nothing about the record itself
    changes: the entry stays non-terminal with a null outcome, and a run that
    does get an end written afterwards is classified terminal by the next
    observation as it always was. ``all_terminal`` therefore stays false while any
    id is here, because a run that stopped without recording an end is not a
    completed one.

    Because such an id resolves nothing by waiting, a caller looping until
    ``all_terminal`` would otherwise re-ask as fast as it can. So a call that
    would return without having waited at all, while at least one id is
    ``stopped_without_end``, first sleeps one poll interval — bounded by whatever
    is left of the window — and observes again. This is a floor on the call, not
    a charge added to it: a call that already waited on a running id has met it,
    and ``max_wait=0`` is untaxed by construction, having no window to spend. The
    extra observation is not wasted either, since it is exactly the interval in
    which a slow end-writer finishes. Pacing belongs here because the boundary
    can enforce it once for every client, while a documented duty to back off is
    satisfied only by the clients that read it.

    Observing does not touch the run. This function only reads: a wait that
    expires, or whose caller cancels or disconnects, leaves the durable record
    exactly as it was — cancelling an observation is not cancelling the work.
    """
    # Imported here rather than at module scope: this module is also imported by
    # the terminal hook the CLI spawns, and that path stays import-light.
    import anyio

    ordered = list(run_ids)
    eff_max = _clamp(max_wait, 0.0, WAIT_MAX_SECONDS)
    eff_poll = _clamp(poll_interval, WAIT_MIN_POLL_SECONDS, WAIT_MAX_POLL_SECONDS)
    deadline = anyio.current_time() + eff_max

    entries: list[dict[str, Any]] = []
    pending: list[str] = []
    stopped: list[str] = []
    waited = False
    while True:
        entries = [_wait_entry(rid) for rid in ordered]
        observed = [e for e in entries if e["error"] is None]
        stopped = [e["run_id"] for e in observed if e["possibly_orphaned"]]
        pending = [
            e["run_id"] for e in observed if not e["terminal"] and not e["possibly_orphaned"]
        ]
        remaining = deadline - anyio.current_time()
        if remaining <= 0:
            break
        # Nothing left worth waiting for. Return now unless the only unresolved
        # ids stopped without an end and this call has not waited at all — that
        # is the shape a loop-until-all_terminal caller repeats as fast as it can
        # ask, so the floor is spent here rather than left to every client.
        if not pending and (waited or not stopped):
            break
        waited = True
        await anyio.sleep(min(eff_poll, remaining))

    errored = any(e["error"] is not None for e in entries)
    return {
        "runs": entries,
        "all_terminal": not pending and not errored and not stopped,
        "timed_out": bool(pending),
        "pending": pending,
        "stopped_without_end": stopped,
        "max_wait": eff_max,
        "poll_interval": eff_poll,
        "requested_max_wait": max_wait,
        "requested_poll_interval": poll_interval,
    }


def mark_terminal(run_id: str, cli_status: str) -> dict[str, Any] | None:
    """Record a terminal status for *run_id* (called by the CLI notify hook).

    The CLI's terminal status string is authoritative and recorded verbatim. An
    earlier version matched it against a local set and fell through to
    ``"completed"`` on any miss, which silently turned every status the set did
    not list — ``timed_out`` (the CLI's spelling for a timeout), ``cancelled``,
    ``aborted``, ``completed_empty`` — into a false success. The hook fires only
    on a genuine terminal, so the incoming status is trusted as-is and
    ``finished_at`` marks the record terminal.
    """
    job = _read_job(run_id)
    if job is None:
        return None
    job["status"] = cli_status
    job["cli_status"] = cli_status
    job["finished_at"] = _now_iso()
    _write_job(job)
    return job


def record_notify_delivery(run_id: str, outcome: dict[str, Any]) -> None:
    """Record whether the terminal notice was delivered (called by the notify hook).

    Surfaced by ``status`` so a completion notice that failed to send is visible
    rather than silently lost — the detached-spawn pattern relies on that signal.
    """
    job = _read_job(run_id)
    if job is None:
        return
    job["notify_delivery"] = outcome
    _write_job(job)
