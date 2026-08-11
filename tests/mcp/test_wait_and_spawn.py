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


def _live_process(monkeypatch, alive=lambda pid: True) -> None:
    """Make both process probes agree that the pid holds a live process.

    A pid is asked two separate questions: whether it holds a live process at
    all, and when that process started. Answering only the first describes a
    state no operating system produces — a pid that answers ``kill -0`` and is
    absent from the process table is a process that has exited and is waiting to
    be reaped, which is the opposite of alive.
    """
    monkeypatch.setattr(jobs, "_pid_alive", alive)
    monkeypatch.setattr(jobs, "_process_create_time", lambda pid: ("found", 1_700_000_000.0))


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


def test_a_conclusively_gone_process_ends_the_run_as_lost(sandbox, monkeypatch):
    """A process positively established gone, with nothing reported, is over.

    Nothing survived that could ever write this run's end, so leaving it
    non-terminal leaves it non-terminal forever. The end is the observer's, and
    it says so: `lost` is not a failure, and the reason names why there is no
    result rather than classifying one.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    rid = jobs.new_run_id()
    _record(rid, pid=999_999)

    st = jobs.status(rid)
    assert st["terminal"] is True
    assert st["outcome"] == jobs.OUTCOME_INDETERMINATE
    assert st["reason_code"] == jobs.LOST_REASON
    assert st["possibly_orphaned"] is False
    assert st["terminal_source"] == jobs.TERMINAL_SOURCE_ORPHAN_REAPER


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
    _live_process(monkeypatch)
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
    _live_process(monkeypatch)
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
    _live_process(monkeypatch)
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
    _live_process(monkeypatch)
    rid = jobs.new_run_id()
    _record(rid, pid=4242)
    before = (config.job_dir(rid) / "job.json").read_text()

    res = await jobs.wait([rid], max_wait=0.05, poll_interval=0.01)

    assert res["timed_out"] is True
    assert (config.job_dir(rid) / "job.json").read_text() == before


async def test_wait_stops_as_soon_as_every_id_is_terminal(sandbox, monkeypatch):
    """The call returns on the transition, not on the deadline."""
    alive = {"value": True}
    # Both probes, because a live pid whose creation time cannot be matched is
    # now a run this module ends rather than one it keeps polling.
    _live_process(monkeypatch, alive=lambda pid: alive["value"])
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

    assert slept == []
    assert res["pending"] == []
    # The run was ended by the observation itself, so there is nothing left to
    # wait for and nothing to report under the compatibility field.
    assert res["stopped_without_end"] == []
    assert res["timed_out"] is False
    assert res["all_terminal"] is True
    assert res["runs"][0]["terminal"] is True
    assert res["runs"][0]["outcome"] == jobs.OUTCOME_INDETERMINATE
    assert res["runs"][0]["possibly_orphaned"] is False
    assert res["runs"][0]["error"] is None


async def test_wait_still_waits_for_a_running_id_beside_a_stopped_one(sandbox, monkeypatch):
    """A stopped id is dropped from the wait; the ids that can still finish keep it.

    The poll count also pins the floor as a minimum rather than a surcharge: this
    call waited on a running id, so it has already met the floor and pays nothing
    extra for the stopped one sitting beside it.
    """
    alive = {"value": True}
    _live_process(monkeypatch, alive=lambda pid: pid == 4242 and alive["value"])
    gone = jobs.new_run_id()
    _record(gone, pid=999_999)
    busy = jobs.new_run_id()
    _record(busy, pid=4242)
    # A run that ended badly, so the aggregate below covers all four outcomes a
    # wait can carry at once rather than three of them.
    dud = jobs.new_run_id()
    _record(dud, pid=999_998)
    jobs.mark_terminal(dud, "failed")

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
    res = await jobs.wait([gone, busy, dud], max_wait=30, poll_interval=0.01)

    assert polls["n"] == 2  # the wait did keep observing the running id
    assert res["pending"] == []
    assert res["stopped_without_end"] == []
    assert res["timed_out"] is False
    # Every id has a recorded end, so the aggregate is true — and it is the
    # per-entry outcomes, in the order they were asked for, that say the three
    # runs came out differently.
    assert res["all_terminal"] is True
    assert [r["outcome"] for r in res["runs"]] == [
        jobs.OUTCOME_INDETERMINATE,
        "succeeded",
        "failed",
    ]
    assert res["runs"][1]["terminal"] is True
    assert res["runs"][2]["terminal"] is True


async def test_a_stopped_run_that_later_records_an_end_is_terminal(sandbox, monkeypatch):
    """Dropping a stopped id from the wait says nothing about the record.

    An end written afterwards by either writer classifies exactly as it always
    did, and the id is no longer reported as stopped without one.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    rid = jobs.new_run_id()
    _record(rid, pid=999_999)

    ended = await jobs.wait([rid], max_wait=0, poll_interval=1)
    assert ended["stopped_without_end"] == []
    assert ended["runs"][0]["outcome"] == jobs.OUTCOME_INDETERMINATE

    # A hook arriving afterwards cannot replace an end that is already recorded.
    jobs.mark_terminal(rid, "completed")
    res = await jobs.wait([rid], max_wait=0, poll_interval=1)

    assert res["stopped_without_end"] == []
    assert res["pending"] == []
    assert res["all_terminal"] is True
    assert res["runs"][0]["terminal"] is True
    assert res["runs"][0]["outcome"] == jobs.OUTCOME_INDETERMINATE
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
    assert res["stopped_without_end"] == []
    assert res["runs"][0]["outcome"] == jobs.OUTCOME_INDETERMINATE


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
    assert res["stopped_without_end"] == []
    assert res["all_terminal"] is True
    assert res["runs"][0]["possibly_orphaned"] is False
    assert res["runs"][0]["terminal"] is True
    assert res["runs"][0]["outcome"] == jobs.OUTCOME_INDETERMINATE
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
    _record(rid, pid="999999")  # unaskable: stopped-looking, never conclusive

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
    _live_process(monkeypatch, alive=lambda pid: pid == 4242)
    running = jobs.new_run_id()
    _record(running, pid=4242)
    stopped = jobs.new_run_id()
    _record(stopped, pid="999999")  # unaskable: stopped-looking, never conclusive
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


