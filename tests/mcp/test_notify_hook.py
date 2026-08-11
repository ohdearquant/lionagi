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
import sys
import time
from pathlib import Path

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
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        # The hook reads both streams to classify a failure, so a stand-in for a
        # finished process has to carry them.
        self.stdout = stdout
        self.stderr = stderr


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
    assert jobs.status(job)["notify_delivery"] == {"attempted": False}


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
    assert payload == {
        "run_id": job,
        "status": "failed",
        "label": "t1",
        "target": "downstream",
        # Empty, not absent: no sender was given, and the notifier is told that
        # rather than left to fill the gap from its working directory.
        "sender": "",
    }
    assert rec["notify_delivery"] == {
        "attempted": True,
        "ok": True,
        "exit_code": 0,
        "error": None,
        # a delivery that succeeded has no failure to classify
        "failure_class": None,
        # named from the configured template, so the record says which notifier
        # this was without keeping anything the command itself printed
        "command": "notify",
    }
    assert jobs.status(job)["notify_delivery"]["ok"] is True


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
        # The command said nothing this hook's closed vocabulary recognises, so
        # the classification is `unknown` rather than a quote of what it said.
        "error": None,
        "failure_class": "unknown",
        "command": "notify",
    }
    assert jobs.status(job)["notify_delivery"]["ok"] is False


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
    # No job record on disk: mark_terminal reports an absent record, delivery
    # still resolves to nothing, and the hook exits cleanly.
    rc = _notify_hook.main(["--run-id", "nope", "--status", "completed"])
    assert rc == 0
    assert calls == []


def _console_log(run_id: str) -> str:
    path = config.job_dir(run_id) / "console.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_failed_delivery_ends_the_leg_log_with_a_stated_failure(job, monkeypatch):
    """The log is the fallback for the notice, so it must say the notice died.

    Without this the log of a run whose notice never arrived is
    indistinguishable from the log of a run still working: it simply ends.
    """
    config.job_dir(job).mkdir(parents=True, exist_ok=True)
    (config.job_dir(job) / "console.log").write_text("work happened\n", encoding="utf-8")
    monkeypatch.setattr(_notify_hook.subprocess, "run", lambda *a, **k: _FakeCompleted(7))

    command = json.dumps(["notify", "{status}"])
    _notify_hook.main(["--run-id", job, "--status", "completed", "--command", command])

    log = _console_log(job)
    assert "work happened" in log  # appended, never rewritten
    assert "NOT delivered" in log
    assert "exit code 7" in log


def test_spawn_error_is_named_in_the_leg_log(job, monkeypatch):
    def boom(*_a, **_k):
        raise OSError("no such command")

    config.job_dir(job).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_notify_hook.subprocess, "run", boom)

    command = json.dumps(["nonexistent-notifier", "{status}"])
    _notify_hook.main(["--run-id", job, "--status", "completed", "--command", command])

    assert "OSError" in _console_log(job)


def test_a_configured_but_unusable_notifier_also_reaches_the_log(job, monkeypatch):
    """Recorded as a failure on the job, so it must read as one in the log too."""
    config.job_dir(job).mkdir(parents=True, exist_ok=True)
    _no_settings_notifier(monkeypatch)
    monkeypatch.setattr(_notify_hook.subprocess, "run", lambda *a, **k: _FakeCompleted(0))

    _notify_hook.main(["--run-id", job, "--status", "completed", "--command", "not json ["])

    assert "delivery_command_is_not_valid_json" in _console_log(job)


def test_successful_delivery_writes_nothing_to_the_log(job, monkeypatch):
    config.job_dir(job).mkdir(parents=True, exist_ok=True)
    (config.job_dir(job) / "console.log").write_text("work happened\n", encoding="utf-8")
    monkeypatch.setattr(_notify_hook.subprocess, "run", lambda *a, **k: _FakeCompleted(0))

    command = json.dumps(["notify", "{status}"])
    _notify_hook.main(["--run-id", job, "--status", "completed", "--command", command])

    assert _console_log(job) == "work happened\n"


