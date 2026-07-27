# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A child that refuses its own arguments dies in the first second, before it is
far enough along to report anything: the terminal notify hook runs in the child,
so a child that never got past parsing its instruction has nothing left to run
it.

Measured, on two real deaths minutes apart, both from the same submit surface:

    run 20260727T193608-efe5e6
      log: "spec field 'artifacts' is invalid: artifact contract must be a dict, got list"
    run 20260727T194158-cd9b08
      log: "spec field 'prompt' exceeds maximum length of 8192 characters"

Two different causes, one identical outward signature — a run_id, a live-looking
pid, status running, notify_delivery null — and in both cases the cause was
sitting in the log the whole time and reached nobody. A caller handed the message
fixes it in seconds; a caller handed a run id waits, and resubmits blind.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from lionagi.mcp import jobs


@pytest.fixture
def job_home(tmp_path, monkeypatch):
    # The package conftest shortens the watch for tests that fake the spawn.
    # These spawn real children, so they need a window long enough for a process
    # to start and exit on a loaded machine.
    monkeypatch.setattr(jobs, "_EARLY_EXIT_WATCH_SECONDS", 10.0)
    monkeypatch.setattr(jobs.config, "job_dir", lambda run_id: tmp_path / run_id)
    return tmp_path


def _spawn(script: str, job_home, run_id: str = "test-run"):
    """Run a real child that behaves like the CLI would, and hand it to the
    watcher exactly as submit() does."""
    d = job_home / run_id
    d.mkdir(parents=True, exist_ok=True)
    log_path = d / "console.log"
    with open(log_path, "wb") as log_f:
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=log_f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
    jobs._write_job(
        {
            "run_id": run_id,
            "pid": proc.pid,
            "kind": "play",
            "status": "running",
            "spawn_state": "started",
            "log": str(log_path),
            "submitted_at": jobs._now_iso(),
            "finished_at": None,
        }
    )
    return proc, log_path


_DIES_AT_VALIDATION = (
    "import sys; "
    "sys.stdout.write(\"error: spec field 'prompt' exceeds maximum length of 8192 characters\\n\"); "
    "sys.stdout.flush(); "
    "sys.exit(2)"
)


def test_a_startup_death_becomes_a_refusal_carrying_the_cause(job_home):
    proc, log_path = _spawn(_DIES_AT_VALIDATION, job_home)

    refusal = jobs._refusal_from_early_exit(proc, "test-run", log_path)

    assert refusal is not None
    assert "exceeds maximum length of 8192" in str(refusal)
    assert refusal.run_id == "test-run"


def test_the_record_stops_claiming_the_run_is_going(job_home):
    """The whole failure is a record that reads as running against a dead pid.
    Both halves have to change: the caller is told, and the record agrees."""
    proc, log_path = _spawn(_DIES_AT_VALIDATION, job_home)

    jobs._refusal_from_early_exit(proc, "test-run", log_path)

    record = jobs._read_job("test-run")
    assert record["status"] == "failed"
    assert record["finished_at"] is not None
    assert "8192" in record["reason"]


def test_the_log_is_kept_rather_than_cleared(job_home):
    """The cause is evidence. A refusal that deletes it leaves the caller with a
    message and nothing to go back to."""
    proc, log_path = _spawn(_DIES_AT_VALIDATION, job_home)

    jobs._refusal_from_early_exit(proc, "test-run", log_path)

    assert "8192" in log_path.read_text()


def test_a_different_cause_comes_through_verbatim(job_home):
    """The second measured death. The watcher must not classify causes, only
    carry them: it cannot know what the next spec defect will be."""
    script = (
        "import sys; "
        "sys.stdout.write(\"error: spec field 'artifacts' is invalid: "
        'artifact contract must be a dict, got list\\n"); '
        "sys.stdout.flush(); "
        "sys.exit(2)"
    )
    proc, log_path = _spawn(script, job_home)

    refusal = jobs._refusal_from_early_exit(proc, "test-run", log_path)

    assert "artifact contract must be a dict, got list" in str(refusal)


def test_a_silent_failure_still_refuses(job_home):
    """A child that dies without explaining itself is still a death. The exit
    status is a poor cause but it is not nothing."""
    proc, log_path = _spawn("import sys; sys.exit(3)", job_home)

    refusal = jobs._refusal_from_early_exit(proc, "test-run", log_path)

    assert refusal is not None
    assert "exit 3" in str(refusal)


