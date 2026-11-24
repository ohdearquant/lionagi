# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""agent.submit answers a profile's ``require_hooks`` before it records the run."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from lionagi import iModel
from lionhub.providers.openai import _codex_hook_trust as trust
from lionhub.serve.mcp import config, dispatch, jobs

_COMMAND = {"type": "command", "command": "echo hi"}


class _FakeProcess:
    pid = 4242

    def wait(self, timeout=None):
        return 0


def _codex_home(root: Path, name: str, *, trusted: bool = False) -> tuple[Path, str]:
    home = Path(os.path.realpath(root)) / name
    home.mkdir()
    path = home / "hooks.json"
    path.write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [_COMMAND]}]}}),
        encoding="utf-8",
    )
    hook = f"{path}:pre_tool_use:0:0"
    if trusted:
        digest = trust.hook_hash(trust.hook_identity("PreToolUse", "Bash", _COMMAND))
        (home / "config.toml").write_text(
            f"[hooks.state.{json.dumps(hook)}]\ntrusted_hash = {json.dumps(digest)}\n",
            encoding="utf-8",
        )
    return home, hook


@pytest.fixture
def submission(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr("lionhub.orchestration.runs.RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(config, "li_command", lambda: ["echo"])
    monkeypatch.setattr(config, "child_actor_suffix", lambda: None)
    monkeypatch.setattr(jobs, "_read_lifecycle", lambda _run_id: None)
    monkeypatch.setattr("lionhub.orchestration.spawn._plugin_agent_profiles", lambda: {})
    home = tmp_path / "home"
    (home / ".lionagi").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    server_codex = Path(os.path.realpath(tmp_path)) / "server-codex"
    server_codex.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(server_codex))
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    (cwd / ".lionagi" / "agents").mkdir(parents=True)
    spawned: list[list[str]] = []

    def popen(argv, **kwargs):
        if argv[0] == "echo":
            spawned.append(argv)
        return _FakeProcess()

    monkeypatch.setattr(jobs.subprocess, "Popen", popen)
    return cwd, home, server_codex, spawned


def _profile(name: str, frontmatter: str) -> None:
    """Write a profile to the user-level directory, where a profile may name a codex home."""
    agents = Path(os.environ["HOME"]) / ".lionagi" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{name}.md").write_text(
        f"---\n{frontmatter}---\nA guarded profile.", encoding="utf-8"
    )


def _submit(args):
    fingerprint = asyncio.run(dispatch.request(help="agent.submit"))["schema_fingerprint"]
    response = asyncio.run(
        dispatch.request(
            ops=[{"op": "agent.submit", "args": args, "schema_fingerprint": fingerprint}]
        )
    )
    return response["ops"][0]


def _no_run_recorded(spawned) -> bool:
    jobs_dir = Path(config.JOBS_DIR)
    recorded = [p for p in jobs_dir.iterdir() if p.is_dir()] if jobs_dir.exists() else []
    return not spawned and not recorded


def test_a_codex_profile_with_an_untrusted_hook_refuses_before_any_record(submission, tmp_path):
    cwd, _home, server_codex, spawned = submission
    home, hook = _codex_home(tmp_path, "account-codex")
    _profile("guarded", f"model: codex/gpt-5\nrequire_hooks: true\ncodex_home: {home}\n")
    result = _submit({"agent": "guarded", "prompt": "do work", "cwd": str(cwd)})
    assert result["ok"] is False, result
    assert result["error"]["kind"] == "invalid_input", result
    assert f"{hook} (untrusted)" in result["error"]["message"]
    assert _no_run_recorded(spawned)


def test_the_same_codex_profile_with_the_hook_trusted_is_accepted(submission, tmp_path):
    cwd, _home, _server_codex, spawned = submission
    home, _hook = _codex_home(tmp_path, "account-codex", trusted=True)
    _profile("guarded", f"model: codex/gpt-5\nrequire_hooks: true\ncodex_home: {home}\n")
    result = _submit({"agent": "guarded", "prompt": "do work", "cwd": str(cwd)})
    assert result["ok"], result
    assert len(spawned) == 1


