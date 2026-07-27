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


def test_kill_reaps_a_live_group_whose_leader_exited(sandbox, monkeypatch, no_stray_signal):
    """The case a leader-liveness gate refuses: `li` exits, its workers do not.

    The children are spawned into the leader's group and outlive it, so the
    group is what has to be signalled — and it is still identifiable after the
    leader is gone, because every member started after the run did.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(
        jobs, "_live_group_members", lambda pgid: ([(5001, _SPAWNED_AT + 3.0)], True)
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
        jobs, "_live_group_members", lambda pgid: ([(5001, _SPAWNED_AT - 60.0)], True)
    )

    rid = _identity_record()
    monkeypatch.setattr(jobs, "_process_marker", lambda pid: ("found", rid))
    out = jobs.kill(rid)

    assert no_stray_signal == [(7777, signal.SIGTERM)]
    assert out["killed"] is True and out["reason_code"] == jobs.KILL_SIGNALLED


def test_kill_refuses_a_group_carrying_another_runs_marker(sandbox, monkeypatch, no_stray_signal):
    """The same evidence pointing the other way, where start time would allow.

    Every member started after this run did, so the inequality is satisfied and
    the fallback would signal. The marker names a different run, which is a
    positive identification that the group number has been handed on.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(
        jobs, "_live_group_members", lambda pgid: ([(5001, _SPAWNED_AT + 3.0)], True)
    )
    monkeypatch.setattr(jobs, "_process_marker", lambda pid: ("found", "some-other-run"))

    out = jobs.kill(_identity_record())

    assert no_stray_signal == []
    assert out["killed"] is False and out["reason_code"] == jobs.KILL_GROUP_FOREIGN
    assert "started by a different run" in out["reason"]


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
        lambda pgid: ([(5001, _SPAWNED_AT + 1.0), (5002, _SPAWNED_AT + 2.0)], True),
    )

    rid = _identity_record()
    seen = {pid: (rid if m == "this-run" else m) for pid, m in zip([5001, 5002], order)}
    monkeypatch.setattr(jobs, "_process_marker", lambda pid: ("found", seen[pid]))

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
        lambda pgid: ([(5001, _SPAWNED_AT - 60.0), (5002, _SPAWNED_AT - 30.0)], True),
    )

    rid = _identity_record()
    monkeypatch.setattr(jobs, "_process_marker", lambda pid: ("found", rid))
    out = jobs.kill(rid)

    assert no_stray_signal == [(7777, signal.SIGTERM)]
    assert out["killed"] is True and out["reason_code"] == jobs.KILL_SIGNALLED


@pytest.mark.parametrize(
    "marker",
    [
        # Read fine, but the process predates the marker: a run recorded by an
        # older build, whose children were never stamped.
        ("found", None),
        # The environment could not be read at all.
        ("unknown", None),
    ],
)
def test_kill_falls_back_to_start_time_when_the_marker_is_no_help(
    sandbox, monkeypatch, no_stray_signal, marker
):
    """Neither confirmed nor refuted, so the inequality decides — as before."""
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(
        jobs, "_live_group_members", lambda pgid: ([(5001, _SPAWNED_AT + 3.0)], True)
    )
    monkeypatch.setattr(jobs, "_process_marker", lambda pid: marker)

    out = jobs.kill(_identity_record())

    assert no_stray_signal == [(7777, signal.SIGTERM)]
    assert out["killed"] is True and out["reason_code"] == jobs.KILL_SIGNALLED


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
        jobs, "_live_group_members", lambda pgid: ([(5001, _SPAWNED_AT + 3.0)], True)
    )

    out = jobs.kill(_identity_record())

    assert out["killed"] is True and no_stray_signal == [(7777, signal.SIGTERM)]


@pytest.mark.parametrize(
    ("members", "expected_code"),
    [
        # A member older than the run: this group number was reused. Settled —
        # a retry reads the same thing.
        (([(5001, _SPAWNED_AT - 60.0)], True), jobs.KILL_GROUP_PREDATES_RUN),
        # The scan could not read every candidate, so a member may be unseen.
        # A measurement that failed, and a retry may answer.
        (([(5001, _SPAWNED_AT + 3.0)], False), jobs.KILL_GROUP_SCAN_INCOMPLETE),
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
        jobs, "_live_group_members", lambda pgid: ([(5001, _SPAWNED_AT + 3.0)], True)
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
        jobs, "_live_group_members", lambda pgid: ([(5001, _SPAWNED_AT - 60)], True)
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
    [
        # No identity fields at all: written before they were captured.
        {},
        # Present but malformed, which says as little as absent does.
        {"pid_create_time": "not-a-number", "pgid": 7777},
        {"pid_create_time": _SPAWNED_AT, "pgid": "7777"},
        {"pid_create_time": _SPAWNED_AT, "pgid": 0},
        {"pid_create_time": None, "pgid": None},
    ],
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
    assert out["killed"] is False and out["reason_code"] == jobs.KILL_LEGACY_NO_IDENTITY
    assert out["pid"] == 4242, "the operator needs the number to reap the group by hand"
    assert "predates process-identity capture" in out["reason"]
    assert jobs._read_job(rid)["status"] == "running", "a refusal changes no recorded status"


@pytest.mark.parametrize("created", [float("nan"), float("inf"), float("-inf"), True, False])
def test_kill_refuses_a_record_whose_start_time_cannot_be_compared(
    sandbox, monkeypatch, no_stray_signal, created
):
    """A start time that cannot act as one says nothing about the pid.

    Each of these satisfies the type check and then loses every comparison below
    it. A NaN is never within tolerance of a live start time. A boolean is an int
    to isinstance, so ``true`` becomes 1.0 and compares as a moment in 1970. Either
    way the leader would be reported as a reused pid, and that is the wrong fact —
    nothing has been established about the pid at all, only that the record cannot
    describe it. The refusal is the same, but the code and the reason must not
    claim otherwise.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: pytest.fail("pid must not be probed"))

    out = jobs.kill(_identity_record(created=created))

    assert no_stray_signal == []
    assert out["killed"] is False
    assert out["reason_code"] == jobs.KILL_IDENTITY_UNUSABLE
    assert "reused" not in out["reason"], "the pid was never established to be anything"


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
    submit() records, the leader really exits and is really reaped, and the
    group is enumerated from the OS.
    """
    import subprocess
    import time

    proc = subprocess.Popen(  # noqa: S603,S607
        ["sh", "-c", "sleep 30 & sleep 0.5"],
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

        rid = _identity_record(pid=proc.pid, pgid=pgid, created=created)
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
