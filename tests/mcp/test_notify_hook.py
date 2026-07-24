# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the terminal notify hook.

The hook is best-effort: it always records the terminal status on the job, then
delivers a notice only through a *configured* command (never a hardcoded one),
substituting run fields into its argv. The delivery outcome is recorded on the
job so a dead notice is visible, not silently lost. subprocess.run is mocked so
no real command is spawned.
"""

from __future__ import annotations

import json

import pytest

from lionagi.mcp import _notify_hook, config, jobs


@pytest.fixture
def job(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.delenv("LIONAGI_MCP_NOTIFY_COMMAND", raising=False)
    monkeypatch.delenv("LIONAGI_MCP_NOTIFY_TARGET", raising=False)
    rid = jobs.new_run_id()
    jobs._write_job(
        {
            "run_id": rid,
            "pid": 4242,
            "kind": "agent",
            "label": "t1",
            "cwd": None,
            "status": "running",
            "log": None,
        }
    )
    return rid


class _FakeCompleted:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode


def _no_settings_notifier(monkeypatch):
    monkeypatch.setattr(
        "lionagi.state.lifecycle.notify_settings.resolve_notify_config",
        lambda **_kw: None,
    )


def test_marks_terminal_without_delivery(job, monkeypatch):
    """No command configured: the status is recorded and nothing is spawned."""
    calls: list = []
    monkeypatch.setattr(_notify_hook.subprocess, "run", lambda *a, **k: calls.append((a, k)))
    _no_settings_notifier(monkeypatch)  # lionagi's notify.on_terminal resolves to nothing

    rc = _notify_hook.main(["--run-id", job, "--status", "completed"])
    assert rc == 0
    rec = jobs._read_job(job)
    assert rec["status"] == "completed"
    assert calls == []  # nothing delivered
    assert rec["notify_delivery"] == {"attempted": False}


def test_command_override_substitutes_and_delivers(job, monkeypatch):
    captured: dict = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["input"] = kw.get("input")
        return _FakeCompleted(0)

    monkeypatch.setattr(_notify_hook.subprocess, "run", fake_run)

    command = json.dumps(["notify", "{run_id}", "{status}", "{label}", "{target}"])
    rc = _notify_hook.main(
        ["--run-id", job, "--status", "failed", "--target", "downstream", "--command", command]
    )
    assert rc == 0
    rec = jobs._read_job(job)
    assert rec["status"] == "failed"
    assert captured["argv"] == ["notify", job, "failed", "t1", "downstream"]
    # the same fields are offered as a JSON payload on stdin
    payload = json.loads(captured["input"])
    assert payload == {"run_id": job, "status": "failed", "label": "t1", "target": "downstream"}
    assert rec["notify_delivery"] == {
        "attempted": True,
        "ok": True,
        "exit_code": 0,
        "error": None,
    }


def test_delivery_failure_is_recorded_not_silent(job, monkeypatch):
    """A dead completion notice surfaces on the record, never a silent drop."""
    monkeypatch.setattr(_notify_hook.subprocess, "run", lambda *a, **k: _FakeCompleted(7))

    command = json.dumps(["notify", "{status}"])
    rc = _notify_hook.main(["--run-id", job, "--status", "completed", "--command", command])
    assert rc == 0
    assert jobs._read_job(job)["notify_delivery"] == {
        "attempted": True,
        "ok": False,
        "exit_code": 7,
        "error": None,
    }


def test_delivery_spawn_error_is_recorded(job, monkeypatch):
    def boom(*_a, **_k):
        raise OSError("no such command")

    monkeypatch.setattr(_notify_hook.subprocess, "run", boom)

    command = json.dumps(["nonexistent-notifier", "{status}"])
    rc = _notify_hook.main(["--run-id", job, "--status", "completed", "--command", command])
    assert rc == 0
    outcome = jobs._read_job(job)["notify_delivery"]
    assert outcome["attempted"] is True and outcome["ok"] is False
    assert outcome["error"] == "OSError"


def test_malformed_command_override_delivers_nothing(job, monkeypatch):
    calls: list = []
    monkeypatch.setattr(_notify_hook.subprocess, "run", lambda *a, **k: calls.append(a))
    _no_settings_notifier(monkeypatch)

    rc = _notify_hook.main(["--run-id", job, "--status", "completed", "--command", "not json ["])
    assert rc == 0
    assert calls == []
    assert jobs._read_job(job)["notify_delivery"] == {"attempted": False}


def test_unknown_run_id_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.delenv("LIONAGI_MCP_NOTIFY_COMMAND", raising=False)
    calls: list = []
    monkeypatch.setattr(_notify_hook.subprocess, "run", lambda *a, **k: calls.append(a))
    _no_settings_notifier(monkeypatch)
    # No job record on disk: mark_terminal returns None, delivery still resolves
    # to nothing, and the hook exits cleanly.
    rc = _notify_hook.main(["--run-id", "nope", "--status", "completed"])
    assert rc == 0
    assert calls == []
