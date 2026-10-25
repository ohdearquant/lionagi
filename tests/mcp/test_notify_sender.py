# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The terminal notice says who it is from, instead of leaving the notifier to
resolve an identity from whatever directory the run happened to work in."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from lionhub.serve.mcp import _notify_hook, config, jobs


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


def test_submitter_identity_uses_the_canonical_khive_resolver(monkeypatch, tmp_path):
    """Admission asks the same directory-aware resolver the notifier trusts."""
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        payload = {
            "results": [
                {
                    "ok": True,
                    "result": {"actor_id": "agent:submitter"},
                    "tool": "whoami",
                }
            ],
            "status": "success",
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload).encode(), b"")

    monkeypatch.setattr(jobs.shutil, "which", lambda command: "/opt/khive/bin/kkernel")
    monkeypatch.setattr(jobs.subprocess, "run", fake_run)

    assert jobs._resolve_submitter_identity(str(tmp_path)) == "agent:submitter"
    assert captured["argv"] == [
        "/opt/khive/bin/kkernel",
        "exec",
        "whoami()",
        "--presentation",
        "agent",
        "--output-format",
        "json",
        "--strict",
    ]
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["timeout"] == jobs._NOTIFY_IDENTITY_TIMEOUT_SECONDS
    assert captured["kwargs"]["check"] is False


def _submit_sandbox(monkeypatch, tmp_path):
    submit_dir = _signing_dir(tmp_path / "submitter")
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "li_command", lambda: ["echo"])
    monkeypatch.setattr(jobs, "_submit_cwd", lambda: str(submit_dir))
    monkeypatch.setattr(jobs, "_read_lifecycle", lambda _run_id: None)
    monkeypatch.delenv(config.SENDER_ROOTS_ENV_VAR, raising=False)
    return submit_dir


def test_submit_rejects_a_sender_the_submitting_directory_cannot_sign_for(monkeypatch, tmp_path):
    submit_dir = _submit_sandbox(monkeypatch, tmp_path)
    monkeypatch.setattr(jobs, "_resolve_submitter_identity", lambda _cwd: "agent:actual-submitter")
    spawned: list = []
    monkeypatch.setattr(
        jobs.subprocess, "Popen", lambda *args, **kwargs: spawned.append((args, kwargs))
    )

    with pytest.raises(jobs.NotifySenderMismatchError) as caught:
        jobs.submit(
            "agent",
            [],
            prompt="work",
            notify_sender="agent:requested-sender",
            no_mcp_config=True,
        )

    message = str(caught.value)
    assert "agent:requested-sender" in message
    assert "agent:actual-submitter" in message
    assert str(submit_dir) in message
    assert spawned == []
    assert not config.JOBS_DIR.exists(), "identity refusal must precede run reservation"


def test_submit_without_a_sender_does_not_require_identity_resolution(monkeypatch, tmp_path):
    submit_dir = _submit_sandbox(monkeypatch, tmp_path)

    def should_not_resolve(_cwd):
        raise AssertionError("an omitted notify_sender must not resolve an identity")

    class Spawned:
        pid = 999_999

    monkeypatch.setattr(jobs, "_resolve_submitter_identity", should_not_resolve)
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *args, **kwargs: Spawned())

    handle = jobs.submit("agent", [], prompt="work", no_mcp_config=True)
    record = jobs._read_job(handle["run_id"])

    assert record["notify_sender"] is None
    assert record["submit_cwd"] == str(submit_dir)


def test_submit_without_a_sender_is_allowed_when_its_directory_is_unavailable(
    monkeypatch, tmp_path
):
    _submit_sandbox(monkeypatch, tmp_path)
    monkeypatch.setattr(jobs, "_submit_cwd", lambda: None)

    def should_not_resolve(_cwd):
        raise AssertionError("an omitted notify_sender must not resolve an identity")

    class Spawned:
        pid = 999_999

    spawned: list = []
    monkeypatch.setattr(jobs, "_resolve_submitter_identity", should_not_resolve)
    monkeypatch.setattr(
        jobs.subprocess, "Popen", lambda *args, **kwargs: spawned.append(args) or Spawned()
    )

    handle = jobs.submit("agent", [], prompt="work", no_mcp_config=True)
    record = jobs._read_job(handle["run_id"])

    assert spawned
    assert record["notify_sender"] is None
    assert record["submit_cwd"] is None