def test_the_home_checked_at_submit_is_the_profile_codex_home_not_the_server(submission, tmp_path):
    """The server's home declares a hook nobody trusted; the profile's home declares none."""
    cwd, _home, server_codex, spawned = submission
    (server_codex / "hooks.json").write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [_COMMAND]}]}}), encoding="utf-8"
    )
    clean = Path(os.path.realpath(tmp_path)) / "profile-codex"
    clean.mkdir()
    _profile("guarded", f"model: codex/gpt-5\nrequire_hooks: true\ncodex_home: {clean}\n")
    assert _submit({"agent": "guarded", "prompt": "do work", "cwd": str(cwd)})["ok"]
    # With no codex_home the server's home is the run's, and its hook refuses.
    _profile("plain-home", "model: codex/gpt-5\nrequire_hooks: true\n")
    refused = _submit({"agent": "plain-home", "prompt": "do work", "cwd": str(cwd)})
    assert refused["ok"] is False
    assert str(server_codex) in refused["error"]["message"]
    assert len(spawned) == 1


def test_a_named_account_is_the_home_checked_at_submit(submission, tmp_path, monkeypatch):
    from lionhub.agent import subscription_accounts

    cwd, _home, _server_codex, spawned = submission
    account_home, hook = _codex_home(tmp_path, "account-codex")
    profile_home, _ = _codex_home(tmp_path, "profile-codex", trusted=True)
    asked: list[dict] = []

    class _Leg:
        env = {"CODEX_HOME": str(account_home)}

    def resolve(provider, **kwargs):
        asked.append({"provider": provider, **kwargs})
        return _Leg()

    monkeypatch.setattr(subscription_accounts, "resolve_leg_account", resolve)
    _profile("guarded", f"model: codex/gpt-5\nrequire_hooks: true\ncodex_home: {profile_home}\n")
    result = _submit({"agent": "guarded", "prompt": "do work", "cwd": str(cwd), "account": "work"})
    assert result["ok"] is False, result
    assert f"{hook} (untrusted)" in result["error"]["message"]
    assert asked == [{"provider": "codex", "account": "work", "codex_home": None}]
    assert _no_run_recorded(spawned)


def test_a_launch_that_ignores_the_user_config_is_checked_as_it_starts(submission, tmp_path):
    """A profile with mcp: none starts codex hermetic, so its `[hooks]` table does not load
    and the launch is refused for it; the submit reaches the same refusal. Without the table
    the trusted hook of hooks.json is met."""
    cwd, _home, _server_codex, spawned = submission
    home, _hook = _codex_home(tmp_path, "account-codex", trusted=True)
    _profile("guarded", f"model: codex/gpt-5\nrequire_hooks: true\nmcp: none\ncodex_home: {home}\n")
    assert _submit({"agent": "guarded", "prompt": "do work", "cwd": str(cwd)})["ok"]
    with (home / "config.toml").open("a", encoding="utf-8") as config_file:
        config_file.write(
            '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\ncommand = "echo bye"\n'
        )
    refused = _submit({"agent": "guarded", "prompt": "do work", "cwd": str(cwd)})
    assert refused["ok"] is False, refused
    assert "ignore-user-config" in refused["error"]["message"]
    assert len(spawned) == 1


@pytest.mark.parametrize(
    "settings",
    [
        "hooks_external:\n  - event: pre_tool\n    command: [/bin/true]\n    trusted: true\n",
        "hooks_external:\n  - event: pre_tool\n    command: [/bin/true]\n",
    ],
)
def test_an_api_model_profile_refuses_at_submit_when_settings_declare_a_hook(submission, settings):
    cwd, home, _server_codex, spawned = submission
    (home / ".lionagi" / "settings.yaml").write_text(settings, encoding="utf-8")
    _profile("guarded", "model: openai/gpt-4.1\nrequire_hooks: true\n")
    result = _submit({"agent": "guarded", "prompt": "do work", "cwd": str(cwd)})
    assert result["ok"] is False, result
    assert "applies no lionagi hooks" in result["error"]["message"]
    assert _no_run_recorded(spawned)


def test_an_api_model_profile_with_no_declared_hook_is_accepted(submission):
    cwd, _home, _server_codex, spawned = submission
    _profile("guarded", "model: openai/gpt-4.1\nrequire_hooks: true\n")
    assert _submit({"agent": "guarded", "prompt": "do work", "cwd": str(cwd)})["ok"]
    assert len(spawned) == 1


