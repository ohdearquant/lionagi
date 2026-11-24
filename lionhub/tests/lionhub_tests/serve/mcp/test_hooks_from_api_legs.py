# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Hook entries from a ``hooks_from`` file run on an API-model leg, and only once trusted.

ADR-0048 Amendment 1 H4 and H5: an API-model leg wires the file's hook entries onto its
branch; every command needs a ``trusted_hook_commands`` record, and neither its program nor
a file it names may lie in a directory the leg can write. ``li hooks trust --from`` records
that trust for a file that was never imported.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lionagi._errors import ConfigurationError
from lionhub.hooks.external import compute_trust_record
from lionhub.hooks.hooks_from import (
    hook_command_refusals,
    hook_entries,
    plan_carry,
    writable_roots,
)
from lionhub.serve.mcp import config, dispatch, jobs, verbs

agent_cli = importlib.import_module("lionhub.serve.cli.agent")
child_sandbox = importlib.import_module("lionhub.agent.child_sandbox")
hooks_cli = importlib.import_module("lionhub.serve.cli.hooks")
user_settings = importlib.import_module("lionagi.plugins._user_settings")


@pytest.fixture
def trust_store(monkeypatch, tmp_path):
    """An in-memory user settings file, and hook copies kept under tmp."""
    store: dict[str, Any] = {}
    monkeypatch.setattr(user_settings, "read_user_settings", lambda: json.loads(json.dumps(store)))

    def write(settings: dict[str, Any]) -> None:
        store.clear()
        store.update(json.loads(json.dumps(settings)))

    monkeypatch.setattr(user_settings, "write_user_settings", write)
    private = tmp_path / "private"
    private.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(private))
    return store


@pytest.fixture
def sandbox_settings(monkeypatch):
    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: {"child_sandbox": {}})


def _program(directory: Path, name: str = "guard.sh", body: str = "exit 0") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    work = tmp_path.resolve() / "work"
    work.mkdir(exist_ok=True)
    return work, tmp_path.resolve() / "guards"


def _document(*commands: list[str], matcher: str = "Bash") -> dict[str, Any]:
    hooks = [{"type": "command", "command": command} for command in commands]
    return {"hooks": {"PreToolUse": [{"matcher": matcher, "hooks": hooks}]}}


def _plan(document: dict[str, Any], work: Path, carry=("hooks",)):
    return plan_carry(
        document,
        fmt="claude",
        carry=list(carry),
        source_path=str(work / "guards.json"),
        leg_cwd=str(work),
    )


def _trust(store: dict[str, Any], command: list[str], cwd: Path) -> None:
    store.setdefault("trusted_hook_commands", []).append(compute_trust_record(command, str(cwd)))


# ---- the checks before a hook command may run ------------------------------------


def test_each_entry_is_labelled_by_its_place_in_the_file(tmp_path):
    work, _ = _dirs(tmp_path)
    document = {
        "hooks": {
            "PreToolUse": [
                {"hooks": []},
                {"matcher": "Bash", "hooks": [{"command": "/bin/true"}, {"command": "/bin/echo"}]},
            ]
        }
    }

    entries = hook_entries(_plan(document, work))

    assert [entry["where"] for entry in entries] == [
        "hooks.PreToolUse[1].hooks[0]",
        "hooks.PreToolUse[1].hooks[1]",
    ]
    assert entries[0]["source"] == "imported:claude"
    assert entries[0]["matcher"] == "Bash"
    assert entries[0]["timeout"] == 60.0


def test_an_untrusted_command_refuses_and_names_the_trust_step(tmp_path, trust_store):
    work, guards = _dirs(tmp_path)
    program = _program(guards)
    plan = _plan(_document([str(program)]), work)

    (refusal,) = hook_command_refusals(plan, leg_cwd=str(work), writable=[str(work)])

    assert refusal[0] == "hooks.PreToolUse[0].hooks[0]"
    assert "li hooks trust --from" in refusal[1]
    assert str(program) not in refusal[1]
    _trust(trust_store, [str(program)], work)
    assert hook_command_refusals(plan, leg_cwd=str(work), writable=[str(work)]) == []


def test_a_program_the_leg_can_write_refuses_even_when_trusted(tmp_path, trust_store):
    work, _ = _dirs(tmp_path)
    program = _program(work / "bin")
    _trust(trust_store, [str(program)], work)
    plan = _plan(_document([str(program)]), work)

    (refusal,) = hook_command_refusals(plan, leg_cwd=str(work), writable=[str(work)])

    assert "its program lies in a directory the leg can write" in refusal[1]