def test_an_unverified_delivery_warns_in_the_log_instead_of_passing_silently(job, monkeypatch):
    """A degraded result only the record knows about is one nobody acts on.

    The command shape here exits zero when its send was refused, so the zero is
    not evidence. Recording that on the job was half the job: the log is where an
    operator actually looks, and an ordinary success there means they stop
    looking. The line has to say the notice is unconfirmed while not claiming a
    failure that probably did not happen.
    """
    config.job_dir(job).mkdir(parents=True, exist_ok=True)
    (config.job_dir(job) / "console.log").write_text("work happened\n", encoding="utf-8")
    monkeypatch.setattr(_notify_hook.subprocess, "run", lambda *a, **k: _FakeCompleted(0))

    command = json.dumps(["kkernel", "exec", "comm.send(to='recipient', content='{status}')"])
    _notify_hook.main(["--run-id", job, "--status", "completed", "--command", command])

    log = _console_log(job)
    assert "work happened" in log  # appended, never rewritten
    assert "WARNING" in log
    assert "kkernel_exec_without_strict_exits_zero_on_a_refused_op" in log
    # not reported as a failure: the notice most likely did arrive
    assert "NOT delivered" not in log
    # and the record still says the run's delivery did not fail
    assert jobs._read_job(job)["notify_delivery"]["ok"] is True
    assert jobs._read_job(job)["notify_delivery"]["delivery_verified"] is False


def test_the_same_command_with_strict_is_verified_and_stays_silent(job, monkeypatch):
    """The control: --strict makes the exit code mean what it says.

    Without this, a test asserting the warning fires proves only that the log can
    be written to, not that the marker is what decides it.
    """
    config.job_dir(job).mkdir(parents=True, exist_ok=True)
    (config.job_dir(job) / "console.log").write_text("work happened\n", encoding="utf-8")
    monkeypatch.setattr(_notify_hook.subprocess, "run", lambda *a, **k: _FakeCompleted(0))

    command = json.dumps(
        ["kkernel", "exec", "--strict", "comm.send(to='recipient', content='{status}')"]
    )
    _notify_hook.main(["--run-id", job, "--status", "completed", "--command", command])

    assert _console_log(job) == "work happened\n"
    assert "delivery_verified" not in jobs._read_job(job)["notify_delivery"]


def test_silence_by_choice_writes_nothing_to_the_log(job, monkeypatch):
    """Nothing configured is the documented default, not a delivery failure."""
    config.job_dir(job).mkdir(parents=True, exist_ok=True)
    (config.job_dir(job) / "console.log").write_text("work happened\n", encoding="utf-8")
    _no_settings_notifier(monkeypatch)

    _notify_hook.main(["--run-id", job, "--status", "completed"])

    assert _console_log(job) == "work happened\n"


def test_an_unwritable_log_does_not_break_the_terminal_path(job, monkeypatch):
    """The run already finished; a log that cannot be appended to is not fatal."""
    monkeypatch.setattr(_notify_hook.subprocess, "run", lambda *a, **k: _FakeCompleted(7))
    # A directory where the log file belongs: opening it for append raises an
    # OSError from the real filesystem rather than from a patched stand-in.
    (config.job_dir(job) / "console.log").mkdir(parents=True, exist_ok=True)

    command = json.dumps(["notify", "{status}"])
    rc = _notify_hook.main(["--run-id", job, "--status", "completed", "--command", command])

    assert rc == 0
    assert jobs._read_job(job)["notify_delivery"]["ok"] is False


def test_the_record_names_which_notifier_failed(job, monkeypatch):
    """A failed delivery says which notifier it was, without keeping its output.

    The exit code alone says a delivery failed; on a host with more than one
    notifier configured over time it does not say which. The program name is
    operator configuration, already readable by whoever wrote it, so naming it
    costs nothing the command's own output would have cost.
    """
    monkeypatch.setattr(_notify_hook.subprocess, "run", lambda *a, **k: _FakeCompleted(3))

    command = json.dumps(["notify-webhook", "--status", "{status}"])
    rc = _notify_hook.main(["--run-id", job, "--status", "completed", "--command", command])

    assert rc == 0
    outcome = jobs._read_job(job)["notify_delivery"]
    assert outcome["command"] == "notify-webhook"
    assert outcome["exit_code"] == 3 and outcome["error"] is None