@pytest.mark.parametrize(
    ("model", "named"),
    [("claude_code/sonnet", "claude_code"), ("gemini-cli/gemini-2.5-pro", "gemini")],
)
def test_another_cli_provider_refuses_at_submit_naming_it(submission, model, named):
    cwd, _home, _server_codex, spawned = submission
    _profile("guarded", f"model: {model}\nrequire_hooks: true\n")
    result = _submit({"agent": "guarded", "prompt": "do work", "cwd": str(cwd)})
    assert result["ok"] is False, result
    assert named in result["error"]["message"]
    assert "checked for codex and API-model legs only" in result["error"]["message"]
    assert _no_run_recorded(spawned)


def _run_flag(argv: list[str], monkeypatch) -> bool:
    """The ``ignore_user_config`` flag on the codex request `li agent` builds from *argv*,
    the line the submission spawned, read off the model its new-branch path builds."""
    import argparse

    import lionhub.serve.cli.agent as agent_mod

    parser = argparse.ArgumentParser()
    agent_mod.add_agent_subparser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(argv[1:])
    flags: list[bool] = []
    real = agent_mod.build_chat_model

    class _Built(Exception):
        pass

    def built(*a, **kw):
        model = real(*a, **kw)
        if isinstance(model, str):
            model = iModel(model=model)
        payload, _ = model.endpoint.create_payload({"prompt": "x"})
        flags.append(payload["request"].ignore_user_config)
        raise _Built

    monkeypatch.setattr(agent_mod, "build_chat_model", built)
    monkeypatch.setattr(agent_mod, "stamp_agent_depth", lambda name: 1)
    with pytest.raises(_Built):
        agent_mod.run_agent(args)
    assert len(flags) == 1
    return flags[0]


@pytest.mark.parametrize(
    ("case", "ignored"),
    [
        ("profile_declares_mcp_none", True),
        ("caller_passes_no_mcp_config", True),
        ("named_config_lists_no_servers", True),
        ("snapshot_with_every_server_excluded", True),
        ("snapshot_with_a_server", False),
        ("no_config_found", False),
    ],
)
def test_the_flag_the_submit_check_builds_is_the_flag_the_run_builds(
    submission, tmp_path, monkeypatch, case, ignored
):
    """The submit check and the run it admits build the codex request with one flag.

    The submit side is read off the request the check builds; the run side off the request
    `li agent` builds from the spawned line. No allowlist case appears: the agent run's
    builder passes none, so on agent.submit the flag never depends on one.
    """
    from lionhub.orchestration import spawn

    cwd, _home, _server_codex, spawned = submission
    clean = Path(os.path.realpath(tmp_path)) / "profile-codex"
    clean.mkdir()
    frontmatter = f"model: codex/gpt-5\nrequire_hooks: true\ncodex_home: {clean}\n"
    args = {"agent": "guarded", "prompt": "do work", "cwd": str(cwd)}
    if case == "profile_declares_mcp_none":
        frontmatter += "mcp: none\n"
    elif case == "caller_passes_no_mcp_config":
        args["no_mcp_config"] = True
    elif case == "named_config_lists_no_servers":
        named = Path(os.path.realpath(tmp_path)) / "named.json"
        named.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        args["mcp_config"] = str(named)
    elif case.startswith("snapshot"):
        (cwd / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"probe": {"command": "true"}}}), encoding="utf-8"
        )
        if case == "snapshot_with_every_server_excluded":
            args["exclude_mcp_servers"] = ["probe"]
    _profile("guarded", frontmatter)
    checked: list[bool] = []
    real = spawn.build_chat_model

    def built(*a, **kw):
        model = real(*a, **kw)
        built_model = iModel(model=model) if isinstance(model, str) else model
        payload, _ = built_model.endpoint.create_payload({"prompt": "x"})
        checked.append(payload["request"].ignore_user_config)
        return model

    monkeypatch.setattr(spawn, "build_chat_model", built)
    result = _submit(args)
    assert result["ok"], result
    assert checked == [ignored]
    assert len(spawned) == 1
    assert _run_flag(spawned[0], monkeypatch) is ignored


_HOOK_ENTRIES = {
    "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"command": "/bin/true"}]}]}
}
_DENY_RULES = {"permissions": {"deny": ["Bash(rm:*)"]}}
_NEITHER = {"permissions": {"deny": []}}


