# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the background job engine.

Popen is mocked throughout so no real `li` process is spawned; the tests assert
on the argv/env the engine builds and on the on-disk job records it reads back.
"""

from __future__ import annotations

import builtins
import os
import signal
from pathlib import Path

import pytest

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

    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    assert jobs.status(rid)["status"] == "running"

    # pid gone, no terminal record captured -> exited
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    assert jobs.status(rid)["status"] == "exited"

    # authoritative terminal recorded by the notify hook
    jobs.mark_terminal(rid, "completed")
    assert jobs.status(rid)["status"] == "completed"


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


def _identity_record(pid: int = 4242, pgid: int = 7777, created: float = _SPAWNED_AT, **extra):
    """A job record carrying the process identity submit() now writes."""
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
    monkeypatch.setattr(jobs, "_process_marker", lambda pid: ("unknown", None))
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
    """No identity was captured, so a pid probe is all this record ever had.

    Flipping these to stopped would claim the process is gone on the strength of
    data that says nothing about it either way.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        jobs,
        "_process_create_time",
        lambda pid: pytest.fail("a record with no usable identity has nothing to compare"),
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
    """
    import psutil

    monkeypatch.setattr(psutil, "pids", lambda: [5001])
    monkeypatch.setattr(jobs.os, "getpgid", lambda pid: pgid)
    reads = iter(create_times)
    monkeypatch.setattr(
        jobs, "_process_create_time", lambda pid: ("found", next(reads, create_times[-1]))
    )
    monkeypatch.setattr(jobs, "_process_marker", lambda pid: ("found", marker))


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
    monkeypatch.setattr(jobs, "_process_marker", lambda pid: ("unknown", None))

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

    monkeypatch.setattr(jobs, "_process_marker", lambda pid: ("unknown", None))
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

    job = jobs.mark_terminal(rid, "failed")
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
