# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the background job engine.

Popen is mocked throughout so no real `li` process is spawned; the tests assert
on the argv/env the engine builds and on the on-disk job records it reads back.
"""

from __future__ import annotations

import builtins
import errno
import json
import logging
import math
import os
import signal
from pathlib import Path

import pytest

from lionagi.cli import _mcp_resolve
from lionagi.ln import _proc
from lionagi.mcp import config, jobs


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Point job/run state at a tmp dir so tests never touch the real ~/.lionagi."""
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "li_command", lambda: ["echo"])
    # Popen is doubled for the whole module here, and subprocess.run goes
    # through Popen — so the lifecycle read cannot run in this file at all.
    # Stubbed to the answer a failed read gives ("learned nothing"), which is
    # what these tests assume; the read itself is covered in test_lifecycle.py.
    monkeypatch.setattr(jobs, "_read_lifecycle", lambda run_id: None)
    return tmp_path


class _FakeProc:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid


def test_new_run_id_format():
    rid = jobs.new_run_id()
    ts, dash, suffix = rid.partition("-")
    assert dash == "-"
    assert len(ts) == len("YYYYMMDDTHHMMSS") and "T" in ts
    assert len(suffix) == 6


def test_submit_records_and_returns_handle(sandbox, monkeypatch):
    captured: dict = {}

    def fake_popen(argv, **kw):
        captured["argv"] = argv
        captured["kw"] = kw
        return _FakeProc(4242)

    monkeypatch.setattr(jobs.subprocess, "Popen", fake_popen)

    res = jobs.submit(
        "agent",
        ["-a", "reviewer"],
        prompt="do the thing",
        label="t1",
        notify_target="downstream",
    )
    rid = res["run_id"]

    assert res["pid"] == 4242 and res["status"] == "running"
    # run_id handed to the child via env (race-free naming)
    assert captured["kw"]["env"][config.RUN_ID_ENV_VAR] == rid
    # detached into its own session
    assert captured["kw"]["start_new_session"] is True
    # CLAUDECODE stripped from the child env
    assert "CLAUDECODE" not in captured["kw"]["env"]
    # prompt via --prompt-file, notify wired, profile flag present
    argv = captured["argv"]
    assert "--prompt-file" in argv and "--notify" in argv and "-a" in argv
    # record persisted
    rec = jobs._read_job(rid)
    assert rec["kind"] == "agent"
    assert rec["status"] == "running"
    assert rec["notify_target"] == "downstream"


def _capture_popen(captured: dict):
    def fake_popen(argv, **kw):
        captured["argv"] = argv
        return _FakeProc()

    return fake_popen


def test_notify_template_bakes_hook_and_target(sandbox, monkeypatch):
    """The --notify value invokes the terminal hook by interpreter -m, carries a
    substitutable {status}, and bakes --target when a target is given."""
    captured: dict = {}
    monkeypatch.setattr(jobs.subprocess, "Popen", _capture_popen(captured))

    jobs.submit("agent", ["-a", "reviewer"], prompt="x", notify_target="downstream")
    argv = captured["argv"]
    template = argv[argv.index("--notify") + 1]
    assert "-m lionagi.mcp._notify_hook" in template
    assert "--status {status}" in template
    assert "--target downstream" in template