# --- an unresolved spawn is not something waiting can fix -----------------------


async def test_wait_reports_an_aged_preparing_spawn_as_unresolved_not_pending(sandbox, monkeypatch):
    """A spawn that never resolved leaves ``pending``, so ``timed_out`` stops lying.

    Such a record was reported as pending with ``timed_out`` set, which is the same
    shape a live run presents, so a caller waiting for it waited for as long as it
    kept asking. Nothing about the record changes here: no end is written, it stays
    non-terminal, and the classifier's refusal to judge the spawn's fate is intact.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    rid = jobs.new_run_id()
    _record(rid, pid=None, spawn_state="preparing")  # submitted_at defaults to 2026-07-25

    res = await jobs.wait([rid], max_wait=0, poll_interval=1)

    # Every top-level field that can move for this row class, asserted together.
    # The derived ones are the point: timed_out is pending being non-empty, so it
    # cannot be checked by inspecting pending and reasoning about it.
    assert res["unresolved_spawn"] == [rid]
    assert res["pending"] == []
    assert res["stopped_without_end"] == []
    assert res["timed_out"] is False
    assert res["all_terminal"] is False  # a run this cannot account for is not done
    assert res["unresolved_spawn_after"] == jobs.UNRESOLVED_SPAWN_AFTER_SECONDS
    assert res["runs"][0]["terminal"] is False
    assert res["runs"][0]["outcome"] is None
    assert res["runs"][0]["possibly_orphaned"] is False


async def test_wait_keeps_a_freshly_submitted_preparing_spawn_pending(sandbox, monkeypatch):
    """The guard against a bucket that would swallow every submission.

    ``preparing`` is a legitimate phase for as long as a spawn legitimately takes,
    and it does advance. A record derived flag with no age in it would report every
    fresh submit as unresolved, which is why the age is what decides and why this
    test exists rather than only its aged twin.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    rid = jobs.new_run_id()
    _record(rid, pid=None, spawn_state="preparing", submitted_at=jobs._now_iso())

    res = await jobs.wait([rid], max_wait=0, poll_interval=1)

    assert res["unresolved_spawn"] == []
    assert res["pending"] == [rid]
    assert res["timed_out"] is True


async def test_wait_does_not_read_a_null_spawn_phase_as_never_attempted(sandbox, monkeypatch):
    """Null means "no phase this can vouch for", never "never attempted".

    A record written before the phase field existed carries null, and the phase that
    does mean never-attempted says ``preparing`` explicitly. So the bucket is keyed
    on equality with ``preparing`` and not on difference from it; the natural
    inverted form would sweep every pre-field record into a bucket asserting the
    opposite of what is known about it.

    Both records here are LIVE, which is what makes the test discriminating rather
    than merely true. A pid-less record cannot reach the phase test at all — it is
    excluded one condition earlier as an orphan — so it would pass under the
    inverted form too and prove nothing. A running process is the case the inversion
    actually damages: it would report every live run as an unresolved spawn.
    """
    _live_process(monkeypatch)
    no_phase = jobs.new_run_id()
    _record(no_phase, pid=4242, spawn_state=None)  # written before the field existed
    started = jobs.new_run_id()
    _record(started, pid=4242, spawn_state="started")

    res = await jobs.wait([no_phase, started], max_wait=0, poll_interval=1)

    assert res["unresolved_spawn"] == []
    assert res["pending"] == [no_phase, started]
    assert res["timed_out"] is True
    assert res["runs"][0]["spawn_state"] is None
    assert res["runs"][1]["spawn_state"] == "started"


