# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The terminal notice says who it is from, instead of leaving the notifier to
resolve an identity from whatever directory the run happened to work in."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from lionagi.mcp import _notify_hook, config, jobs


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
    submit_dir = tmp_path / "submitter"
    submit_dir.mkdir()
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "li_command", lambda: ["echo"])
    monkeypatch.setattr(jobs, "_submit_cwd", lambda: str(submit_dir))
    monkeypatch.setattr(jobs, "_read_lifecycle", lambda _run_id: None)
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


def test_an_unreachable_resolver_names_the_fallback_that_works(monkeypatch, tmp_path):
    """The refusal has to say which door is still open.

    An unreachable resolver refuses the SENDER, not the run: dropping
    `notify_sender` submits normally, because validation is skipped when there
    is nothing to validate. Without that sentence the caller reads `unavailable`
    as "spawning is down" and stops, which is a wrong-cause diagnosis produced by
    a correct refusal.
    """

    def unreachable(_cwd):
        raise jobs.NotifySenderResolutionError("could not launch kkernel: [Errno 2]")

    monkeypatch.setattr(jobs, "_resolve_submitter_identity", unreachable)

    with pytest.raises(jobs.NotifySenderResolutionError) as caught:
        jobs._validate_notify_sender("agent:someone", str(tmp_path))

    message = str(caught.value)
    assert "omit notify_sender" in message, message
    assert "could not launch kkernel" in message, "the underlying cause must survive"
    assert "agent:someone" in message


def test_an_unavailable_submitting_directory_names_it_too():
    """The other unresolvable path. Both or neither -- one of two is a trap."""
    with pytest.raises(jobs.NotifySenderResolutionError) as caught:
        jobs._validate_notify_sender("agent:someone", None)

    assert "omit notify_sender" in str(caught.value)


def test_a_mismatch_keeps_its_own_wording(monkeypatch, tmp_path):
    """The must-not-match arm: the unsigned fallback belongs to the outage paths only.

    A mismatch is a claim about identity, not an outage, and the caller still
    has a SIGNED option -- so that message names the actor to use, and it
    already mentions omitting the field. What it must not grow is the *unsigned
    run* sentence, which would read as "ask for less and it goes through": the
    wrong lesson to draw from a refusal about identity.
    """
    monkeypatch.setattr(jobs, "_resolve_submitter_identity", lambda _cwd: "agent:actual")

    with pytest.raises(jobs.NotifySenderMismatchError) as caught:
        jobs._validate_notify_sender("agent:requested", str(tmp_path))

    message = str(caught.value)
    assert jobs._NOTIFY_SENDER_FALLBACK not in message, message
    assert "without sender attestation" not in message, message
    assert "agent:actual" in message


def test_the_fallback_does_not_promise_a_notice_the_run_may_not_get(monkeypatch, tmp_path):
    """The advice and the delivery path have to agree, and they did not.

    Dropping `notify_sender` is sound advice about the SUBMISSION: validation is
    skipped when there is nothing to validate, and the run goes through. It was
    stated as sound advice about the NOTICE too, and there it is false whenever
    the configured command interpolates `{sender}` -- that template is refused
    outright with no sender, so the run is submitted and its completion notice
    never arrives. A refusal the caller can see, traded for a silence they cannot.

    The delivery half is asserted alongside the wording, and re-runs the hook to
    do it, because that is what keeps the wording assertion honest: if the
    refusal in the delivery path is ever dropped, this goes red on the first
    assert instead of leaving behind a qualification that no longer qualifies.

    Read off the raised error rather than off the module constant, because the
    constant is not what any caller sees. A wording assertion that passes on a
    string no longer reaching the message is exactly the shape this test exists
    to refuse.
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
    assert spawned == [], "the sender-less run really does go unnotified under this template"

    with pytest.raises(jobs.NotifySenderResolutionError) as caught:
        jobs._validate_notify_sender("agent:someone", None)
    message = str(caught.value)

    assert "or is not sent at all" in message, message
    assert "{sender}" in message, "the condition has to name what triggers it"


def test_the_fallback_promises_nothing_where_the_notifier_is_simply_off(monkeypatch, tmp_path):
    """The row the conditional clause does not cover, and must not have to.

    With no notify command configured at all, nothing is delivered -- and the
    sender is not why. Supplying one produces the same `attempted: False`, so
    this is the notifier being off rather than the advice costing the caller
    anything. Both are asserted: it is the pair that shows the omission is not
    the cause, and one of them alone would read as another suppression case.

    What the wording therefore may not do is assert a notice, because in this
    configuration there is none to assert. Speaking of whatever notice the
    configuration sends claims nothing here and stays exact where a notice does
    go out, which is why the fix is a weaker sentence rather than a longer one
    enumerating configurations the caller already knows about.
    """
    _job(monkeypatch, tmp_path)
    # Asserted rather than assumed: `_job` clears this as a side effect, and a
    # premise held by somebody else's helper is one refactor from making this
    # test measure a configured notifier while still passing.
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
    assert with_sender == without, "the sender is not what decides this row"

    with pytest.raises(jobs.NotifySenderResolutionError) as caught:
        jobs._validate_notify_sender("agent:someone", None)
    message = str(caught.value)

    assert "the run then notifies" not in message, message
    assert "whatever notice" in message, message


def test_the_conditional_clause_names_the_environment_that_lifts_it(monkeypatch, tmp_path):
    """The row that made the unqualified sentence false.

    `notify_sender` is not the only way a sender reaches the notifier:
    LIONAGI_MCP_NOTIFY_SENDER feeds the same field. So a `{sender}` template is
    substituted and sent even when the submission carried no sender, and the
    advice saying such a notice "is not sent at all" was simply wrong wherever
    that variable is set -- wrong in a sentence a person reads while deciding
    what to do, which is the kind that survives.

    The delivery half is asserted first and reads the spawned argv rather than
    the fact of spawning, because a notice sent with the placeholder left
    unsubstituted would satisfy a bare "did it spawn" and mean the opposite.
    """
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
        "the environment sender has to be substituted, not merely not-refused"
    )

    with pytest.raises(jobs.NotifySenderResolutionError) as caught:
        jobs._validate_notify_sender("agent:someone", None)
    message = str(caught.value)

    assert "LIONAGI_MCP_NOTIFY_SENDER supplies none" in message, message