def test_notify_template_no_target_no_command_when_absent(sandbox, monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(jobs.subprocess, "Popen", _capture_popen(captured))

    res = jobs.submit("agent", ["-a", "reviewer"], prompt="x")  # no notify target/command
    argv = captured["argv"]
    template = argv[argv.index("--notify") + 1]
    assert "--target" not in template
    assert "--command" not in template
    rec = jobs._read_job(res["run_id"])
    assert rec["notify_target"] is None
    assert rec["notify_command"] is None


def test_notify_template_bakes_command_override(sandbox, monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(jobs.subprocess, "Popen", _capture_popen(captured))

    jobs.submit(
        "agent",
        ["-a", "reviewer"],
        prompt="x",
        notify_command='["notify-send", "{status}"]',
    )
    argv = captured["argv"]
    template = argv[argv.index("--notify") + 1]
    assert "--command" in template


def test_flow_prompt_is_positional(sandbox, monkeypatch):
    captured: dict = {}

    def fake_popen(argv, **kw):
        captured["argv"] = argv
        return _FakeProc()

    monkeypatch.setattr(jobs.subprocess, "Popen", fake_popen)
    jobs.submit("flow", ["-a", "orchestrator"], prompt="build the DAG")
    argv = captured["argv"]
    assert "--prompt-file" not in argv  # flow takes the prompt as a positional
    assert argv[-1] == "build the DAG"


def test_submit_rejects_unknown_kind(sandbox):
    with pytest.raises(ValueError):
        jobs.submit("bogus", [])


def test_status_running_then_terminal(sandbox, monkeypatch):
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *a, **k: _FakeProc(999_999))
    rid = jobs.submit("agent", [], prompt="x")["run_id"]

    _live_process(monkeypatch)
    assert jobs.status(rid)["status"] == "running"

    # authoritative terminal recorded by the notify hook, which runs while the
    # run's own process is still there to run it
    jobs.mark_terminal(rid, "completed")
    st = jobs.status(rid)
    assert st["status"] == "completed"
    assert st["terminal"] is True and st["outcome"] == "succeeded"

    # the process going away afterwards adds nothing and takes nothing away:
    # the end is on the record, and that is what every reader answers from
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    st = jobs.status(rid)
    assert st["status"] == "completed"
    assert st["terminal"] is True and st["outcome"] == "succeeded"


def test_pid_alive_reaps_zombie_child():
    """A detached child that exited must not read as alive via kill -0 (zombie)."""
    import subprocess
    import time

    p = subprocess.Popen(["sleep", "0.05"], start_new_session=True)
    time.sleep(0.35)  # exited, but an unreaped zombie of this process
    assert jobs._pid_alive(p.pid) is False


def test_kill_guards_low_pid(sandbox):
    rid = jobs.new_run_id()
    jobs._write_job({"run_id": rid, "pid": 1, "kind": "agent", "status": "running", "log": None})
    out = jobs.kill(rid)
    assert out["killed"] is False and "no pid" in out["reason"]


def test_kill_unknown_job(sandbox):
    out = jobs.kill("nope")
    assert out["killed"] is False and out["reason"] == "no such job"


@pytest.mark.parametrize(
    "recorded",
    [
        {"status": "completed", "finished_at": "2026-01-01T00:00:00+00:00"},
        {"status": "cancelled", "finished_at": "2026-01-01T00:00:00+00:00"},
        {"status": "timed_out", "finished_at": "2026-01-01T00:00:00+00:00"},
        {"status": "failed", "spawn_state": "failed", "finished_at": "2026-01-01T00:00:00+00:00"},
        # No finished_at: a spawn failure is terminal on the spawn state alone,
        # which is what `status` derives from it. Without this case the guard
        # could drop its spawn-state arm and every other case here would still
        # pass, putting kill and status back into disagreement.
        {"status": "failed", "spawn_state": "failed"},
    ],
)
def test_kill_refuses_a_record_that_already_ended(sandbox, monkeypatch, recorded):
    """A run that ended is never probed and never signalled, however it ended.

    The pid stays on the record after the run ends and pid numbers get reused, so
    a liveness probe of that number can find an unrelated process — signalling it
    would kill a stranger's process group. The recorded end must also survive: a
    kill that should be a no-op must not relabel a completed or cancelled run.
    """
    killpg_calls: list[tuple] = []
    monkeypatch.setattr(jobs.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(jobs.os, "killpg", lambda *a: killpg_calls.append(a))
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)

    rid = jobs.new_run_id()
    jobs._write_job({"run_id": rid, "pid": 4242, "kind": "agent", "log": None, **recorded})

    out = jobs.kill(rid)

    assert killpg_calls == [], "a job that already ended must not be signalled"
    assert out["killed"] is False
    # The refusal reports the pid: a group that really did outlive its recorded
    # end can only be found by an operator if this number survives the refusal.
    assert out["pid"] == 4242
    # kill and status must call the same record terminal. Whichever arm of the
    # predicate this case exercises, disagreement here is the bug being guarded.
    assert jobs.status(rid)["terminal"] is True
    after = jobs._read_job(rid)
    assert after["status"] == recorded["status"]
    assert after.get("finished_at") == recorded.get("finished_at")


_SPAWNED_AT = 1_700_000_000.0


def _raise(exc: BaseException):
    """A stand-in for an OS call that fails, whatever arguments it is handed.

    The signatures differ across the calls being doubled, so the replacement
    accepts anything and raises the one exception the test is about. Raising an
    instance rather than a class keeps errno and message available to the code
    under test, which several of these guards put into what they report.
    """

    def _fail(*args, **kwargs):
        raise exc

    return _fail


def _live_process(monkeypatch, created: float = _SPAWNED_AT):
    """Make both process probes agree that the pid holds a live process.

    A pid is asked two separate questions: whether it holds a live process at
    all, and when that process started. A double that answers only the first
    describes a state no operating system produces — a pid that answers ``kill
    -0`` and is absent from the process table is a process that has exited and
    is waiting to be reaped, which is the opposite of alive. Tests that mean
    "this run is still running" have to say so to both.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_process_create_time", lambda pid: ("found", created))


def _identity_record(pid: int = 4242, pgid: int = 7777, created: float = _SPAWNED_AT, **extra):
    """A job record carrying the process identity submit() now writes.

    Some of these deliberately carry a start time that cannot act as one, to ask
    what a *reader* does with it — and among those are the non-finite floats the
    writer now refuses, because json.dumps would put the bare token NaN or
    Infinity on disk. A reader still has to survive one: records written before
    that refusal existed are on disk already, and the job store is shared with
    whatever else writes into it. So a record the writer will not produce is
    published directly here, and every record the writer *can* produce still goes
    through it, which is what keeps this fixture's shape the production shape.
    """
    rec = {
        "run_id": jobs.new_run_id(),
        "pid": pid,
        "pid_create_time": created,
        "pgid": pgid,
        "kind": "agent",
        "status": "running",
        "log": None,
    }
    rec.update(extra)
    if isinstance(created, float) and not math.isfinite(created):
        d = config.job_dir(rec["run_id"])
        d.mkdir(parents=True, exist_ok=True)
        (d / "job.json").write_text(json.dumps(rec, indent=2))
    else:
        jobs._write_job(rec)
    return rec["run_id"]


@pytest.fixture
def no_stray_signal(monkeypatch):
    """Keep a test's invented pids away from every real process on this machine.

    Three jobs, all following from the same fact: the pids in these records are
    numbers the test made up, and some live process may well hold each of them.
    It records what was signalled so a test can assert on it; it replaces
    os.getpgid with a raise, so a test that needs the live leader's group has to
    say which group that is rather than reading whatever real process holds its
    invented pid; and it stubs the marker read to "unreadable", so no test
    reaches into a real process's environment. A test exercising the marker or
    the leader's group overrides the relevant stub with its own.
    """
    calls: list[tuple] = []

    def refuse_getpgid(pid):
        raise AssertionError(f"pid {pid} is invented; the test must stub its group")

    monkeypatch.setattr(jobs.os, "getpgid", refuse_getpgid)
    monkeypatch.setattr(jobs.os, "killpg", lambda *a: calls.append(a))
    monkeypatch.setattr(_proc, "process_marker", lambda pid, marker_var: ("unknown", None))
    return calls


def test_submit_records_the_identity_of_the_process_it_spawned(sandbox, monkeypatch):
    """A pid alone is not an identity, so the start time and group go with it."""
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *a, **k: _FakeProc(4242))
    monkeypatch.setattr(jobs, "_process_create_time", lambda pid: ("found", _SPAWNED_AT))
    monkeypatch.setattr(jobs, "_spawned_pgid", lambda pid: pid)

    rec = jobs._read_job(jobs.submit("agent", [], prompt="x")["run_id"])

    assert rec["pid"] == 4242
    assert rec["pid_create_time"] == _SPAWNED_AT
    # start_new_session makes the child its own group leader, so the group is
    # its own pid — recorded rather than re-derived at kill time.
    assert rec["pgid"] == 4242


def test_kill_signals_the_recorded_group_when_identity_matches(
    sandbox, monkeypatch, no_stray_signal
):
    """The happy path: the leader is alive, is the process we started, and is
    in the group the record names."""
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_process_create_time", lambda pid: ("found", _SPAWNED_AT + 0.02))
    monkeypatch.setattr(jobs.os, "getpgid", lambda pid: 7777)

    rid = _identity_record()
    out = jobs.kill(rid)

    assert no_stray_signal == [(7777, signal.SIGTERM)]
    assert out["killed"] is True and out["reason_code"] == jobs.KILL_SIGNALLED
    assert out["pgid"] == 7777
    after = jobs._read_job(rid)
    assert after["status"] == "killed" and after["finished_at"] is not None


def test_kill_refuses_when_the_live_leader_is_in_a_different_group(
    sandbox, monkeypatch, no_stray_signal
):
    """A stored group number that the confirmed leader is not actually in.

    The leader passes the identity check, so the old code signalled the recorded
    group on the strength of it having been an integer above one. A record whose
    pgid was damaged or edited would then aim a signal at whatever group holds
    that number. Neither number is signalled.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_process_create_time", lambda pid: ("found", _SPAWNED_AT))
    monkeypatch.setattr(jobs.os, "getpgid", lambda pid: 4242)

    rid = _identity_record(pgid=987654)
    out = jobs.kill(rid)

    assert no_stray_signal == [], "a record's pgid alone must license no signal"
    assert out["killed"] is False
    assert out["reason_code"] == jobs.KILL_LEADER_GROUP_MISMATCH
    assert jobs._read_job(rid)["status"] == "running"


@pytest.mark.parametrize("error", [ProcessLookupError(), PermissionError(1, "not permitted")])
def test_kill_refuses_when_the_live_leaders_group_cannot_be_read(
    sandbox, monkeypatch, no_stray_signal, error
):
    """An unreadable group is a probe that failed, and it refuses like any other.

    Its own code, separate from a mismatch: nothing has been established about
    the record here, so this is the case where a later call may still succeed.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_process_create_time", lambda pid: ("found", _SPAWNED_AT))

    def raising_getpgid(pid):
        raise error

    monkeypatch.setattr(jobs.os, "getpgid", raising_getpgid)

    out = jobs.kill(_identity_record())

    assert no_stray_signal == []
    assert out["killed"] is False
    assert out["reason_code"] == jobs.KILL_LEADER_GROUP_UNREADABLE


def test_kill_refuses_a_live_leader_that_says_it_belongs_to_another_run(
    sandbox, monkeypatch, no_stray_signal
):
    """A live leader whose every number matches, and which names another run.

    The record's pid, start time and group all describe this process, so the
    numbers alone license the signal. The process itself disagrees: it carries a
    different run's id in the environment its parent gave it, which is exactly
    the evidence the group route refuses on when the leader is gone. The same
    evidence has to reach the same conclusion on the route where the leader is
    still alive, or one branch of this decision trusts what the other rejects.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_process_create_time", lambda pid: ("found", _SPAWNED_AT))
    monkeypatch.setattr(jobs.os, "getpgid", lambda pid: 7777)
    monkeypatch.setattr(jobs, "_process_marker", lambda pid: ("found", "some-other-run"))

    rid = _identity_record()
    out = jobs.kill(rid)

    assert no_stray_signal == [], "a process naming another run must not be signalled"
    assert out["killed"] is False and out["reason_code"] == jobs.KILL_GROUP_FOREIGN
    assert jobs._read_job(rid)["status"] == "running"


@pytest.mark.parametrize("marker", [("unknown", None), ("found", None)])
def test_kill_signals_a_live_leader_whose_environment_names_no_run(
    sandbox, monkeypatch, no_stray_signal, marker
):
    """No marker withholds nothing, on the route where the leader is alive too.

    This one passes before the marker was read here at all, and that is what it
    is for: the marker may veto a signal and may never be required to permit
    one. A process whose environment cannot be read and one whose environment is
    genuinely empty arrive as the same answer — some platforms hand back an empty
    environment for a protected binary rather than raising — so requiring a
    marker to signal would strand every job whose processes cannot be read. Both
    of those answers are covered here, because the distinction the code must not
    start drawing between them is invisible to a single case.

    It is also where the scope of the whole identity check ends, and the case is
    pinned here rather than only described in prose. A record rewritten to hold
    a live stranger's pid, start time and group reaches this same assertion: if
    that stranger names no run, it is signalled. Nothing in the record can say
    who wrote it, so the guarantee is relative to a record this run wrote, and a
    store that can be rewritten is a store whose writer could signal these
    processes without going through here at all.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_process_create_time", lambda pid: ("found", _SPAWNED_AT))
    monkeypatch.setattr(jobs.os, "getpgid", lambda pid: 7777)
    monkeypatch.setattr(jobs, "_process_marker", lambda pid: marker)

    out = jobs.kill(_identity_record())

    assert no_stray_signal == [(7777, signal.SIGTERM)]
    assert out["killed"] is True and out["reason_code"] == jobs.KILL_SIGNALLED


def _leader_reads(monkeypatch, answers):
    """Answer each read of the leader's start time from *answers*, in call order.

    Lets a test hand the leader's pid to another process partway through the
    kill without a real race.
    """
    reads = iter(answers)
    monkeypatch.setattr(jobs, "_process_create_time", lambda pid: next(reads, answers[-1]))


def test_kill_refuses_a_leader_whose_number_is_handed_on_under_the_group_read(
    sandbox, monkeypatch, no_stray_signal
):
    """The live leader's group and marker have to describe the pid that matched.

    A run's leader is started in its own session, so the one number is both its
    pid and its pgid. When it exits and its group drains, that number is free,
    and the OS can hand it to a new session leader whose pgid is again the same
    number. A kill that matched the start time just before that happened then
    reads a group equal to the recorded one — from a process this run never
    spawned — and the marker cannot correct it, since an absent or unreadable one
    only ever withholds a signal.

    So the start time is read again after those reads and required to be
    unchanged, and here it is not.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    _leader_reads(monkeypatch, [("found", _SPAWNED_AT), ("found", _SPAWNED_AT + 400.0)])
    monkeypatch.setattr(jobs.os, "getpgid", lambda pid: 4242)

    rid = _identity_record(pid=4242, pgid=4242)
    out = jobs.kill(rid)

    assert no_stray_signal == [], "a group read from a replacement licenses nothing"
    assert out["killed"] is False
    assert out["reason_code"] == jobs.KILL_LEADER_IDENTITY_CHANGED
    assert jobs._read_job(rid)["status"] == "running"


@pytest.mark.parametrize("second_read", [("unknown", None), ("gone", None)])
def test_kill_refuses_when_the_leaders_start_time_cannot_be_read_back(
    sandbox, monkeypatch, no_stray_signal, second_read
):
    """A bracket that cannot be closed is a measurement that did not come off.

    Whether the second read errored or found nothing there, what the group and
    the marker said is no longer tied to the process that matched, and an
    untied answer is never licence to signal.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    _leader_reads(monkeypatch, [("found", _SPAWNED_AT), second_read])
    monkeypatch.setattr(jobs.os, "getpgid", lambda pid: 4242)

    out = jobs.kill(_identity_record(pid=4242, pgid=4242))

    assert no_stray_signal == []
    assert out["killed"] is False
    assert out["reason_code"] == jobs.KILL_LEADER_IDENTITY_CHANGED


def test_kill_signals_a_leader_that_is_the_same_process_at_both_reads(
    sandbox, monkeypatch, no_stray_signal
):
    """The control the refusals above are worth nothing without.

    Every one of them asserts that a kill refused, which a bracket that always
    refused would satisfy as readily as a correct one. An ordinary leader — alive,
    unchanged across both reads, in the recorded group — is still signalled.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    _leader_reads(monkeypatch, [("found", _SPAWNED_AT), ("found", _SPAWNED_AT)])
    monkeypatch.setattr(jobs.os, "getpgid", lambda pid: 4242)

    out = jobs.kill(_identity_record(pid=4242, pgid=4242))

    assert no_stray_signal == [(4242, signal.SIGTERM)]
    assert out["killed"] is True and out["reason_code"] == jobs.KILL_SIGNALLED


def test_kill_refuses_a_record_that_names_a_different_run(sandbox, monkeypatch):
    """A record found under one run, describing another.

    Every write of a record stamps the run it belongs to, so this field is not a
    measurement that can fail — a record whose own id is not the one being killed
    was put there by something other than the run being killed, and the process
    its numbers describe is some other run's. Nothing is probed: the pid on such
    a record has no claim on this call.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: pytest.fail("pid must not be probed"))

    rid = jobs.new_run_id()
    other = jobs.new_run_id()
    _write_raw_record(
        rid,
        f'{{"run_id": "{other}", "pid": 4242, "pgid": 7777, '
        f'"pid_create_time": {_SPAWNED_AT}, "status": "running"}}',
    )

    out = jobs.kill(rid)

    assert out["killed"] is False
    assert out["reason_code"] == jobs.KILL_RECORD_FOREIGN_RUN
    # The run the record does name is reported: it is the only handle a caller
    # has for stopping the run that record actually describes.
    assert other in out["reason"]


def test_kill_refuses_a_recycled_pid(sandbox, monkeypatch, no_stray_signal):
    """An alive pid that started at a different time is a different process."""
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_process_create_time", lambda pid: ("found", _SPAWNED_AT + 900))

    rid = _identity_record()
    out = jobs.kill(rid)

    assert no_stray_signal == [], "a reused pid must cost a stranger nothing"
    assert out["killed"] is False and out["reason_code"] == jobs.KILL_PID_RECYCLED
    assert jobs._read_job(rid)["status"] == "running"


def test_kill_refuses_when_the_leaders_start_time_is_unreadable(
    sandbox, monkeypatch, no_stray_signal
):
    """A probe that errored is unknown, and unknown is never licence to signal."""
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_process_create_time", lambda pid: ("unknown", None))

    out = jobs.kill(_identity_record())

    assert no_stray_signal == []
    assert out["killed"] is False and out["reason_code"] == jobs.KILL_LEADER_UNVERIFIABLE


def test_status_reports_a_reused_pid_as_not_this_runs_process(
    sandbox, monkeypatch, no_stray_signal
):
    """A stranger holding the run's old pid number must not read as the run.

    Nothing recorded an end for this run, so the only thing standing between it
    and "running forever" is the identity check: without it the liveness probe
    answers about whatever process now holds the number, and the run reports
    healthy for as long as that process lives. It also silences the one field
    that would say otherwise, since a run flagged possibly_orphaned is exactly a
    run whose process is gone with no end recorded.

    kill() refuses this same record as a reused pid, so the two surfaces are
    asserted together: one module answering one record two ways is the defect.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_process_create_time", lambda pid: ("found", _SPAWNED_AT + 5000))

    rid = _identity_record()
    st = jobs.status(rid)

    assert st["alive"] is False
    assert st["pid_identity"] == "recycled"
    assert st["status"] == "exited"
    assert st["terminal"] is False
    assert st["outcome"] is None
    assert st["possibly_orphaned"] is True
    assert jobs.kill(rid)["reason_code"] == jobs.KILL_PID_RECYCLED
    assert no_stray_signal == []


def test_status_reports_a_confirmed_process_as_running(sandbox, monkeypatch):
    """The healthy case is unchanged, and the comparison keeps its tolerance.

    The start time is read from a clock kept in ticks, so a live read of the
    process this run spawned can differ from the recorded value in the last
    decimal. Comparing it exactly would report every live run as recycled.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_process_create_time", lambda pid: ("found", _SPAWNED_AT + 0.02))

    st = jobs.status(_identity_record())

    assert st["alive"] is True
    assert st["pid_identity"] == "confirmed"
    assert st["status"] == "running"
    assert st["terminal"] is False
    assert st["possibly_orphaned"] is False


def test_status_does_not_read_a_failed_identity_probe_as_death(sandbox, monkeypatch):
    """A probe that errored established nothing, so the liveness probe stands.

    Reporting the run as stopped here would invent a death out of a measurement
    that did not come off, and stopped is what a caller stops waiting on.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_process_create_time", lambda pid: ("unknown", None))

    st = jobs.status(_identity_record())

    assert st["alive"] is True
    assert st["pid_identity"] == "unreadable"
    assert st["status"] == "running"
    assert st["possibly_orphaned"] is False


def test_status_reports_a_pid_that_emptied_between_the_two_reads(sandbox, monkeypatch):
    """The process exited between the liveness probe and the identity read."""
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_process_create_time", lambda pid: ("gone", None))

    st = jobs.status(_identity_record())

    assert st["alive"] is False
    assert st["pid_identity"] == "gone"
    assert st["status"] == "exited"
    assert st["possibly_orphaned"] is True


@pytest.mark.parametrize(
    ("recorded", "identity"),
    [
        # submit() writes a null start time whenever the read at spawn failed,
        # and a record written before identity capture has the same shape.
        (None, "not_recorded"),
        # Present, and holding something no start time can be compared against:
        # a damaged record, which is different news from an old one.
        ("not-a-number", "unusable"),
        (float("nan"), "unusable"),
        (True, "unusable"),
    ],
)
def test_status_leaves_a_record_that_cannot_identify_its_process_on_the_pid_probe(
    sandbox, monkeypatch, recorded, identity
):
    """No identity was captured, so nothing is compared — liveness still is.

    Two questions are asked of a pid, and only the second one needs this record.
    Whether the pid holds a live process is answerable from the pid alone, so it
    is settled here as on every other path. Whether that live process is *this
    run's* is what these records cannot say, and nothing is compared to pretend
    otherwise. Flipping these to stopped on the strength of the missing field
    would claim the process is gone on data that says nothing about it either way.
    """
    _live_process(monkeypatch)
    monkeypatch.setattr(
        jobs,
        "_start_time_matches",
        lambda *a: pytest.fail("a record with no usable identity has nothing to compare"),
    )

    st = jobs.status(_identity_record(created=recorded))

    assert st["alive"] is True
    assert st["pid_identity"] == identity
    assert st["status"] == "running"
    assert st["terminal"] is False
    assert st["possibly_orphaned"] is False


def test_status_identifies_nothing_when_no_live_pid_holds_the_number(sandbox, monkeypatch):
    """Nothing answered the liveness probe, so there was no process to identify."""
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(
        jobs,
        "_process_create_time",
        lambda pid: pytest.fail("a pid holding nothing must not be probed for identity"),
    )

    st = jobs.status(_identity_record())

    assert st["alive"] is False
    assert st["pid_identity"] is None
    assert st["status"] == "exited"
    assert st["possibly_orphaned"] is True


@pytest.mark.parametrize("recorded", [None, "not-a-number", float("nan"), True])
def test_a_process_that_exited_reports_gone_even_with_no_identity_to_compare(
    sandbox, monkeypatch, recorded
):
    """The record cannot identify the process, and the process has still exited.

    A pid that answers ``kill -0`` and holds no process is one that exited and
    has not been reaped. Reading the record first and the process second would
    make every record without a usable start time report an exited run as
    running, and turn ``possibly_orphaned`` off in the one case it exists for.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_process_create_time", lambda pid: ("gone", None))

    st = jobs.status(_identity_record(created=recorded))

    assert st["alive"] is False
    assert st["pid_identity"] == "gone"
    assert st["status"] == "exited"
    assert st["terminal"] is False
    assert st["possibly_orphaned"] is True


def test_a_process_that_exited_under_another_parent_does_not_read_as_alive(sandbox):
    """The same case with a real process instead of a double.

    The liveness probe reaps only its own children, so it can only settle the
    question for a job it spawned itself. Every other server sharing the job
    store is in the position this test is in: the exited process here is a
    grandchild, so nothing this test does can reap it, and ``kill -0`` keeps
    answering yes for as long as it sits there. A live control runs through the
    same assertions in the same invocation, so a probe that had simply stopped
    working could not produce this result.
    """
    import subprocess
    import sys
    import time

    # Fork a grandchild that exits at once, report its pid, and stay alive
    # without reaping it. The parent of the zombie is the child, never pytest.
    source = (
        "import os, sys, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os._exit(0)\n"
        "sys.stdout.write(f'{pid}\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", source], stdout=subprocess.PIPE, text=True, start_new_session=True
    )
    try:
        zombie = int(child.stdout.readline().strip())
        deadline = time.monotonic() + 10
        while jobs._process_create_time(zombie)[0] != "gone" and time.monotonic() < deadline:
            time.sleep(0.02)
        if jobs._process_create_time(zombie)[0] != "gone":
            pytest.skip("the grandchild did not become an unreaped zombie here")

        # The probe that cannot tell, and the probe that can, on the same pid.
        assert jobs._pid_alive(zombie) is True
        assert jobs._process_create_time(zombie)[0] == "gone"

        exited = jobs.status(_identity_record(pid=zombie, created=None))
        assert exited["alive"] is False
        assert exited["pid_identity"] == "gone"
        assert exited["possibly_orphaned"] is True

        # Live control: the child itself, running, read the same way.
        running = jobs.status(_identity_record(pid=child.pid, created=None))
        assert running["alive"] is True
        assert running["pid_identity"] == "not_recorded"
        assert running["status"] == "running"
    finally:
        child.kill()
        child.wait()


@pytest.mark.parametrize("recorded_pid", ["4242", 4242.0, [4242], {"pid": 4242}, 10**40, 2**63])
def test_a_pid_the_os_cannot_be_asked_about_is_refused_rather_than_raised(
    sandbox, recorded_pid, no_stray_signal
):
    """A record is JSON from disk, and every probe below it takes a C integer.

    A value of the wrong type or past what the platform can express raises out of
    the first probe to touch it, which turns a damaged record into a failed read
    of both surfaces rather than into the refusal the record has earned.
    """
    rid = _identity_record(pid=recorded_pid)

    out = jobs.kill(rid)
    assert out["killed"] is False
    assert out["reason_code"] == jobs.KILL_NO_PID
    assert "no pid on record" in out["reason"]

    st = jobs.status(rid)
    assert st["alive"] is False
    assert st["pid_identity"] == "unusable_pid"
    assert st["known"] is True
    assert st["record_state"] == "ok"


def test_a_usable_pid_is_not_refused_by_the_shape_gate(sandbox, monkeypatch, no_stray_signal):
    """The control for the table above: a well-formed pid still reaches the probes."""
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)

    st = jobs.status(_identity_record(pid=4242))

    assert st["pid_identity"] != "unusable_pid"
    assert st["alive"] is False


def test_kill_reaps_a_live_group_whose_leader_exited(sandbox, monkeypatch, no_stray_signal):
    """The case a leader-liveness gate refuses: `li` exits, its workers do not.

    The children are spawned into the leader's group and outlive it, so the
    group is what has to be signalled — and it is still identifiable after the
    leader is gone, because the run stamped its id into every process it started
    and a surviving member reads it back.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(
        jobs, "_live_group_members", lambda pgid: ([(5001, _SPAWNED_AT + 3.0, rid, True)], True)
    )

    rid = _identity_record()
    out = jobs.kill(rid)

    assert no_stray_signal == [(7777, signal.SIGTERM)]
    assert out["killed"] is True and out["reason_code"] == jobs.KILL_SIGNALLED
    after = jobs._read_job(rid)
    assert after["status"] == "killed" and after["finished_at"] is not None


def test_submit_stamps_the_run_id_into_the_child_environment(sandbox, monkeypatch):
    """The marker every later identity check reads back off the group."""
    seen: dict = {}

    def fake_popen(*a, **k):
        seen.update(k.get("env") or {})
        return _FakeProc(4242)

    monkeypatch.setattr(jobs.subprocess, "Popen", fake_popen)

    rid = jobs.submit("agent", [], prompt="x")["run_id"]

    assert seen[config.JOB_MARKER_ENV_VAR] == rid


def test_a_real_child_carries_the_marker_and_it_reads_back(sandbox):
    """The mechanism itself, against a real process rather than a stub.

    Reading another process's environment is a platform capability, not a
    given, and the whole marker rule rests on it. This is the check that says
    it works here — and, if it ever stops working, says so directly instead of
    leaving the identity rule to quietly fall back forever.
    """
    import subprocess
    import sys
    import time

    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env={**os.environ, config.JOB_MARKER_ENV_VAR: "marker-under-test"},
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state, marker = jobs._process_marker(proc.pid)
            if state == "found" and marker is not None:
                break
            time.sleep(0.05)
        assert (state, marker) == ("found", "marker-under-test")
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_process_marker_reports_an_unreadable_process_as_unknown():
    """A probe that failed is unknown — never "carries no marker"."""
    # A pid that cannot be read: reaped children of another parent are gone by
    # the time we ask, and this one was ours and is fully waited on.
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", ""])  # noqa: S603
    proc.wait(timeout=10)

    assert jobs._process_marker(proc.pid) == ("unknown", None)


def test_kill_identifies_the_group_by_the_marker_the_run_stamped(
    sandbox, monkeypatch, no_stray_signal
):
    """A positive identification, where the start-time rule alone would refuse.

    The member here is *older* than the recorded spawn, which the start-time
    inequality reads as a reused group number. The marker says otherwise and
    outranks it: members share a pgid, so one member carrying this run's id
    makes the group this run's whatever the clock suggests.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(
        jobs, "_live_group_members", lambda pgid: ([(5001, _SPAWNED_AT - 60.0, rid, True)], True)
    )

    rid = _identity_record()
    out = jobs.kill(rid)

    assert no_stray_signal == [(7777, signal.SIGTERM)]
    assert out["killed"] is True and out["reason_code"] == jobs.KILL_SIGNALLED


def test_kill_refuses_a_group_carrying_another_runs_marker(sandbox, monkeypatch, no_stray_signal):
    """The same evidence pointing the other way, and it is what excludes.

    Every member started after this run did, so the start time excludes nothing.
    The marker names a different run, which is what settles it.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(
        jobs,
        "_live_group_members",
        lambda pgid: ([(5001, _SPAWNED_AT + 3.0, "some-other-run", True)], True),
    )

    out = jobs.kill(_identity_record())

    assert no_stray_signal == []
    assert out["killed"] is False and out["reason_code"] == jobs.KILL_GROUP_FOREIGN
    # The sentence reports what was read, not the history that would explain it.
    # An environment variable is what was observed; who spawned the process, and
    # whether a group number was handed on, was not.
    assert "carries a different run's id in its environment" in out["reason"]
    assert "started by" not in out["reason"]
    assert "reused" not in out["reason"]


@pytest.mark.parametrize("order", [("this-run", "other-run"), ("other-run", "this-run")])
def test_kill_refuses_a_group_whose_members_carry_conflicting_markers(
    sandbox, monkeypatch, no_stray_signal, order
):
    """Two markers disagree, and the verdict must not depend on which is read first.

    Deciding on the first readable marker made the answer a function of the order
    the process table was enumerated in: the same two members returned "ours" one
    way round and "not_ours" the other. Both orders now reach the same refusal,
    and a disagreement is its own outcome rather than being reported as either
    ownership claim.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(
        jobs,
        "_live_group_members",
        lambda pgid: (
            [
                (5001, _SPAWNED_AT + 1.0, seen[5001], True),
                (5002, _SPAWNED_AT + 2.0, seen[5002], True),
            ],
            True,
        ),
    )

    rid = _identity_record()
    seen = {pid: (rid if m == "this-run" else m) for pid, m in zip([5001, 5002], order)}

    out = jobs.kill(rid)

    assert no_stray_signal == [], "an unexplained group must not be signalled"
    assert out["killed"] is False
    assert out["reason_code"] == jobs.KILL_GROUP_MARKERS_CONFLICT
    assert "different run ids" in out["reason"]


def test_kill_identifies_a_group_whose_members_all_carry_this_runs_marker(
    sandbox, monkeypatch, no_stray_signal
):
    """Agreeing markers across several members still identify the group.

    The conflict rule must refuse disagreement without refusing agreement: a run
    that spawned more than one process is the ordinary case, and every member
    reading back the same run id is the strongest evidence available here.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(
        jobs,
        "_live_group_members",
        # Both older than the record, so only the marker can license this signal.
        lambda pgid: (
            [(5001, _SPAWNED_AT - 60.0, rid, True), (5002, _SPAWNED_AT - 30.0, rid, True)],
            True,
        ),
    )

    rid = _identity_record()
    out = jobs.kill(rid)

    assert no_stray_signal == [(7777, signal.SIGTERM)]
    assert out["killed"] is True and out["reason_code"] == jobs.KILL_SIGNALLED


def test_kill_refuses_a_group_that_is_merely_young_enough(sandbox, monkeypatch, no_stray_signal):
    """Starting after this run did is not evidence of belonging to it.

    Every member here is younger than the record, which is exactly what an
    unrelated group occupying a reused group number looks like: the number was
    freed when this run's group emptied, and whoever took it necessarily started
    later. The inequality can rule a group out and can never rule one in, so with
    no marker to read there is nothing left that identifies this group, and both
    ways of failing to read one — no id present, and no readable environment —
    have to refuse.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(
        jobs, "_live_group_members", lambda pgid: ([(5001, _SPAWNED_AT + 3.0, None, True)], True)
    )

    out = jobs.kill(_identity_record())

    assert no_stray_signal == [], "a young group is not thereby this run's group"
    assert out["killed"] is False
    assert out["reason_code"] == jobs.KILL_GROUP_OWNERSHIP_UNPROVEN
    assert "not evidence of belonging to it" in out["reason"]


def test_kill_still_excludes_a_group_holding_a_member_older_than_the_run(
    sandbox, monkeypatch, no_stray_signal
):
    """The start time keeps the half of its job that is sound.

    It cannot identify a group, but it can still rule one out: a process that was
    already running before this run started cannot be work this run spawned. That
    refusal is a different fact from having no evidence at all, and keeps its own
    code.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(
        jobs,
        "_live_group_members",
        lambda pgid: (
            [(5001, _SPAWNED_AT + 3.0, None, True), (5002, _SPAWNED_AT - 60.0, None, True)],
            True,
        ),
    )

    out = jobs.kill(_identity_record())

    assert no_stray_signal == []
    assert out["reason_code"] == jobs.KILL_GROUP_PREDATES_RUN
    # Observation, not inferred history: a member's age is what was measured.
    assert "started before this run did" in out["reason"]
    assert "reused" not in out["reason"]


def test_kill_decides_a_dead_leaders_group_without_reading_the_leader_again(
    sandbox, monkeypatch, no_stray_signal
):
    """Guards a precondition the group branch depends on.

    The liveness probe reaps an exited child, and the OS is then free to hand
    that pid to an unrelated process. So once the leader reads as gone, its pid
    is a number that no longer describes anything, and the group decision is
    taken from the group's own members instead. Nothing here fails today; it
    fails the moment someone reintroduces a leader read into this branch, which
    would otherwise go unnoticed and be wrong only intermittently.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(
        jobs,
        "_process_create_time",
        lambda pid: pytest.fail(f"pid {pid} may have been reaped and reused"),
    )
    monkeypatch.setattr(
        jobs, "_live_group_members", lambda pgid: ([(5001, _SPAWNED_AT + 3.0, rid, True)], True)
    )

    rid = _identity_record()
    out = jobs.kill(rid)

    assert out["killed"] is True and no_stray_signal == [(7777, signal.SIGTERM)]


@pytest.mark.parametrize(
    ("members", "expected_code"),
    [
        # A member older than the run: this group number was reused. Settled —
        # a retry reads the same thing.
        (([(5001, _SPAWNED_AT - 60.0, None, True)], True), jobs.KILL_GROUP_PREDATES_RUN),
        # The scan could not read every candidate, so a member may be unseen.
        # A measurement that failed, and a retry may answer.
        (([(5001, _SPAWNED_AT + 3.0, None, True)], False), jobs.KILL_GROUP_SCAN_INCOMPLETE),
        (([], False), jobs.KILL_GROUP_SCAN_INCOMPLETE),
    ],
)
def test_kill_refuses_a_group_it_cannot_confirm(
    sandbox, monkeypatch, no_stray_signal, members, expected_code
):
    """A pgid is a pid number and is reused like one.

    With the leader gone, the recorded group number alone licenses nothing: an
    accurate refusal is the outcome being aimed at here, not the largest number
    of processes stopped. Each refusal carries its own code, because "this group
    is another run's" and "the inspection did not finish" are different news to a
    caller deciding whether to try again.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(jobs, "_live_group_members", lambda pgid: members)

    out = jobs.kill(_identity_record())

    assert no_stray_signal == []
    assert out["killed"] is False and out["reason_code"] == expected_code


def test_kill_reports_a_group_with_nothing_live_left_in_it(sandbox, monkeypatch, no_stray_signal):
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(jobs, "_live_group_members", lambda pgid: ([], True))

    out = jobs.kill(_identity_record())

    assert no_stray_signal == []
    assert out["killed"] is False and out["reason_code"] == jobs.KILL_GROUP_GONE
    assert "already exited" in out["reason"]


def test_kill_reaps_a_live_group_behind_a_terminal_record(sandbox, monkeypatch, no_stray_signal):
    """A recorded end does not mean the work stopped, and reaping it is the point.

    The notify hook marks the record terminal when the run reports its status;
    processes it left in its group can still be running. Once that group is
    confirmed to be this run's, it is signalled — and the recorded end survives,
    because how the run came out is not the same fact as how its stragglers were
    cleaned up.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(
        jobs, "_live_group_members", lambda pgid: ([(5001, _SPAWNED_AT + 3.0, rid, True)], True)
    )

    rid = _identity_record(status="completed", finished_at="2026-01-01T00:00:00+00:00")
    out = jobs.kill(rid)

    assert no_stray_signal == [(7777, signal.SIGTERM)]
    assert out["killed"] is True
    after = jobs._read_job(rid)
    assert after["status"] == "completed"
    assert after["finished_at"] == "2026-01-01T00:00:00+00:00"
    assert after["group_reaped_at"] is not None


def test_kill_refuses_a_terminal_record_whose_group_is_unconfirmable(
    sandbox, monkeypatch, no_stray_signal
):
    """The refusal says identity is unverified, not that reuse is certain."""
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(
        jobs, "_live_group_members", lambda pgid: ([(5001, _SPAWNED_AT - 60, None, True)], True)
    )

    rid = _identity_record(status="completed", finished_at="2026-01-01T00:00:00+00:00")
    out = jobs.kill(rid)

    assert no_stray_signal == []
    assert out["reason_code"] == jobs.KILL_GROUP_PREDATES_RUN
    assert "could not be confirmed" in out["reason"]
    assert jobs._read_job(rid)["status"] == "completed"


@pytest.mark.parametrize("bad_pid", [None, 0, 1, "4242"])
def test_kill_never_dereferences_a_pid_it_must_not_signal(
    sandbox, monkeypatch, no_stray_signal, bad_pid
):
    """0 is the caller's own process group to killpg and 1 is init.

    Refused before any probe, and before the identity fields are read: a record
    carrying a placeholder must not reach a group signal by any route, including
    the ones this change added.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: pytest.fail("pid must not be probed"))

    rid = jobs.new_run_id()
    jobs._write_job(
        {
            "run_id": rid,
            "pid": bad_pid,
            "pid_create_time": _SPAWNED_AT,
            "pgid": 7777,
            "kind": "agent",
            "status": "running",
            "log": None,
        }
    )
    out = jobs.kill(rid)

    assert no_stray_signal == []
    assert out["killed"] is False and out["reason_code"] == jobs.KILL_NO_PID