def test_submit_accepts_the_actor_resolved_from_the_submitting_directory(monkeypatch, tmp_path):
    submit_dir = _submit_sandbox(monkeypatch, tmp_path)

    class Spawned:
        pid = 999_999

    monkeypatch.setattr(jobs, "_resolve_submitter_identity", lambda _cwd: "agent:submitter")
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *args, **kwargs: Spawned())

    handle = jobs.submit(
        "agent",
        [],
        prompt="work",
        notify_sender="agent:submitter",
        no_mcp_config=True,
    )
    record = jobs._read_job(handle["run_id"])

    assert record["notify_sender"] == "agent:submitter"
    assert record["submit_cwd"] == str(submit_dir)


def _job(monkeypatch, tmp_path):
    from lionhub.serve.mcp import config

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
    """A notice requiring a sender records why delivery was not attempted."""
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


def test_a_resolver_error_explains_omitting_the_sender(monkeypatch, tmp_path):
    signer = _signing_dir(tmp_path / "signer")

    def fail_resolution(_cwd):
        raise jobs.NotifySenderResolutionError("identity resolution timed out")

    monkeypatch.setattr(jobs, "_resolve_submitter_identity", fail_resolution)

    with pytest.raises(jobs.NotifySenderResolutionError) as caught:
        jobs._validate_notify_sender("agent:someone", str(signer))

    message = str(caught.value)
    assert "omit notify_sender" in message, message
    assert "identity resolution timed out" in message
    assert "whatever notice" in message
    assert "agent:someone" in message


def test_an_unavailable_submitting_directory_names_the_unsigned_option():
    with pytest.raises(jobs.NotifySenderResolutionError) as caught:
        jobs._validate_notify_sender("agent:someone", None)

    message = str(caught.value)
    assert "the submitting directory is unavailable" in message
    assert "omit notify_sender" in message


def test_a_mismatch_keeps_its_own_wording(monkeypatch, tmp_path):
    """A mismatch names the resolved actor and the option that matches it."""
    signer = _signing_dir(tmp_path / "signer")
    monkeypatch.setattr(jobs, "_resolve_submitter_identity", lambda _cwd: "agent:actual")

    with pytest.raises(jobs.NotifySenderMismatchError) as caught:
        jobs._validate_notify_sender("agent:requested", str(signer))

    message = str(caught.value)
    assert message == (
        "notify_sender 'agent:requested' does not match actor 'agent:actual' resolved from "
        f"signing directory {str(signer)!r}; use 'agent:actual' or omit notify_sender"
    )


def test_the_fallback_does_not_promise_delivery_when_notifications_are_off(
    monkeypatch, tmp_path, plugin_home
):
    """An omitted sender does not guarantee a configured notification."""
    assert os.environ["HOME"] == str(plugin_home)
    _job(monkeypatch, tmp_path)
    assert "LIONAGI_MCP_NOTIFY_COMMAND" not in os.environ
    monkeypatch.setattr(
        _notify_hook.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("nothing is configured; nothing may be spawned"),
    )

    without = _notify_hook.deliver_terminal_notice("run-1", {}, "completed")
    with_sender = _notify_hook.deliver_terminal_notice(
        "run-1", {}, "completed", sender="agent:someone"
    )

    assert without == {"attempted": False}
    assert with_sender == without, "the sender does not change this result"

    def fail_resolution(_cwd):
        raise jobs.NotifySenderResolutionError("identity resolution timed out")

    monkeypatch.setattr(jobs, "_resolve_submitter_identity", fail_resolution)
    signer = _signing_dir(tmp_path / "signer")
    with pytest.raises(jobs.NotifySenderResolutionError) as caught:
        jobs._validate_notify_sender("agent:someone", str(signer))
    message = str(caught.value)

    assert "omit notify_sender" in message, message
    assert "whatever notice" in message, message
    assert "not sent at all" in message, message


def test_environment_sender_fills_a_template_without_a_submit_sender(monkeypatch, tmp_path):
    """The process-wide sender can fill a command's ``{sender}`` placeholder."""
    rid = _job(monkeypatch, tmp_path)
    spawned: list = []
    monkeypatch.setattr(
        _notify_hook.subprocess, "Popen", lambda *a, **k: spawned.append(a) or _Completed()
    )
    monkeypatch.setenv("LIONAGI_MCP_NOTIFY_SENDER", "agent:from-the-environment")

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
    assert spawned, "a sender from the environment fills {sender}, so the notice does go out"
    assert "agent:from-the-environment" in str(spawned), (
        "the environment sender must be substituted into the command"
    )


# --- the sender is judged and signed at the run's directory ------------------


