# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The lifecycle read: a run stopped by `li kill` has to read as terminal.

The kill path writes the StateDB row and signals the process, but writes
neither the MCP job record nor the CLI run manifest -- so before this seam
existed, a killed run was indistinguishable from an orphan (a dead pid with
no recorded end) and `job.wait` sat on it for its whole window.

The first test here is end to end on purpose: a real submit, a real child
that persists a real session row, a real `li kill`, and a real `jobs.wait`.
A unit test over `_derive` with a hand-built record would only assert its
own fixture -- the defect lived in the gap between the writers, not in the
classifier.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from lionagi.mcp import config, jobs

# A stand-in for the installed console script. Named `li` because the kill
# path verifies a pid's identity from its command line before signalling it,
# and a shebang-launched console script is `<python> <.../li>`.
#
# It forwards to the real CLI for everything except the run itself: `agent`
# becomes a child that persists a session row through the production path and
# then stays alive, which is what a run being killed looks like without needing
# a model behind it.
_LI_SHIM = """
import sys

if len(sys.argv) > 1 and sys.argv[1] != "agent":
    from lionagi.cli.main import main

    sys.exit(main(sys.argv[1:]))

import time

import anyio

from lionagi import Branch
from lionagi.cli._runs import allocate_run, setup_agent_persist


async def _main():
    allocate_run()
    live = await setup_agent_persist(Branch(), agent_name="lifecycle-e2e")
    print("SESSION " + live["session_id"], flush=True)
    while True:
        time.sleep(0.05)


anyio.run(_main)
"""


def _reap_submitted_jobs(run_ids: list[str], timeout: float = 10.0) -> list[str]:
    """Stop any submitted process group still alive and return the leaked run ids."""
    leaked: list[str] = []
    failures: list[str] = []

    for run_id in run_ids:
        record = jobs._read_job(run_id)
        if not isinstance(record, dict):
            failures.append(f"{run_id}: job record is unavailable")
            continue
        pid = record.get("pid")
        leader_alive = isinstance(pid, int) and jobs._pid_alive(pid)

        try:
            term = jobs.kill(run_id)
        except Exception as exc:  # noqa: BLE001 — teardown must continue to the next run
            failures.append(f"{run_id}: SIGTERM cleanup raised {type(exc).__name__}: {exc}")
            term = {"killed": False}

        if leader_alive or term["killed"]:
            leaked.append(run_id)

        if isinstance(pid, int):
            deadline = time.monotonic() + timeout
            while jobs._pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.05)

        try:
            # Re-check the process group after its leader exits. A detached
            # descendant can outlive that leader and still belongs to this run.
            force = jobs.kill(run_id, sig=signal.SIGKILL)
        except Exception as exc:  # noqa: BLE001 — report every cleanup failure together
            failures.append(f"{run_id}: SIGKILL cleanup raised {type(exc).__name__}: {exc}")
            continue

        if force["killed"] and run_id not in leaked:
            leaked.append(run_id)
        if isinstance(pid, int):
            deadline = time.monotonic() + timeout
            while jobs._pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            if jobs._pid_alive(pid):
                failures.append(
                    f"{run_id}: pid {pid} remained alive after SIGTERM and SIGKILL cleanup"
                )

    if failures:
        raise AssertionError("MCP job teardown failed:\n" + "\n".join(failures))
    return leaked