def test_a_child_that_finishes_cleanly_is_not_a_refusal(job_home):
    """A fast success is a success. Its own terminal path has already recorded
    it, and rewriting it here would turn finished work into a failure."""
    proc, log_path = _spawn("pass", job_home)

    assert jobs._refusal_from_early_exit(proc, "test-run", log_path) is None
    assert jobs._read_job("test-run")["status"] == "running"


def test_a_live_child_is_left_alone(job_home, monkeypatch):
    """The ordinary case: the run gets past its arguments and keeps going. The
    watcher gives up at its deadline and reports nothing."""
    monkeypatch.setattr(jobs, "_EARLY_EXIT_WATCH_SECONDS", 0.3)
    proc, log_path = _spawn("import time; time.sleep(30)", job_home)
    try:
        assert jobs._refusal_from_early_exit(proc, "test-run", log_path) is None
        assert jobs._read_job("test-run")["status"] == "running"
    finally:
        proc.kill()
        proc.wait()


def test_the_watch_is_bounded(job_home, monkeypatch):
    """A run that outlives the window must not hold the submit open for the
    length of the run."""
    monkeypatch.setattr(jobs, "_EARLY_EXIT_WATCH_SECONDS", 0.2)
    proc, log_path = _spawn("import time; time.sleep(30)", job_home)
    try:
        import time as _time

        started = _time.monotonic()
        jobs._refusal_from_early_exit(proc, "test-run", log_path)
        elapsed = _time.monotonic() - started
    finally:
        proc.kill()
        proc.wait()

    assert elapsed < 2.0


def test_a_death_after_the_window_is_not_this_watchers_business(job_home, monkeypatch):
    """Deliberately bounded: a run that dies minutes in has a terminal hook to
    report it. Widening this window to cover that case would hold every submit
    open for the length of the run."""
    monkeypatch.setattr(jobs, "_EARLY_EXIT_WATCH_SECONDS", 0.2)
    proc, log_path = _spawn("import time; time.sleep(2); raise SystemExit(2)", job_home)
    try:
        assert jobs._refusal_from_early_exit(proc, "test-run", log_path) is None
    finally:
        proc.wait()


# ── The same thing through submit(), which is where a caller meets it ─────────


def test_submit_refuses_rather_than_handing_back_a_dead_run(tmp_path, monkeypatch):
    """End to end: the surface a director actually calls. Before this, submit()
    returned a run_id, a pid and status running for a run that was already
    gone."""
    monkeypatch.setattr(jobs, "_EARLY_EXIT_WATCH_SECONDS", 10.0)
    monkeypatch.setattr(jobs.config, "job_dir", lambda run_id: tmp_path / run_id)
    monkeypatch.setattr(
        jobs.config,
        "li_command",
        lambda: [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write(\"error: spec field 'prompt' exceeds maximum "
            'length of 8192 characters\\n"); sys.exit(2)',
        ],
    )

    with pytest.raises(jobs.SpawnError) as exc_info:
        jobs.submit("play", ["-p", "some-playbook"], no_mcp_config=True)

    assert "8192" in str(exc_info.value)
    assert exc_info.value.record["status"] == "failed"


def test_submit_still_returns_a_handle_for_a_run_that_starts(tmp_path, monkeypatch):
    """The counterfactual that matters: the refusal must not swallow ordinary
    submits. A run that gets past its arguments comes back as a handle."""
    monkeypatch.setattr(jobs, "_EARLY_EXIT_WATCH_SECONDS", 0.2)
    monkeypatch.setattr(jobs.config, "job_dir", lambda run_id: tmp_path / run_id)
    monkeypatch.setattr(
        jobs.config, "li_command", lambda: [sys.executable, "-c", "import time; time.sleep(30)"]
    )

    handle = jobs.submit("play", ["-p", "some-playbook"], no_mcp_config=True)
    try:
        assert handle["status"] == "running"
        assert handle["spawn_state"] == "started"
        assert handle["pid"] > 0
    finally:
        import os
        import signal

        os.kill(handle["pid"], signal.SIGKILL)
