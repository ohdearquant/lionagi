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


def _read_job(run_id: str) -> dict[str, Any] | None:
    p = config.job_dir(run_id) / "job.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


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
    p = config.run_manifest(run_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
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


def _tail(path: str | None, limit: int = 4000) -> str | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = p.read_text(errors="replace")
    except OSError:
        return None
    return data[-limit:] if len(data) > limit else data


def _list_artifacts(run_id: str) -> list[str]:
    adir = config.run_dir(run_id) / "artifacts"
    if not adir.exists():
        return []
    return sorted(str(p.relative_to(adir)) for p in adir.rglob("*") if p.is_file())


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


def _notify_template(run_id: str, notify_target: str | None, notify_command: str | None) -> str:
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

    # Wire the CLI's terminal hook back to the MCP server so we record a reliable
    # finished_at/status (and fire the configured delivery) even across a restart.
    options = ["--notify", _notify_template(run_id, notify_target, notify_command), *options]

    argv = [*config.li_command(), *_KIND_ARGV[kind], *options, *positionals]

    # Drop the parent harness marker so the detached child does not inherit an
    # environment that claims it is running under an interactive harness.
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    env[config.RUN_ID_ENV_VAR] = run_id

    # Only "agent" hands the instruction over in a file; flow and fanout take it
    # as a positional, so a long one has to fit in the process argument vector.
    # Checked before anything is written, because Popen raising this late would
    # leave a job recorded as "running" for a run that never started.
    _reject_oversized_argv(argv, env, kind=kind)

    d.mkdir(parents=True, exist_ok=True)
    if prompt_path is not None:
        prompt_path.write_text(prompt)

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
        "kind": kind,
        "argv": argv,
        "cwd": cwd,
        "label": label,
        "notify_command": notify_command,
        "notify_target": notify_target,
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
    latest = _read_job(run_id) or record
    latest["pid"] = proc.pid
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
    ``possibly_orphaned`` flags a run whose process is gone with no end recorded;
    it is advisory and never makes the run terminal.
    ``notify_delivery`` reports whether the terminal notice was delivered.
    ``server`` identifies the implementation that answered, so a caller can tell
    which build it is talking to rather than inferring it from behaviour.
    """
    job = _read_job(run_id)
    manifest = _read_run_manifest(run_id)
    pid = job.get("pid") if job else None
    alive = _pid_alive(pid)

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
        "pid": pid,
        "submitted_at": (job or {}).get("submitted_at"),
        "finished_at": (job or {}).get("finished_at"),
        "notify_delivery": (job or {}).get("notify_delivery"),
        "run": manifest,
        "log_tail": _tail((job or {}).get("log")),
        "known": job is not None,
        "server": _server_identity(),
    }


def output(run_id: str, tail_chars: int = 20000) -> dict[str, Any]:
    """Terminal output of *run_id*: the console (an agent's final response prints
    here) plus any persisted artifacts."""
    job = _read_job(run_id)
    if job is None:
        return {"run_id": run_id, "known": False, "error": "no such job"}
    st = status(run_id)
    return {
        "run_id": run_id,
        "known": True,
        "status": st["status"],
        "terminal": st["terminal"],
        "outcome": st["outcome"],
        "reason_code": st["reason_code"],
        "console": _tail(job.get("log"), limit=tail_chars),
        "artifacts": _list_artifacts(run_id),
        "run_dir": str(config.run_dir(run_id)),
    }


def kill(run_id: str, sig: int = signal.SIGTERM) -> dict[str, Any]:
    """Signal the whole process group of *run_id*."""
    job = _read_job(run_id)
    if job is None:
        return {"run_id": run_id, "killed": False, "reason": "no such job"}
    # Before the pid is read, let alone signalled. A record that already ended
    # keeps its pid, and the operating system reuses pid numbers: probing that
    # number can find an unrelated live process, and signalling it would kill a
    # stranger's process group and report success. The write below would also
    # relabel a run that completed or was cancelled as "killed".
    if _record_is_terminal(job):
        recorded = job.get("status", "unknown")
        return {"run_id": run_id, "killed": False, "reason": f"already ended as {recorded}"}
    pid = job.get("pid")
    if not pid or pid <= 1:  # never signal pgid 0/1 (self/init)
        return {"run_id": run_id, "killed": False, "reason": "no pid on record"}
    if not _pid_alive(pid):
        return {"run_id": run_id, "killed": False, "reason": "already exited"}

    reason: str | None = None
    try:
        os.killpg(os.getpgid(pid), sig)
        killed = True
    except ProcessLookupError:
        killed, reason = False, "process gone"
    except PermissionError as e:
        killed, reason = False, f"permission denied: {e}"

    if killed:
        job["status"] = "killed"
        job["finished_at"] = _now_iso()
        _write_job(job)
    return {"run_id": run_id, "killed": killed, "reason": reason, "pid": pid}


def list_jobs(limit: int = 50, status_filter: str | None = None) -> list[dict[str, Any]]:
    """Recent jobs, newest first (run_id sorts by timestamp)."""
    if not config.JOBS_DIR.exists():
        return []
    out: list[dict[str, Any]] = []
    for d in sorted(config.JOBS_DIR.iterdir(), reverse=True):
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
        entry["error"] = {"kind": "not_found", "message": f"no job with id {run_id}"}
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
    id, in the order they were requested, plus ``all_terminal``, ``timed_out`` and
    the ids still ``pending`` — never a bare boolean, because mixed outcomes are
    the normal case and collapsing them forces the follow-up poll this call exists
    to replace.

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
    while True:
        entries = [_wait_entry(rid) for rid in ordered]
        pending = [e["run_id"] for e in entries if e["error"] is None and not e["terminal"]]
        if not pending:
            break
        remaining = deadline - anyio.current_time()
        if remaining <= 0:
            break
        await anyio.sleep(min(eff_poll, remaining))

    errored = any(e["error"] is not None for e in entries)
    return {
        "runs": entries,
        "all_terminal": not pending and not errored,
        "timed_out": bool(pending),
        "pending": pending,
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
