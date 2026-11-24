# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""An agent submission that names ``hooks_from`` carries the file's deny rules to its leg.

The server reads the file once, refuses guards the leg cannot apply, writes a copy beside
the job record and hands the leg the copy's path and digest. The leg reads the copy, checks
the digest and the same refusals, and gives the deny rules to its Claude Code models, which
put them in the one ``--settings`` value they pass the CLI. Hook entries are applied by an
API-model leg only (``test_hooks_from_api_legs.py``).
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lionagi._errors import ConfigurationError
from lionhub.hooks.hooks_from import (
    CarryPlan,
    HooksFromError,
    carry_refusal,
    read_hooks_file,
    read_snapshot,
    write_snapshot,
)
from lionhub.serve.mcp import config, dispatch, jobs

agent_cli = importlib.import_module("lionhub.serve.cli.agent")
child_sandbox = importlib.import_module("lionhub.agent.child_sandbox")
claude_code = importlib.import_module("lionhub.providers.anthropic.claude_code")

RULE = "Bash(rm:*)"
DENY_ONLY = {"permissions": {"deny": [RULE]}}
WITH_HOOKS = {
    "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"command": "/bin/true"}]}]},
    "permissions": {"deny": [RULE]},
}


def _hooks_file(tmp_path: Path, document: dict[str, Any], name: str = "guards.json") -> Path:
    path = tmp_path.resolve() / name
    path.write_text(json.dumps(document))
    return path


def _plan(*, hooks: dict | None = None, deny: tuple[str, ...] = ()) -> CarryPlan:
    return CarryPlan(
        hooks=hooks or {},
        deny_rules=deny,
        ask_rules_as_deny=0,
        left_out=(),
        dropped_keys=(),
        dropped_other=0,
    )


# ---- the copy and the refusals ----------------------------------------------------


def test_the_copy_holds_the_accepted_bytes_and_is_owner_only(tmp_path):
    source = read_hooks_file(str(_hooks_file(tmp_path, DENY_ONLY)))
    target = tmp_path / "copy.json"

    write_snapshot(target, source)

    assert target.read_bytes() == source.data
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_the_copy_is_never_written_over_an_existing_file(tmp_path):
    source = read_hooks_file(str(_hooks_file(tmp_path, DENY_ONLY)))
    target = tmp_path / "copy.json"
    target.write_text("kept")

    with pytest.raises(FileExistsError):
        write_snapshot(target, source)
    assert target.read_text() == "kept"


def test_the_copy_is_never_written_through_a_link(tmp_path):
    source = read_hooks_file(str(_hooks_file(tmp_path, DENY_ONLY)))
    elsewhere = tmp_path / "elsewhere.json"
    target = tmp_path / "copy.json"
    target.symlink_to(elsewhere)

    with pytest.raises(OSError):
        write_snapshot(target, source)
    assert not elsewhere.exists()


def test_a_copy_read_back_must_hash_to_the_recorded_digest(tmp_path):
    path = _hooks_file(tmp_path, DENY_ONLY)
    digest = read_hooks_file(str(path)).sha256

    assert read_snapshot(str(path), digest).document == DENY_ONLY
    path.write_text(json.dumps({"permissions": {"deny": []}}))
    with pytest.raises(HooksFromError) as caught:
        read_snapshot(str(path), digest)
    assert caught.value.rule == "digest"


def test_hook_entries_are_refused_on_every_cli_provider_with_the_way_out():
    plan = _plan(hooks={"PreToolUse": [{"hooks": []}]}, deny=(RULE,))

    for claude in (True, False):
        reason = carry_refusal(plan, claude_code=claude)
        assert reason is not None and 'hooks_from_carry=["deny"]' in reason


def test_hook_entries_are_applied_by_an_api_model_leg():
    plan = _plan(hooks={"PreToolUse": [{"hooks": []}]})

    assert carry_refusal(plan, claude_code=False, api=True) is None
    reason = carry_refusal(_plan(deny=(RULE,)), claude_code=False, api=True)
    assert reason is not None and 'hooks_from_carry=["hooks"]' in reason


def test_deny_rules_are_applied_by_a_claude_code_leg_only():
    plan = _plan(deny=(RULE,))

    assert carry_refusal(plan, claude_code=True) is None
    assert "only a claude_code leg" in (carry_refusal(plan, claude_code=False) or "")


def test_a_plan_with_nothing_to_apply_runs_anywhere():
    assert carry_refusal(_plan(), claude_code=False) is None