def _signing_dir(path):
    """A directory khive can resolve an actor from: the one file its resolver reads."""
    (path / ".khive").mkdir(parents=True, exist_ok=True)
    (path / ".khive" / "config.toml").write_text('[actor]\nid = "agent:test"\n')
    return path


def test_a_sender_is_judged_at_the_run_directory_the_submission_names(monkeypatch, tmp_path):
    """A server serving several callers from one directory cannot answer for them.

    The run directory is the caller's, so that is where the claim is checked and
    where the notice is later signed.
    """
    _submit_sandbox(monkeypatch, tmp_path)
    signer = _signing_dir(tmp_path / "signer")
    monkeypatch.setenv(config.SENDER_ROOTS_ENV_VAR, str(tmp_path))
    asked: list[str] = []

    class Spawned:
        pid = 999_999

    def resolve(cwd):
        asked.append(cwd)
        return "agent:signer"

    monkeypatch.setattr(jobs, "_resolve_submitter_identity", resolve)
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *args, **kwargs: Spawned())

    handle = jobs.submit(
        "agent",
        [],
        prompt="work",
        cwd=str(signer),
        notify_sender="agent:signer",
        no_mcp_config=True,
    )
    record = jobs._read_job(handle["run_id"])

    assert asked == [os.path.abspath(signer)], "the claim was judged somewhere else"
    assert record["sender_cwd"] == os.path.abspath(signer)
    assert record["submit_cwd"] != record["sender_cwd"]


def test_without_configured_roots_a_declared_sender_is_judged_at_the_submitting_directory(
    monkeypatch, tmp_path
):
    """Unset roots keep the pre-existing anchor and accept what it accepted."""
    submit_dir = _submit_sandbox(monkeypatch, tmp_path)
    elsewhere = _signing_dir(tmp_path / "elsewhere")
    asked: list[str] = []

    class Spawned:
        pid = 999_999

    def resolve(cwd):
        asked.append(cwd)
        return "agent:submitter"

    monkeypatch.setattr(jobs, "_resolve_submitter_identity", resolve)
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *args, **kwargs: Spawned())

    handle = jobs.submit(
        "agent",
        [],
        prompt="work",
        cwd=str(elsewhere),
        notify_sender="agent:submitter",
        no_mcp_config=True,
    )
    record = jobs._read_job(handle["run_id"])

    assert asked == [str(submit_dir)]
    assert record["sender_cwd"] == str(submit_dir)
    assert record["submit_cwd"] == str(submit_dir)


def test_without_configured_roots_the_server_directory_need_not_hold_an_actor_config(
    monkeypatch, tmp_path
):
    """Resolution alone decided the server directory's claim before this change."""
    submit_dir = tmp_path / "submitter"
    submit_dir.mkdir()
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "li_command", lambda: ["echo"])
    monkeypatch.setattr(jobs, "_submit_cwd", lambda: str(submit_dir))
    monkeypatch.setattr(jobs, "_read_lifecycle", lambda _run_id: None)
    monkeypatch.delenv(config.SENDER_ROOTS_ENV_VAR, raising=False)
    monkeypatch.setattr(jobs, "_resolve_submitter_identity", lambda _cwd: "agent:submitter")

    class Spawned:
        pid = 999_999

    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *args, **kwargs: Spawned())

    jobs.submit("agent", [], prompt="work", notify_sender="agent:submitter", no_mcp_config=True)


def test_a_submission_without_a_sender_records_no_signing_directory(monkeypatch, tmp_path):
    _submit_sandbox(monkeypatch, tmp_path)
    signer = _signing_dir(tmp_path / "signer")

    class Spawned:
        pid = 999_999

    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *args, **kwargs: Spawned())

    handle = jobs.submit("agent", [], prompt="work", cwd=str(signer), no_mcp_config=True)

    assert jobs._read_job(handle["run_id"])["sender_cwd"] is None


def test_a_caller_named_anchor_outside_the_configured_roots_is_refused(monkeypatch, tmp_path):
    """A caller picks which of its directories judges it; the server bounds the set."""
    allowed = _signing_dir(tmp_path / "allowed" / "signer")
    elsewhere = _signing_dir(tmp_path / "elsewhere" / "signer")
    monkeypatch.setenv(config.SENDER_ROOTS_ENV_VAR, str(tmp_path / "allowed"))
    monkeypatch.setattr(jobs, "_resolve_submitter_identity", lambda _cwd: "agent:signer")

    with pytest.raises(jobs.NotifySenderResolutionError) as caught:
        jobs._validate_notify_sender("agent:signer", str(elsewhere), caller_named=True)
    message = str(caught.value)
    assert str(tmp_path / "allowed") in message
    assert str(elsewhere) in message

    # the control: inside the root, the same claim passes
    jobs._validate_notify_sender("agent:signer", str(allowed), caller_named=True)