def test_a_relative_argument_naming_a_file_in_the_leg_directory_refuses(tmp_path, trust_store):
    work, guards = _dirs(tmp_path)
    program = _program(guards)
    (work / "check.py").write_text("print('guard')\n")
    named = [str(program), "check.py"]
    absent = [str(program), "missing.py"]
    _trust(trust_store, named, work)
    _trust(trust_store, absent, work)

    (refusal,) = hook_command_refusals(
        _plan(_document(named), work), leg_cwd=str(work), writable=[str(work)]
    )

    assert "an argument names a file in a directory the leg can write" in refusal[1]
    assert "check.py" not in refusal[1]
    # The control: an argument naming no existing file is not refused for it.
    assert (
        hook_command_refusals(
            _plan(_document(absent), work), leg_cwd=str(work), writable=[str(work)]
        )
        == []
    )


def test_an_absolute_argument_in_another_writable_root_refuses(tmp_path, trust_store):
    work, guards = _dirs(tmp_path)
    program = _program(guards)
    cache = tmp_path.resolve() / "cache"
    cache.mkdir()
    (cache / "rules.txt").write_text("x\n")
    command = [str(program), str(cache / "rules.txt")]
    _trust(trust_store, command, work)
    plan = _plan(_document(command), work)

    assert hook_command_refusals(plan, leg_cwd=str(work), writable=[str(work)]) == []
    (refusal,) = hook_command_refusals(plan, leg_cwd=str(work), writable=[str(work), str(cache)])
    assert "an argument names a file" in refusal[1]


def test_a_program_that_cannot_be_found_refuses(tmp_path, trust_store):
    work, guards = _dirs(tmp_path)
    plan = _plan(_document([str(guards / "absent.sh")]), work)

    (refusal,) = hook_command_refusals(plan, leg_cwd=str(work), writable=[str(work)])

    assert "not an executable file" in refusal[1]


@pytest.mark.usefixtures("sandbox_settings")
def test_the_writable_roots_are_the_leg_directory_and_allow_write(monkeypatch, tmp_path):
    home = tmp_path.resolve() / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    work, _ = _dirs(tmp_path)

    roots = writable_roots(str(work))

    assert roots[0] == str(work)
    assert str(home / ".cache" / "uv") in roots


# ---- the leg ----------------------------------------------------------------------


def _spec(tmp_path: Path, document: dict[str, Any], carry=("hooks", "deny")) -> dict[str, Any]:
    path = tmp_path.resolve() / "copy.json"
    path.write_text(json.dumps(document))
    return {
        "copy": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "format": "claude",
        "carry": list(carry),
        "source": str(tmp_path.resolve() / "guards.json"),
    }


def _model(provider: str, *, is_cli: bool):
    return SimpleNamespace(
        is_cli=is_cli,
        endpoint=SimpleNamespace(config=SimpleNamespace(provider=provider, kwargs={})),
    )


def _branch(chat, parse=None):
    pre: list[Any] = []
    acts = SimpleNamespace(pre=pre, add_tool_pre_hook=pre.append, add_tool_post_hook=pre.append)
    return SimpleNamespace(
        chat_model=chat,
        parse_model=parse,
        metadata={},
        acts=acts,
        id="branch-1",
        _owning_session_id=None,
        _pending_hook_bus_entries=[],
        _hooks=None,
    )


@pytest.mark.usefixtures("sandbox_settings")
def test_an_api_leg_runs_a_trusted_hook_from_its_copy(tmp_path, trust_store):
    work, guards = _dirs(tmp_path)
    program = _program(guards, body="echo 'blocked by the parent guard' >&2\nexit 2")
    _trust(trust_store, [str(program)], work)
    branch = _branch(_model("anthropic", is_cli=False))

    agent_cli._apply_hooks_from(_spec(tmp_path, _document([str(program)])), branch, str(work))

    (pre_hook,) = branch.acts.pre
    decision = asyncio.run(pre_hook("Bash", {"command": "ls"}))
    assert decision.decision == "deny"
    assert "blocked by the parent guard" in (decision.reason or "")
    assert asyncio.run(pre_hook("Read", {"file_path": "x"})) is None


@pytest.mark.usefixtures("sandbox_settings")
def test_an_api_leg_refuses_an_untrusted_hook_before_wiring_it(tmp_path, trust_store):
    work, guards = _dirs(tmp_path)
    program = _program(guards)
    branch = _branch(_model("anthropic", is_cli=False))

    with pytest.raises(ConfigurationError, match="li hooks trust --from"):
        agent_cli._apply_hooks_from(_spec(tmp_path, _document([str(program)])), branch, str(work))
    assert branch.acts.pre == []