def test_the_named_program_is_the_configured_token_not_the_substituted_one(job, monkeypatch):
    """The name comes from the template, so no run field can reach the record.

    Substitution puts run fields into the argv that is spawned. Taking the name
    from the template instead means what lands on the record is exactly the token
    an operator wrote in their settings, whatever the run was called.
    """
    spawned: list = []
    monkeypatch.setattr(
        _notify_hook.subprocess,
        "run",
        lambda argv, **_k: spawned.append(argv) or _FakeCompleted(1),
    )

    command = json.dumps(["notify-{status}"])
    rc = _notify_hook.main(["--run-id", job, "--status", "completed", "--command", command])

    assert rc == 0
    assert spawned == [["notify-completed"]]  # the run's field reached the argv
    assert jobs._read_job(job)["notify_delivery"]["command"] == "notify-{status}"


def test_a_notifier_that_never_started_stays_tellable_from_one_that_ran(job, monkeypatch):
    """Naming the program must not blur the two ways a delivery fails.

    "the notifier is not there" and "the notifier ran and rejected this" send an
    operator to different places, and the record has always told them apart by
    which of exit_code / error is filled in. Adding the name leaves that intact.
    """
    command = json.dumps(["notify-webhook", "{status}"])

    def _boom(*_a, **_k):
        raise OSError("no such command")

    monkeypatch.setattr(_notify_hook.subprocess, "run", _boom)
    _notify_hook.main(["--run-id", job, "--status", "completed", "--command", command])
    never_started = jobs._read_job(job)["notify_delivery"]

    monkeypatch.setattr(_notify_hook.subprocess, "run", lambda *a, **k: _FakeCompleted(3))
    _notify_hook.main(["--run-id", job, "--status", "completed", "--command", command])
    ran_and_failed = jobs._read_job(job)["notify_delivery"]

    assert never_started["command"] == ran_and_failed["command"] == "notify-webhook"
    assert never_started["exit_code"] is None and never_started["error"] == "OSError"
    assert ran_and_failed["exit_code"] == 3 and ran_and_failed["error"] is None


def test_a_configuration_with_no_program_in_it_names_none(job, monkeypatch):
    """An override that never parsed has no program, and none is invented."""
    monkeypatch.setattr(_notify_hook.subprocess, "run", lambda *a, **k: _FakeCompleted(0))
    _no_settings_notifier(monkeypatch)

    rc = _notify_hook.main(["--run-id", job, "--status", "completed", "--command", "not json ["])

    assert rc == 0
    outcome = jobs._read_job(job)["notify_delivery"]
    assert outcome["error"] == "delivery_command_is_not_valid_json"
    assert outcome["command"] is None


def test_the_delivery_commands_own_output_is_still_never_kept(job, monkeypatch):
    """Naming the program changes nothing about what the command may say.

    Its stdout/stderr are free text that can carry a credential the command
    obtained anywhere, so the record holds only fields this hook chose. That
    invariant is unchanged; what changed is how it's kept: output used to be
    discarded at the pipe, which also discarded any way to tell one failure
    from another, so every failed delivery recorded a bare exit code. It is
    now read, matched against a closed vocabulary, and dropped -- only the
    matched name is stored, so a future change letting an unmatched failure
    contribute its own words fails here instead of passing review.
    """
    seen: dict = {}

    def _run(argv, **kwargs):
        seen.update(kwargs)
        return _FakeCompleted(4, stdout="token=sk-live-AAAA", stderr="permission denied for /etc/x")

    monkeypatch.setattr(_notify_hook.subprocess, "run", _run)

    command = json.dumps(["notify-webhook"])
    _notify_hook.main(["--run-id", job, "--status", "completed", "--command", command])

    outcome = jobs._read_job(job)["notify_delivery"]
    assert set(outcome) == {"attempted", "ok", "exit_code", "error", "failure_class", "command"}
    assert outcome["failure_class"] in {n for n, _ in _notify_hook._FAILURE_CLASSES} | {
        _notify_hook._FAILURE_UNKNOWN
    }
    # Nothing the command said survives into the persisted record.
    for token in ("sk-live", "AAAA", "/etc/x"):
        assert token not in json.dumps(outcome)