@pytest.mark.parametrize(
    "record",
    # No identity keys at all. A record that carries the keys and holds bad values
    # is a different observation and gets its own answer.
    [{}],
)
def test_kill_refuses_a_record_that_cannot_confirm_an_identity(
    sandbox, monkeypatch, no_stray_signal, record
):
    """A record with no usable identity is refused rather than signalled.

    Deriving a group from the pid at this point is the pid-reuse step the identity
    fields exist to remove: the pid may have been handed to an unrelated process
    since, and its group would then be a stranger's. Nothing about the process is
    probed either — a liveness answer would not distinguish the two cases, so the
    refusal does not depend on one.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: pytest.fail("pid must not be probed"))
    monkeypatch.setattr(
        jobs,
        "_live_group_members",
        lambda pgid: pytest.fail("a record without an identity has no group to verify"),
    )

    rid = jobs.new_run_id()
    jobs._write_job(
        {
            "run_id": rid,
            "pid": 4242,
            "kind": "agent",
            "status": "running",
            "log": None,
            **record,
        }
    )
    out = jobs.kill(rid)

    assert no_stray_signal == [], "a pid without an identity must license no signal"
    assert out["killed"] is False and out["reason_code"] == jobs.KILL_NO_RECORDED_IDENTITY
    assert out["pid"] == 4242, "the operator needs the number to reap the group by hand"
    # What was read off the record: both fields missing, so the pid is unusable.
    assert "carries neither a start time nor a process group" in out["reason"]
    assert jobs._read_job(rid)["status"] == "running", "a refusal changes no recorded status"


def test_kill_does_not_date_a_record_that_carries_no_identity(
    sandbox, monkeypatch, no_stray_signal
):
    """Both fields absent is an observation about the record, not about its age.

    This record is written here, seconds ago, by the current writer, with the two
    identity keys removed. The refusal it draws must therefore not tell an operator
    the record was written before those fields were captured: no current writer
    omitting them rules out one origin, and a record altered after it was written
    reads exactly like an old one. The remedies differ — age out a stale record
    versus inspect one that was changed — so the sentence would point the wrong way.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: pytest.fail("pid must not be probed"))

    rid = _identity_record()
    record = jobs._read_job(rid)
    del record["pid_create_time"], record["pgid"]
    jobs._write_job(record)

    out = jobs.kill(rid)

    assert no_stray_signal == []
    assert out["killed"] is False
    assert out["reason_code"] == jobs.KILL_NO_RECORDED_IDENTITY
    assert out["pid"] == 4242, "the operator needs the number to reap the group by hand"
    assert "carries neither a start time nor a process group" in out["reason"]
    for claim in ("written before", "predates", "legacy", "older", "since those fields"):
        assert claim not in out["reason"], f"the refusal must not date the record: {claim!r}"
    assert "legacy" not in out["reason_code"], "the code names the observation, not an origin"