@pytest.mark.usefixtures("sandbox_settings")
def test_an_api_leg_refuses_a_hook_program_inside_its_directory(tmp_path, trust_store):
    work, _ = _dirs(tmp_path)
    program = _program(work)
    _trust(trust_store, [str(program)], work)
    branch = _branch(_model("anthropic", is_cli=False))

    with pytest.raises(ConfigurationError, match="directory the leg can write"):
        agent_cli._apply_hooks_from(_spec(tmp_path, _document([str(program)])), branch, str(work))
    assert branch.acts.pre == []


@pytest.mark.parametrize(
    "models",
    [
        (_model("claude_code", is_cli=True), None),
        (_model("codex", is_cli=True), None),
        (_model("anthropic", is_cli=False), _model("codex", is_cli=True)),
    ],
)
def test_a_leg_with_a_cli_model_refuses_hook_entries(tmp_path, trust_store, models):
    work, guards = _dirs(tmp_path)
    program = _program(guards)
    _trust(trust_store, [str(program)], work)
    branch = _branch(*models)

    with pytest.raises(ConfigurationError, match="only an API-model leg"):
        agent_cli._apply_hooks_from(_spec(tmp_path, _document([str(program)])), branch, str(work))
    assert branch.acts.pre == []


def test_an_api_leg_refuses_deny_rules_and_names_the_hooks_way_out(tmp_path):
    work, _ = _dirs(tmp_path)
    branch = _branch(_model("anthropic", is_cli=False))

    with pytest.raises(ConfigurationError, match=r'hooks_from_carry=\["hooks"\]'):
        agent_cli._apply_hooks_from(
            _spec(tmp_path, {"permissions": {"deny": ["Bash(rm:*)"]}}), branch, str(work)
        )


# ---- agent.submit -----------------------------------------------------------------


class _FakeProc:
    pid = 4242

    def wait(self, timeout: float | None = None) -> int:
        return 0