# The run whose log this is, standing in for the CLI: it writes ordinary output,
# fires its --notify template the way the CLI does (shlex-split, ``{status}``
# replaced), and then writes more output before exiting. That last part is the
# whole point — the hook appends to a log whose writer is still running and
# still holding the descriptor it was spawned with.
_LI_SHIM_THAT_KEEPS_WRITING = """\
import shlex, subprocess, sys

argv = sys.argv[1:]
template = argv[argv.index("--notify") + 1]

sys.stdout.write("ordinary output before the notice\\n")
subprocess.run(
    [tok.replace("{status}", "completed") for tok in shlex.split(template)],
    check=False,
)
sys.stdout.write("ordinary final output\\n")
"""


_LI_SHIM_WITH_A_SHORT_OUTER_NOTIFY_BUDGET = """\
import asyncio, json, os, sys, time
from pathlib import Path

from lionagi.cli.orchestrate._notify import register_flow_notify_scope
from lionagi.mcp import config
from lionagi.state.lifecycle.callbacks import (
    EntityRef,
    RunTerminalEnvelope,
    TerminalCallbackRegistry,
)


async def main():
    argv = sys.argv[1:]
    template = argv[argv.index("--notify") + 1]
    run_id = os.environ[config.RUN_ID_ENV_VAR]
    run_dir = config.run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = run_dir / "run.json"
    manifest.write_text(json.dumps({"status": "running"}))

    registry = TerminalCallbackRegistry(budget_seconds=1.0)
    register_flow_notify_scope(
        registry,
        override=template,
        entity_kind="session",
        entity_id="session-1",
        invocation_id=None,
        flow_kind="agent",
        playbook=None,
        save_dir=None,
        cwd=os.getcwd(),
        started_at=time.time(),
    )
    await registry.emit(
        RunTerminalEnvelope(
            event_id="event-1",
            entity=EntityRef(kind="session", id="session-1"),
            previous_status="running",
            terminal_status="completed",
            reason_code="run.completed.ok",
            occurred_at=time.time(),
        )
    )
    manifest.write_text(json.dumps({"status": "completed"}))
    print("terminal manifest written", flush=True)


asyncio.run(main())
"""


_DELIVERY_THAT_SIGNALS_THEN_HANGS = """\
import sys, time
from pathlib import Path

Path(sys.argv[1]).write_text("delivered")
time.sleep(5)
"""


def test_outer_notify_timeout_keeps_an_attempt_record_after_delivery(monkeypatch, tmp_path):
    """A delivered notice cannot become null when the CLI times out its hook.

    The CLI callback owns a shorter deadline than the hook's delivery command.
    The delivery signals its externally visible side effect, then remains alive
    until the outer callback cancels the hook process group. The CLI writes its
    terminal manifest only after that callback returns, matching production
    ordering rather than calling the two writers sequentially in the test.
    """
    monkeypatch.setenv("LIONAGI_HOME", str(tmp_path))
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2]))
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "mcp" / "jobs")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")

    shim = tmp_path / "li_shim.py"
    shim.write_text(_LI_SHIM_WITH_A_SHORT_OUTER_NOTIFY_BUDGET)
    delivery = tmp_path / "delivery.py"
    delivery.write_text(_DELIVERY_THAT_SIGNALS_THEN_HANGS)
    delivered = tmp_path / "delivered"

    def _li_command():
        return [sys.executable, str(shim)]

    monkeypatch.setattr(config, "li_command", _li_command)

    handle = jobs.submit(
        "agent",
        [],
        notify_command=json.dumps([sys.executable, str(delivery), str(delivered)]),
        no_mcp_config=True,
    )
    run_id = handle["run_id"]
    manifest = tmp_path / "runs" / run_id / "run.json"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if delivered.exists() and manifest.exists():
            if json.loads(manifest.read_text()).get("status") == "completed":
                break
        time.sleep(0.05)
    else:
        raise AssertionError(f"the timeout race did not complete; log:\n{_console_log(run_id)}")

    assert delivered.read_text() == "delivered"
    status = jobs.status(run_id)
    assert status["terminal_source"] == jobs.TERMINAL_SOURCE_HOOK
    assert status["notify_delivery"]["attempted"] is True
    assert status["notify_delivery"]["ok"] is None
    assert status["notify_delivery"]["error"] == "delivery_outcome_unknown"
    row = next(item for item in jobs.list_jobs() if item["run_id"] == run_id)
    assert row["notify_delivery_state"] == "unknown"