def test_the_server_s_own_directory_is_not_bounded_by_the_roots(monkeypatch, tmp_path):
    """The list bounds a caller's choice; the server's own directory is nobody's to pick."""
    monkeypatch.setenv(config.SENDER_ROOTS_ENV_VAR, str(tmp_path / "somewhere-else"))
    monkeypatch.setattr(jobs, "_resolve_submitter_identity", lambda _cwd: "agent:signer")

    jobs._validate_notify_sender("agent:signer", str(_signing_dir(tmp_path / "server")))


def test_a_link_under_an_allowed_root_that_points_out_of_it_is_refused(monkeypatch, tmp_path):
    """Containment is tested on the resolved path, not the written one."""
    outside = _signing_dir(tmp_path / "outside" / "signer")
    (tmp_path / "allowed").mkdir()
    link = tmp_path / "allowed" / "signer"
    link.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv(config.SENDER_ROOTS_ENV_VAR, str(tmp_path / "allowed"))
    monkeypatch.setattr(jobs, "_resolve_submitter_identity", lambda _cwd: "agent:signer")

    with pytest.raises(jobs.NotifySenderResolutionError):
        jobs._validate_notify_sender("agent:signer", str(link), caller_named=True)


def test_a_relative_sender_root_is_refused_by_name(monkeypatch, tmp_path):
    """A relative root would resolve against whatever directory this process holds."""
    monkeypatch.setenv(config.SENDER_ROOTS_ENV_VAR, "signers")

    with pytest.raises(jobs.NotifySenderResolutionError) as caught:
        jobs._validate_notify_sender("agent:signer", str(tmp_path), caller_named=True)
    assert "'signers'" in str(caught.value) and "absolute" in str(caught.value)


def test_a_caller_named_directory_without_an_actor_config_cannot_sign_for_anyone(
    monkeypatch, tmp_path
):
    """A caller-chosen anchor must name its actor before its claim is resolved."""
    monkeypatch.setenv(config.SENDER_ROOTS_ENV_VAR, str(tmp_path))
    (tmp_path / "bare").mkdir()

    def should_not_resolve(_cwd):
        raise AssertionError("a directory with no actor config must be refused before resolution")

    monkeypatch.setattr(jobs, "_resolve_submitter_identity", should_not_resolve)

    with pytest.raises(jobs.NotifySenderResolutionError) as caught:
        jobs._validate_notify_sender("agent:someone", str(tmp_path / "bare"), caller_named=True)

    message = str(caught.value)
    assert ".khive/config.toml" in message
    assert "omit notify_sender" in message


def test_delivery_runs_from_the_directory_the_sender_was_attested_against(tmp_path):
    signer = _signing_dir(tmp_path / "signer")
    submit_dir = _signing_dir(tmp_path / "submitter")
    job = {"sender_cwd": str(signer), "submit_cwd": str(submit_dir)}

    assert _notify_hook._resolve_delivery_cwd(job, None) == (str(signer), None)
    # the control: with nothing attested the submitting directory is still the anchor
    assert _notify_hook._resolve_delivery_cwd(
        {"sender_cwd": None, "submit_cwd": str(submit_dir)}, None
    ) == (str(submit_dir), None)
    # and an explicit override still wins over both
    assert _notify_hook._resolve_delivery_cwd(job, str(submit_dir)) == (str(submit_dir), None)


def test_a_value_holding_placeholder_text_is_not_expanded():
    argv = _notify_hook._substitute(
        ["notify", "{label}", "{target}"],
        {"label": "my-{sender}", "sender": "seat-a", "target": "{run_id}", "run_id": "r"},
    )
    assert argv == ["notify", "my-{sender}", "{run_id}"]


def test_a_reason_placeholder_with_no_value_becomes_empty():
    argv = _notify_hook._substitute(
        ["notify", "{status}", "{reason_code}", "{reason_detail}"], {"status": "completed"}
    )
    assert argv == ["notify", "completed", "", ""]


def test_a_terminal_reason_code_fills_its_placeholder():
    argv = _notify_hook._substitute(
        ["notify", "{status}", "{reason_code}"],
        {
            "status": "completed",
            "reason_code": "run.completed.spawn_refused",
        },
    )
    assert argv == ["notify", "completed", "run.completed.spawn_refused"]
