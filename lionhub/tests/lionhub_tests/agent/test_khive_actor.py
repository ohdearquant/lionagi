# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Actor attribution for the khive MCP servers a child agent is handed."""

from __future__ import annotations

import copy

import pytest

from lionhub.agent.khive_actor import attribute_khive_servers, child_khive_actor


def _khive(env=None, *, command="/usr/local/bin/kkernel", **extra):
    server = {"command": command, "args": ["mcp"], **extra}
    if env is not None:
        server["env"] = env
    return server


# --- child_khive_actor -----------------------------------------------------


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ("implementer", "agent:implementer"),
        ("code-reviewer", "agent:code-reviewer"),
        ("a.b_c-1", "agent:a.b_c-1"),
        ("X", "agent:X"),
    ],
)
def test_valid_profile_names_the_actor(profile, expected):
    assert child_khive_actor(profile) == expected


@pytest.mark.parametrize(
    "profile",
    [None, "", "a/b", "has space", " lead", "tab\tname", "new\nline", "caf\u00e9", 7, ["x"]],
)
def test_unusable_profile_names_fall_back_to_anonymous(profile):
    assert child_khive_actor(profile) == "agent:anonymous"


# --- attribute_khive_servers ----------------------------------------------


def test_none_stays_none():
    assert attribute_khive_servers(None, "agent:x") is None


def test_empty_mapping_gives_an_empty_new_dict():
    source: dict = {}
    result = attribute_khive_servers(source, "agent:x")
    assert result == {}
    assert result is not source


def test_kkernel_server_gets_the_actor_and_keeps_its_packs():
    servers = {"khive": _khive({"KHIVE_PACKS": "kg, comm ,memory"})}
    result = attribute_khive_servers(servers, "agent:implementer")
    env = result["khive"]["env"]
    assert env == {"KHIVE_ACTOR": "agent:implementer", "KHIVE_PACKS": "kg, comm ,memory"}


def test_pack_flags_in_the_arguments_are_kept():
    args = ["mcp", "--pack", "kg", "--pack", "comm", "--pack=comm"]
    servers = {"khive": _khive({"KHIVE_PACKS": "kg"}, args=args)}
    result = attribute_khive_servers(servers, "agent:x")
    assert result["khive"]["args"] == args


def test_an_existing_actor_is_replaced():
    servers = {"khive": _khive({"KHIVE_ACTOR": "lion", "OTHER": "kept"})}
    result = attribute_khive_servers(servers, "agent:reviewer")
    env = result["khive"]["env"]
    assert env["KHIVE_ACTOR"] == "agent:reviewer"
    assert env["OTHER"] == "kept"


def test_absent_packs_stay_absent():
    servers = {"khive": _khive({"KHIVE_ACTOR": "lion"})}
    result = attribute_khive_servers(servers, "agent:x")
    assert result["khive"]["env"] == {"KHIVE_ACTOR": "agent:x"}


def test_a_server_with_no_env_gets_one():
    servers = {"khive": _khive()}
    result = attribute_khive_servers(servers, "agent:x")
    assert result["khive"]["env"] == {"KHIVE_ACTOR": "agent:x"}


def test_a_null_env_is_treated_as_empty():
    servers = {"khive": {"command": "kkernel", "args": ["mcp"], "env": None}}
    result = attribute_khive_servers(servers, "agent:x")
    assert result["khive"]["env"] == {"KHIVE_ACTOR": "agent:x"}


def test_other_keys_of_the_server_pass_through():
    servers = {"khive": _khive({"A": "1"}, cwd="/work", timeout=30)}
    result = attribute_khive_servers(servers, "agent:x")
    server = result["khive"]
    assert server["command"] == "/usr/local/bin/kkernel"
    assert server["args"] == ["mcp"]
    assert server["cwd"] == "/work"
    assert server["timeout"] == 30


@pytest.mark.parametrize(
    "command",
    ["kkernel", "/opt/khive/bin/kkernel", "./target/release/kkernel"],
)
def test_kkernel_is_recognised_by_basename(command):
    servers = {"k": _khive({"KHIVE_PACKS": "comm"}, command=command)}
    result = attribute_khive_servers(servers, "agent:x")
    assert result["k"]["env"]["KHIVE_ACTOR"] == "agent:x"


@pytest.mark.parametrize(
    "command",
    ["kkernel-old", "mykkernel", "/opt/kkernel/run", "python", "", None, 3],
)
def test_other_commands_are_not_kkernel(command):
    server = {"command": command, "args": ["mcp"], "env": {"KHIVE_PACKS": "kg,comm"}}
    result = attribute_khive_servers({"s": server}, "agent:x")
    assert result["s"] is server
    assert result["s"]["env"] == {"KHIVE_PACKS": "kg,comm"}


