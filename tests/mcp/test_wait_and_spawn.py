# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Lifecycle-contract tests: bounded observation and the spawn-failure record.

These cover the two places where a wrong answer is silent rather than loud — a
run classified as finished when it is not, and a run that can never be finished
because nothing recorded that its spawn failed.
"""

from __future__ import annotations

import pytest

from lionagi.mcp import config, jobs


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Point job/run state at a tmp dir so tests never touch the real ~/.lionagi."""
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "li_command", lambda: ["echo"])
    return tmp_path


class _FakeProc:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid


def _record(rid: str, **fields) -> None:
    base = {
        "run_id": rid,
        "pid": None,
        "kind": "agent",
        "label": None,
        "status": "running",
        "spawn_state": "started",
        "submitted_at": "2026-07-25T00:00:00+00:00",
        "finished_at": None,
        "log": None,
    }
    base.update(fields)
    jobs._write_job(base)


# --- terminal / outcome derivation ---------------------------------------------


@pytest.mark.parametrize(
    ("cli_status", "outcome", "reason_code"),
    [
        ("completed", "succeeded", None),
        ("completed_empty", "failed", "no_artifacts"),
        ("timed_out", "failed", None),
        ("cancelled", "cancelled", None),
        ("aborted", "cancelled", None),
        ("a_status_this_build_never_heard_of", "failed", None),
    ],
)
def test_terminal_outcome_from_recorded_end(sandbox, cli_status, outcome, reason_code):
    """A recorded end makes a run terminal; the status itself only picks outcome.

    ``completed_empty`` is the case the two fields exist for: it ended, and it did
    not succeed. An unrecognised status is reported verbatim and classified as a
    failure, because a stale success list turning a timeout into a success is the
    defect this shape removes.
    """
    rid = jobs.new_run_id()
    _record(rid, status=cli_status, finished_at="2026-07-25T00:01:00+00:00")

    st = jobs.status(rid)
    assert st["status"] == cli_status  # verbatim, never re-spelled
    assert st["terminal"] is True
    assert st["outcome"] == outcome
    assert st["reason_code"] == reason_code


def test_orphan_is_not_terminal_and_has_no_outcome(sandbox, monkeypatch):
    """A process gone with no end recorded has stopped and is still not terminal."""
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    rid = jobs.new_run_id()
    _record(rid, pid=999_999)

    st = jobs.status(rid)
    assert st["status"] == "exited"
    assert st["terminal"] is False
    assert st["outcome"] is None  # null whenever terminal is false, not just while running
    assert st["possibly_orphaned"] is True