@pytest.fixture
def submitted(monkeypatch, tmp_path, trust_store, sandbox_settings):
    """Job state under tmp and three profiles; the run's own spawn is recorded, not started."""
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr("lionhub.orchestration.runs.RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(config, "li_command", lambda: ["echo"])
    monkeypatch.setattr(jobs, "_read_lifecycle", lambda run_id: None)
    monkeypatch.delenv(config.CHILD_ACTOR_SUFFIX_ENV_VAR, raising=False)
    work, _ = _dirs(tmp_path)
    monkeypatch.chdir(work)
    home = tmp_path / "home"
    agents = home / ".lionagi" / "agents"
    agents.mkdir(parents=True)
    (agents / "on-api.md").write_text("---\nmodel: anthropic/claude-haiku-5-5\n---\nWork.\n")
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


def _submit(args: dict[str, Any]) -> dict[str, Any]:
    fingerprint = asyncio.run(dispatch.request(help="agent.submit"))["schema_fingerprint"]
    op = {"op": "agent.submit", "args": args, "schema_fingerprint": fingerprint}
    return asyncio.run(dispatch.request(ops=[op]))["ops"][0]


def _file(tmp_path: Path, document: dict[str, Any]) -> str:
    path = tmp_path.resolve() / "guards.json"
    path.write_text(json.dumps(document))
    return str(path)


def _args(agent: str, path: str) -> dict[str, Any]:
    return {
        "agent": agent,
        "prompt": "w",
        "no_mcp_config": True,
        "hooks_from": path,
        "hooks_from_format": "claude",
    }


def test_an_api_leg_is_submitted_with_a_trusted_hook(submitted, tmp_path, trust_store):
    work, guards = _dirs(tmp_path)
    program = _program(guards)
    _trust(trust_store, [str(program)], work)

    answer = _submit(_args("on-api", _file(tmp_path, _document([str(program)]))))

    assert answer["ok"] is True, answer
    (argv,) = submitted
    assert "--hooks-from-snapshot" in argv


def test_an_untrusted_hook_refuses_and_the_file_without_it_is_submitted(
    submitted, tmp_path, trust_store
):
    work, guards = _dirs(tmp_path)
    trusted, untrusted = _program(guards, "trusted.sh"), _program(guards, "untrusted.sh")
    _trust(trust_store, [str(trusted)], work)

    refused = _submit(_args("on-api", _file(tmp_path, _document([str(trusted)], [str(untrusted)]))))

    assert refused["ok"] is False, refused
    text = json.dumps(refused)
    assert "hooks.PreToolUse[0].hooks[1]" in text and "li hooks trust --from" in text
    assert "untrusted.sh" not in text
    assert submitted == []
    accepted = _submit(_args("on-api", _file(tmp_path, _document([str(trusted)]))))
    assert accepted["ok"] is True, accepted


def test_a_hook_program_in_the_leg_directory_refuses_the_submission(
    submitted, tmp_path, trust_store
):
    work, _ = _dirs(tmp_path)
    program = _program(work)
    _trust(trust_store, [str(program)], work)

    answer = _submit(_args("on-api", _file(tmp_path, _document([str(program)]))))

    assert answer["ok"] is False, answer
    assert "directory the leg can write" in json.dumps(answer)
    assert submitted == []


@pytest.mark.parametrize(
    ("agent", "document", "message"),
    [
        ("on-api", {"permissions": {"deny": ["Bash(rm:*)"]}}, "only a claude_code leg"),
        ("on-claude", "HOOKS", "only an API-model leg"),
        ("on-codex", "HOOKS", "only an API-model leg"),
        ("on-codex", {"permissions": {"deny": ["Bash(rm:*)"]}}, "only a claude_code leg"),
    ],
)
def test_each_provider_refuses_the_guards_it_cannot_apply(
    submitted, tmp_path, trust_store, agent, document, message
):
    work, guards = _dirs(tmp_path)
    program = _program(guards)
    _trust(trust_store, [str(program)], work)
    if document == "HOOKS":
        document = _document([str(program)])

    answer = _submit(_args(agent, _file(tmp_path, document)))

    assert answer["ok"] is False, answer
    assert message in json.dumps(answer)
    assert submitted == []


# ---- li hooks trust --from ---------------------------------------------------------


def test_trust_from_records_a_file_that_was_never_imported(tmp_path, trust_store):
    work, guards = _dirs(tmp_path)
    program = _program(guards)
    path = _file(tmp_path, _document([str(program)]))
    plan = _plan(_document([str(program)]), work)
    assert hook_command_refusals(plan, leg_cwd=str(work), writable=[str(work)]) != []

    code = hooks_cli._run_trust(str(work), assume_yes=True, from_path=path, from_format="claude")

    assert code == 0
    assert trust_store["trusted_hook_commands"] == [compute_trust_record([str(program)], str(work))]
    assert not (work / ".lionagi").exists()
    assert hook_command_refusals(plan, leg_cwd=str(work), writable=[str(work)]) == []


def test_trust_from_refuses_a_file_with_an_entry_a_leg_cannot_carry(tmp_path, trust_store, capsys):
    work, guards = _dirs(tmp_path)
    program = _program(guards)
    document = {
        "hooks": {
            "PreToolUse": [{"hooks": [{"command": str(program)}, {"type": "http", "url": "x"}]}]
        }
    }

    code = hooks_cli._run_trust(
        str(work), assume_yes=True, from_path=_file(tmp_path, document), from_format="claude"
    )

    assert code == 1
    assert "hooks.PreToolUse[0].hooks[1]" in capsys.readouterr().out
    assert "trusted_hook_commands" not in trust_store


@pytest.mark.parametrize(
    ("from_path", "from_format", "message"),
    [("FILE", None, "--from needs --format"), (None, "claude", "--format needs --from")],
)
def test_trust_from_and_format_go_together(
    tmp_path, trust_store, capsys, from_path, from_format, message
):
    work, _ = _dirs(tmp_path)
    path = _file(tmp_path, {}) if from_path == "FILE" else None

    code = hooks_cli._run_trust(str(work), assume_yes=True, from_path=path, from_format=from_format)

    assert code == 1
    assert message in capsys.readouterr().out


def test_trust_from_parses_on_the_command_line():
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    hooks_cli.add_hooks_subparser(sub)

    args = parser.parse_args(
        ["hooks", "trust", "--from", "/abs/guards.json", "--format", "codex", "--yes"]
    )

    assert (args.from_path, args.from_format, args.yes) == ("/abs/guards.json", "codex", True)


def test_hooks_trust_stays_unreachable_from_the_mcp_server():
    assert "hooks trust" in verbs.FENCED_PATHS
    assert [name for name, verb in verbs.VERBS.items() if verb.cli_path == "hooks trust"] == []