@pytest.mark.parametrize(
    "created",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        True,
        False,
        # A JSON integer has no bound, so a record can carry one that is not a
        # float at all. Converting it to compare it is what fails.
        int("9" * 400),
        -int("9" * 400),
    ],
)
def test_kill_refuses_a_record_whose_start_time_cannot_be_compared(
    sandbox, monkeypatch, no_stray_signal, created
):
    """A start time that cannot act as one says nothing about the pid.

    Each of these satisfies the type check and then fails to act as a start time.
    A NaN is never within tolerance of a live one. A boolean is an int to
    isinstance, so ``true`` becomes 1.0 and compares as a moment in 1970. An
    integer too large for a float cannot be converted at all, and would leave the
    call raising out of a tool that promises a refusal. Each would otherwise have
    the leader reported as a reused pid, or nothing reported at all, and both are
    the wrong answer — nothing has been established about the pid, only that the
    record cannot describe it. The refusal is the same, but the code and the
    reason must not claim otherwise.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: pytest.fail("pid must not be probed"))

    out = jobs.kill(_identity_record(created=created))

    assert no_stray_signal == []
    assert out["killed"] is False
    assert out["reason_code"] == jobs.KILL_IDENTITY_UNUSABLE
    assert "reused" not in out["reason"], "the pid was never established to be anything"
    # The value came off disk and a JSON number has no length limit, so it must not
    # be able to set the size of a reason a caller has to read.
    assert len(out["reason"]) < 400, "a record must not choose how long the answer is"


def _write_raw_record(rid: str, text: str) -> None:
    d = config.job_dir(rid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "job.json").write_text(text)


@pytest.mark.parametrize(
    ("text", "expected_code"),
    [
        # Present, and its bytes cannot be parsed. A retry may read differently.
        ("{", jobs.KILL_RECORD_UNREADABLE),
        ('{"run_id": "x", ', jobs.KILL_RECORD_UNREADABLE),
        # Present, parses cleanly, and is not an object. A retry cannot help.
        ("[]", jobs.KILL_RECORD_WRONG_SHAPE),
        ('"a string"', jobs.KILL_RECORD_WRONG_SHAPE),
        ("null", jobs.KILL_RECORD_WRONG_SHAPE),
        ("42", jobs.KILL_RECORD_WRONG_SHAPE),
    ],
)
def test_kill_refuses_a_damaged_record_without_calling_it_absent(
    sandbox, no_stray_signal, text, expected_code
):
    """A file that is present and unusable is not a run that never existed.

    Both were reported as "no such job", which tells an operator to stop looking
    for a record that is sitting on disk — and two of these shapes did not refuse
    at all, they raised out of the call. The refusal now says which of the two
    happened, and says the file is the thing to look at.
    """
    rid = jobs.new_run_id()
    _write_raw_record(rid, text)

    out = jobs.kill(rid)

    assert no_stray_signal == []
    assert out["killed"] is False
    assert out["reason_code"] == expected_code
    assert "no such job" not in out["reason"]
    assert rid in out["reason"]


@pytest.mark.parametrize("text", ["[]", '"a string"', "42", "{"])
def test_every_surface_survives_a_record_it_cannot_use(sandbox, text):
    """The record is read by more than one verb, so one of them refusing is not enough.

    A JSON value that is not an object used to reach ``.get()`` on whichever surface
    read it, so status and output raised as readily as kill did — and status is what
    an observer polls. Each must return something a caller can read.
    """
    rid = jobs.new_run_id()
    _write_raw_record(rid, text)

    assert jobs.kill(rid)["killed"] is False
    assert jobs.status(rid)["run_id"] == rid
    assert jobs.output(rid)["run_id"] == rid
    assert isinstance(jobs.list_jobs(), list)


@pytest.mark.parametrize(
    ("text", "expected_state"),
    [
        ("{", "unreadable"),
        ('{"run_id": "x", ', "unreadable"),
        ("[]", "wrong_shape"),
        ("null", "wrong_shape"),
    ],
)
def test_the_read_surfaces_name_a_damaged_record_rather_than_an_unknown_run(
    sandbox, text, expected_state
):
    """kill() tells a damaged record from an absent one; the readers did not.

    They dropped the state the read established and answered from the record
    being None, so a file sitting on disk came back as ``known: false``, as "no
    such job", and as a wait entry saying the run was not found. An operator
    reading any of those stops looking for a run whose record is right there,
    and the surfaces disagree with kill() about the same file.

    Each assertion below carries an id with no record at all beside it, so the
    values are shown to distinguish the two cases rather than merely to exist.
    """
    rid = jobs.new_run_id()
    _write_raw_record(rid, text)
    absent = jobs.new_run_id()

    st = jobs.status(rid)
    assert st["known"] is False, "no usable record was obtained, and that has not changed"
    assert st["record_state"] == expected_state
    assert jobs.status(absent)["record_state"] == "absent"

    got = jobs.output(rid)
    assert got["known"] is False
    assert got["record_state"] == expected_state
    assert "no such job" not in got["error"]
    assert jobs.output(absent)["error"] == "no such job"

    entry = jobs._wait_entry(rid)
    assert entry["error"]["kind"] == "record_unusable"
    assert jobs._wait_entry(absent)["error"]["kind"] == "not_found"

    listed = {j["run_id"]: j["record_state"] for j in jobs.list_jobs()}
    assert listed == {rid: expected_state}


def test_a_usable_record_reads_back_as_one(sandbox, monkeypatch):
    """The control for the four cases above.

    They assert that a damaged record is named as damaged. A reader that named
    every record damaged would satisfy all of them, and would fail this.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    rid = _identity_record()

    assert jobs.status(rid)["record_state"] == "ok"
    assert jobs.status(rid)["known"] is True
    assert jobs.output(rid)["record_state"] == "ok"
    assert jobs._wait_entry(rid)["error"] is None
    assert [j["record_state"] for j in jobs.list_jobs()] == ["ok"]


def test_a_readable_record_is_still_read(sandbox, monkeypatch, no_stray_signal):
    """Guards the precondition every refusal above depends on.

    All of this rests on the reader admitting an ordinary record unchanged. If the
    shape check ever rejected one, every surface would degrade to a refusal and the
    tests for the damaged cases would keep passing, because they only ever assert
    that a refusal happened.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(
        jobs, "_live_group_members", lambda pgid: ([(5001, _SPAWNED_AT + 3.0, rid, True)], True)
    )

    rid = _identity_record()

    assert jobs._read_job(rid)["pgid"] == 7777
    assert jobs.kill(rid)["killed"] is True
    assert no_stray_signal == [(7777, signal.SIGTERM)]


def _scan_one_candidate(monkeypatch, pgid, create_times, marker):
    """Drive a real group scan over a single invented pid.

    The process table yields one candidate whose group matches; every read of
    that pid is answered from *create_times* in call order, so a caller can make
    the pid change identity partway through the scan without a real race.

    Substituted on ``lionagi.ln._proc``, where the group scan resolves them, and
    not on this module: patching the job surface's own wrappers would leave the
    scan reading the real process table and the substitution would cover nothing
    it was written to cover.
    """
    import psutil

    monkeypatch.setattr(psutil, "pids", lambda: [5001])
    monkeypatch.setattr(jobs.os, "getpgid", lambda pid: pgid)
    reads = iter(create_times)
    monkeypatch.setattr(
        _proc, "process_create_time", lambda pid: ("found", next(reads, create_times[-1]))
    )
    monkeypatch.setattr(_proc, "process_marker", lambda pid, marker_var: ("found", marker))


def test_a_member_that_changes_identity_mid_scan_does_not_identify_the_group(
    sandbox, monkeypatch, no_stray_signal
):
    """A marker and the pid it was read from have to be the same process.

    Membership, start time and marker are three reads addressed by pid, and a
    pid the OS reassigns between them answers the later ones as the replacement.
    A replacement carrying this run's id — a descendant moved into another group,
    say — would then license a signal to a group no live member of which was ever
    shown to be this run's. The scan has to notice that the identity moved, and
    report itself incomplete rather than answer for the group.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    rid = _identity_record()
    # The first read describes the member; every later one describes whoever
    # holds the pid now. The marker belongs to that second process.
    _scan_one_candidate(monkeypatch, 7777, [_SPAWNED_AT + 3.0, _SPAWNED_AT + 90.0], rid)

    out = jobs.kill(rid)

    assert no_stray_signal == [], "evidence from two processes identifies neither"
    assert out["killed"] is False
    assert out["reason_code"] == jobs.KILL_GROUP_SCAN_INCOMPLETE


def test_a_member_that_changes_identity_mid_scan_cannot_lose_the_start_time_exclusion(
    sandbox, monkeypatch, no_stray_signal
):
    """The same composition, on the rule that refuses rather than the one that allows.

    A member older than the run rules the group out. Read the start time off a
    younger process that has since taken the pid and the exclusion disappears,
    which is the direction that costs something: the group stops being refused
    for a reason the scan can no longer see. An unpinnable member makes the scan
    incomplete, so what is reported is a measurement that did not finish.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    rid = _identity_record()
    # A young replacement read first, the original older member read second.
    _scan_one_candidate(monkeypatch, 7777, [_SPAWNED_AT + 3.0, _SPAWNED_AT - 60.0], None)

    out = jobs.kill(rid)

    assert no_stray_signal == []
    assert out["killed"] is False
    assert out["reason_code"] == jobs.KILL_GROUP_SCAN_INCOMPLETE


def test_a_member_whose_environment_is_closed_still_counts_as_a_member(
    sandbox, monkeypatch, no_stray_signal
):
    """An unreadable environment withholds a marker; it does not hide a member.

    A process this user cannot introspect is still a process the scan saw: its
    pid, group and start time all answered. Counting it is what keeps the group
    from being answered for as gone and signalled away, so it is reported as a
    member — one whose marker is unread rather than absent, which is why the
    refusal is the retryable one.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    rid = _identity_record()
    _scan_one_candidate(monkeypatch, 7777, [_SPAWNED_AT + 3.0], None)
    monkeypatch.setattr(_proc, "process_marker", lambda pid, marker_var: ("unknown", None))

    members, complete = jobs._live_group_members(7777)
    out = jobs.kill(rid)

    assert no_stray_signal == []
    assert [pid for pid, _, _, _ in members] == [5001], "the member was seen, so it counts"
    assert complete, "a member that was seen opens no gap in the membership"
    assert out["reason_code"] == jobs.KILL_GROUP_SCAN_INCOMPLETE


def test_an_unread_marker_is_not_reported_as_a_group_carrying_none(
    sandbox, monkeypatch, no_stray_signal
):
    """The two ways of coming back without a marker are different news.

    A member whose environment was read and holds no marker settles the group:
    every retry reads the same nothing, and only an operator can take it further.
    A member whose environment would not open settles nothing — the marker it
    withheld is one the next call may well read. Reported as the same answer,
    the second tells an operator to stop retrying on a measurement that never
    came off.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    rid = _identity_record()

    _scan_one_candidate(monkeypatch, 7777, [_SPAWNED_AT + 3.0], None)
    read_and_absent = jobs.kill(rid)

    monkeypatch.setattr(_proc, "process_marker", lambda pid, marker_var: ("unknown", None))
    could_not_read = jobs.kill(rid)

    assert no_stray_signal == []
    assert read_and_absent["reason_code"] == jobs.KILL_GROUP_OWNERSHIP_UNPROVEN
    assert could_not_read["reason_code"] == jobs.KILL_GROUP_SCAN_INCOMPLETE
    assert read_and_absent["killed"] is False and could_not_read["killed"] is False


def test_one_readable_marker_still_identifies_a_group_holding_an_unreadable_member(
    sandbox, monkeypatch, no_stray_signal
):
    """Unread markers cost nothing that was reapable before.

    Members share a pgid, so one member reading back this run's id identifies the
    group whatever its neighbours would or would not disclose. The marker rule
    runs ahead of every completeness question for exactly that reason, and a
    group with one process this user cannot introspect is still reaped.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    rid = _identity_record()
    monkeypatch.setattr(
        jobs,
        "_live_group_members",
        lambda pgid: (
            [(5001, _SPAWNED_AT + 1.0, None, False), (5002, _SPAWNED_AT + 2.0, rid, True)],
            True,
        ),
    )

    out = jobs.kill(rid)

    assert no_stray_signal == [(7777, signal.SIGTERM)]
    assert out["killed"] is True and out["reason_code"] == jobs.KILL_SIGNALLED


