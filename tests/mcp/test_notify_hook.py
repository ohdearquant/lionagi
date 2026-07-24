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
from lionagi.state.lifecycle.notify_settings import (
    NotifyConfigResolution,
    ResolvedNotifyHandler,
)


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
        lambda **_kw: NotifyConfigResolution(),
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


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ("not json [", "delivery_command_is_not_valid_json"),
        (json.dumps({"cmd": "notify"}), "delivery_command_is_not_a_list_of_strings"),
        (json.dumps(["notify", 7]), "delivery_command_is_not_a_list_of_strings"),
        (json.dumps([]), "delivery_command_is_empty"),
    ],
)
def test_unusable_command_override_is_recorded_as_a_failure(job, monkeypatch, override, reason):
    """A configured-but-unusable notifier must not read as an unconfigured one.

    Both deliver nothing, so the record is the only thing that tells them apart.
    A caller waiting on a completion notice that can never arrive has to be able
    to find out why, and the named reason is where it says so.
    """
    calls: list = []
    monkeypatch.setattr(_notify_hook.subprocess, "run", lambda *a, **k: calls.append(a))
    _no_settings_notifier(monkeypatch)

    rc = _notify_hook.main(["--run-id", job, "--status", "completed", "--command", override])
    assert rc == 0  # the terminal path still never fails
    assert calls == []  # and nothing is spawned
    outcome = jobs._read_job(job)["notify_delivery"]
    assert outcome["attempted"] is False
    assert outcome["ok"] is False
    assert outcome["error"] == reason
    # The distinction that matters: this is not the shape a silent default takes.
    assert outcome != {"attempted": False}