# ---- the Claude Code request ------------------------------------------------------


@pytest.fixture
def claude_stubs(monkeypatch, tmp_path):
    home = tmp_path.resolve() / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(claude_code, "resolve_claude_cli", lambda: "claude")
    monkeypatch.setattr(
        child_sandbox, "_cli_version", lambda binary: child_sandbox.CLAUDE_CLI_FLOOR
    )
    monkeypatch.setattr(child_sandbox, "check_sandbox_backend", lambda: None)


@pytest.fixture
def sandbox_on(monkeypatch):
    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: {"child_sandbox": {}})


@pytest.fixture
def sandbox_off(monkeypatch):
    monkeypatch.setattr(
        child_sandbox, "_global_settings", lambda: {"child_sandbox": {"enabled": False}}
    )


def _settings_values(request) -> list[str]:
    args = request.as_cmd_args()
    return [args[i + 1] for i, arg in enumerate(args) if arg == "--settings"]


def _work(tmp_path: Path) -> Path:
    work = tmp_path.resolve() / "work"
    work.mkdir(exist_ok=True)
    return work


@pytest.mark.usefixtures("claude_stubs", "sandbox_on")
def test_carried_rules_join_the_sandbox_settings(tmp_path):
    request = claude_code.ClaudeCodeRequest(prompt="hi", repo=_work(tmp_path), carried_deny=(RULE,))

    (value,) = _settings_values(request)
    settings = json.loads(value)
    assert RULE in settings["permissions"]["deny"]
    assert "sandbox" in settings


@pytest.mark.usefixtures("claude_stubs", "sandbox_off")
def test_carried_rules_are_passed_with_the_sandbox_off(tmp_path):
    control = claude_code.ClaudeCodeRequest(prompt="hi", repo=_work(tmp_path))
    assert _settings_values(control) == []

    request = control.model_copy(update={"carried_deny": (RULE,)})

    (value,) = _settings_values(request)
    assert json.loads(value) == {"permissions": {"deny": [RULE]}}


@pytest.mark.usefixtures("claude_stubs", "sandbox_off")
def test_carried_rules_keep_the_settings_the_leg_already_passes(tmp_path):
    work = _work(tmp_path)
    (work / "mine.json").write_text(
        json.dumps({"model": "kept", "permissions": {"deny": ["Read(//etc/kept)"]}})
    )
    for existing in ("mine.json", (work / "mine.json").read_text()):
        request = claude_code.ClaudeCodeRequest(
            prompt="hi", repo=work, settings=existing, carried_deny=(RULE,)
        )

        (value,) = _settings_values(request)
        settings = json.loads(value)
        assert settings["model"] == "kept"
        assert settings["permissions"]["deny"] == ["Read(//etc/kept)", RULE]


def test_the_carried_rules_are_left_out_of_the_serialized_request(tmp_path):
    request = claude_code.ClaudeCodeRequest(prompt="hi", repo=_work(tmp_path), carried_deny=(RULE,))

    assert "carried_deny" not in request.model_dump()


# ---- agent.submit -----------------------------------------------------------------


class _FakeProc:
    pid = 4242

    def wait(self, timeout: float | None = None) -> int:
        return 0


