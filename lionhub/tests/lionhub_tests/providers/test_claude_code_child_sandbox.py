# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The Claude CLI argv carries the child sandbox as one merged ``--settings`` value.

A request that names no policy resolves the default one, a policy the caller already
has in ``settings`` is merged and never dropped, and a disabled policy leaves the argv as
it was. The Claude binary and its version are stubbed, so nothing here starts a process
or reads the real home.
"""

from __future__ import annotations

import importlib
import json

import pytest

from lionagi._errors import ConfigurationError

child_sandbox = importlib.import_module("lionhub.agent.child_sandbox")
claude_code = importlib.import_module("lionhub.providers.anthropic.claude_code")

ClaudeCodeRequest = claude_code.ClaudeCodeRequest
LIGHT_DISALLOWED_TOOLS = claude_code.LIGHT_DISALLOWED_TOOLS
CLAUDE_CLI_FLOOR = child_sandbox.CLAUDE_CLI_FLOOR
DEFAULT_DENY_READ = child_sandbox.DEFAULT_DENY_READ
ChildSandboxPolicy = child_sandbox.ChildSandboxPolicy
claude_settings_for = child_sandbox.claude_settings_for

# The real backend check, captured before the fixture below replaces it for each test.
_REAL_BACKEND_CHECK = child_sandbox.check_sandbox_backend

POLICY = ChildSandboxPolicy(True, ("~/.ssh", "/opt/keys"), ("~/.cache/uv",))
OFF = ChildSandboxPolicy(False, (), ())


@pytest.fixture(autouse=True)
def _stubbed_cli(monkeypatch, tmp_path):
    """A throwaway HOME, a Claude binary at the supported floor and no inherited cache setting.

    The host's sandbox backend is stubbed too, so no test here depends on ``bwrap`` or ``socat``.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.setattr(claude_code, "resolve_claude_cli", lambda: "claude")
    monkeypatch.setattr(child_sandbox, "_cli_version", lambda binary: CLAUDE_CLI_FLOOR)
    monkeypatch.setattr(child_sandbox, "check_sandbox_backend", lambda: None)


def _request(**kwargs):
    kwargs.setdefault("child_sandbox", POLICY.to_dict())
    return ClaudeCodeRequest(prompt="hi", **kwargs)


def _settings_values(args):
    return [args[i + 1] for i, arg in enumerate(args) if arg == "--settings"]


def _settings_json(args):
    values = _settings_values(args)
    assert len(values) == 1
    return json.loads(values[0])


# --------------------------------------------------------------------------- the sandbox block


def test_the_argv_carries_one_settings_flag_with_the_sandbox_block(tmp_path):
    args = _request(repo=tmp_path).as_cmd_args()
    assert _settings_json(args) == claude_settings_for(POLICY)


def test_the_policy_field_is_not_a_command_line_flag(tmp_path):
    req = _request(repo=tmp_path)
    assert not any("child_sandbox" in a or "child-sandbox" in a for a in req.as_cmd_args())
    assert "child_sandbox" not in req.model_dump()


def test_a_light_request_keeps_its_tool_denials_beside_the_sandbox(tmp_path):
    args = _request(repo=tmp_path, workload="light").as_cmd_args()
    assert _settings_json(args) == claude_settings_for(POLICY)
    text = " ".join(args)
    assert all(tool in text for tool in LIGHT_DISALLOWED_TOOLS)


# --------------------------------------------------------------------------- the default policy


def test_a_request_that_names_no_policy_resolves_the_default(monkeypatch, tmp_path):
    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: {})
    args = ClaudeCodeRequest(prompt="hi", repo=tmp_path).as_cmd_args()
    block = _settings_json(args)["sandbox"]
    assert block["enabled"] is True
    assert block["failIfUnavailable"] is True
    assert block["allowUnsandboxedCommands"] is False
    assert block["filesystem"]["denyRead"] == list(DEFAULT_DENY_READ)


def test_the_default_policy_follows_the_global_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(
        child_sandbox, "_global_settings", lambda: {"child_sandbox": {"deny_read": ["~/vault"]}}
    )
    args = ClaudeCodeRequest(prompt="hi", repo=tmp_path).as_cmd_args()
    deny = _settings_json(args)["sandbox"]["filesystem"]["denyRead"]
    assert deny == [*DEFAULT_DENY_READ, "~/vault"]


def test_the_global_settings_can_turn_the_default_off(monkeypatch, tmp_path):
    monkeypatch.setattr(
        child_sandbox, "_global_settings", lambda: {"child_sandbox": {"enabled": False}}
    )
    assert "--settings" not in ClaudeCodeRequest(prompt="hi", repo=tmp_path).as_cmd_args()


def test_a_bad_global_setting_refuses_the_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(
        child_sandbox, "_global_settings", lambda: {"child_sandbox": {"surprise": 1}}
    )
    with pytest.raises(ConfigurationError, match="unknown keys"):
        ClaudeCodeRequest(prompt="hi", repo=tmp_path).as_cmd_args()