def test_preparing_record_is_not_a_spawn_failure(sandbox, monkeypatch):
    """A record written before the pid is attached says nothing about the spawn.

    A healthy child has no pid for the window between the pre-spawn write and the
    write that attaches it, so nothing may read that absence as a failure.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    rid = jobs.new_run_id()
    _record(rid, pid=None, spawn_state="preparing")

    st = jobs.status(rid)
    assert st["terminal"] is False
    assert st["outcome"] is None
    assert st["possibly_orphaned"] is False
    assert st["spawn_state"] == "preparing"


def test_running_job_carries_null_outcome(sandbox, monkeypatch):
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    rid = jobs.new_run_id()
    _record(rid, pid=4242)

    st = jobs.status(rid)
    assert (st["status"], st["terminal"], st["outcome"]) == ("running", False, None)


def test_submit_handle_and_list_rows_carry_the_derivations(sandbox, monkeypatch):
    """Every status-bearing response carries terminal and outcome, not only status."""
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *a, **k: _FakeProc())
    handle = jobs.submit("agent", [], prompt="x")
    assert handle["status"] == "running"
    assert handle["terminal"] is False
    assert handle["outcome"] is None
    assert handle["spawn_state"] == "started"

    jobs.mark_terminal(handle["run_id"], "completed_empty")
    row = jobs.list_jobs()[0]
    assert row["run_id"] == handle["run_id"]
    assert (row["terminal"], row["outcome"], row["reason_code"]) == (True, "failed", "no_artifacts")

    out = jobs.output(handle["run_id"])
    assert (out["terminal"], out["outcome"]) == (True, "failed")


# --- spawn failure --------------------------------------------------------------


def test_spawn_failure_writes_a_terminal_record(sandbox, monkeypatch):
    """A Popen that raises leaves a run nothing else can ever finish, so the
    producer that caught it records the end itself."""

    def boom(*a, **k):
        raise OSError(8, "Exec format error")

    monkeypatch.setattr(jobs.subprocess, "Popen", boom)
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)

    with pytest.raises(jobs.SpawnError) as excinfo:
        jobs.submit("agent", [], prompt="x")

    rid = excinfo.value.run_id  # the id survives the failure; the caller is not left guessing
    rec = jobs._read_job(rid)
    assert rec["spawn_state"] == "failed"
    assert rec["finished_at"] is not None
    assert "spawn failed" in rec["reason"]

    st = jobs.status(rid)
    assert st["terminal"] is True
    assert st["outcome"] == "failed"
    assert st["reason_code"] == "spawn_failed"


def test_a_spawn_refusal_that_is_not_an_errno_still_terminalises(sandbox, monkeypatch):
    """The record is marked because it was written, not because of what failed.

    A spawn can be refused for reasons that carry no errno at all — an argument
    the exec cannot represent raises ``ValueError`` — and a handler that names
    the errno family leaves exactly those runs claiming to be running forever.
    Kept separate from the caller-side refusal that stops such a value earlier,
    because with that refusal in place nothing reaches this path, and a guard
    only one test can reach is a guard that can be removed silently.
    """

    def boom(*a, **k):
        raise ValueError("embedded null byte")

    monkeypatch.setattr(jobs.subprocess, "Popen", boom)
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)

    with pytest.raises(jobs.SpawnError) as excinfo:
        jobs.submit("agent", [], prompt="x")

    st = jobs.status(excinfo.value.run_id)
    assert st["terminal"] is True
    assert st["outcome"] == "failed"
    assert st["reason_code"] == "spawn_failed"


def test_spawn_failure_terminalises_without_a_pid_rule(sandbox, monkeypatch):
    """The terminal comes from the recorded spawn failure, not from pid absence.

    Proved by stripping the recorded failure from an otherwise identical record:
    the same pid-less record must then read as non-terminal.
    """

    def boom(*a, **k):
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(jobs.subprocess, "Popen", boom)
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    with pytest.raises(jobs.SpawnError) as excinfo:
        jobs.submit("agent", [], prompt="x")
    rid = excinfo.value.run_id

    rec = jobs._read_job(rid)
    rec.update({"spawn_state": "preparing", "status": "running", "finished_at": None})
    jobs._write_job(rec)

    st = jobs.status(rid)
    assert st["pid"] is None
    assert st["terminal"] is False
    assert st["outcome"] is None


# --- bounded observation --------------------------------------------------------


async def test_wait_returns_one_entry_per_id_in_input_order(sandbox, monkeypatch):
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    ids = [jobs.new_run_id() for _ in range(3)]
    for rid in ids:
        _record(rid, status="completed", finished_at="2026-07-25T00:01:00+00:00")

    asked = [ids[2], ids[0], ids[1]]
    res = await jobs.wait(asked, max_wait=0, poll_interval=1)

    assert [e["run_id"] for e in res["runs"]] == asked
    assert all(e["terminal"] and e["outcome"] == "succeeded" for e in res["runs"])
    assert res["all_terminal"] is True
    assert res["timed_out"] is False
    assert res["pending"] == []


async def test_wait_snapshot_with_zero_max_wait(sandbox, monkeypatch):
    """max_wait=0 is a legal request for one observation, not an error."""
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    slept: list[float] = []

    async def no_sleep(seconds):
        slept.append(seconds)

    rid = jobs.new_run_id()
    _record(rid, pid=4242)

    import anyio

    monkeypatch.setattr(anyio, "sleep", no_sleep)
    res = await jobs.wait([rid], max_wait=0, poll_interval=5)

    assert slept == []  # observed once and returned
    assert res["pending"] == [rid]
    assert res["timed_out"] is True
    assert res["all_terminal"] is False
    assert res["runs"][0]["status"] == "running"


async def test_wait_expiry_keeps_what_was_learned(sandbox, monkeypatch):
    """A closed window is not an error: finished ids are still reported."""
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    done = jobs.new_run_id()
    _record(done, status="completed", finished_at="2026-07-25T00:01:00+00:00")
    busy = jobs.new_run_id()
    _record(busy, pid=4242)

    res = await jobs.wait([done, busy], max_wait=0.05, poll_interval=0.01)

    assert res["timed_out"] is True
    assert res["all_terminal"] is False
    assert res["pending"] == [busy]
    assert res["runs"][0]["terminal"] is True
    assert res["runs"][0]["outcome"] == "succeeded"
    assert res["runs"][1]["terminal"] is False


async def test_wait_reports_unknown_ids_per_entry(sandbox, monkeypatch):
    """One bad id costs the caller that id and nothing else."""
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    good = jobs.new_run_id()
    _record(good, status="completed", finished_at="2026-07-25T00:01:00+00:00")

    res = await jobs.wait([good, "no-such-run", ""], max_wait=0, poll_interval=1)

    assert res["runs"][0]["error"] is None and res["runs"][0]["terminal"] is True
    assert res["runs"][1]["error"]["kind"] == "not_found"
    assert res["runs"][2]["error"]["kind"] == "invalid_input"
    # An id that cannot be observed is not pending: waiting longer cannot resolve it.
    assert res["pending"] == []
    assert res["all_terminal"] is False
    assert res["timed_out"] is False


async def test_wait_clamps_and_echoes_the_effective_numbers(sandbox, monkeypatch):
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    rid = jobs.new_run_id()
    _record(rid, status="completed", finished_at="2026-07-25T00:01:00+00:00")

    res = await jobs.wait([rid], max_wait=10**9, poll_interval=-4)

    assert res["max_wait"] == jobs.WAIT_MAX_SECONDS
    assert res["poll_interval"] == jobs.WAIT_MIN_POLL_SECONDS
    assert res["requested_max_wait"] == 10**9
    assert res["requested_poll_interval"] == -4


async def test_wait_does_not_touch_the_run(sandbox, monkeypatch):
    """An expired wait leaves the durable record byte-for-byte as it was."""
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    rid = jobs.new_run_id()
    _record(rid, pid=4242)
    before = (config.job_dir(rid) / "job.json").read_text()

    res = await jobs.wait([rid], max_wait=0.05, poll_interval=0.01)

    assert res["timed_out"] is True
    assert (config.job_dir(rid) / "job.json").read_text() == before


async def test_wait_stops_as_soon_as_every_id_is_terminal(sandbox, monkeypatch):
    """The call returns on the transition, not on the deadline."""
    alive = {"value": True}
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: alive["value"])
    rid = jobs.new_run_id()
    _record(rid, pid=4242)

    polls = {"n": 0}
    real_status = jobs.status

    def counting_status(run_id):
        polls["n"] += 1
        if polls["n"] == 2:  # the run ends between the first and second observation
            alive["value"] = False
            jobs.mark_terminal(run_id, "completed")
        return real_status(run_id)

    monkeypatch.setattr(jobs, "status", counting_status)
    res = await jobs.wait([rid], max_wait=30, poll_interval=0.01)

    assert res["all_terminal"] is True
    assert res["timed_out"] is False
    assert res["runs"][0]["outcome"] == "succeeded"
    assert polls["n"] == 2


async def test_a_stopped_run_costs_one_poll_interval_and_no_more(sandbox, monkeypatch):
    """A run whose process is gone cannot be resolved by waiting the window out.

    Both writers of an end are past it, so the window is not held open for it —
    but returning instantly would let a caller looping until ``all_terminal``
    re-ask as fast as it can, so the boundary spends one poll interval first.
    The assertion is on the sleeps actually entered, not on how long the call
    felt: exactly one, of one interval. Against the previous behaviour the same
    ids held the window for its full 600 seconds.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    rid = jobs.new_run_id()
    _record(rid, pid=999_999)

    import anyio

    slept: list[float] = []

    async def no_sleep(seconds):
        slept.append(seconds)
        # The sleep is a no-op, so a version that keeps this id pending would
        # spin here for the whole 600s window. Fail on the fourth interval
        # instead, naming what it was still waiting for.
        if len(slept) > 3:
            raise AssertionError(f"still polling after {len(slept)} intervals on a stopped run")

    monkeypatch.setattr(anyio, "sleep", no_sleep)
    res = await jobs.wait([rid], max_wait=600, poll_interval=5)

    assert slept == [5]
    assert res["pending"] == []
    assert res["stopped_without_end"] == [rid]
    assert res["timed_out"] is False
    # Stopped is not finished: nothing recorded how this run came out.
    assert res["all_terminal"] is False
    assert res["runs"][0]["terminal"] is False
    assert res["runs"][0]["outcome"] is None
    assert res["runs"][0]["possibly_orphaned"] is True
    assert res["runs"][0]["error"] is None


