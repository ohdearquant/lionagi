# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the background job engine.

Popen is mocked throughout so no real `li` process is spawned; the tests assert
on the argv/env the engine builds and on the on-disk job records it reads back.
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