def test_the_failure_notice_survives_the_runs_own_remaining_output(monkeypatch, tmp_path):
    """The appended notice must not be overwritten by the run that is still writing.

    The hook appends to the log while the run that owns it is alive, so the two
    write to one file through different descriptors. A run whose descriptor
    carries its own offset writes its next line back over whatever was appended
    behind it, and the notice — the only trace of a notice that never arrived —
    goes with it, on a log that was perfectly writable.

    End to end through ``submit`` because that is where the descriptor is
    opened: the mode it is opened in is the behaviour under test, and a test
    that opened its own would assert about itself.
    """
    # A lionagi home of this test's own, for this process and for every process
    # it starts: the hook runs in one of those and derives its own job directory
    # from the environment, so patching the constant here alone would leave the
    # two halves of this test writing to two different directories.
    monkeypatch.setenv("LIONAGI_HOME", str(tmp_path))
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "mcp" / "jobs")
    # The run is launched by absolute script path, so it imports whichever
    # lionagi its interpreter resolves — which is this checkout only when the
    # installed distribution happens to point here.
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2]))
    # A real delivery that really fails: the outcome the code builds from an
    # exit code of its own, not one handed to it.
    monkeypatch.setenv("LIONAGI_MCP_NOTIFY_COMMAND", json.dumps(["/bin/sh", "-c", "exit 1"]))
    shim = tmp_path / "li_shim.py"
    shim.write_text(_LI_SHIM_THAT_KEEPS_WRITING)
    monkeypatch.setattr(config, "li_command", lambda: [sys.executable, str(shim)])

    handle = jobs.submit("agent", [], no_mcp_config=True)
    _wait_for_run_to_finish(handle["run_id"])

    log = _console_log(handle["run_id"])
    # Positive control: the run itself wrote, and wrote last, so a missing
    # notice cannot be read as a probe that never ran the hook at all.
    assert "ordinary output before the notice" in log
    assert "ordinary final output" in log
    assert jobs._read_job(handle["run_id"])["notify_delivery"] == {
        "attempted": True,
        "ok": False,
        "exit_code": 1,
        "error": None,
        "failure_class": "unknown",
        "command": "/bin/sh",
    }
    assert "[notify]" in log
    assert "NOT delivered" in log