async def test_wait_does_not_collapse_an_orphan_into_an_unresolved_spawn(sandbox, monkeypatch):
    """The two non-terminal buckets stay distinct, because they are different news.

    ``possibly_orphaned`` presupposes a pid that existed and is now unaskable; an
    unresolved spawn never acquired one. Collapsing them would give one field two
    structurally different meanings, and an operator sent to look for a process that
    was never started is sent to look for nothing.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    orphan = jobs.new_run_id()
    _record(orphan, pid="999999")  # a pid the OS cannot even be asked about

    res = await jobs.wait([orphan], max_wait=0, poll_interval=1)

    assert res["stopped_without_end"] == [orphan]
    assert res["unresolved_spawn"] == []
    assert res["runs"][0]["possibly_orphaned"] is True


async def test_wait_and_list_agree_on_the_spawn_phase(sandbox, monkeypatch):
    """The two observation surfaces report the same phase for the same record.

    Asserted between the surfaces rather than against a literal on each: a literal
    would let both drift together, and the defect this closes was precisely the two
    surfaces disagreeing about which facts a caller gets.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    rid = jobs.new_run_id()
    _record(rid, pid=None, spawn_state="preparing")

    waited = await jobs.wait([rid], max_wait=0, poll_interval=1)
    listed = [row for row in jobs.list_jobs() if row["run_id"] == rid]

    assert len(listed) == 1
    assert waited["runs"][0]["spawn_state"] == listed[0]["spawn_state"]
    assert waited["runs"][0]["submitted_at"] == listed[0]["submitted_at"]


async def test_a_spawn_that_starts_between_observations_returns_to_pending(sandbox, monkeypatch):
    """The bucket is a reading of the current record, never a latch.

    This is what makes the bucket safe for a live-but-slow spawn: the row
    leaves it the moment its phase advances, so the cost of a wrong guess is
    one poll interval, not a run written off. Asserted across two
    observations inside ONE wait, not across two calls -- two calls would
    pass even if a latch survived for the length of a call, exactly where a
    cache would sit. The clock and the sleep are both driven rather than
    waited on, since a wall-clock version would race its own deadline and a
    flaky stickiness test is worse than none.

    The pid moves with the phase, and has to: a record claiming ``started``
    with no pid reads as an orphan, not a running run, to the classifier, so
    a fixture that only advanced the phase would assert a transition into a
    state it cannot represent. Liveness is answered per-pid rather than by a
    flat ``False`` for the same reason -- the source state needs a dead
    probe and the destination state needs a live one, inside one test.
    """
    import anyio

    live_pid = 4242
    _live_process(monkeypatch, alive=lambda pid: pid == live_pid)
    rid = jobs.new_run_id()
    _record(rid, pid=None, spawn_state="preparing")

    # First establish the row really is in the new bucket, so what follows is a
    # transition OUT of it rather than a row that was never in it. Without this the
    # test would pass against a build where the bucket never captured anything.
    before = await jobs.wait([rid], max_wait=0, poll_interval=1)
    assert before["unresolved_spawn"] == [rid]
    assert before["pending"] == []

    clock = {"t": 1_000.0}
    seen: list[str | None] = []

    async def flip_after_the_first_observation(delay):
        clock["t"] += delay
        # The phase the loop was looking at when it decided to keep waiting,
        # captured before this sleep changes anything.
        seen.append(jobs._wait_entry(rid)["spawn_state"])
        if len(seen) == 1:
            _record(rid, pid=live_pid, spawn_state="started")

    monkeypatch.setattr(anyio, "current_time", lambda: clock["t"])
    monkeypatch.setattr(anyio, "sleep", flip_after_the_first_observation)

    res = await jobs.wait([rid], max_wait=2, poll_interval=1)

    # The two observations the single call actually made, in order.
    assert seen == ["preparing", "started"]
    assert res["unresolved_spawn"] == []
    assert res["pending"] == [rid]
    # And the triple reads as "keep waiting" again, which is the point: the row is
    # back to being something waiting can resolve.
    assert res["timed_out"] is True
    assert res["all_terminal"] is False