def test_a_malformed_policy_dict_refuses_the_launch(tmp_path):
    with pytest.raises(ConfigurationError, match="unknown keys"):
        _request(repo=tmp_path, child_sandbox={"surprise": 1}).as_cmd_args()


# --------------------------------------------------------------------------- merging settings


def test_inline_settings_are_merged_into_the_one_flag(tmp_path):
    own = json.dumps(
        {
            "model": "haiku",
            "sandbox": {"filesystem": {"denyRead": ["/already"]}},
            "permissions": {"deny": ["Bash(rm:*)"]},
        }
    )
    args = _request(repo=tmp_path, settings=own).as_cmd_args()
    merged = _settings_json(args)
    ours = claude_settings_for(POLICY)
    assert merged["model"] == "haiku"
    assert merged["sandbox"]["enabled"] is True
    assert merged["sandbox"]["filesystem"]["denyRead"] == ["/already", *POLICY.deny_read]
    assert merged["sandbox"]["filesystem"]["allowWrite"] == list(POLICY.allow_write)
    assert merged["permissions"]["deny"] == ["Bash(rm:*)", *ours["permissions"]["deny"]]


def test_a_settings_file_is_read_and_merged_into_the_one_flag(tmp_path):
    (tmp_path / "base.json").write_text(json.dumps({"model": "haiku"}), encoding="utf-8")
    args = _request(repo=tmp_path, settings="base.json").as_cmd_args()
    merged = _settings_json(args)
    assert merged["model"] == "haiku"
    assert merged["sandbox"] == claude_settings_for(POLICY)["sandbox"]
    assert "base.json" not in args


def test_a_missing_settings_file_refuses_the_launch(tmp_path):
    with pytest.raises(ConfigurationError, match="cannot read settings file"):
        _request(repo=tmp_path, settings="absent.json").as_cmd_args()


def test_a_settings_file_that_is_not_an_object_refuses_the_launch(tmp_path):
    (tmp_path / "list.json").write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="must be a JSON object"):
        _request(repo=tmp_path, settings="list.json").as_cmd_args()


# --------------------------------------------------------------------------- a disabled policy


def test_a_disabled_policy_emits_no_sandbox(tmp_path):
    args = _request(repo=tmp_path, child_sandbox=OFF.to_dict()).as_cmd_args()
    assert "--settings" not in args
    assert not any('"sandbox"' in a for a in args)


def test_a_disabled_policy_leaves_the_callers_settings_as_they_were(tmp_path):
    own = json.dumps({"model": "haiku"})
    args = _request(repo=tmp_path, settings=own, child_sandbox=OFF.to_dict()).as_cmd_args()
    assert _settings_values(args) == [own]


def test_a_disabled_policy_runs_no_version_probe(monkeypatch, tmp_path):
    def probe(binary):
        raise AssertionError("the version probe ran for a disabled policy")

    monkeypatch.setattr(child_sandbox, "_cli_version", probe)
    _request(repo=tmp_path, child_sandbox=OFF.to_dict()).as_cmd_args()


# --------------------------------------------------------------------------- the CLI floor


def test_a_cli_below_the_floor_refuses_the_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(child_sandbox, "_cli_version", lambda binary: (2, 1, 322))
    with pytest.raises(ConfigurationError, match="older than"):
        _request(repo=tmp_path).as_cmd_args()


def test_a_cli_at_the_floor_launches(tmp_path):
    assert _settings_values(_request(repo=tmp_path).as_cmd_args())


# --------------------------------------------------------------------------- the sandbox backend


def test_a_host_with_no_sandbox_backend_refuses_the_launch(monkeypatch, tmp_path):
    def refuse():
        raise ConfigurationError("no sandbox backend on this host")

    monkeypatch.setattr(child_sandbox, "check_sandbox_backend", refuse)
    with pytest.raises(ConfigurationError, match="no sandbox backend"):
        _request(repo=tmp_path).as_cmd_args()


def test_the_backend_check_runs_before_the_cli_probe(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(child_sandbox, "check_sandbox_backend", lambda: calls.append("backend"))
    monkeypatch.setattr(claude_code, "resolve_claude_cli", lambda: calls.append("cli") or "claude")
    _request(repo=tmp_path)._child_sandbox_settings()
    assert calls == ["backend", "cli"]


def test_a_disabled_policy_runs_no_backend_check(monkeypatch, tmp_path):
    def probe():
        raise AssertionError("the backend check ran for a disabled policy")

    monkeypatch.setattr(child_sandbox, "check_sandbox_backend", probe)
    _request(repo=tmp_path, child_sandbox=OFF.to_dict()).as_cmd_args()


def test_the_real_backend_check_names_the_missing_programs(monkeypatch, tmp_path):
    request = _request(repo=tmp_path)
    monkeypatch.setattr(child_sandbox, "check_sandbox_backend", _REAL_BACKEND_CHECK)
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(child_sandbox.shutil, "which", lambda name: None)
    with pytest.raises(ConfigurationError, match="bwrap") as caught:
        request._child_sandbox_settings()
    assert str(caught.value).endswith(child_sandbox.CHILD_SANDBOX_OPT_OUT)