async def test_wait_still_waits_for_a_running_id_beside_a_stopped_one(sandbox, monkeypatch):
    """A stopped id is dropped from the wait; the ids that can still finish keep it.

    The poll count also pins the floor as a minimum rather than a surcharge: this
    call waited on a running id, so it has already met the floor and pays nothing
    extra for the stopped one sitting beside it.
    """
    alive = {"value": True}
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: pid == 4242 and alive["value"])
    gone = jobs.new_run_id()
    _record(gone, pid=999_999)
    busy = jobs.new_run_id()
    _record(busy, pid=4242)

    polls = {"n": 0}
    real_status = jobs.status

    def counting_status(run_id):
        if run_id == busy:
            polls["n"] += 1
            if polls["n"] == 2:  # the running run ends between two observations
                alive["value"] = False
                jobs.mark_terminal(run_id, "completed")
        return real_status(run_id)

    monkeypatch.setattr(jobs, "status", counting_status)
    res = await jobs.wait([gone, busy], max_wait=30, poll_interval=0.01)

    assert polls["n"] == 2  # the wait did keep observing the running id
    assert res["pending"] == []
    assert res["stopped_without_end"] == [gone]
    assert res["timed_out"] is False
    assert res["all_terminal"] is False  # one id never recorded an end
    assert res["runs"][1]["terminal"] is True
    assert res["runs"][1]["outcome"] == "succeeded"