@contextmanager
def _submitted_job_guard():
    """Track submissions for one test, reap leftovers, and make a leak visible."""
    submitted_run_ids: list[str] = []
    submit = jobs.submit

    def tracked_submit(*args, **kwargs):
        handle = submit(*args, **kwargs)
        submitted_run_ids.append(handle["run_id"])
        return handle

    try:
        yield tracked_submit
    finally:
        leaked = _reap_submitted_jobs(submitted_run_ids)
        if leaked:
            pytest.fail(
                "submitted MCP job(s) were still alive at test teardown: " + ", ".join(leaked)
            )


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A whole lionagi home of our own, for this process and every child."""
    with _submitted_job_guard() as tracked_submit:
        monkeypatch.setattr(jobs, "submit", tracked_submit)
        monkeypatch.setenv("LIONAGI_HOME", str(tmp_path))
        # Children are launched by absolute script path, so their `sys.path[0]` is
        # the directory that script sits in, and this checkout is not on it. They
        # then import whichever `lionagi` the interpreter's environment resolves,
        # which is the same one only when the installed distribution happens to
        # point here. Develop in a second checkout — a worktree, say — and it points
        # at the first, so these end-to-end tests exercise a CLI that is not the one
        # being changed, and do it silently.
        #
        # To see the difference, run any script by absolute path with and without
        # this variable and print `lionagi.__file__`.
        monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2]))
        monkeypatch.delenv("LIONAGI_SESSION_ID", raising=False)
        monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "mcp" / "jobs")
        monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
        shim = tmp_path / "li"
        shim.write_text(_LI_SHIM)
        monkeypatch.setattr(config, "li_command", lambda: [sys.executable, str(shim)])
        yield tmp_path


def _await_session_id(run_id: str, timeout: float = 90.0) -> str:
    """Read the session id the child prints once its row is persisted."""
    deadline = time.monotonic() + timeout
    log = config.job_dir(run_id) / "console.log"
    while time.monotonic() < deadline:
        if log.exists():
            for line in log.read_text(errors="replace").splitlines():
                if line.startswith("SESSION "):
                    return line.split(" ", 1)[1].strip()
        time.sleep(0.2)
    tail = log.read_text(errors="replace")[-2000:] if log.exists() else "<no console log>"
    raise AssertionError(f"child never persisted a session row. console:\n{tail}")


def _li(home_dir: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 — the test's own shim, no shell
        [sys.executable, str(home_dir / "li"), *argv],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def _li_kill(home_dir: Path, session_id: str, child_pid: int) -> subprocess.CompletedProcess:
    """Run `li kill` while reaping the submitted child, as a server would.

    The child is spawned detached but is still this process's child, so once it
    exits on SIGTERM it lingers as a zombie until someone waits on it — and a
    zombie has no readable command line, which `li kill` reads back to confirm
    it is not escalating SIGKILL onto a recycled pid. A live server polls
    ``status()`` throughout a run and reaps there; this loop stands in for that
    poll so the kill sees the process actually go away.
    """
    proc = subprocess.Popen(  # noqa: S603 — the test's own shim, no shell
        [sys.executable, str(home_dir / "li"), "kill", session_id],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    while proc.poll() is None:
        jobs._pid_alive(child_pid)  # reaps the child once it exits
        time.sleep(0.05)
    out, err = proc.communicate()
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


@pytest.mark.slow
def test_submitted_job_cleanup_reaps_a_live_stub(home):
    pid: int | None = None
    with pytest.raises(pytest.fail.Exception, match="still alive at test teardown"):
        with _submitted_job_guard() as submit:
            handle = submit("agent", [], prompt="stay up")
            pid = jobs._read_job(handle["run_id"])["pid"]
            assert jobs._pid_alive(pid)

    assert pid is not None
    assert not jobs._pid_alive(pid)


# --- end to end ----------------------------------------------------------------


@pytest.mark.slow
async def test_a_killed_run_reads_terminal_end_to_end(home):
    """Submit, kill by the path a person uses, and stop waiting.

    Nothing is stubbed between the submit and the verdict: `li kill` writes only
    the StateDB row, and that has to be enough for `wait` to return.
    """
    handle = jobs.submit("agent", [], prompt="stay up")
    run_id = handle["run_id"]
    session_id = _await_session_id(run_id)

    killed = _li_kill(home, session_id, jobs._read_job(run_id)["pid"])
    assert killed.returncode == 0, killed.stderr

    started = time.monotonic()
    result = await jobs.wait([run_id], max_wait=60.0, poll_interval=0.5)
    elapsed = time.monotonic() - started

    entry = result["runs"][0]
    assert entry["terminal"] is True, entry
    assert entry["outcome"] == "cancelled"
    assert entry["status"] == "cancelled"
    assert entry["reason_code"] in (
        "run.cancelled.manual_kill",
        "run.cancelled.force_kill",
    )
    assert result["all_terminal"] is True
    assert result["timed_out"] is False
    # "promptly" is the whole point: the defect was waiting out the window.
    assert elapsed < 30.0


@pytest.mark.slow
async def test_the_lifecycle_end_is_cached_onto_the_job_record(home):
    """The second observation of a killed run answers from the record.

    The sidecar becomes a cache of the end, so a caller polling a finished run
    does not spawn a CLI read per poll — and, more importantly, two observations
    of one unchanged run cannot answer differently.
    """
    run_id = jobs.submit("agent", [], prompt="stay up")["run_id"]
    session_id = _await_session_id(run_id)
    assert _li_kill(home, session_id, jobs._read_job(run_id)["pid"]).returncode == 0

    first = await jobs.wait([run_id], max_wait=60.0, poll_interval=0.5)
    assert first["runs"][0]["terminal"] is True

    record = jobs._read_job(run_id)
    assert record["finished_at"] is not None
    assert record["status"] == "cancelled"

    calls: list[str] = []

    def _must_not_be_called(rid: str) -> None:
        calls.append(rid)
        raise AssertionError("the cached end should have answered this")

    original = jobs._read_lifecycle
    jobs._read_lifecycle = _must_not_be_called
    try:
        second = jobs.status(run_id)
    finally:
        jobs._read_lifecycle = original

    assert calls == []
    assert second["terminal"] is True
    assert second["outcome"] == "cancelled"
    assert second["reason_code"] == first["runs"][0]["reason_code"]


@pytest.mark.slow
async def test_a_run_that_completes_normally_still_reads_succeeded(home):
    """The ordinary path is unchanged with the new read in place.

    The terminal hook records the end first, so nothing is asked of the
    lifecycle store at all — and the answer is a success, not a cancellation.
    """
    run_id = jobs.submit("agent", [], prompt="stay up")["run_id"]
    session_id = _await_session_id(run_id)

    # What the notify hook does on a clean finish, then stop the child.
    jobs.mark_terminal(run_id, "completed")
    assert _li_kill(home, session_id, jobs._read_job(run_id)["pid"]).returncode == 0

    result = await jobs.wait([run_id], max_wait=30.0, poll_interval=0.5)
    entry = result["runs"][0]
    assert entry["terminal"] is True
    assert entry["outcome"] == "succeeded"
    assert entry["status"] == "completed"


# --- the windows where a recorded end does not exist ---------------------------


@pytest.mark.slow
async def test_a_kill_in_the_pre_spawn_window_does_not_terminalise(home):
    """Before the child persists anything, there is no row to have cancelled.

    A run recorded as preparing, with no pid, must stay non-terminal: the
    lifecycle store answers definitively that it knows of no session, and
    "no record" is not "ended".
    """
    run_id = jobs.new_run_id()
    jobs._write_job(
        {
            "run_id": run_id,
            "pid": None,
            "kind": "agent",
            "label": None,
            "status": "running",
            "spawn_state": "preparing",
            "submitted_at": "2026-07-25T00:00:00+00:00",
            "finished_at": None,
            "log": None,
        }
    )

    # The store is real and readable; it simply has nothing under this run id.
    assert _li(home, "lifecycle", run_id, "--machine").returncode == 0

    st = jobs.status(run_id)
    assert st["terminal"] is False
    assert st["outcome"] is None
    assert st["spawn_state"] == "preparing"


@pytest.mark.slow
async def test_a_run_whose_pid_was_already_gone_still_reads_the_cancellation(home):
    """The kill signals a pid that is not there, and records the row anyway.

    Liveness cannot answer this: the process was gone before the kill. What
    makes it terminal is the recorded cancellation, not the dead pid.
    """
    run_id = jobs.submit("agent", [], prompt="stay up")["run_id"]
    session_id = _await_session_id(run_id)

    # Stop the child ourselves, so the kill finds nothing to signal.
    record = jobs._read_job(run_id)
    assert _li_kill(home, session_id, record["pid"]).returncode == 0
    deadline = time.monotonic() + 30.0
    while jobs._pid_alive(record["pid"]) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not jobs._pid_alive(record["pid"])

    st = jobs.status(run_id)
    assert st["alive"] is False
    assert st["terminal"] is True
    assert st["outcome"] == "cancelled"
    assert st["possibly_orphaned"] is False


# --- degradation ---------------------------------------------------------------


def test_a_lifecycle_read_that_fails_leaves_the_run_where_it_was(home, monkeypatch):
    """A read that cannot be made concludes nothing.

    The command is replaced by one that exits non-zero with no envelope. The
    run must come back exactly as it did before this seam existed — orphaned
    and not terminal — never as a terminal it never earned.
    """
    monkeypatch.setattr(config, "li_command", lambda: [sys.executable, "-c", "raise SystemExit(3)"])
    run_id = jobs.new_run_id()
    jobs._write_job(
        {
            "run_id": run_id,
            "pid": 999_999,
            "kind": "agent",
            "label": None,
            "status": "running",
            "spawn_state": "started",
            "submitted_at": "2026-07-25T00:00:00+00:00",
            "finished_at": None,
            "log": None,
        }
    )
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)

    st = jobs.status(run_id)
    # The read established nothing, so nothing it could have said is on the
    # record: the end this run gets is the observer's own, attributed to it and
    # carrying no status, reason or time from a store that never answered.
    assert st["status"] == "exited"
    assert st["outcome"] == jobs.OUTCOME_INDETERMINATE
    assert st["reason_code"] == jobs.LOST_REASON
    assert st["terminal_source"] == jobs.TERMINAL_SOURCE_ORPHAN_REAPER
    assert st["possibly_orphaned"] is False


def test_a_lifecycle_read_that_times_out_leaves_the_run_where_it_was(home, monkeypatch):
    """A command that never answers is a read that was not made."""
    monkeypatch.setattr(jobs, "LIFECYCLE_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(
        config, "li_command", lambda: [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    run_id = jobs.new_run_id()
    jobs._write_job(
        {
            "run_id": run_id,
            "pid": 999_999,
            "kind": "agent",
            "label": None,
            "status": "running",
            "spawn_state": "started",
            "submitted_at": "2026-07-25T00:00:00+00:00",
            "finished_at": None,
            "log": None,
        }
    )
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)

    st = jobs.status(run_id)
    assert st["outcome"] == jobs.OUTCOME_INDETERMINATE
    assert st["terminal_source"] == jobs.TERMINAL_SOURCE_ORPHAN_REAPER, (
        "a read that learned nothing must not be credited with the end"
    )


def test_an_unavailable_lifecycle_answer_is_not_read_as_no_record(home, monkeypatch):
    """ "The store could not be read" must not arrive as "the run never ran".

    The command answers with a well-formed envelope whose lifecycle value is
    unavailable. That is a refusal to state a fact, and nothing may be
    concluded from it.
    """
    envelope = {
        "ok": True,
        "contract_version": 1,
        "data": {
            "run_id": "x",
            "lifecycle": {
                "available": False,
                "value": None,
                "reason_code": "unreadable",
                "detail": "disk on fire",
            },
        },
        "error": None,
    }
    monkeypatch.setattr(
        config,
        "li_command",
        lambda: [sys.executable, "-c", f"print({json.dumps(json.dumps(envelope))})"],
    )
    run_id = jobs.new_run_id()
    jobs._write_job(
        {
            "run_id": run_id,
            "pid": 999_999,
            "kind": "agent",
            "label": None,
            "status": "running",
            "spawn_state": "started",
            "submitted_at": "2026-07-25T00:00:00+00:00",
            "finished_at": None,
            "log": None,
        }
    )
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)

    assert jobs._read_lifecycle(run_id) is None
    st = jobs.status(run_id)
    assert st["outcome"] == jobs.OUTCOME_INDETERMINATE
    assert st["terminal_source"] == jobs.TERMINAL_SOURCE_ORPHAN_REAPER, (
        "a read that learned nothing must not be credited with the end"
    )


def test_output_that_defeats_the_parser_is_a_read_that_learned_nothing(home, monkeypatch):
    """The reader promises None for any reason at all, and this is the reason a
    list of exception types would have missed.

    The bytes are well under the size limit and are not malformed in a way the
    parser reports: they exhaust the decoder's stack instead, so a guard naming
    parse errors does not cover them. What arrives here is another program's
    stdout, so the parse is the one surface in this function whose input is
    genuinely unconstrained, and a caller polling a run cannot be handed an
    exception in place of "learned nothing".
    """
    monkeypatch.setattr(
        config,
        "li_command",
        lambda: [sys.executable, "-c", "print('[' * 10000)"],
    )
    run_id = jobs.new_run_id()
    jobs._write_job(
        {
            "run_id": run_id,
            "pid": 999_999,
            "kind": "agent",
            "label": None,
            "status": "running",
            "spawn_state": "started",
            "submitted_at": "2026-07-25T00:00:00+00:00",
            "finished_at": None,
            "log": None,
        }
    )
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)

    assert jobs._read_lifecycle(run_id) is None
    st = jobs.status(run_id)
    assert st["outcome"] == jobs.OUTCOME_INDETERMINATE
    assert st["terminal_source"] == jobs.TERMINAL_SOURCE_ORPHAN_REAPER, (
        "a read that learned nothing must not be credited with the end"
    )


def test_a_spawn_failure_nobody_anticipated_is_also_a_read_that_learned_nothing(home, monkeypatch):
    """The other half of the same promise, checked the same way.

    Spawning is the guard whose failures are hardest to enumerate, because they
    come from the operating system rather than from this package. The failure
    injected here means nothing on purpose: what is being checked is that the
    reader treats a command it could not run as a fact it did not learn, rather
    than recognising a particular way of not running it.
    """
    run_id = jobs.new_run_id()
    jobs._write_job(
        {
            "run_id": run_id,
            "pid": 999_999,
            "kind": "agent",
            "label": None,
            "status": "running",
            "spawn_state": "started",
            "submitted_at": "2026-07-25T00:00:00+00:00",
            "finished_at": None,
            "log": None,
        }
    )
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)

    class UnanticipatedSpawnFailure(Exception):
        pass

    def refusing_run(*a, **kw):
        raise UnanticipatedSpawnFailure("nothing in the reader has heard of this")

    monkeypatch.setattr(subprocess, "run", refusing_run)

    assert jobs._read_lifecycle(run_id) is None
    st = jobs.status(run_id)
    assert st["outcome"] == jobs.OUTCOME_INDETERMINATE
    assert st["terminal_source"] == jobs.TERMINAL_SOURCE_ORPHAN_REAPER, (
        "a read that learned nothing must not be credited with the end"
    )


def test_a_healthy_running_job_is_never_asked_about(home, monkeypatch):
    """The read is only made where the record cannot already answer.

    A poll of a live run must not spawn a CLI process — a `wait` over several
    running ids polls every second, and paying a subprocess per id per poll
    would be a real cost for an answer the record already has.
    """
    # Both process probes, because the reader asks the pid two questions and a
    # double that answers only "is it alive" describes a zombie, which is not.
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_process_create_time", lambda pid: ("found", 1_700_000_000.0))
    monkeypatch.setattr(
        jobs,
        "_read_lifecycle",
        lambda run_id: pytest.fail("a live run must be answered from the record"),
    )
    run_id = jobs.new_run_id()
    jobs._write_job(
        {
            "run_id": run_id,
            "pid": 4242,
            "kind": "agent",
            "label": None,
            "status": "running",
            "spawn_state": "started",
            "submitted_at": "2026-07-25T00:00:00+00:00",
            "finished_at": None,
            "log": None,
        }
    )

    assert jobs.status(run_id)["status"] == "running"


# --- which store the read opens -------------------------------------------------


async def test_a_run_in_a_configured_store_is_found_there(home, monkeypatch):
    """`LIONAGI_STATE_DB_URL` moves the store, and the read has to follow it.

    The reader opens whatever `StateDB()` resolves, which is the configured URL
    when one is set. A precondition asked of the default path instead is asking
    about a file that need not be involved at all: with the store configured
    elsewhere the default is absent, so every run in the configured store reads
    back as `unavailable`, and a caller that cannot distinguish "no store" from
    "no such run" gets neither. The run below exists and is finished; the only
    reason it could be missed is that the answer came from the wrong file.
    """
    from lionagi.state.db import StateDB

    configured = home / "elsewhere" / "configured.db"
    configured.parent.mkdir(parents=True)
    run_id = "20260725T120000-c0ffee"

    async with StateDB(configured) as db:
        progression_id = uuid.uuid4().hex
        await db.create_progression(progression_id)
        await db.create_session(
            {
                "id": uuid.uuid4().hex[:12],
                "progression_id": progression_id,
                "run_id": run_id,
                "status": "completed",
                "invocation_kind": "agent",
                "started_at": time.time(),
                "ended_at": time.time(),
            }
        )

    assert not (home / "state.db").exists(), "the default store must stay absent"
    monkeypatch.setenv("LIONAGI_STATE_DB_URL", str(configured))

    result = _li(home, "lifecycle", run_id, "--machine")
    assert result.returncode == 0, result.stderr
    envelope = json.loads(result.stdout)

    lifecycle = envelope["data"]["lifecycle"]
    assert lifecycle["available"] is True, lifecycle
    assert lifecycle["value"]["found"] is True, lifecycle