@pytest.fixture
def submitted(monkeypatch, tmp_path):
    """Job state under tmp and two profiles; the run's own spawn is recorded, not started."""
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr("lionhub.orchestration.runs.RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(config, "li_command", lambda: ["echo"])
    monkeypatch.setattr(jobs, "_read_lifecycle", lambda run_id: None)
    monkeypatch.delenv(config.CHILD_ACTOR_SUFFIX_ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    agents = home / ".lionagi" / "agents"
    agents.mkdir(parents=True)
    (agents / "on-claude.md").write_text("---\nmodel: claude_code/sonnet\n---\nWork.\n")
    (agents / "on-codex.md").write_text("---\nmodel: codex/gpt-5\n---\nWork.\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    spawned: list[list[str]] = []
    real_popen = jobs.subprocess.Popen

    def fake_popen(argv, **kw):
        if argv[0] != "echo":
            return real_popen(argv, **kw)
        spawned.append(list(argv))
        return _FakeProc()

    monkeypatch.setattr(jobs.subprocess, "Popen", fake_popen)
    return spawned


def _call(**kwargs):
    return asyncio.run(dispatch.request(**kwargs))


def _submit(args: dict[str, Any]) -> dict[str, Any]:
    fingerprint = _call(help="agent.submit")["schema_fingerprint"]
    op = {"op": "agent.submit", "args": args, "schema_fingerprint": fingerprint}
    return _call(ops=[op])["ops"][0]


def _snapshot_flag(argv: list[str]) -> dict[str, Any]:
    (index,) = [i for i, token in enumerate(argv) if token == "--hooks-from-snapshot"]
    return json.loads(argv[index + 1])


def test_a_claude_leg_gets_a_copy_its_flag_and_a_record(submitted, tmp_path):
    path = _hooks_file(tmp_path, DENY_ONLY)
    original = path.read_bytes()

    answer = _submit(
        {
            "agent": "on-claude",
            "prompt": "w",
            "no_mcp_config": True,
            "hooks_from": str(path),
            "hooks_from_format": "claude",
        }
    )

    assert answer["ok"] is True, answer
    run_id = answer["result"]["run_id"]
    record = jobs._read_job(run_id)["hooks_from"]
    assert record["sha256"] == hashlib.sha256(original).hexdigest()
    assert (record["format"], record["carry"]) == ("claude", ["hooks", "deny"])
    assert record["resolved_path"] == str(path)
    copy = Path(record["copy"])
    assert copy.read_bytes() == original
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600
    assert answer["result"]["hooks_from"] == record
    (argv,) = submitted
    assert _snapshot_flag(argv) == {
        "copy": record["copy"],
        "sha256": record["sha256"],
        "format": "claude",
        "carry": ["hooks", "deny"],
        "source": str(path),
    }


def test_an_edit_after_submit_does_not_reach_the_copy(submitted, tmp_path):
    path = _hooks_file(tmp_path, DENY_ONLY)
    answer = _submit(
        {
            "agent": "on-claude",
            "prompt": "w",
            "no_mcp_config": True,
            "hooks_from": str(path),
            "hooks_from_format": "claude",
        }
    )
    assert answer["ok"] is True, answer
    record = jobs._read_job(answer["result"]["run_id"])["hooks_from"]

    path.write_text(json.dumps({}))

    assert read_snapshot(record["copy"], record["sha256"]).document == DENY_ONLY


def test_hook_entries_refuse_the_submission_until_deny_alone_is_asked(submitted, tmp_path):
    path = _hooks_file(tmp_path, WITH_HOOKS)
    args = {
        "agent": "on-claude",
        "prompt": "w",
        "no_mcp_config": True,
        "hooks_from": str(path),
        "hooks_from_format": "claude",
    }

    refused = _submit(args)

    assert refused["ok"] is False, refused
    assert "hook entries" in json.dumps(refused)
    assert submitted == []
    accepted = _submit({**args, "hooks_from_carry": ["deny"]})
    assert accepted["ok"] is True, accepted
    assert _snapshot_flag(submitted[0])["carry"] == ["deny"]


def test_deny_rules_refuse_a_leg_on_another_provider(submitted, tmp_path):
    path = _hooks_file(tmp_path, DENY_ONLY)

    answer = _submit(
        {
            "agent": "on-codex",
            "prompt": "w",
            "no_mcp_config": True,
            "hooks_from": str(path),
            "hooks_from_format": "claude",
        }
    )

    assert answer["ok"] is False, answer
    assert "only a claude_code leg" in json.dumps(answer)
    assert submitted == []


def test_a_named_model_decides_the_provider(submitted, tmp_path):
    path = _hooks_file(tmp_path, DENY_ONLY)

    answer = _submit(
        {
            "agent": "on-codex",
            "query": ["claude_code/sonnet"],
            "prompt": "w",
            "no_mcp_config": True,
            "hooks_from": str(path),
            "hooks_from_format": "claude",
        }
    )

    assert answer["ok"] is True, answer


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"hooks_from_format": "claude"}, "need hooks_from"),
        ({"hooks_from_carry": ["deny"]}, "need hooks_from"),
        ({"hooks_from": "guards.json", "hooks_from_format": "claude"}, "absolute path"),
        ({"hooks_from": "FILE"}, "needs hooks_from_format"),
        ({"hooks_from": "FILE", "hooks_from_format": "claude", "resume": "r1"}, "resume"),
    ],
)
def test_a_submission_that_cannot_carry_its_file_is_refused(submitted, tmp_path, extra, message):
    path = str(_hooks_file(tmp_path, DENY_ONLY))
    args = {"agent": "on-claude", "prompt": "w", "no_mcp_config": True}
    args.update({key: path if value == "FILE" else value for key, value in extra.items()})

    answer = _submit(args)

    assert answer["ok"] is False, answer
    assert message in json.dumps(answer)
    assert submitted == []