def test_a_member_older_than_the_run_still_excludes_past_an_unread_marker(
    sandbox, monkeypatch, no_stray_signal
):
    """Unread markers do not displace the refusal the start time already reaches.

    A member that was running before this run started rules the group out, and
    that is a settled fact about a process the scan pinned. It keeps its own code
    ahead of any question about what a neighbour's environment would disclose.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(
        jobs,
        "_live_group_members",
        lambda pgid: (
            [(5001, _SPAWNED_AT + 3.0, None, False), (5002, _SPAWNED_AT - 60.0, None, True)],
            True,
        ),
    )

    out = jobs.kill(_identity_record())

    assert no_stray_signal == []
    assert out["killed"] is False
    assert out["reason_code"] == jobs.KILL_GROUP_PREDATES_RUN


def test_every_surface_survives_a_record_it_cannot_get_at(sandbox, monkeypatch, no_stray_signal):
    """A directory that cannot be searched is not a run that does not exist.

    Asking whether the file is there and then reading it answers two questions,
    and the first one cannot fail: a path under a directory with no search
    permission is neither present nor absent to it. Reading directly is what
    tells the two apart, and every surface that reads a record has to come back
    with an answer rather than the errno.
    """
    if os.geteuid() == 0:
        pytest.skip("root searches a directory whose mode denies it, so the case cannot be set up")

    rid = _identity_record()
    job_dir = config.job_dir(rid)
    os.chmod(job_dir, 0o000)
    try:
        try:
            (job_dir / "job.json").read_text()
        except PermissionError:
            pass
        else:
            pytest.skip("this filesystem does not enforce directory search permission")

        out = jobs.kill(rid)
        assert no_stray_signal == []
        assert out["killed"] is False
        assert out["reason_code"] == jobs.KILL_RECORD_UNREADABLE
        assert "could not be read" in out["reason"]

        st = jobs.status(rid)
        assert st["known"] is False

        got = jobs.output(rid)
        assert got["known"] is False
    finally:
        os.chmod(job_dir, 0o700)


def test_every_surface_survives_a_run_directory_it_cannot_get_at(sandbox, monkeypatch):
    """The same rule, for the run's own directory rather than the job record.

    The log, the artifacts and the run manifest live under the CLI's run
    directory, which the job record only points at. So a readable record can name
    a directory this process cannot search, and asking whether those paths exist
    raises there exactly as it does for the record.

    The manifest is read before the log tail, so a fix confined to the tail would
    leave all three surfaces raising on the manifest instead. This asserts on the
    surfaces rather than on any one read, which is what makes it notice that.
    """
    if os.geteuid() == 0:
        pytest.skip("root searches a directory whose mode denies it, so the case cannot be set up")

    rid = jobs.new_run_id()
    run_dir = config.run_dir(rid)
    (run_dir / "artifacts").mkdir(parents=True)
    (run_dir / "artifacts" / "a.txt").write_text("x")
    (run_dir / "run.json").write_text(f'{{"run_id": "{rid}"}}')
    log = run_dir / "job.log"
    log.write_text("a line of log\n")
    jobs._write_job(
        {"run_id": rid, "pid": None, "kind": "agent", "status": "running", "log": str(log)}
    )

    # The control arm has to reach all three, or the permission arm below proves
    # nothing: a surface that never opened the log cannot demonstrate surviving
    # an unreadable one.
    st = jobs.status(rid)
    assert st["known"] is True
    assert st["log_tail"] == "a line of log\n"
    assert st["run"] == {"run_id": rid}
    assert jobs.output(rid)["artifacts"] == ["a.txt"]
    assert jobs.output(rid)["artifacts_state"] == "ok"
    assert [j["run_id"] for j in jobs.list_jobs()] == [rid]

    os.chmod(run_dir, 0o000)
    try:
        try:
            log.read_text()
        except PermissionError:
            pass
        else:
            pytest.skip("this filesystem does not enforce directory search permission")

        st = jobs.status(rid)
        assert st["known"] is True, "the record is readable; only what it points at is not"
        assert st["log_tail"] is None
        assert st["run"] is None

        got = jobs.output(rid)
        assert got["known"] is True
        assert got["console"] is None
        # The empty list is not the answer here — it is what "no artifacts" looks
        # like, and this run wrote one. The state is what carries the difference,
        # and without it the caller is told the run produced nothing.
        assert got["artifacts_state"] == "unreadable"

        assert [j["run_id"] for j in jobs.list_jobs()] == [rid]
    finally:
        os.chmod(run_dir, 0o700)


# Each entry is a way a record on disk can be damaged. They are listed together
# because the reader's contract is one claim about all of them — a record that
# cannot be used is reported as unusable — and because the ways bytes fail are
# not guessable in advance. Four of these were found one at a time, each after
# the previous had been called handled. A new shape belongs in this list.
DAMAGED_RECORDS = [
    pytest.param(b'{"run_id": "truncated"', "unreadable", id="truncated-json"),
    pytest.param(b'\xff\xfe{"run_id": "x"}', "unreadable", id="not-utf8"),
    pytest.param(b"[" * 10000, "unreadable", id="nested-past-the-decoder-stack"),
    pytest.param(b'"a string, not an object"', "wrong_shape", id="json-but-not-an-object"),
    pytest.param(b"[1, 2, 3]", "wrong_shape", id="json-array"),
    pytest.param(b"", "unreadable", id="empty-file"),
]


@pytest.mark.parametrize("payload,expected_state", DAMAGED_RECORDS)
def test_a_damaged_record_is_reported_as_damaged_by_every_surface(sandbox, payload, expected_state):
    """One contract, asserted at the surfaces rather than at the reader.

    The reader promises that a record which cannot be used is reported as
    unusable, never raised. Asserting that at the helper would miss what makes it
    matter: four public surfaces share this reader, and the listing in particular
    promises that one damaged record does not cost the caller the runs beside it.
    An exception out of the shared reader breaks that promise for every run at
    once, so a healthy sibling record is part of every case here.
    """
    ok_rid = jobs.new_run_id()
    jobs._write_job({"run_id": ok_rid, "pid": None, "kind": "agent", "status": "running"})

    bad_rid = jobs.new_run_id()
    bad_dir = config.job_dir(bad_rid)
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "job.json").write_bytes(payload)

    st = jobs.status(bad_rid)
    assert st["record_state"] == expected_state
    assert st["known"] is False
    assert jobs.output(bad_rid)["record_state"] == expected_state
    assert jobs.kill(bad_rid)["killed"] is False

    listed = {j["run_id"]: j.get("record_state") for j in jobs.list_jobs()}
    assert listed[bad_rid] == expected_state
    assert listed[ok_rid] == "ok", "one damaged record must not cost the runs beside it"


def test_a_read_failure_nobody_anticipated_is_still_an_unusable_record(sandbox, monkeypatch):
    """The classification is total, and this is what says so.

    The table above covers the damage shapes that are known. The reader's promise
    is stronger than that list: it says every way the read can fail yields a state,
    and a promise about shapes nobody has thought of cannot be tested by naming
    more of them. So the failure here is deliberately one that means nothing --
    what is being checked is that the reader classifies rather than recognises.
    """
    rid = jobs.new_run_id()
    jobs._write_job({"run_id": rid, "pid": None, "kind": "agent", "status": "running"})
    assert jobs.status(rid)["record_state"] == "ok", "control: the record reads before patching"

    class UnanticipatedFailure(Exception):
        pass

    real_read_text = Path.read_text

    def failing_read_text(self, *a, **kw):
        if self.name == "job.json":
            raise UnanticipatedFailure("nothing in the reader has heard of this")
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", failing_read_text)

    assert jobs.status(rid)["record_state"] == "unreadable"
    assert jobs.output(rid)["record_state"] == "unreadable"
    assert jobs.kill(rid)["reason_code"] == jobs.KILL_RECORD_UNREADABLE
    assert [j["record_state"] for j in jobs.list_jobs()] == ["unreadable"]


def test_a_run_manifest_of_invalid_bytes_does_not_escape_the_surface_that_reads_it(sandbox):
    """The manifest is advisory, which is an argument about its value and not a
    licence to raise. A read that returns nothing costs the caller one display
    field; a read that raises costs them the whole surface.
    """
    rid = jobs.new_run_id()
    jobs._write_job({"run_id": rid, "pid": None, "kind": "agent", "status": "running"})
    manifest = config.run_manifest(rid)
    manifest.parent.mkdir(parents=True, exist_ok=True)

    manifest.write_text('{"run_id": "readable"}')
    assert jobs.status(rid)["run"] == {"run_id": "readable"}, "control: the manifest is read"

    manifest.write_bytes(b"\xff\xfe not utf-8")
    assert jobs.status(rid)["run"] is None
    assert jobs.status(rid)["known"] is True, "the record is fine; only the manifest is not"


def test_a_manifest_read_failure_nobody_anticipated_still_costs_only_the_manifest(
    sandbox, monkeypatch
):
    """The manifest reader makes the same total promise the record reader does, so
    it is checked the same way: with a failure that means nothing, which no list of
    exception types could have named in advance.

    The failure is aimed at the manifest alone. The record is read on the same call
    and must survive, because the claim is not that the surface tolerates damage
    generally -- it is that a manifest nobody can read costs the caller that one
    field and nothing else.
    """
    rid = jobs.new_run_id()
    jobs._write_job({"run_id": rid, "pid": None, "kind": "agent", "status": "running"})
    manifest = config.run_manifest(rid)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text('{"run_id": "readable"}')
    assert jobs.status(rid)["run"] == {"run_id": "readable"}, "control: the manifest is read"

    class UnanticipatedFailure(Exception):
        pass

    real_read_text = Path.read_text

    def failing_read_text(self, *a, **kw):
        if self.name == "run.json":
            raise UnanticipatedFailure("nothing in the reader has heard of this")
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", failing_read_text)

    answer = jobs.status(rid)
    assert answer["run"] is None
    assert answer["record_state"] == "ok", "the record was reachable and still is"
    assert answer["known"] is True


def test_an_artifacts_directory_that_denies_its_own_read_is_not_a_run_that_wrote_nothing(
    sandbox,
):
    """The failing read that does not raise.

    Denying the search bit on a *parent* makes the traversal raise, and the test
    above covers that. Denying read on the artifacts directory *itself* does not
    raise: the walk simply yields nothing. So a traversal that learns about failure
    only by catching exceptions reports this run as having written no artifacts,
    and reports that answer as complete.

    That is the same conflation the state field exists to remove, surviving inside
    the field meant to remove it, which is worse than the ambiguity it replaced: a
    caller reading no artifacts and a state of ok has been told the read succeeded.
    """
    if os.geteuid() == 0:
        pytest.skip("root reads a directory whose mode denies it, so the case cannot be set up")

    rid = jobs.new_run_id()
    adir = config.run_dir(rid) / "artifacts"
    adir.mkdir(parents=True)
    (adir / "a.txt").write_text("x")
    jobs._write_job({"run_id": rid, "pid": None, "kind": "agent", "status": "running"})

    # Control: the artifact is found while the directory is readable. Without it,
    # an empty list below could mean the fixture never wrote anything.
    assert jobs.output(rid)["artifacts"] == ["a.txt"]
    assert jobs.output(rid)["artifacts_state"] == "ok"

    os.chmod(adir, 0o000)
    try:
        try:
            list(adir.iterdir())
        except PermissionError:
            pass
        else:
            pytest.skip("this filesystem does not enforce directory read permission")

        got = jobs.output(rid)
        assert got["known"] is True
        assert got["artifacts_state"] == "unreadable"
    finally:
        os.chmod(adir, 0o700)


def test_an_entry_whose_metadata_is_out_of_reach_does_not_escape_the_listing(sandbox):
    """A directory can be listable and not searchable at the same time.

    Read permission gets the walk the names; search permission is what it takes
    to stat them. With one and not the other, an entry arrives from the walk and
    then fails inspection, which is a per-entry failure the directory-level error
    callback never sees. The traversal is short by that entry, which is what the
    state says, and the entries beside it are still true.
    """
    if os.geteuid() == 0:
        pytest.skip("root searches a directory whose mode denies it, so the case cannot be set up")

    rid = jobs.new_run_id()
    adir = config.run_dir(rid) / "artifacts"
    adir.mkdir(parents=True)
    (adir / "a.txt").write_text("x")
    jobs._write_job({"run_id": rid, "pid": None, "kind": "agent", "status": "running"})

    assert jobs.output(rid)["artifacts"] == ["a.txt"], "control: readable and searchable"
    assert jobs.output(rid)["artifacts_state"] == "ok"

    os.chmod(adir, 0o600)  # readable, not searchable
    try:
        try:
            os.stat(adir / "a.txt")
        except PermissionError:
            pass
        else:
            pytest.skip("this filesystem does not enforce directory search permission")

        got = jobs.output(rid)
        assert got["artifacts_state"] == "unreadable"
        assert got["known"] is True, "only the traversal fell short"
    finally:
        os.chmod(adir, 0o755)


def test_a_partly_readable_artifact_tree_returns_what_it_reached(sandbox):
    """A read that fails part way through still found real files.

    Discarding them would trade one false answer for another: the run did write
    these, and saying so costs nothing. The state carries the fact that the list is
    short, so the caller can tell a complete listing from a truncated one without
    losing the part that was read.
    """
    if os.geteuid() == 0:
        pytest.skip("root reads a directory whose mode denies it, so the case cannot be set up")

    rid = jobs.new_run_id()
    adir = config.run_dir(rid) / "artifacts"
    locked = adir / "locked"
    locked.mkdir(parents=True)
    (adir / "top.txt").write_text("x")
    (locked / "hidden.txt").write_text("y")
    jobs._write_job({"run_id": rid, "pid": None, "kind": "agent", "status": "running"})

    assert jobs.output(rid)["artifacts"] == ["locked/hidden.txt", "top.txt"]

    os.chmod(locked, 0o000)
    try:
        try:
            list(locked.iterdir())
        except PermissionError:
            pass
        else:
            pytest.skip("this filesystem does not enforce directory read permission")

        got = jobs.output(rid)
        assert got["artifacts"] == ["top.txt"], "what was reached is still true"
        assert got["artifacts_state"] == "unreadable", "and the listing is known to be short"
    finally:
        os.chmod(locked, 0o700)


def test_listing_a_jobs_directory_it_cannot_get_at_is_not_an_empty_listing(sandbox):
    """An unsearchable jobs directory is not an empty one.

    A listing has no field in which to say the read failed, so answering the empty
    list says "there are no jobs at all" about a directory nobody could look in.
    The read is allowed to fail instead, and the surface turns that into a per-op
    error rather than into a wrong answer.
    """
    if os.geteuid() == 0:
        pytest.skip("root searches a directory whose mode denies it, so the case cannot be set up")

    rid = _identity_record()
    assert [j["run_id"] for j in jobs.list_jobs()] == [rid]

    os.chmod(config.JOBS_DIR, 0o000)
    try:
        try:
            list(config.JOBS_DIR.iterdir())
        except PermissionError:
            pass
        else:
            pytest.skip("this filesystem does not enforce directory search permission")

        with pytest.raises(OSError):
            jobs.list_jobs()

        import asyncio

        from lionagi.mcp import dispatch

        out = asyncio.run(dispatch.request([{"op": "job.list"}]))["ops"][0]
        assert out["ok"] is False
        assert out["error"]["kind"] == "internal"
    finally:
        os.chmod(config.JOBS_DIR, 0o700)


def test_listing_jobs_before_any_job_is_written_is_empty(sandbox):
    """A jobs directory that is not there is no jobs, and says so.

    Nothing creates it until the first record is written, so the fresh case has to
    stay an ordinary empty listing rather than becoming an error about a read.
    """
    assert not config.JOBS_DIR.exists()
    assert jobs.list_jobs() == []


@pytest.mark.parametrize(
    "identity",
    [
        {"pid_create_time": "not-a-number", "pgid": 7777},
        {"pid_create_time": _SPAWNED_AT, "pgid": "7777"},
        {"pid_create_time": _SPAWNED_AT, "pgid": 0},
        {"pid_create_time": None, "pgid": None},
        # Half-written: the keys exist because the writer knows about them.
        {"pid_create_time": _SPAWNED_AT},
        {"pgid": 7777},
    ],
)
def test_kill_does_not_call_a_damaged_identity_an_absent_one(
    sandbox, monkeypatch, no_stray_signal, identity
):
    """Present-but-wrong is not the same news as absent, and must not borrow its sentence.

    A record carrying the keys was written by something that knows about them, so
    what an operator has to look at is the value it wrote — which is a different
    thing to look at than a record that names no identity at all. Neither refusal
    dates the record.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: pytest.fail("pid must not be probed"))

    rid = jobs.new_run_id()
    jobs._write_job(
        {"run_id": rid, "pid": 4242, "kind": "agent", "status": "running", "log": None, **identity}
    )
    out = jobs.kill(rid)

    assert no_stray_signal == []
    assert out["killed"] is False
    assert out["reason_code"] == jobs.KILL_IDENTITY_UNUSABLE
    assert "predates" not in out["reason"], "the record is not old, it is damaged"
    assert out["pid"] == 4242


def _process_table_enumerable() -> tuple[bool, str]:
    """Whether this machine lets us list the process table at all.

    The group-identity rules are built on enumerating processes, and some
    sandboxes refuse it outright: ``psutil.pids()`` raises, ``_live_group_members``
    correctly reports an incomplete scan, and a test that needs a real measurement
    has nothing to measure. That is an environment that cannot run the check, not
    a regression, and it must not read like one. Only the enumeration itself is
    probed — a machine that can list processes and still gets the wrong answer
    fails, which is the case worth failing on.
    """
    import psutil

    try:
        psutil.pids()
    except (psutil.Error, OSError) as e:
        return False, f"this environment cannot enumerate the process table: {e!r}"
    return True, ""


_CAN_ENUMERATE, _NO_ENUMERATION_REASON = _process_table_enumerable()


@pytest.mark.skipif(not _CAN_ENUMERATE, reason=_NO_ENUMERATION_REASON or "process table readable")
def test_a_group_outlives_its_leader_and_is_reaped_by_identity(sandbox):
    """End to end against real processes: the defect, and the fix, unmocked.

    A leader that backgrounds a child and exits leaves the child running in the
    group it created. Nothing is mocked here: the record carries the identity
    submit() records, the leader is started with the run id in its environment
    exactly as submit() starts one, the leader really exits and is really reaped,
    and the group is enumerated from the OS. The surviving child inherited the
    marker, which is what identifies the group once its leader is gone.

    The survivor is an interpreter rather than a shell utility on purpose. macOS
    does not disclose the environment of its own protected system binaries, so a
    ``sleep`` left in the group would read back as carrying no marker and the run
    would be unidentifiable for a reason that has nothing to do with this code.
    A `li` worker is an interpreter, and this stays faithful to that.
    """
    import shlex
    import subprocess
    import sys
    import time

    rid = jobs.new_run_id()
    survivor = f'{shlex.quote(sys.executable)} -c "import time; time.sleep(30)"'
    proc = subprocess.Popen(  # noqa: S603,S607
        ["sh", "-c", f"{survivor} & sleep 0.5"],
        env={**os.environ, config.JOB_MARKER_ENV_VAR: rid},
        start_new_session=True,
    )
    # start_new_session, so the group is the leader's own pid. Held before
    # anything that can raise, so the cleanup below always has a group to reap.
    pgid = proc.pid
    try:
        state, created = jobs._process_create_time(proc.pid)
        assert state == "found" and created is not None
        assert jobs._spawned_pgid(proc.pid) == pgid

        proc.wait(timeout=10)  # the leader exits and is reaped; the child runs on
        assert jobs._pid_alive(proc.pid) is False

        members, complete = jobs._live_group_members(pgid)
        assert complete and members, "the child outlived its leader in the group"

        _identity_record(pid=proc.pid, pgid=pgid, created=created, run_id=rid)
        out = jobs.kill(rid, signal.SIGKILL)

        assert out["killed"] is True and out["reason_code"] == jobs.KILL_SIGNALLED
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not jobs._live_group_members(pgid)[0]:
                break
            time.sleep(0.05)
        assert jobs._live_group_members(pgid) == ([], True)
    finally:
        # Whatever the assertions did, this test's own group leaves nothing behind.
        if pgid > 1:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


@pytest.mark.parametrize("cli_status", ["timed_out", "cancelled", "aborted", "completed_empty"])
def test_mark_terminal_records_cli_status_verbatim(sandbox, monkeypatch, cli_status):
    """The CLI's terminal status is authoritative and recorded verbatim.

    The CLI spells a timeout ``timed_out`` (agent/flow) and also emits
    ``cancelled`` / ``aborted`` / ``completed_empty`` — none of which mean
    success. A prior version matched against a local set and fell through to
    ``completed`` on a miss, so a timed-out run reported success. Each real
    terminal status must round-trip unchanged.
    """
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)

    rid = jobs.submit("agent", [], prompt="x")["run_id"]
    jobs.mark_terminal(rid, cli_status)

    assert jobs._read_job(rid)["status"] == cli_status
    assert jobs.status(rid)["status"] == cli_status


def test_submit_preserves_terminal_recorded_during_spawn(sandbox, monkeypatch):
    """A terminal recorded in the spawn window is not clobbered back to running.

    submit() persists the record before spawning, so the child's --notify hook
    can mark it terminal immediately; the post-spawn write must only attach the
    pid, never reset the status the hook set.
    """

    def racing_popen(argv, **kw):
        # The child fires its terminal hook the instant it starts. The record
        # already exists (persisted before spawn), so mark_terminal succeeds.
        rid = kw["env"][config.RUN_ID_ENV_VAR]
        jobs.mark_terminal(rid, "failed")
        return _FakeProc(4321)

    monkeypatch.setattr(jobs.subprocess, "Popen", racing_popen)

    res = jobs.submit("agent", [], prompt="x")
    rec = jobs._read_job(res["run_id"])
    assert rec["status"] == "failed"  # terminal survived the pid-attach write
    assert rec["pid"] == 4321  # pid still attached
    assert rec["finished_at"] is not None