async def test_a_stopped_run_that_later_records_an_end_is_terminal(sandbox, monkeypatch):
    """Dropping a stopped id from the wait says nothing about the record.

    An end written afterwards by either writer classifies exactly as it always
    did, and the id is no longer reported as stopped without one.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    rid = jobs.new_run_id()
    _record(rid, pid=999_999)

    stopped = await jobs.wait([rid], max_wait=0, poll_interval=1)
    assert stopped["stopped_without_end"] == [rid]

    jobs.mark_terminal(rid, "completed")
    res = await jobs.wait([rid], max_wait=0, poll_interval=1)

    assert res["stopped_without_end"] == []
    assert res["pending"] == []
    assert res["all_terminal"] is True
    assert res["runs"][0]["terminal"] is True
    assert res["runs"][0]["outcome"] == "succeeded"
    assert res["runs"][0]["possibly_orphaned"] is False


async def test_wait_snapshot_of_a_stopped_run_is_still_a_snapshot(sandbox, monkeypatch):
    """max_wait=0 observes once and returns, whatever the ids turn out to be.

    This is also where the floor stops: a snapshot request has no window to spend,
    so the id that would otherwise buy one poll interval buys nothing here. A
    caller that asked not to wait is not made to.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    rid = jobs.new_run_id()
    _record(rid, pid=999_999)

    import anyio

    slept: list[float] = []

    async def no_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(anyio, "sleep", no_sleep)
    res = await jobs.wait([rid], max_wait=0, poll_interval=5)

    assert slept == []
    assert res["max_wait"] == 0.0
    assert res["stopped_without_end"] == [rid]


async def test_wait_does_not_hold_the_window_open_for_a_reused_pid(sandbox, monkeypatch):
    """A run whose pid now belongs to someone else has stopped, not stalled.

    wait observes through status, so a liveness answer taken from whatever holds
    the number keeps the run in ``pending`` for the whole window and leaves
    ``stopped_without_end`` empty — the caller waits out its budget on a run that
    already ended, and is told nothing about why.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_process_create_time", lambda pid: ("found", 1_700_005_000.0))
    rid = jobs.new_run_id()
    _record(rid, pid=4242, pid_create_time=1_700_000_000.0, pgid=4242)

    res = await jobs.wait([rid], max_wait=0, poll_interval=5)

    assert res["pending"] == []
    assert res["stopped_without_end"] == [rid]
    assert res["all_terminal"] is False
    assert res["runs"][0]["possibly_orphaned"] is True
    assert res["runs"][0]["terminal"] is False
    assert res["runs"][0]["error"] is None


async def test_the_floor_never_outruns_the_window(sandbox, monkeypatch):
    """The floor is bounded by what is left of the window, not by the interval.

    A caller who asked for half a second does not get five because one id stopped
    without an end. Without the bound the floor could overrun a window the caller
    chose, which would make the pacing the producer's decision rather than a
    minimum inside the caller's own budget.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    rid = jobs.new_run_id()
    _record(rid, pid=999_999)

    import anyio

    slept: list[float] = []

    async def no_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(anyio, "sleep", no_sleep)
    res = await jobs.wait([rid], max_wait=0.5, poll_interval=5)

    assert len(slept) == 1
    assert 0 < slept[0] <= 0.5
    assert res["stopped_without_end"] == [rid]