def test_non_kkernel_and_url_servers_are_unchanged():
    other = {"command": "npx", "args": ["-y", "thing"], "env": {"KHIVE_PACKS": "comm"}}
    remote = {"url": "https://example.invalid/mcp", "headers": {"Authorization": "x"}}
    servers = {"khive": _khive({"KHIVE_PACKS": "kg,comm"}), "other": other, "remote": remote}
    result = attribute_khive_servers(servers, "agent:x")
    assert result["other"] == {
        "command": "npx",
        "args": ["-y", "thing"],
        "env": {"KHIVE_PACKS": "comm"},
    }
    assert result["remote"] == {
        "url": "https://example.invalid/mcp",
        "headers": {"Authorization": "x"},
    }
    assert "env" not in result["remote"]
    assert list(result) == ["khive", "other", "remote"]


def test_the_input_is_never_mutated():
    servers = {
        "khive": _khive({"KHIVE_ACTOR": "lion", "KHIVE_PACKS": "kg, comm ,memory"}),
        "other": {"command": "npx", "env": {"X": "1"}},
    }
    before = copy.deepcopy(servers)
    result = attribute_khive_servers(servers, "agent:x")
    assert servers == before
    assert result is not servers
    assert result["khive"] is not servers["khive"]
    assert result["khive"]["env"] is not servers["khive"]["env"]


def test_the_input_env_survives_when_the_server_had_none():
    servers = {"khive": _khive()}
    before = copy.deepcopy(servers)
    attribute_khive_servers(servers, "agent:x")
    assert servers == before
    assert "env" not in servers["khive"]


# --- the codex hand-off ---------------------------------------------------


@pytest.fixture
def codex_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    codex_home = tmp_path / "codex_home"
    home.mkdir()
    codex_home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    return {"HOME": str(home), "CODEX_HOME": str(codex_home)}


def test_a_codex_run_that_ignores_the_user_config_gets_the_actor_on_the_command_line(codex_env):
    from lionhub.agent.factory import apply_forwarded_mcp_servers

    servers = {"khive": {"command": "kkernel", "args": ["mcp"], "env": {"KHIVE_ACTOR": "agent:x"}}}
    kwargs: dict = {"ignore_user_config": True}

    assert apply_forwarded_mcp_servers(kwargs, servers, provider="codex", env=codex_env)

    assert kwargs["config_overrides"] == {
        "mcp_servers.khive.command": "kkernel",
        "mcp_servers.khive.args": ["mcp"],
        "mcp_servers.khive.env.KHIVE_ACTOR": "agent:x",
    }
    assert "profile" not in kwargs


def test_the_rest_of_a_khive_servers_env_stays_off_the_command_line(codex_env):
    from lionhub.agent.factory import apply_forwarded_mcp_servers

    env = {"KHIVE_ACTOR": "agent:x", "TOKEN": "value"}
    servers = {"khive": {"command": "kkernel", "args": ["mcp"], "env": env}}
    kwargs: dict = {}

    assert apply_forwarded_mcp_servers(kwargs, servers, provider="codex", env=codex_env)

    overrides = kwargs["config_overrides"]
    assert overrides["mcp_servers.khive.env.KHIVE_ACTOR"] == "agent:x"
    assert not [key for key in overrides if "TOKEN" in key or key == "mcp_servers.khive.env"]
    assert "value" not in repr(overrides)
    assert kwargs.get("profile")
    assert env == {"KHIVE_ACTOR": "agent:x", "TOKEN": "value"}


def test_another_servers_khive_actor_is_still_a_secret_field(codex_env):
    from lionagi._errors import ConfigurationError
    from lionhub.agent.factory import apply_forwarded_mcp_servers

    servers = {"other": {"command": "npx", "args": ["x"], "env": {"KHIVE_ACTOR": "agent:x"}}}
    kwargs: dict = {"ignore_user_config": True}

    with pytest.raises(ConfigurationError, match="ignores the user config"):
        apply_forwarded_mcp_servers(kwargs, servers, provider="codex", env=codex_env)


@pytest.mark.parametrize(
    "args",
    [
        ["mcp"],
        ["mcp", "--actor", "team:runner"],
        ["mcp", "--namespace=team", "--pack", "kg"],
    ],
)
def test_the_server_arguments_are_left_as_configured(args):
    # `--actor` and `--namespace` choose the server's default namespace, so the
    # child's actor travels only in the attribution variable.
    result = attribute_khive_servers({"khive": {"command": "kkernel", "args": args}}, "agent:x")
    assert result["khive"]["args"] == args
    assert result["khive"]["env"]["KHIVE_ACTOR"] == "agent:x"