def test_jobs_submit_takes_a_hooks_file_for_agent_runs_only(submitted, tmp_path):
    with pytest.raises(jobs.SubmitInputError, match="agent runs only"):
        jobs.submit("flow", [], prompt="w", no_mcp_config=True, hooks_from={})


# ---- the leg ----------------------------------------------------------------------


def _spec(tmp_path: Path, document: dict[str, Any], carry=("hooks", "deny")) -> dict:
    path = _hooks_file(tmp_path, document, name="copy.json")
    return {
        "copy": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "format": "claude",
        "carry": list(carry),
        "source": str(tmp_path.resolve() / "guards.json"),
    }


def _model(provider: str, *, is_cli: bool = True):
    config_ = SimpleNamespace(provider=provider, kwargs={})
    return SimpleNamespace(is_cli=is_cli, endpoint=SimpleNamespace(config=config_))


def _branch(chat, parse=None, metadata=None):
    return SimpleNamespace(chat_model=chat, parse_model=parse, metadata=metadata or {})


def test_a_fresh_leg_reads_its_spec_from_the_flag(tmp_path):
    spec = _spec(tmp_path, DENY_ONLY)

    assert agent_cli._hooks_from_spec(None, _branch(None), False) is None
    assert agent_cli._hooks_from_spec(json.dumps(spec), _branch(None), False) == spec


@pytest.mark.parametrize(
    "flag",
    ["not json", json.dumps([]), json.dumps({"copy": "x"}), json.dumps({"carry": "deny"})],
)
def test_a_flag_that_does_not_name_every_field_refuses(flag):
    with pytest.raises(ConfigurationError, match="--hooks-from-snapshot"):
        agent_cli._hooks_from_spec(flag, _branch(None), False)


def test_a_resumed_leg_keeps_the_spec_it_started_with(tmp_path):
    spec = _spec(tmp_path, DENY_ONLY)
    branch = _branch(None, metadata={"hooks_from": spec})

    assert agent_cli._hooks_from_spec(None, branch, True) == spec
    assert agent_cli._hooks_from_spec(None, _branch(None), True) is None
    with pytest.raises(ConfigurationError, match="resumed branch"):
        agent_cli._hooks_from_spec(json.dumps(spec), branch, True)


def test_a_claude_leg_hands_its_models_the_rules(tmp_path):
    chat, parse = _model("claude_code"), _model("claude_code")

    agent_cli._apply_hooks_from(_spec(tmp_path, DENY_ONLY), _branch(chat, parse), str(tmp_path))

    assert chat.endpoint.config.kwargs["carried_deny"] == [RULE]
    assert parse.endpoint.config.kwargs["carried_deny"] == [RULE]


@pytest.mark.parametrize(
    "branch",
    [
        _branch(_model("codex")),
        _branch(_model("claude_code"), _model("codex")),
        _branch(_model("anthropic", is_cli=False)),
    ],
)
def test_a_leg_with_a_model_that_cannot_apply_the_rules_refuses(tmp_path, branch):
    with pytest.raises(ConfigurationError, match="only a claude_code leg"):
        agent_cli._apply_hooks_from(_spec(tmp_path, DENY_ONLY), branch, str(tmp_path))
    assert branch.chat_model.endpoint.config.kwargs == {}


def test_a_leg_refuses_hook_entries_and_an_edited_copy(tmp_path):
    chat = _model("claude_code")
    with pytest.raises(ConfigurationError, match="hook entries"):
        agent_cli._apply_hooks_from(_spec(tmp_path, WITH_HOOKS), _branch(chat), str(tmp_path))

    spec = _spec(tmp_path, DENY_ONLY)
    Path(spec["copy"]).write_text(json.dumps({}))
    with pytest.raises(ConfigurationError, match="digest"):
        agent_cli._apply_hooks_from(spec, _branch(chat), str(tmp_path))
    assert chat.endpoint.config.kwargs == {}


def test_a_leg_carrying_deny_alone_takes_a_file_with_hook_entries(tmp_path):
    chat = _model("claude_code")

    agent_cli._apply_hooks_from(
        _spec(tmp_path, WITH_HOOKS, carry=("deny",)), _branch(chat), str(tmp_path)
    )

    assert chat.endpoint.config.kwargs["carried_deny"] == [RULE]