async def test_every_unresolved_id_is_named_somewhere_in_the_result(sandbox, monkeypatch):
    """No observed id may be left non-terminal and unnamed.

    A caller is required to hold a policy for every id a wait does not resolve,
    and that duty is only implementable if every such id arrives somewhere it
    can be read. This pins the invariant rather than today's categories: a
    future non-terminal state added to the classifier without being added to a
    list fails here. A written obligation cannot catch that on its own — the
    text sits unchanged while the shape it describes stops occurring, which is
    exactly how the obligation this replaces stopped covering a stopped run.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: pid == 4242)
    running = jobs.new_run_id()
    _record(running, pid=4242)
    stopped = jobs.new_run_id()
    _record(stopped, pid=999_999)
    done = jobs.new_run_id()
    _record(done, pid=999_999)
    jobs.mark_terminal(done, "completed")
    never_recorded = jobs.new_run_id()

    res = await jobs.wait([running, stopped, done, never_recorded, ""], max_wait=0, poll_interval=1)

    named = set(res["pending"]) | set(res["stopped_without_end"])
    assert not (set(res["pending"]) & set(res["stopped_without_end"]))
    for entry in res["runs"]:
        if entry["terminal"]:
            assert entry["run_id"] not in named
        elif entry["error"] is None:
            assert entry["run_id"] in named, (
                f"{entry['run_id']!r} is non-terminal, was observed without error, and is "
                "named in neither pending nor stopped_without_end -- nothing tells a "
                "caller it has a decision to make about this id"
            )
    assert running in res["pending"]
    assert stopped in res["stopped_without_end"]


# --- the argv the child is actually spawned with --------------------------------
#
# Everything above this point either mocks `jobs.submit` or reads records back, so
# nothing in it sees the command line. That is where a value stops being a value:
# the tokens are assembled here from three sources — what the caller asked for,
# what the projection renders, and what the server wires on — and only the
# assembled whole can be parsed by the parser that will read it.


@pytest.fixture
def spawned(sandbox, monkeypatch):
    """Capture the argv `submit` hands to Popen; nothing is executed."""
    seen: dict = {}

    def fake_popen(argv, **kwargs):
        seen["argv"] = list(argv)
        return _FakeProc()

    monkeypatch.setattr(jobs.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(config, "li_command", lambda: ["li"])
    return seen


def _parse(argv: list[str]):
    """Read a captured child argv with the parser that build will read it with."""
    import argparse

    from lionagi.cli.agent import add_agent_subparser

    root = argparse.ArgumentParser(prog="li")
    add_agent_subparser(root.add_subparsers(dest="command"))
    assert argv[0] == "li"
    return root.parse_args(argv[1:])


def test_a_prompt_file_stays_an_option_when_the_query_opened_a_sentinel(spawned):
    jobs.submit("agent", ["--cwd=/tmp", "--", "claude/opus"], prompt="hello")
    parsed = _parse(spawned["argv"])
    assert parsed.query == ["claude/opus"]
    assert parsed.prompt_file and parsed.prompt_file.endswith("prompt.txt")
    assert parsed.cwd == "/tmp"


def test_a_flow_prompt_goes_behind_a_sentinel_even_with_no_rendered_positional(spawned):
    jobs.submit("flow", ["--dry-run"], prompt="-- not a flag")
    argv = spawned["argv"]
    assert argv[-2:] == ["--", "-- not a flag"]


def test_a_value_that_cannot_be_an_argv_token_is_refused_before_any_run_exists(spawned):
    """A NUL in a caller string is refused where it is still the caller's mistake.

    ``execve`` takes NUL-terminated strings, so such a value is not one the
    platform can pass at all. Reaching the spawn with it produces a job record
    first and a failure second, and the caller's own input is then reported as an
    internal error against a run that exists. Refused at rendering, no run is
    minted: the assertion that matters is the empty jobs directory, not the
    message.
    """
    import asyncio

    from lionagi.mcp import dispatch

    fingerprint = asyncio.run(dispatch.request(help="agent.submit"))["schema_fingerprint"]
    answer = asyncio.run(
        dispatch.request(
            ops=[
                {
                    "op": "agent.submit",
                    "args": {"query": ["hi\0there"]},
                    "schema_fingerprint": fingerprint,
                }
            ]
        )
    )
    op = answer["ops"][0]
    assert op["ok"] is False
    assert op["error"]["kind"] == "invalid_input"
    assert "argv" not in spawned
    assert list(config.JOBS_DIR.glob("*")) == []


def test_a_switch_looking_query_reaches_the_child_as_a_positional(spawned):
    jobs.submit("agent", ["--", "--machine"], prompt="hi")
    argv = spawned["argv"]
    parsed = _parse(argv)
    assert parsed.query == ["--machine"]
    # And the scan that runs before any parsing does not see a switch either.
    from lionagi.cli import machine

    assert not machine.has_machine_flag(argv[1:])