def _with_hooks_file(tmp_path: Path, cwd: Path, document: dict) -> dict:
    path = Path(os.path.realpath(tmp_path)) / "guards.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return {
        "agent": "guarded",
        "prompt": "do work",
        "cwd": str(cwd),
        "hooks_from": str(path),
        "hooks_from_format": "claude",
    }


@pytest.mark.parametrize(
    ("document", "refusal"),
    [(_HOOK_ENTRIES, "hook entries"), (_DENY_RULES, "only a claude_code leg applies them")],
)
def test_a_hooks_file_refuses_a_codex_leg_the_key_would_accept(
    submission, tmp_path, document, refusal
):
    """The home's hook is trusted, so the key alone accepts; the file is refused first,
    since no codex leg runs its hook entries or applies its deny rules."""
    cwd, _home, _server_codex, spawned = submission
    home, _hook = _codex_home(tmp_path, "account-codex", trusted=True)
    _profile("guarded", f"model: codex/gpt-5\nrequire_hooks: true\ncodex_home: {home}\n")
    result = _submit(_with_hooks_file(tmp_path, cwd, document))
    assert result["ok"] is False, result
    assert refusal in result["error"]["message"]
    assert _no_run_recorded(spawned)


def test_a_hooks_file_carrying_neither_kind_leaves_the_codex_answer(submission, tmp_path):
    """With the file adding nothing, the home's untrusted hook still refuses and a trusted
    one is still accepted."""
    cwd, _home, _server_codex, spawned = submission
    home, hook = _codex_home(tmp_path, "account-codex")
    _profile("guarded", f"model: codex/gpt-5\nrequire_hooks: true\ncodex_home: {home}\n")
    refused = _submit(_with_hooks_file(tmp_path, cwd, _NEITHER))
    assert refused["ok"] is False, refused
    assert f"{hook} (untrusted)" in refused["error"]["message"]
    trusted, _ = _codex_home(tmp_path, "trusted-codex", trusted=True)
    _profile("guarded", f"model: codex/gpt-5\nrequire_hooks: true\ncodex_home: {trusted}\n")
    assert _submit(_with_hooks_file(tmp_path, cwd, _NEITHER))["ok"]
    assert len(spawned) == 1


def test_a_hook_entry_that_cannot_run_refuses_an_api_model_leg_before_the_key(submission, tmp_path):
    """A hook entry that cannot run is refused by the file's own check, before the key; a
    file that carries neither kind leaves the key met."""
    cwd, _home, _server_codex, spawned = submission
    _profile("guarded", "model: openai/gpt-4.1\nrequire_hooks: true\n")
    refused = _submit(_with_hooks_file(tmp_path, cwd, _HOOK_ENTRIES))
    assert refused["ok"] is False, refused
    # Untrusted where /bin/true exists, not found where it does not: refused either way.
    assert "hook command(s) cannot run" in refused["error"]["message"]
    assert "applies no lionagi hooks" not in refused["error"]["message"]
    assert _no_run_recorded(spawned)
    accepted = _submit(_with_hooks_file(tmp_path, cwd, _NEITHER))
    assert accepted["ok"], accepted
    assert len(spawned) == 1


@pytest.mark.parametrize(
    ("document", "refusal"),
    [
        (_DENY_RULES, "checked for codex and API-model legs only"),
        (_HOOK_ENTRIES, "hook entries"),
    ],
)
def test_a_claude_code_leg_with_a_hooks_file_is_refused(submission, tmp_path, document, refusal):
    """Deny rules pass the file's check and the key refuses, naming claude_code; hook entries
    are refused by the file's check first."""
    cwd, _home, _server_codex, spawned = submission
    _profile("guarded", "model: claude_code/sonnet\nrequire_hooks: true\n")
    result = _submit(_with_hooks_file(tmp_path, cwd, document))
    assert result["ok"] is False, result
    assert refusal in result["error"]["message"]
    if document is _DENY_RULES:
        assert "claude_code" in result["error"]["message"]
    assert _no_run_recorded(spawned)


def test_a_profile_without_the_key_is_not_checked_at_submit(submission, tmp_path):
    cwd, _home, _server_codex, spawned = submission
    home, _hook = _codex_home(tmp_path, "account-codex")
    _profile("plain", f"model: codex/gpt-5\ncodex_home: {home}\n")
    assert _submit({"agent": "plain", "prompt": "do work", "cwd": str(cwd)})["ok"]
    assert len(spawned) == 1