def test_mark_terminal_and_list(sandbox, monkeypatch):
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *a, **k: _FakeProc(4242))
    rid = jobs.submit("agent", [], prompt="x")["run_id"]

    job = jobs.mark_terminal(rid, "failed").record
    assert job["status"] == "failed" and job["finished_at"]
    assert job["cli_status"] == "failed"

    listed = jobs.list_jobs()
    assert listed and listed[0]["run_id"] == rid
    assert jobs.list_jobs(status_filter="failed")[0]["run_id"] == rid
    assert jobs.list_jobs(status_filter="running") == []


def test_write_job_publishes_atomically(sandbox, monkeypatch):
    """A failed write leaves the prior record intact, and a success leaves no temp.

    _write_job stages a temp file then os.replace()s it into place, so a reader
    never sees a torn file and a crash mid-write does not corrupt the existing
    record.
    """
    rid = jobs.new_run_id()
    jobs._write_job({"run_id": rid, "status": "running", "pid": 7, "kind": "agent", "log": None})
    good = jobs._read_job(rid)

    # a successful publish renames the temp away — nothing lingers
    assert not list(config.job_dir(rid).glob(".job.json.*.tmp"))

    # simulate a crash during publish: the rename raises after the temp is written
    def boom(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr(jobs.os, "replace", boom)
    with pytest.raises(OSError):
        jobs._write_job({"run_id": rid, "status": "failed", "pid": 7, "kind": "agent", "log": None})

    # the previously published record is untouched — no partial write reached it
    assert jobs._read_job(rid) == good
    # and the failed publish cleaned up its staging file rather than orphaning it
    assert not list(config.job_dir(rid).glob(".job.json.*.tmp"))


def test_status_reports_which_implementation_answered(sandbox, monkeypatch):
    # Two same-named MCP surfaces can expose identical tool lists, and a server
    # imports its code at startup, so neither the tool list nor the file on disk
    # tells a caller which build is answering. The stamp makes it readable.
    monkeypatch.setattr(
        subprocess := __import__("subprocess"), "Popen", lambda *a, **k: _FakeProc()
    )
    handle = jobs.submit("agent", [], prompt="x")

    st = jobs.status(handle["run_id"])

    from lionagi.version import __version__

    assert st["server"]["version"] == __version__
    # The module path is the one actually imported, not a configured guess.
    assert st["server"]["module"] == str(Path(jobs.__file__).resolve().parent)


def test_status_stamp_survives_an_unreadable_version(sandbox, monkeypatch):
    # Identity is diagnostic; a status read must never fail for want of it.
    monkeypatch.setattr(
        subprocess := __import__("subprocess"), "Popen", lambda *a, **k: _FakeProc()
    )
    handle = jobs.submit("agent", [], prompt="x")
    real_import = builtins.__import__

    def boom(name, *args, **kwargs):
        if name == "lionagi.version":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", boom)
    st = jobs.status(handle["run_id"])

    assert st["server"]["version"] == "unknown"
    assert st["status"]  # the rest of the read is unaffected


def test_oversized_flow_prompt_is_refused_before_a_record_exists(sandbox):
    """A prompt too big for the argument vector must fail before anything is recorded.

    flow and fanout pass the instruction as a positional argument, so a large one
    hits the OS exec limit. If that surfaced from the spawn, the job record would
    already be on disk and would sit at "running" forever for a run that never
    started.
    """
    limit = os.sysconf("SC_ARG_MAX")
    huge = "x" * limit

    # Refused for whichever limit it hits first; the point is that it is refused
    # before anything is recorded, not which of the two bounds caught it.
    with pytest.raises(ValueError, match="cannot submit this flow run"):
        jobs.submit("flow", [], prompt=huge)

    # Nothing was recorded, so nothing shows up as a job that never finishes.
    assert jobs.list_jobs() == []


def test_one_oversized_argument_is_refused_where_the_platform_caps_one(sandbox, monkeypatch):
    """A single argument has its own limit on Linux, below the aggregate one.

    Linux caps one exec argument at MAX_ARG_STRLEN regardless of how much
    aggregate room is left, so a flow prompt between that and SC_ARG_MAX would
    otherwise pass the preflight and die in exec after the record was written.
    The cap is forced on here rather than skipped off Linux, so the rule is
    exercised wherever the tests run.
    """
    monkeypatch.setattr(jobs, "_max_single_arg_bytes", lambda: 131072)
    limit = os.sysconf("SC_ARG_MAX")
    one_arg = "x" * 131073
    assert len(one_arg) < limit, "must fit the aggregate limit, or this tests the wrong thing"

    with pytest.raises(ValueError, match="single argument"):
        jobs.submit("flow", [], prompt=one_arg)

    assert jobs.list_jobs() == []


def test_a_platform_without_a_per_argument_cap_is_bounded_only_by_the_total(sandbox, monkeypatch):
    """Where the OS caps only the total, do not invent a per-argument refusal.

    macOS execs a single argument far larger than Linux's MAX_ARG_STRLEN, so
    applying that number there would reject work the OS would have accepted.
    """
    monkeypatch.setattr(jobs, "_max_single_arg_bytes", lambda: None)
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *a, **k: _FakeProc(4242))

    # Comfortably over Linux's per-argument cap, comfortably under the aggregate.
    accepted = jobs.submit("flow", [], prompt="x" * 200_000)

    assert accepted["status"] == "running"


def test_the_per_argument_cap_tracks_the_platform(monkeypatch):
    """Linux derives it from the page size; elsewhere there is none to apply."""
    monkeypatch.setattr(jobs.sys, "platform", "linux")
    assert jobs._max_single_arg_bytes() == 32 * os.sysconf("SC_PAGESIZE")

    monkeypatch.setattr(jobs.sys, "platform", "darwin")
    assert jobs._max_single_arg_bytes() is None


