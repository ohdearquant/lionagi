# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The terminal notice says who it is from, instead of leaving the notifier to
resolve an identity from whatever directory the run happened to work in."""

from __future__ import annotations

from lionagi.mcp import _notify_hook, jobs


def test_sender_substitutes_into_the_delivery_command():
    argv = _notify_hook._substitute(
        ["notify", "--from", "{sender}", "--to", "{target}"],
        {"sender": "seat-a", "target": "seat-b", "status": "completed", "run_id": "r"},
    )
    assert argv == ["notify", "--from", "seat-a", "--to", "seat-b"]


def test_sender_is_published_to_the_delivery_environment():
    env = _notify_hook._delivery_env("seat-a")
    assert env is not None
    assert env["LIONAGI_NOTIFY_SENDER"] == "seat-a"
    # Inherits the rest: a notifier still needs its own PATH and credentials.
    assert "PATH" in env


def test_no_sender_leaves_the_environment_untouched():
    """Without a sender there is nothing to publish, and an env dict built here
    would claim an identity was set when none was."""
    assert _notify_hook._delivery_env("") is None


def test_hook_command_carries_the_sender_when_one_is_given():
    template = jobs._notify_template("run-1", "seat-b", None, "seat-a")
    assert "--sender seat-a" in template


def test_hook_command_omits_the_sender_when_none_is_given():
    """A guard on the ordinary case: an absent sender must not become an empty
    --sender token, which the hook would read as an explicit empty identity."""
    template = jobs._notify_template("run-1", "seat-b", None, None)
    assert "--sender" not in template


def _job(monkeypatch, tmp_path):
    from lionagi.mcp import config

    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.delenv("LIONAGI_MCP_NOTIFY_COMMAND", raising=False)
    monkeypatch.delenv("LIONAGI_MCP_NOTIFY_SENDER", raising=False)
    rid = jobs.new_run_id()
    jobs._write_job(
        {
            "run_id": rid,
            "pid": 1,
            "kind": "agent",
            "label": "t",
            "cwd": None,
            "status": "running",
            "log": None,
        }
    )
    return rid


def test_no_delivery_runs_when_the_template_needs_a_sender_and_none_was_given(
    monkeypatch, tmp_path
):
    """A command that asks who the notice is from cannot be run without an answer.

    Substituting an empty string hands the delivery tool a blank where an
    identity belongs, and a tool that accepts it — or resolves a sender of its
    own — signs the notice with the wrong seat. Nothing is spawned; the reason
    is recorded, so job_status shows a notifier that could not deliver rather
    than the silence of one never asked.
    """
    rid = _job(monkeypatch, tmp_path)
    spawned: list = []
    monkeypatch.setattr(
        _notify_hook.subprocess, "Popen", lambda *a, **k: spawned.append(a) or _Completed()
    )

    rc = _notify_hook.main(
        [
            "--run-id",
            rid,
            "--status",
            "completed",
            "--command",
            '["notify", "--from", "{sender}", "--to", "seat-b"]',
        ]
    )

    assert rc == 0
    assert spawned == []  # the point: no process was started
    rec = jobs._read_job(rid)
    assert rec["notify_delivery"]["attempted"] is False
    assert rec["notify_delivery"]["error"] == "delivery_command_needs_a_sender_and_none_was_given"
    # A notifier refused for want of a sender is one an operator wants named:
    # the template is dropped here, so the program is taken before that happens.
    assert rec["notify_delivery"]["command"] == "notify"


def test_a_template_that_needs_a_sender_delivers_when_one_is_given(monkeypatch, tmp_path):
    """The guard is on the missing sender alone, not on the placeholder."""
    rid = _job(monkeypatch, tmp_path)
    spawned: list = []

    def fake_popen(argv, **kw):
        spawned.append(argv)
        return _Completed()

    monkeypatch.setattr(_notify_hook.subprocess, "Popen", fake_popen)

    rc = _notify_hook.main(
        [
            "--run-id",
            rid,
            "--status",
            "completed",
            "--sender",
            "seat-a",
            "--command",
            '["notify", "--from", "{sender}"]',
        ]
    )

    assert rc == 0
    assert spawned == [["notify", "--from", "seat-a"]]


def test_a_template_without_the_placeholder_is_unaffected_by_a_missing_sender(
    monkeypatch, tmp_path
):
    """A non-regression guard: a command that never asks who it is from still
    delivers with no sender supplied."""
    rid = _job(monkeypatch, tmp_path)
    spawned: list = []

    def fake_popen(argv, **kw):
        spawned.append(argv)
        return _Completed()

    monkeypatch.setattr(_notify_hook.subprocess, "Popen", fake_popen)

    rc = _notify_hook.main(
        ["--run-id", rid, "--status", "completed", "--command", '["notify", "{run_id}"]']
    )

    assert rc == 0
    assert spawned == [["notify", rid]]


class _Completed:
    """Popen-shaped: the hook holds a handle and drives it through communicate."""

    returncode = 0
    pid = -1

    def communicate(self, input: str | None = None, timeout: float | None = None):
        return "", ""

    def kill(self) -> None:
        pass