def _wait_for_run_to_finish(run_id: str, timeout: float = 60.0) -> None:
    """Wait until the run's last write has landed.

    The run is spawned detached, so there is nothing to wait on here. Waiting
    for its final line rather than for the delivery record is what makes the
    read that follows deterministic: that line is written after the hook has
    already run, so once it is on disk both writers are done and what the log
    holds is what it will hold.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if "ordinary final output" in _console_log(run_id):
            return
        time.sleep(0.05)
    raise AssertionError(f"the run never finished writing. log:\n{_console_log(run_id)}")


# --- the delivery's working directory decides which seat signs the notice ------


def _job_with(monkeypatch, tmp_path, **extra):
    """A job record carrying *extra*, on an isolated jobs root."""
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.delenv("LIONAGI_MCP_NOTIFY_COMMAND", raising=False)
    monkeypatch.delenv("LIONAGI_MCP_NOTIFY_TARGET", raising=False)
    monkeypatch.delenv("LIONAGI_MCP_NOTIFY_CWD", raising=False)
    rid = jobs.new_run_id()
    jobs._write_job({"run_id": rid, "kind": "agent", "label": "t1", "status": "running", **extra})
    return rid


def test_delivery_runs_where_the_run_was_submitted_not_where_it_ran(monkeypatch, tmp_path):
    """The notifier's identity comes from the submitting seat, not the run's directory.

    A notifier that resolves who it is from its working directory signs with
    whoever owns that directory. Inheriting would sign every notice with the
    owner of wherever the run happened to execute — a worktree, another seat's
    repo — silently, and downstream routing acts on that signature.
    """
    submit_dir = tmp_path / "seat"
    submit_dir.mkdir()
    run_dir = tmp_path / "worktree"
    run_dir.mkdir()
    rid = _job_with(monkeypatch, tmp_path, cwd=str(run_dir), submit_cwd=str(submit_dir))

    captured: dict = {}
    monkeypatch.setattr(
        _notify_hook.subprocess,
        "run",
        lambda *_a, **kw: captured.update(kw) or _FakeCompleted(0),
    )

    command = json.dumps(["notify", "{run_id}"])
    assert _notify_hook.main(["--run-id", rid, "--status", "completed", "--command", command]) == 0
    assert captured["cwd"] == str(submit_dir)
    assert captured["cwd"] != str(run_dir)


def test_both_callers_deliver_from_the_same_directory(monkeypatch, tmp_path):
    """The hook and the reap path sign the same notice the same way.

    They never share a working directory — the hook runs in the run's dying
    process, the reap path inside the server — so a delivery that inherits gives
    one run two possible senders depending on which caller got there first. The
    record is what makes them agree.
    """
    submit_dir = tmp_path / "seat"
    submit_dir.mkdir()
    run_dir = tmp_path / "worktree"
    run_dir.mkdir()
    command = json.dumps(["notify", "{run_id}"])
    rid = _job_with(
        monkeypatch,
        tmp_path,
        cwd=str(run_dir),
        submit_cwd=str(submit_dir),
        notify_command=command,
        status="exited",
    )

    seen: list = []
    monkeypatch.setattr(
        _notify_hook.subprocess,
        "run",
        lambda *_a, **kw: seen.append(kw.get("cwd")) or _FakeCompleted(0),
    )

    # caller 1: the run's own terminal hook
    assert _notify_hook.main(["--run-id", rid, "--status", "completed", "--command", command]) == 0
    # caller 2: the observer publishing an end for a run whose process never got there
    jobs._deliver_reap_notice(rid, jobs._read_job(rid))

    assert len(seen) == 2
    assert seen[0] == seen[1] == str(submit_dir)


def test_an_operator_can_override_the_delivery_directory(monkeypatch, tmp_path):
    """The escape hatch for a deployment whose notifier wants to run elsewhere."""
    submit_dir = tmp_path / "seat"
    submit_dir.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    rid = _job_with(monkeypatch, tmp_path, submit_cwd=str(submit_dir))
    monkeypatch.setenv("LIONAGI_MCP_NOTIFY_CWD", str(elsewhere))

    captured: dict = {}
    monkeypatch.setattr(
        _notify_hook.subprocess,
        "run",
        lambda *_a, **kw: captured.update(kw) or _FakeCompleted(0),
    )

    command = json.dumps(["notify", "{run_id}"])
    assert _notify_hook.main(["--run-id", rid, "--status", "completed", "--command", command]) == 0
    assert captured["cwd"] == str(elsewhere)


def test_a_named_directory_that_is_gone_refuses_rather_than_signing_elsewhere(
    monkeypatch, tmp_path
):
    """A missing delivery directory is a recorded refusal, never a quiet fallback.

    Falling back to the inherited directory would deliver the notice under an
    identity nobody chose, and that is invisible afterwards: the notice arrives,
    it is just signed by the wrong seat.
    """
    rid = _job_with(monkeypatch, tmp_path, submit_cwd=str(tmp_path / "removed"))

    calls: list = []
    monkeypatch.setattr(_notify_hook.subprocess, "run", lambda *a, **k: calls.append((a, k)))

    command = json.dumps(["notify", "{run_id}"])
    assert _notify_hook.main(["--run-id", rid, "--status", "completed", "--command", command]) == 0
    assert calls == []  # nothing was spawned
    assert jobs._read_job(rid)["notify_delivery"] == {
        "attempted": False,
        "ok": False,
        "exit_code": None,
        "error": "delivery_cwd_is_not_a_directory",
        "command": "notify",
    }


def test_a_record_without_a_submit_directory_still_delivers(monkeypatch, tmp_path):
    """Records written before the field are not refusals — they inherit, as they always did."""
    rid = _job_with(monkeypatch, tmp_path, cwd=None)

    captured: dict = {}
    monkeypatch.setattr(
        _notify_hook.subprocess,
        "run",
        lambda *_a, **kw: captured.update(kw) or _FakeCompleted(0),
    )

    command = json.dumps(["notify", "{run_id}"])
    assert _notify_hook.main(["--run-id", rid, "--status", "completed", "--command", command]) == 0
    assert captured["cwd"] is None
    assert jobs._read_job(rid)["notify_delivery"]["ok"] is True


def test_a_server_with_no_working_directory_does_not_strand_the_submission(monkeypatch):
    """The record is built before the write that publishes it, so this cannot raise."""
    monkeypatch.setattr(jobs.os, "getcwd", lambda: (_ for _ in ()).throw(OSError("gone")))
    assert jobs._submit_cwd() is None


def test_an_anchor_the_submission_could_not_read_refuses_rather_than_inheriting(
    monkeypatch, tmp_path
):
    """Present-and-null is an unavailable anchor, not an unasked-for one.

    A record whose own submission tried to note where it came from and could not
    must not quietly deliver from wherever the caller happens to be. Left to
    inherit, every job submitted after the server lost its directory would go
    back to the old caller-dependent signing and say nothing about it.
    """
    rid = _job_with(monkeypatch, tmp_path, submit_cwd=None)

    calls: list = []
    monkeypatch.setattr(_notify_hook.subprocess, "run", lambda *a, **k: calls.append((a, k)))

    command = json.dumps(["notify", "{run_id}"])
    assert _notify_hook.main(["--run-id", rid, "--status", "completed", "--command", command]) == 0
    assert calls == []
    assert jobs._read_job(rid)["notify_delivery"]["error"] == "delivery_cwd_unavailable_at_submit"


def test_a_record_missing_the_key_entirely_still_inherits(monkeypatch, tmp_path):
    """The control for the test above.

    These two differ by whether the key is there at all, and nothing else. A
    reader that refused both would satisfy the previous test and fail this one.
    """
    rid = _job_with(monkeypatch, tmp_path)
    assert "submit_cwd" not in jobs._read_job(rid)

    captured: dict = {}
    monkeypatch.setattr(
        _notify_hook.subprocess,
        "run",
        lambda *_a, **kw: captured.update(kw) or _FakeCompleted(0),
    )

    command = json.dumps(["notify", "{run_id}"])
    assert _notify_hook.main(["--run-id", rid, "--status", "completed", "--command", command]) == 0
    assert captured["cwd"] is None
    assert jobs._read_job(rid)["notify_delivery"]["ok"] is True


def test_two_identity_problems_are_both_reported(monkeypatch, tmp_path):
    """One record says everything wrong with the delivery, not the first thing.

    Stopping at the first reason costs an operator a second failed run to learn
    the second one.
    """
    rid = _job_with(monkeypatch, tmp_path, submit_cwd=str(tmp_path / "removed"))

    monkeypatch.setattr(_notify_hook.subprocess, "run", lambda *a, **k: _FakeCompleted(0))

    command = json.dumps(["notify", "{sender}"])  # asks for a sender; none is given
    assert _notify_hook.main(["--run-id", rid, "--status", "completed", "--command", command]) == 0
    error = jobs._read_job(rid)["notify_delivery"]["error"]
    assert "delivery_cwd_is_not_a_directory" in error
    assert "delivery_command_needs_a_sender_and_none_was_given" in error