def test_argument_count_is_charged_not_only_bytes():
    """Entries cost a pointer slot each, so counting bytes alone is not enough.

    Constructed so the strings themselves fit the aggregate limit with room to
    spare and only the per-entry pointer cost pushes the invocation over. A
    byte-only estimate with a flat reserve accepts this and then dies in exec.
    """
    limit = os.sysconf("SC_ARG_MAX")
    argv = ["x"] * (limit // 8)
    env = {"PATH": "/usr/bin"}

    byte_total = sum(len(a.encode()) + 1 for a in argv)
    assert byte_total * 2 < limit, "bytes alone must fit, or this tests the wrong thing"

    with pytest.raises(ValueError, match="OS limit"):
        jobs._reject_oversized_argv(argv, env, kind="flow")


def test_an_ordinary_prompt_is_not_caught_by_the_size_guard(tmp_path, monkeypatch):
    """The guard must not fire on realistic input — it only bounds the extreme."""
    argv = ["li", "o", "flow", "a normal instruction"]
    env = {"PATH": "/usr/bin"}

    # Returns rather than raising.
    assert jobs._reject_oversized_argv(argv, env, kind="flow") is None


# --- The failure classification of each guarded OS read -----------------------
#
# Each guard below decides what the caller is told when one specific probe
# fails. A guard nothing drives is a branch whose answer has never been read
# back, so these exercise them one at a time, each against a control that shows
# the same code path answering differently when the probe succeeds.


def test_a_waitpid_failure_that_is_not_a_missing_child_does_not_decide_liveness(monkeypatch):
    """waitpid can fail for reasons that say nothing about the process.

    Only ChildProcessError carries information here: it means the pid is not
    ours to reap, so the direct probe is authoritative. Any other failure of
    waitpid is a failed measurement, and a failed measurement must not become
    the answer. The direct probe still decides, in both directions.
    """
    monkeypatch.setattr(jobs.os, "waitpid", _raise(OSError(5, "I/O error")))

    monkeypatch.setattr(jobs.os, "kill", lambda pid, sig: None)
    assert jobs._pid_alive(4242) is True

    monkeypatch.setattr(jobs.os, "kill", _raise(ProcessLookupError()))
    assert jobs._pid_alive(4242) is False


def test_a_process_we_may_not_signal_is_alive_rather_than_absent(monkeypatch):
    """Refusing to signal a process is the OS confirming it exists.

    A pid held by another user answers the existence probe with a permission
    error, which is a positive answer to the only question being asked. Reading
    it as absence would report a live run as finished.
    """
    monkeypatch.setattr(jobs.os, "waitpid", _raise(ChildProcessError()))

    monkeypatch.setattr(jobs.os, "kill", _raise(PermissionError(1, "not permitted")))
    assert jobs._pid_alive(4242) is True

    # The control: the same shape of refusal from the same call, for a pid that
    # genuinely holds nothing, is the other answer.
    monkeypatch.setattr(jobs.os, "kill", _raise(ProcessLookupError()))
    assert jobs._pid_alive(4242) is False


def test_a_start_time_probe_that_errors_is_unknown_and_never_gone(monkeypatch):
    """An errored identity probe must not read as death.

    "gone" licenses the caller to stop looking; "unknown" does not. A probe that
    failed knows nothing about the process, which may well be running, so the
    two answers cannot be folded together.
    """
    import psutil

    monkeypatch.setattr(psutil, "Process", _raise(psutil.AccessDenied(4242)))
    assert jobs._process_create_time(4242) == ("unknown", None)

    monkeypatch.setattr(psutil, "Process", _raise(OSError(5, "I/O error")))
    assert jobs._process_create_time(4242) == ("unknown", None)

    # The control: the answer that does mean the process is gone.
    monkeypatch.setattr(psutil, "Process", _raise(psutil.NoSuchProcess(4242)))
    assert jobs._process_create_time(4242) == ("gone", None)


def test_a_member_whose_group_read_fails_is_told_apart_from_one_that_exited(monkeypatch):
    """Two ways a group read ends without an answer, and they are not the same.

    A pid that no longer exists is not a member of the group, which is a fact.
    A group read that failed for any other reason established nothing, and
    reporting it as absence would let a scan call itself complete while a live
    member went unseen.
    """
    monkeypatch.setattr(_proc, "process_create_time", lambda pid: ("found", _SPAWNED_AT))
    monkeypatch.setattr(_proc, "process_marker", lambda pid, marker_var: ("unknown", None))

    monkeypatch.setattr(jobs.os, "getpgid", _raise(ProcessLookupError()))
    assert jobs._pinned_member(4242, 7777) == ("gone", None)

    monkeypatch.setattr(jobs.os, "getpgid", _raise(OSError(1, "not permitted")))
    assert jobs._pinned_member(4242, 7777) == ("unknown", None)

    # The control: the same call succeeding produces a member, so the two
    # refusals above are the guards answering and not the setup failing.
    monkeypatch.setattr(jobs.os, "getpgid", lambda pid: 7777)
    state, member = jobs._pinned_member(4242, 7777)
    assert state == "found" and member is not None and member[0] == 4242


def test_a_process_table_that_cannot_be_read_reports_an_incomplete_scan(monkeypatch):
    """No process table, no membership claim.

    Returning an empty member list with the scan marked complete would say the
    group holds nothing running, on the strength of a read that never happened.
    The empty list is only ever safe alongside the flag that says so.
    """
    import psutil

    monkeypatch.setattr(psutil, "pids", _raise(psutil.AccessDenied()))
    assert jobs._live_group_members(7777) == ([], False)

    monkeypatch.setattr(psutil, "pids", _raise(OSError(1, "not permitted")))
    assert jobs._live_group_members(7777) == ([], False)


def test_a_candidate_whose_group_cannot_be_read_leaves_the_scan_incomplete(monkeypatch):
    """One unreadable candidate is a gap in the membership, not a non-member.

    The pid may be in this group. Skipping it quietly would let the scan report
    a complete view of a group it had not finished reading.
    """
    import psutil

    monkeypatch.setattr(psutil, "pids", lambda: [4242])
    monkeypatch.setattr(jobs, "_process_create_time", lambda pid: ("found", _SPAWNED_AT))
    monkeypatch.setattr(_proc, "process_marker", lambda pid, marker_var: ("unknown", None))

    monkeypatch.setattr(jobs.os, "getpgid", _raise(OSError(1, "not permitted")))
    members, complete = jobs._live_group_members(7777)
    assert members == [] and complete is False

    # A candidate that simply exited is a non-member and no gap at all, so the
    # scan over the same single pid is complete.
    monkeypatch.setattr(jobs.os, "getpgid", _raise(ProcessLookupError()))
    members, complete = jobs._live_group_members(7777)
    assert members == [] and complete is True


def test_a_group_that_is_gone_at_the_moment_of_the_signal_is_reported_as_gone(
    sandbox, monkeypatch, no_stray_signal
):
    """The group can end between being identified and being signalled.

    Nothing was killed, and the reason has to say that rather than claim a
    signal landed. The record is left alone: a run this call did not stop must
    not be written down as stopped by it.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_process_create_time", lambda pid: ("found", _SPAWNED_AT))
    monkeypatch.setattr(jobs.os, "getpgid", lambda pid: 7777)
    monkeypatch.setattr(jobs.os, "killpg", _raise(ProcessLookupError()))

    rid = _identity_record()
    out = jobs.kill(rid)

    assert out["killed"] is False
    assert out["reason_code"] == jobs.KILL_PROCESS_GONE
    assert out["pgid"] == 7777
    assert jobs._read_job(rid)["status"] == "running"


def test_a_group_we_may_not_signal_is_reported_as_refused_not_as_stopped(
    sandbox, monkeypatch, no_stray_signal
):
    """A refused signal is a live group this call did not stop.

    It is the case most easily mistaken for success, because the group was
    correctly identified and the call returned without an error reaching the
    caller. The operator has to be told the process is still running.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_process_create_time", lambda pid: ("found", _SPAWNED_AT))
    monkeypatch.setattr(jobs.os, "getpgid", lambda pid: 7777)
    monkeypatch.setattr(jobs.os, "killpg", _raise(PermissionError(1, "not permitted")))

    rid = _identity_record()
    out = jobs.kill(rid)

    assert out["killed"] is False
    assert out["reason_code"] == jobs.KILL_PERMISSION_DENIED
    assert "permission denied" in out["reason"]
    assert jobs._read_job(rid)["status"] == "running"


def test_an_artifact_that_disappears_after_being_named_is_not_a_failed_listing(
    sandbox, monkeypatch
):
    """A file removed mid-walk withheld nothing.

    The walk names entries and the metadata is read afterwards, so a file that
    is deleted in between is simply not there. That is a true empty answer, and
    marking the listing unreadable for it would report a shortfall that did not
    happen — which is the same error as the opposite case, in the other
    direction.
    """
    adir = config.run_dir("run-x") / "artifacts"
    adir.mkdir(parents=True)
    (adir / "kept.txt").write_text("still here")

    real_stat = Path.stat

    def vanishing(self, *a, **kw):
        if self.name == "gone.txt":
            raise FileNotFoundError(2, "No such file or directory")
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(
        jobs.os, "walk", lambda d, onerror=None: [(str(adir), [], ["kept.txt", "gone.txt"])]
    )
    monkeypatch.setattr(Path, "stat", vanishing)

    found, state = jobs._list_artifacts("run-x")

    assert found == ["kept.txt"]
    assert state == "ok", "a file that vanished is an absence, not an unreadable listing"


def test_an_artifact_whose_metadata_is_refused_does_make_the_listing_unreadable(
    sandbox, monkeypatch
):
    """The control for the case above, and the distinction the state exists for.

    Here the entry is real and was withheld, so the caller is short a file it
    was never told about. Same loop, same continue, opposite state.
    """
    adir = config.run_dir("run-y") / "artifacts"
    adir.mkdir(parents=True)
    (adir / "kept.txt").write_text("still here")

    real_stat = Path.stat

    def refused(self, *a, **kw):
        if self.name == "locked.txt":
            raise PermissionError(1, "Operation not permitted")
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(
        jobs.os, "walk", lambda d, onerror=None: [(str(adir), [], ["kept.txt", "locked.txt"])]
    )
    monkeypatch.setattr(Path, "stat", refused)

    found, state = jobs._list_artifacts("run-y")

    assert found == ["kept.txt"]
    assert state == "unreadable"


def _terminal_run(outcome, status="completed"):
    """A finished run carrying *outcome* as its recorded delivery result."""
    rid = jobs.new_run_id()
    jobs._write_job(
        {
            "run_id": rid,
            "status": status,
            "kind": "agent",
            "pid": None,
            "log": None,
            "finished_at": "2026-01-01T00:00:00Z" if status == "completed" else None,
        }
    )
    if outcome is not None:
        jobs.record_notify_delivery(rid, outcome)
    return rid


_DELIVERED = {"attempted": True, "ok": True, "exit_code": 0, "error": None, "command": "notify"}
_EXITED_NONZERO = {
    "attempted": True,
    "ok": False,
    "exit_code": 1,
    "error": None,
    "command": "notify",
}
_NEVER_STARTED = {
    "attempted": True,
    "ok": False,
    "exit_code": None,
    "error": "OSError",
    "command": "notify",
}
_REFUSED_BEFORE_RUNNING = {
    "attempted": False,
    "ok": False,
    "exit_code": None,
    "error": "delivery_command_is_empty",
    "command": None,
}
_NOTHING_CONFIGURED = {"attempted": False}
_DELIVERED_UNVERIFIED = {
    "attempted": True,
    "ok": True,
    "exit_code": 0,
    "error": None,
    "command": "kkernel",
    "delivery_verified": False,
    "unverified_reason": "kkernel_exec_without_strict_exits_zero_on_a_refused_op",
}


def test_listing_tells_a_failed_notice_apart_from_a_delivered_one(sandbox):
    """The listing distinguishes a run whose notice failed from one that is fine.

    This is the surface a caller polls while waiting on several runs. Reporting
    only the run status there leaves a finished run whose notice never went out
    looking exactly like a run still working — the failure resolving in the
    reassuring direction, which is the worst way for it to resolve.
    """
    delivered = _terminal_run(_DELIVERED)
    exited_nonzero = _terminal_run(_EXITED_NONZERO)
    never_started = _terminal_run(_NEVER_STARTED)
    refused = _terminal_run(_REFUSED_BEFORE_RUNNING)

    states = {j["run_id"]: j["notify_delivery_state"] for j in jobs.list_jobs()}
    assert states[delivered] == "delivered"
    # every way a configured notifier came to nothing reads the same here: to a
    # caller waiting on the notice they are one fact, and status carries the rest
    assert states[exited_nonzero] == "failed"
    assert states[never_started] == "failed"
    assert states[refused] == "failed"


def test_listing_does_not_pass_an_unverified_delivery_off_as_delivered(sandbox):
    """A zero exit that is known not to prove delivery gets its own word.

    This is the whole point of recording the degraded state: an operator scanning
    the listing sees "delivered" and stops looking. If the one shape we know can
    exit zero on a refused send is spelled the same as a confirmed delivery, the
    marker on the record is information nobody ever acts on.

    It is equally not "failed" — the notice probably did arrive, and reporting a
    failure that did not happen sends someone chasing it.
    """
    unverified = _terminal_run(_DELIVERED_UNVERIFIED)
    delivered = _terminal_run(_DELIVERED)
    failed = _terminal_run(_EXITED_NONZERO)

    states = {j["run_id"]: j["notify_delivery_state"] for j in jobs.list_jobs()}
    assert states[unverified] == "delivered_unverified"
    assert states[unverified] != states[delivered]
    assert states[unverified] != states[failed]
    # a caller that treats anything other than "delivered" as needing a look gets
    # the right behaviour without having to know this state exists
    assert states[unverified] != "delivered"


def test_listing_does_not_read_an_absent_notifier_as_a_failure(sandbox):
    """Silence that was chosen, and a run not yet finished, are not failures.

    Nothing configured is the documented default, so a run that asked for no
    notice must never appear alongside the ones whose notice was lost.
    """
    nothing_configured = _terminal_run(_NOTHING_CONFIGURED)
    still_running = _terminal_run(None, status="running")
    delivered = _terminal_run(_DELIVERED)
    failed = _terminal_run(_EXITED_NONZERO)

    states = {j["run_id"]: j["notify_delivery_state"] for j in jobs.list_jobs()}
    assert states[nothing_configured] == "none"
    assert states[still_running] == "none"
    # the three outcomes a caller branches on are three different words
    assert len({states[nothing_configured], states[delivered], states[failed]}) == 3


def test_listing_reads_a_damaged_delivery_record_as_no_delivery(sandbox):
    """A record whose delivery field is not an object must not break the listing.

    One damaged record cannot be allowed to cost the caller the runs beside it,
    and an unreadable field is not evidence that a notice failed.
    """
    rid = jobs.new_run_id()
    jobs._write_job(
        {
            "run_id": rid,
            "status": "completed",
            "kind": "agent",
            "pid": None,
            "log": None,
            "notify_delivery": "delivered!",
        }
    )

    assert jobs.list_jobs()[0]["notify_delivery_state"] == "none"


def test_status_still_reports_the_whole_delivery_outcome(sandbox):
    """status is the diagnosis surface and is unchanged: it carries the object.

    The listing's collapsed state is an addition to the listing, not a
    replacement for what status has always reported.
    """
    rid = _terminal_run(_EXITED_NONZERO)

    st = jobs.status(rid)
    assert st["notify_delivery"] == _EXITED_NONZERO
    # and the collapsed form stays out of status: one field, one meaning
    assert "notify_delivery_state" not in st


def _popen_that_writes_the_runs_own_line(runs_written: list):
    """A stand-in child that writes one line down the descriptor it was handed.

    The descriptor is what a collision is felt through — two runs holding a
    writable handle to one file — so the double has to use it rather than only
    record it.
    """

    def fake_popen(argv, **kw):
        line = kw["env"][config.RUN_ID_ENV_VAR]
        kw["stdout"].write(f"{line} wrote this\n".encode())
        runs_written.append(line)
        return _FakeProc()

    return fake_popen


def test_a_second_submission_never_lands_on_a_running_runs_directory(sandbox, monkeypatch):
    """An id already taken costs a retry, not the run that holds it.

    A run id is a timestamp to the second plus six random hex digits, so two
    submissions in one second can mint the same one. What that used to mean was
    not a collision of names but a collision of runs: the second wrote its record
    over the first's and its child wrote into the first's log, and afterwards
    nothing could tell the two apart or say what the first one had been.

    Asserted on what an operator can see — two ids, two directories, each log
    holding only its own run's output and each record naming only its own run.
    How the retry gets there is this function's business and is not asserted.
    """
    written: list[str] = []
    monkeypatch.setattr(jobs.subprocess, "Popen", _popen_that_writes_the_runs_own_line(written))

    # Positive control first: ordinary submissions, ids that differ on their own.
    # Without it, a change that refused every second submission — or handed every
    # one of them a fresh id it never used — would read as this test passing.
    minted = iter(["20260101T000000-aaaaaa", "20260101T000000-bbbbbb"])
    monkeypatch.setattr(jobs, "new_run_id", lambda: next(minted))
    first = jobs.submit("agent", [], label="first")["run_id"]
    second = jobs.submit("agent", [], label="second")["run_id"]

    assert first != second
    for rid, label in ((first, "first"), (second, "second")):
        assert (config.JOBS_DIR / rid).is_dir()
        assert (config.JOBS_DIR / rid / "console.log").read_text() == f"{rid} wrote this\n"
        assert jobs._read_job(rid)["label"] == label

    # Now the collision: the next submission mints an id that is already taken
    # before it mints one that is not.
    minted = iter(["20260101T000000-bbbbbb", "20260101T000000-cccccc"])
    monkeypatch.setattr(jobs, "new_run_id", lambda: next(minted))
    third = jobs.submit("agent", [], label="third")["run_id"]

    assert third not in (first, second)
    assert (config.JOBS_DIR / third / "console.log").read_text() == f"{third} wrote this\n"
    assert jobs._read_job(third)["label"] == "third"
    # The run whose id was taken is untouched: its log holds its own line alone
    # and its record still says whose it is.
    assert (config.JOBS_DIR / second / "console.log").read_text() == f"{second} wrote this\n"
    assert jobs._read_job(second)["label"] == "second"
    assert written == [first, second, third]


def test_a_submission_that_is_refused_leaves_no_directory_behind(sandbox, monkeypatch):
    """The reservation is taken back when the submission does not happen.

    Every directory under the jobs root is a job to the listing, so one left
    behind by a rejected submission is a job with no kind that never finishes.
    """

    def refuse(argv, env, *, kind):
        raise ValueError("argv is too long for this platform")

    monkeypatch.setattr(jobs, "new_run_id", lambda: "20260101T000000-dddddd")
    monkeypatch.setattr(jobs, "_reject_oversized_argv", refuse)

    with pytest.raises(ValueError):
        jobs.submit("agent", [], prompt="x")

    assert not (config.JOBS_DIR / "20260101T000000-dddddd").exists()
    assert jobs.list_jobs() == []


def test_a_reserved_directory_is_not_listed_until_its_job_record_is_published(sandbox):
    run_id, _ = jobs._reserve_run_dir()

    assert jobs.list_jobs() == []

    jobs._write_job({"run_id": run_id, "kind": "agent", "status": "running"})

    assert [entry["run_id"] for entry in jobs.list_jobs()] == [run_id]


def test_a_submission_that_fails_between_its_writes_leaves_no_job_behind(sandbox, monkeypatch):
    """A submission that gets partway through writing is still not a job.

    The refusal above happens before anything is written, so an empty directory
    is all it leaves. A submission that writes its prompt and then fails to write
    its MCP snapshot leaves a directory with a file in it, and giving that back
    takes more than a removal that only works on an empty one. Both leave the
    same thing behind for an operator: a listed job with no kind that never
    finishes, which is why the listing is what this asserts on.
    """
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda argv, **kw: _FakeProc(4242))
    monkeypatch.setattr(
        _mcp_resolve,
        "resolve_spawn_mcp_servers",
        lambda launch_dir: _mcp_resolve.McpResolution(
            servers={"a-server": {"command": "true"}},
            reason=None,
            source=Path("/somewhere/.mcp.json"),
            searched_from=Path("/somewhere"),
        ),
    )

    # Positive control: the same call, unobstructed. Without it a change that
    # refused every submission of this shape would read as this test passing.
    monkeypatch.setattr(jobs, "new_run_id", lambda: "20260101T000000-eeeeee")
    control = jobs.submit("agent", [], prompt="x", label="control")["run_id"]
    assert [(j["run_id"], j["kind"]) for j in jobs.list_jobs()] == [(control, "agent")]

    # Now fail the second of the two writes. The first has already landed, so
    # what is left behind is a directory that is not empty.
    prompt_was_already_written: list[bool] = []
    real_write_text = Path.write_text

    def refuse_the_snapshot(self, *args, **kwargs):
        if self.name == "mcp-servers.json":
            prompt_was_already_written.append((self.parent / "prompt.txt").exists())
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", refuse_the_snapshot)
    monkeypatch.setattr(jobs, "new_run_id", lambda: "20260101T000000-ffffff")

    with pytest.raises(OSError):
        jobs.submit("agent", [], prompt="x", label="interrupted")

    # The failure landed after the first write, not before it — otherwise this
    # would be the empty-directory case the test above already covers.
    assert prompt_was_already_written == [True]
    assert [(j["run_id"], j["kind"]) for j in jobs.list_jobs()] == [(control, "agent")]


def test_a_submission_whose_record_never_publishes_leaves_no_job_behind(sandbox, monkeypatch):
    """The record is the last thing that can strand a reservation.

    The two writes above it are covered; the write that publishes the record is
    the one that decides whether any of it was a job at all. When it fails there
    is no record to mark and no child to mark it, so what is left is the same
    directory with no kind that never finishes — reached one step later than the
    other failures, and given back the same way.
    """
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda argv, **kw: _FakeProc(4242))

    # Positive control: the same call, unobstructed. Without it a change that
    # refused every submission of this shape would read as this test passing.
    monkeypatch.setattr(jobs, "new_run_id", lambda: "20260101T000000-aaabbb")
    control = jobs.submit("agent", [], prompt="x", label="control")["run_id"]
    assert [(j["run_id"], j["kind"]) for j in jobs.list_jobs()] == [(control, "agent")]

    # The prompt has already been written by the time the record is published,
    # so this is the non-empty directory case, not the empty one.
    prompt_was_already_written: list[bool] = []
    real_replace = os.replace

    def refuse_to_publish(src, dst, *args, **kwargs):
        if str(dst).endswith("job.json"):
            prompt_was_already_written.append((Path(dst).parent / "prompt.txt").exists())
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", refuse_to_publish)
    monkeypatch.setattr(jobs, "new_run_id", lambda: "20260101T000000-cccddd")

    with pytest.raises(OSError):
        jobs.submit("agent", [], prompt="x", label="unpublished")

    assert prompt_was_already_written == [True]
    assert not (config.JOBS_DIR / "20260101T000000-cccddd").exists()
    assert [(j["run_id"], j["kind"]) for j in jobs.list_jobs()] == [(control, "agent")]


def test_an_interrupted_publication_leaves_nothing_that_blocks_the_cleanup(sandbox, monkeypatch):
    """An interrupt is one of the failures the reservation is given back for.

    Giving it back ends in an rmdir, which refuses a directory holding anything
    at all — so the staging file the record write uses to be atomic is the one
    thing able to defeat the cleanup that runs because that write failed. The
    two have to answer for the same set of failures, which is why this asserts
    on an exception outside the errno family.
    """
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda argv, **kw: _FakeProc(4242))

    # Positive control: the same call, unobstructed.
    monkeypatch.setattr(jobs, "new_run_id", lambda: "20260101T000000-eee111")
    control = jobs.submit("agent", [], prompt="x", label="control")["run_id"]
    assert [(j["run_id"], j["kind"]) for j in jobs.list_jobs()] == [(control, "agent")]

    real_replace = os.replace

    def interrupt_the_publication(src, dst, *args, **kwargs):
        if str(dst).endswith("job.json"):
            raise KeyboardInterrupt
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", interrupt_the_publication)
    monkeypatch.setattr(jobs, "new_run_id", lambda: "20260101T000000-fff222")

    with pytest.raises(KeyboardInterrupt):
        jobs.submit("agent", [], prompt="x", label="interrupted")

    assert not (config.JOBS_DIR / "20260101T000000-fff222").exists()
    assert [(j["run_id"], j["kind"]) for j in jobs.list_jobs()] == [(control, "agent")]


def test_a_failed_cleanup_does_not_answer_in_place_of_the_failure_that_caused_it(
    sandbox, monkeypatch
):
    """Tidying up after a failure never becomes the failure a caller is told about.

    The staging removal runs inside a handler that catches everything, so its own
    error would otherwise take the place of the one that sent us there: a caller
    waiting on an interrupt would be handed whatever the tidying refused with. The
    file may then survive, which is the same trade the reservation give-back
    already makes — a removal that fails leaves something nobody claimed, and that
    is worth less than the error underneath it.
    """
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda argv, **kw: _FakeProc(4242))
    monkeypatch.setattr(jobs, "new_run_id", lambda: "20260101T000000-777aaa")

    real_replace = os.replace
    real_unlink = Path.unlink

    def interrupt_the_publication(src, dst, *args, **kwargs):
        if str(dst).endswith("job.json"):
            raise KeyboardInterrupt
        return real_replace(src, dst, *args, **kwargs)

    def refuse_to_remove(self, *args, **kwargs):
        if self.name.startswith(".job.json.") and self.name.endswith(".tmp"):
            raise PermissionError(errno.EACCES, "cleanup denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(os, "replace", interrupt_the_publication)
    monkeypatch.setattr(Path, "unlink", refuse_to_remove)

    # The interrupt, not the PermissionError the tidying raised.
    with pytest.raises(KeyboardInterrupt):
        jobs.submit("agent", [], prompt="x", label="interrupted")


def test_an_interrupt_arriving_during_cleanup_is_not_swallowed_by_it(sandbox, monkeypatch):
    """Being asked to stop is not the same as a removal being refused.

    The removal is allowed to fail without answering in place of the failure
    underneath it, but only for the failures a filesystem actually produces. An
    interrupt arriving while it runs is nobody's removal failing — it is a
    request for the process to stop, and answering that with whatever the run
    was already failing at loses the request entirely.
    """
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda argv, **kw: _FakeProc(4242))
    monkeypatch.setattr(jobs, "new_run_id", lambda: "20260101T000000-888bbb")

    real_replace = os.replace
    real_unlink = Path.unlink

    def fail_the_publication(src, dst, *args, **kwargs):
        if str(dst).endswith("job.json"):
            raise ValueError("publication failed")
        return real_replace(src, dst, *args, **kwargs)

    def interrupt_during_removal(self, *args, **kwargs):
        if self.name.startswith(".job.json.") and self.name.endswith(".tmp"):
            raise KeyboardInterrupt
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(os, "replace", fail_the_publication)
    monkeypatch.setattr(Path, "unlink", interrupt_during_removal)

    # The interrupt, not the ValueError it arrived on top of.
    with pytest.raises(KeyboardInterrupt):
        jobs.submit("agent", [], prompt="x", label="interrupted-while-tidying")