def test_configured_notifier_without_a_command_is_recorded_as_a_failure(job, monkeypatch):
    """A notifier this hook cannot run is configured, not absent."""
    calls: list = []
    monkeypatch.setattr(_notify_hook.subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(
        "lionagi.state.lifecycle.notify_settings.resolve_notify_config",
        lambda **_kw: NotifyConfigResolution(
            handler=ResolvedNotifyHandler(python_ref="os.path:join")
        ),
    )

    rc = _notify_hook.main(["--run-id", job, "--status", "completed"])
    assert rc == 0
    assert calls == []
    outcome = jobs._read_job(job)["notify_delivery"]
    assert outcome["error"] == "configured_notifier_has_no_delivery_command"


def test_unreadable_notify_settings_are_recorded_as_a_failure(job, monkeypatch):
    """Settings that raise must not be reported as no notifier configured."""
    calls: list = []
    monkeypatch.setattr(_notify_hook.subprocess, "run", lambda *a, **k: calls.append(a))

    def _boom(**_kw):
        raise RuntimeError("settings file is corrupt")

    monkeypatch.setattr("lionagi.state.lifecycle.notify_settings.resolve_notify_config", _boom)

    rc = _notify_hook.main(["--run-id", job, "--status", "completed"])
    assert rc == 0  # a broken settings file still cannot break the terminal path
    assert calls == []
    outcome = jobs._read_job(job)["notify_delivery"]
    assert outcome["error"] == "notify_settings_unreadable:RuntimeError"


def _settings_notifier(monkeypatch, on_terminal):
    """Drive the real resolver with *on_terminal* as lionagi's own setting."""
    monkeypatch.setattr(
        "lionagi.state.lifecycle.notify_settings.load_settings",
        lambda project_dir=None: {"notify": {"on_terminal": on_terminal}},
    )


@pytest.mark.parametrize(
    ("on_terminal", "reason"),
    [
        ("notify-hook | grep x", "on_terminal_command_requires_shell_features"),
        ('notify-hook "unbalanced', "on_terminal_command_not_parseable"),
        ("   ", "on_terminal_command_is_empty"),
        (12345, "on_terminal_not_string_or_mapping"),
        (
            {"enabled": True, "adapter": {"kind": "exec", "argv": []}},
            "on_terminal_command_is_empty",
        ),
        (
            {"enabled": True, "adapter": {"kind": "exec", "argv": "notify-hook"}},
            "exec_adapter_argv_not_a_list_of_strings",
        ),
        ({"enabled": True}, "enabled_without_adapter"),
        (
            {"enabled": True, "adapter": {"kind": "carrier-pigeon"}},
            "adapter_kind_unsupported",
        ),
        (
            {"enabled": True, "adapter": {"kind": "python", "ref": "no-colon"}},
            "python_adapter_ref_invalid",
        ),
        (
            {
                "enabled": True,
                "adapter": {"kind": "exec", "argv": ["notify-hook"]},
                "filter": "session",
            },
            "filter_not_a_mapping",
        ),
        (
            {
                "enabled": True,
                "adapter": {"kind": "exec", "argv": ["notify-hook"]},
                "filter": {"unexpected": True},
            },
            "filter_has_unknown_keys",
        ),
        (
            {
                "enabled": True,
                "adapter": {"kind": "exec", "argv": ["notify-hook"]},
                "filter": {"kinds": 0},
            },
            "filter_kinds_not_a_list_of_strings",
        ),
        (
            {
                "enabled": True,
                "adapter": {"kind": "exec", "argv": ["notify-hook"]},
                "filter": {"kinds": ["not-a-terminal-entity"]},
            },
            "filter_kinds_unsupported",
        ),
        (
            {
                "enabled": True,
                "adapter": {"kind": "exec", "argv": ["notify-hook"]},
                "filter": {"ids": 1},
            },
            "filter_ids_not_a_list_of_strings",
        ),
    ],
)
def test_rejected_settings_notifier_is_recorded_as_a_failure(job, monkeypatch, on_terminal, reason):
    """A notifier that was configured wrong is a failure, never the default silence.

    Every one of these settings shapes asked for a notice. None of them can
    deliver one. Reporting them the way an unconfigured notifier is reported
    would tell the operator they configured nothing, when what they actually
    have is a notice that will never arrive.
    """
    calls: list = []
    monkeypatch.setattr(_notify_hook.subprocess, "run", lambda *a, **k: calls.append(a))
    _settings_notifier(monkeypatch, on_terminal)

    rc = _notify_hook.main(["--run-id", job, "--status", "completed"])
    assert rc == 0  # the terminal path still never fails
    assert calls == []  # and nothing is spawned
    outcome = jobs._read_job(job)["notify_delivery"]
    assert outcome["error"] == reason
    assert outcome["ok"] is False
    assert outcome != {"attempted": False}  # not the shape a silent default takes


@pytest.mark.parametrize(
    "on_terminal",
    [
        None,  # notify.on_terminal absent entirely
        {"enabled": False, "adapter": {"kind": "exec", "argv": ["should-not-run"]}},
        {"adapter": None},  # a mapping that never asked to be enabled
    ],
)
def test_silence_by_choice_stays_silence(job, monkeypatch, on_terminal):
    """The chosen-silence shapes must not become failures: nothing was asked for."""
    calls: list = []
    monkeypatch.setattr(_notify_hook.subprocess, "run", lambda *a, **k: calls.append(a))
    _settings_notifier(monkeypatch, on_terminal)

    rc = _notify_hook.main(["--run-id", job, "--status", "completed"])
    assert rc == 0
    assert calls == []
    assert jobs._read_job(job)["notify_delivery"] == {"attempted": False}


def test_settings_notifier_resolves_and_delivers(job, monkeypatch):
    """The happy path still resolves through settings and delivers."""
    captured: dict = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return _FakeCompleted(0)

    monkeypatch.setattr(_notify_hook.subprocess, "run", fake_run)
    _settings_notifier(monkeypatch, "notify-hook {run_id} {status}")

    rc = _notify_hook.main(["--run-id", job, "--status", "completed"])
    assert rc == 0
    assert captured["argv"] == ["notify-hook", job, "completed"]
    assert jobs._read_job(job)["notify_delivery"]["ok"] is True


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
