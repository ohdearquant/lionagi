# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Offline checks for serialized run/global notification outcomes."""

from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from lionagi.cli._notify_outcomes import DurableRunRecorder
from lionagi.cli._runs import RunDir


@pytest.fixture
def run(tmp_path):
    return RunDir(
        run_id="run-a", state_root=tmp_path / "state", artifact_root=tmp_path / "artifacts"
    )


def test_recorder_is_inert_before_first_attempt(run):
    DurableRunRecorder(run)
    assert not run.state_root.exists()


@pytest.mark.parametrize("first", ["run", "global"])
@pytest.mark.parametrize(
    "run_ok,global_ok", [(True, True), (True, False), (False, True), (False, False)]
)
def test_both_outcomes_survive_and_failure_selection_is_stable(run, first, run_ok, global_ok):
    recorder = DurableRunRecorder(run)
    calls = {
        "run": dict(
            ok=run_ok,
            exit_code=0 if run_ok else 12,
            stderr_text=None,
            reason=None if run_ok else "run_failed",
        ),
        "global": dict(
            ok=global_ok,
            exit_code=0 if global_ok else 13,
            stderr_text=None,
            reason=None if global_ok else "global_failed",
        ),
    }
    for key in (first, "global" if first == "run" else "run"):
        recorder.record(key, **calls[key])
    payload = json.loads(run.notify_outcome_path.read_text())
    assert set(payload["subscriptions"]) == {"run", "global"}
    assert payload["ok"] is (run_ok and global_ok)
    selected = "run" if not run_ok or global_ok else "global"
    assert payload["exit_code"] == calls[selected]["exit_code"]
    assert payload.get("reason") == calls[selected]["reason"]


def test_concurrent_subscriptions_are_both_recorded(run):
    recorder = DurableRunRecorder(run)
    ready = Barrier(2)

    def record(key, code):
        ready.wait(timeout=3)
        return recorder.record(
            key, ok=False, exit_code=code, stderr_text=None, reason=f"{key}_failed"
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(record, "run", 2), pool.submit(record, "global", 3)]
        assert [future.result(timeout=5) for future in futures] == [None, None]
    payload = json.loads(run.notify_outcome_path.read_text())
    assert set(payload["subscriptions"]) == {"run", "global"}
    assert payload["ok"] is False
    assert payload["exit_code"] == 2
    assert payload["reason"] == "run_failed"


@pytest.mark.skipif(not hasattr(os, "fchmod"), reason="mode bits unavailable")
def test_existing_global_stderr_is_owner_only_and_payload_never_contains_text(run):
    run.state_root.mkdir(parents=True)
    stderr_path = run.state_root / "notify_stderr.global.log"
    stderr_path.write_text("old")
    stderr_path.chmod(0o666)
    recorder = DurableRunRecorder(run)
    secret = "sensitive adapter output"
    assert recorder.record("global", ok=False, exit_code=9, stderr_text=secret, reason=None) == str(
        stderr_path
    )
    assert stat.S_IMODE(stderr_path.stat().st_mode) == 0o600
    assert stderr_path.read_text() == secret
    assert secret not in run.notify_outcome_path.read_text()


def test_recording_failure_does_not_raise_or_log_stderr(run, monkeypatch, caplog):
    recorder = DurableRunRecorder(run)
    secret = "sensitive adapter output"

    def fail(*args, **kwargs):
        raise OSError(secret)

    monkeypatch.setattr(DurableRunRecorder, "_capture_stderr", fail)
    monkeypatch.setattr(RunDir, "write_notify_outcome", fail)
    assert recorder.record("global", ok=False, exit_code=1, stderr_text=secret, reason=None) is None
    assert secret not in caplog.text


def test_cancellation_is_not_swallowed(run, monkeypatch):
    recorder = DurableRunRecorder(run)

    def cancel(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(RunDir, "write_notify_outcome", cancel)
    with pytest.raises(KeyboardInterrupt):
        recorder.record("run", ok=False, exit_code=None, stderr_text=None, reason="rejected")


def test_existing_mcp_outcome_reader_sees_any_subscription_failure(run, monkeypatch):
    from lionhub.serve.mcp import jobs

    recorder = DurableRunRecorder(run)
    monkeypatch.setattr(jobs.config, "run_dir", lambda run_id: run.state_root)
    assert not jobs._notice_recorded_undelivered(run.run_id)
    recorder.record("global", ok=True, exit_code=0, stderr_text=None, reason=None)
    assert not jobs._notice_recorded_undelivered(run.run_id)
    recorder.record("run", ok=False, exit_code=2, stderr_text=None, reason="run_failed")
    assert jobs._notice_recorded_undelivered(run.run_id)


@pytest.mark.parametrize(
    "subscription,name", [("run", "notify_stderr.log"), ("global", "notify_stderr.global.log")]
)
@pytest.mark.parametrize("link_kind", ["hard", "symbolic"])
def test_stderr_replacement_does_not_write_through_existing_links(
    run, tmp_path, subscription, name, link_kind
):
    run.state_root.mkdir(parents=True)
    target = tmp_path / "unrelated.txt"
    target.write_text("unchanged")
    destination = run.state_root / name
    if link_kind == "hard":
        os.link(target, destination)
    else:
        destination.symlink_to(target)
    recorder = DurableRunRecorder(run)
    assert recorder.record(
        subscription, ok=False, exit_code=1, stderr_text="private", reason=None
    ) == str(destination)
    assert target.read_text() == "unchanged"
    assert destination.read_text() == "private"
    assert not destination.is_symlink()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_stderr_fd_is_closed_if_fdopen_fails(run, monkeypatch):
    from lionagi.cli import _notify_outcomes

    recorder = DurableRunRecorder(run)
    original_fdopen = os.fdopen
    failed_fds = []

    def fail_once(fd, *args, **kwargs):
        if not failed_fds:
            failed_fds.append(fd)
            raise OSError("fdopen failed")
        return original_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(_notify_outcomes.os, "fdopen", fail_once)
    assert (
        recorder.record("global", ok=False, exit_code=1, stderr_text="private", reason=None) is None
    )
    with pytest.raises(OSError):
        os.fstat(failed_fds[0])
    assert not list(run.state_root.glob(".notify_stderr.global.log.*"))


def test_global_configuration_rejection_survives_run_success(run):
    recorder = DurableRunRecorder(run)
    recorder.record("run", ok=True, exit_code=0, stderr_text=None, reason=None)
    recorder.record(
        "global", ok=False, exit_code=None, stderr_text=None, reason="python_adapter_unresolvable"
    )
    payload = json.loads(run.notify_outcome_path.read_text())
    assert payload["ok"] is False
    assert payload["exit_code"] is None
    assert payload["stderr_path"] is None
    assert payload["reason"] == "python_adapter_unresolvable"


def test_run_rejection_takes_precedence_over_global_failure_stderr(run):
    recorder = DurableRunRecorder(run)
    recorder.record("global", ok=False, exit_code=9, stderr_text="private", reason="global_failed")
    recorder.record("run", ok=False, exit_code=None, stderr_text=None, reason="run_rejected")
    payload = json.loads(run.notify_outcome_path.read_text())
    assert payload["ok"] is False
    assert payload["exit_code"] is None
    assert payload["stderr_path"] is None
    assert payload["reason"] == "run_rejected"
    assert payload["subscriptions"]["global"]["stderr_path"] is not None