def test_discarding_a_reservation_removes_what_the_submission_wrote(sandbox):
    """Both of the names a submission writes come back with the directory."""
    d = config.JOBS_DIR / "20260101T000000-111111"
    d.mkdir(parents=True)
    (d / "prompt.txt").write_text("x")
    (d / "mcp-servers.json").write_text("{}")

    assert jobs._discard_reservation(d) is True

    assert not d.exists()


def test_a_failed_submission_does_not_delete_the_config_its_caller_named(
    sandbox, tmp_path, monkeypatch
):
    """A caller's own MCP config is theirs, wherever they keep it.

    Being named by a submission that failed does not make a file part of the
    directory being given back, and the file a caller names is not under it.
    """
    callers_own_config = tmp_path / "elsewhere" / ".mcp.json"
    callers_own_config.parent.mkdir()
    callers_own_config.write_text('{"mcpServers": {}}')

    def refuse(self, *args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(jobs, "new_run_id", lambda: "20260101T000000-333333")
    monkeypatch.setattr(Path, "write_text", refuse)

    with pytest.raises(OSError):
        jobs.submit("agent", [], prompt="x", mcp_config=str(callers_own_config))

    assert callers_own_config.read_text() == '{"mcpServers": {}}'
    assert jobs.list_jobs() == []


def test_discarding_a_reservation_refuses_a_directory_holding_anything_else(sandbox):
    """The refusal is the safeguard, and nothing is checked ahead of it.

    A directory with a run's own state in it survives being handed to this, so
    the removal cannot cost a real job whatever sends us there.
    """
    d = config.JOBS_DIR / "20260101T000000-222222"
    d.mkdir(parents=True)
    (d / "prompt.txt").write_text("x")
    (d / "console.log").write_bytes(b"a run wrote this\n")

    assert jobs._discard_reservation(d) is False

    assert (d / "console.log").read_bytes() == b"a run wrote this\n"


def test_a_rollback_that_could_not_run_marks_the_directory_it_left_behind(sandbox):
    """A stranded reservation is not indistinguishable from one cleanly given back.

    Both cases previously left nothing behind to tell them apart: a directory
    under the jobs root either vanished or, if the giveback failed, sat there
    exactly as anonymous as a job in progress. The marker this leaves is the
    signal an operator greping for stranded runs has something to correlate
    against.
    """
    d = config.JOBS_DIR / "20260101T000000-444444"
    d.mkdir(parents=True)
    (d / "prompt.txt").write_text("x")
    (d / "console.log").write_bytes(b"a run wrote this\n")

    assert jobs._discard_reservation(d) is False

    marker = d / jobs._RESERVATION_STRANDED_MARKER
    assert marker.exists()
    assert "giveback" in marker.read_text()


def test_a_clean_rollback_leaves_no_stranded_marker(sandbox):
    """The marker names a failure; a successful giveback has nothing to mark —
    and nowhere left to mark it, since the directory is gone."""
    d = config.JOBS_DIR / "20260101T000000-555555"
    d.mkdir(parents=True)
    (d / "prompt.txt").write_text("x")

    assert jobs._discard_reservation(d) is True

    assert not d.exists()


def test_a_marker_write_that_also_fails_still_reaches_the_caller_as_a_warning(
    sandbox, monkeypatch, caplog
):
    """The marker is best-effort; the caller's own diagnostics are not.

    Both cleanup call sites in `submit()` reach a rollback through
    `_discard_reservation_and_warn`, never `_discard_reservation` directly.
    When the giveback fails AND the marker write meant to record that also
    fails (disk full, permissions), the boolean returned by
    `_discard_reservation` is the only signal left — this asserts the wrapper
    actually turns it into a warning instead of letting it join the exception
    being reraised on the way out.
    """
    d = config.JOBS_DIR / "20260101T000000-666666"
    d.mkdir(parents=True)
    (d / "console.log").write_bytes(b"a run wrote this\n")

    real_write_text = Path.write_text

    def refuse_the_marker(self, *args, **kwargs):
        if self.name == jobs._RESERVATION_STRANDED_MARKER:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", refuse_the_marker)

    with caplog.at_level(logging.WARNING, logger=jobs._log.name):
        jobs._discard_reservation_and_warn(d, "20260101T000000-666666")

    assert not (d / jobs._RESERVATION_STRANDED_MARKER).exists()
    assert d.exists()
    assert any("20260101T000000-666666" in record.message for record in caplog.records)


def test_a_lock_that_cannot_be_taken_says_so_even_when_the_descriptor_will_not_close(
    sandbox, monkeypatch
):
    """Failing to acquire the lock is a state, and tidying up cannot turn it into a raise.

    Failing to create the lock file and failing to acquire it are the same fact —
    this section was not entered — and both are reported as a state rather than
    escaping a context manager whose contract is that it yields. Handing the
    descriptor back is tidying up after that fact, so a close that refuses must
    not become the answer: it is worth less than the fact underneath it, and it
    would leave every caller of this receiving an exception where the contract
    promises a state.
    """
    run_id = "20260101T000000-aaa111"
    (config.JOBS_DIR / run_id).mkdir(parents=True)

    taken = {}
    real_close = os.close

    def refuse_the_lock(fd):
        taken["fd"] = fd
        raise OSError(errno.EAGAIN, "Resource temporarily unavailable")

    def refuse_to_close(fd):
        if fd == taken.get("fd"):
            real_close(fd)
            raise OSError(errno.EBADF, "Bad file descriptor")
        return real_close(fd)

    monkeypatch.setattr(jobs, "_lock_fd", refuse_the_lock)
    monkeypatch.setattr(os, "close", refuse_to_close)

    with jobs._locked_job(run_id) as guard:
        assert guard.record is None
        assert guard.state == jobs.LOCK_UNAVAILABLE


def test_releasing_the_lock_does_not_answer_in_place_of_what_the_body_raised(sandbox, monkeypatch):
    """The body is where the failures a caller acts on come from.

    A record that will not serialize, a write the filesystem refuses — those
    reach a caller through this section, and a release that fails on the way out
    is worth less than any of them. The release also runs on every exit, so an
    unguarded one puts itself in front of every failure the section can produce
    rather than in front of some rare one.
    """
    run_id = "20260101T000000-bbb222"
    (config.JOBS_DIR / run_id).mkdir(parents=True)
    jobs._write_job({"run_id": run_id, "kind": "agent", "status": "running"})

    def refuse_to_release(fd):
        raise OSError(errno.EBADF, "Bad file descriptor")

    monkeypatch.setattr(jobs, "_unlock_fd", refuse_to_release)

    # The body's ValueError, not the release's OSError.
    with pytest.raises(ValueError, match="what the caller needs to see"):
        with jobs._locked_job(run_id) as guard:
            assert guard.record is not None
            raise ValueError("what the caller needs to see")


def test_an_unlock_failure_after_a_successful_body_reaches_the_caller(sandbox, monkeypatch):
    run_id = "20260101T000000-bbb333"
    (config.JOBS_DIR / run_id).mkdir(parents=True)
    jobs._write_job({"run_id": run_id, "kind": "agent", "status": "running"})

    real_unlock = jobs._unlock_fd

    def unlock_then_refuse(fd):
        real_unlock(fd)
        raise OSError(errno.EIO, "unlock failed")

    monkeypatch.setattr(jobs, "_unlock_fd", unlock_then_refuse)

    with pytest.raises(OSError, match="unlock failed"):
        with jobs._locked_job(run_id) as guard:
            assert guard.record is not None


def test_a_close_failure_after_a_successful_body_reaches_the_caller(sandbox, monkeypatch):
    run_id = "20260101T000000-bbb444"
    (config.JOBS_DIR / run_id).mkdir(parents=True)
    jobs._write_job({"run_id": run_id, "kind": "agent", "status": "running"})

    taken = {}
    real_lock = jobs._lock_fd
    real_close = os.close

    def remember_the_fd(fd):
        taken["fd"] = fd
        return real_lock(fd)

    def close_then_refuse(fd):
        if fd == taken.get("fd"):
            real_close(fd)
            raise OSError(errno.EIO, "close failed")
        return real_close(fd)

    monkeypatch.setattr(jobs, "_lock_fd", remember_the_fd)
    monkeypatch.setattr(os, "close", close_then_refuse)

    with pytest.raises(OSError, match="close failed"):
        with jobs._locked_job(run_id) as guard:
            assert guard.record is not None


def test_an_interrupt_arriving_during_release_is_not_swallowed_and_still_closes(
    sandbox, monkeypatch
):
    """Being asked to stop is not a release refusing, and the close is tried anyway.

    The release is allowed to fail without answering for the body, but only for
    what a filesystem refusal looks like. An interrupt delivered while it runs is
    a request for the process to stop, and cleanup that absorbs it loses the
    request entirely.

    The close is still attempted on that way out, which is what this asserts: a
    lock nobody released is worse than either failure, and process exit is the
    only thing that puts a ceiling on how long it stays held. A ceiling is not a
    schedule — the close attempted right here takes the lock down earlier every
    time it succeeds, which is the whole reason for attempting it. Attempted is
    all that can be asserted, here or anywhere: a close that raises may or may
    not have released the descriptor, and there is no second call that could
    settle it safely.
    """
    run_id = "20260101T000000-ccc333"
    (config.JOBS_DIR / run_id).mkdir(parents=True)
    jobs._write_job({"run_id": run_id, "kind": "agent", "status": "running"})

    taken = {}
    closed = []
    real_lock = jobs._lock_fd
    real_close = os.close

    def remember_the_fd(fd):
        taken["fd"] = fd
        return real_lock(fd)

    def stop_during_release(fd):
        raise KeyboardInterrupt

    def record_close(fd):
        if fd == taken.get("fd"):
            closed.append(fd)
        return real_close(fd)

    monkeypatch.setattr(jobs, "_lock_fd", remember_the_fd)
    monkeypatch.setattr(jobs, "_unlock_fd", stop_during_release)
    monkeypatch.setattr(os, "close", record_close)

    # The interrupt, not the ValueError it arrived on top of.
    with pytest.raises(KeyboardInterrupt):
        with jobs._locked_job(run_id) as guard:
            assert guard.record is not None
            raise ValueError("the failure the interrupt arrived during")

    assert closed == [taken["fd"]]


def test_submit_records_the_directory_the_submission_came_from(sandbox, monkeypatch, tmp_path):
    """The anchor on the record is this process's own directory, not the run's.

    Every other test of the delivery path writes `submit_cwd` by hand, so this is
    the one that asks whether the writer puts the right value there. The two are
    deliberately different here: a run pinned to one directory, submitted from
    another.
    """
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *a, **k: _FakeProc(4242))
    submitted_from = tmp_path / "seat"
    submitted_from.mkdir()
    ran_in = tmp_path / "worktree"
    ran_in.mkdir()
    monkeypatch.chdir(submitted_from)

    rid = jobs.submit("agent", [], prompt="x", cwd=str(ran_in))["run_id"]

    rec = jobs._read_job(rid)
    assert rec["cwd"] == str(ran_in)
    assert rec["submit_cwd"] == str(submitted_from)


def test_a_submission_that_could_not_read_its_directory_records_that_it_could_not(
    sandbox, monkeypatch
):
    """The anchor is absent as a value, not as a key.

    The key being there with nothing in it is what lets the delivery path tell
    "this run tried to record an anchor and could not" from "this record predates
    the field". They are different facts and only the second may inherit.

    Submitted with no MCP config on purpose. Resolving one reads the working
    directory too, earlier and unguarded, so on that path a submission never
    reaches the anchor — it fails at the resolve, inside a block that gives the
    reserved directory back. This is the path where the anchor is the first
    reader, which is the only one where the null it records can be observed.
    """
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *a, **k: _FakeProc(4242))
    monkeypatch.setattr(jobs.os, "getcwd", lambda: (_ for _ in ()).throw(OSError("gone")))

    rid = jobs.submit("agent", [], prompt="x", no_mcp_config=True)["run_id"]

    rec = jobs._read_job(rid)
    assert "submit_cwd" in rec
    assert rec["submit_cwd"] is None


def test_the_listing_says_which_running_rows_never_started(sandbox, monkeypatch):
    """`running` is two different facts, and the listing has to tell them apart.

    A record whose spawn was never attempted stays `running` on purpose — the
    classifier refuses to resolve it by a bound that cannot tell a loaded machine
    from a dead spawn. That refusal is right and it is exactly why the listing
    must carry the spawn state: this is the surface a caller reads to answer what
    is in flight, and a run that never began must not be counted there as one
    doing work.
    """
    _live_process(monkeypatch)
    never_started = jobs.new_run_id()
    jobs._write_job(
        {
            "run_id": never_started,
            "pid": None,
            "kind": "agent",
            "status": "running",
            "spawn_state": "preparing",
            "log": None,
        }
    )
    working = jobs.new_run_id()
    jobs._write_job(
        {
            "run_id": working,
            "pid": 4242,
            "pid_create_time": _SPAWNED_AT,
            "kind": "agent",
            "status": "running",
            "spawn_state": "started",
            "log": None,
        }
    )

    listed = {j["run_id"]: j for j in jobs.list_jobs()}
    assert set(listed) == {never_started, working}

    # Both wear the same word, which is what makes the listing alone misleading.
    assert listed[never_started]["status"] == listed[working]["status"] == "running"
    # And the field that separates them is present without a per-run status call.
    assert listed[never_started]["spawn_state"] == "preparing"
    assert listed[working]["spawn_state"] == "started"


def test_a_row_that_names_no_spawn_phase_is_not_read_as_never_started(sandbox, monkeypatch):
    """Null is "no phase this listing can vouch for", not "never attempted".

    A record written before the field existed reports null. A record carrying a
    phase this code does not recognise reports that value verbatim, because the
    listing repeats what the record says rather than judging it. Neither may be
    mistaken for the phase that genuinely means never-attempted, which names
    itself.
    """
    _live_process(monkeypatch)
    legacy = jobs.new_run_id()
    jobs._write_job(
        {"run_id": legacy, "pid": None, "kind": "agent", "status": "running", "log": None}
    )
    junk = jobs.new_run_id()
    jobs._write_job(
        {
            "run_id": junk,
            "pid": None,
            "kind": "agent",
            "status": "running",
            "spawn_state": "starting?",
            "log": None,
        }
    )

    listed = {j["run_id"]: j for j in jobs.list_jobs()}
    # A record with no phase in it says so, and says nothing more.
    assert listed[legacy]["spawn_state"] is None
    assert listed[legacy]["record_state"] == "ok"
    # An unrecognised value is passed through rather than laundered into a known
    # phase, so a caller can see that the record says something it should not.
    assert listed[junk]["spawn_state"] == "starting?"
    # Neither is the value that means never-attempted.
    assert listed[legacy]["spawn_state"] != "preparing"
    assert listed[junk]["spawn_state"] != "preparing"
