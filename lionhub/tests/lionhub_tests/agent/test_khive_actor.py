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


def test_kkernel_server_gets_the_actor_and_loses_comm():
    servers = {"khive": _khive({"KHIVE_PACKS": "kg, comm ,memory"})}
    result = attribute_khive_servers(servers, "agent:implementer")
    env = result["khive"]["env"]
    assert env["KHIVE_ACTOR"] == "agent:implementer"
    assert env["KHIVE_PACKS"] == "kg,memory"


def test_pack_order_is_kept_and_empty_entries_are_dropped():
    servers = {"khive": _khive({"KHIVE_PACKS": "memory,,comm,  ,kg,gtd"})}
    result = attribute_khive_servers(servers, "agent:x")
    assert result["khive"]["env"]["KHIVE_PACKS"] == "memory,kg,gtd"


def test_comm_is_matched_as_a_whole_pack_name():
    servers = {"khive": _khive({"KHIVE_PACKS": "kg,communications,comm2,comm"})}
    result = attribute_khive_servers(servers, "agent:x")
    assert result["khive"]["env"]["KHIVE_PACKS"] == "kg,communications,comm2"


CHILD_PACKS = "kg,gtd,memory,brain,schedule,knowledge,session,tool,exec,git,code,workspace,blob"


def test_a_comm_only_list_becomes_the_built_in_set_without_comm():
    # khive ignores an empty KHIVE_PACKS and falls back to a set that includes comm.
    servers = {"khive": _khive({"KHIVE_PACKS": " comm "})}
    result = attribute_khive_servers(servers, "agent:x")
    assert result["khive"]["env"]["KHIVE_PACKS"] == CHILD_PACKS


def test_a_whitespace_separated_list_loses_comm():
    servers = {"khive": _khive({"KHIVE_PACKS": "kg comm\tmemory"})}
    result = attribute_khive_servers(servers, "agent:x")
    assert result["khive"]["env"]["KHIVE_PACKS"] == "kg,memory"


def test_a_pack_flag_for_comm_is_removed_from_the_arguments():
    servers = {
        "khive": _khive(
            {"KHIVE_PACKS": "kg"},
            args=["mcp", "--pack", "kg", "--pack", "comm", "--pack=comm", "--pack", "gtd"],
        )
    }
    result = attribute_khive_servers(servers, "agent:x")
    assert result["khive"]["args"] == ["mcp", "--pack", "kg", "--pack", "gtd"]
    assert servers["khive"]["args"][4] == "comm"


def test_an_existing_actor_is_replaced():
    servers = {"khive": _khive({"KHIVE_ACTOR": "lion", "OTHER": "kept"})}
    result = attribute_khive_servers(servers, "agent:reviewer")
    env = result["khive"]["env"]
    assert env["KHIVE_ACTOR"] == "agent:reviewer"
    assert env["OTHER"] == "kept"


def test_absent_packs_become_the_built_in_set_without_comm():
    servers = {"khive": _khive({"KHIVE_ACTOR": "lion"})}
    result = attribute_khive_servers(servers, "agent:x")
    assert result["khive"]["env"]["KHIVE_PACKS"] == CHILD_PACKS
    assert "comm" not in result["khive"]["env"]["KHIVE_PACKS"].split(",")


def test_a_server_with_no_env_gets_one():
    servers = {"khive": _khive()}
    result = attribute_khive_servers(servers, "agent:x")
    assert result["khive"]["env"] == {"KHIVE_ACTOR": "agent:x", "KHIVE_PACKS": CHILD_PACKS}


def test_a_null_env_is_treated_as_empty():
    servers = {"khive": {"command": "kkernel", "args": ["mcp"], "env": None}}
    result = attribute_khive_servers(servers, "agent:x")
    assert result["khive"]["env"] == {"KHIVE_ACTOR": "agent:x", "KHIVE_PACKS": CHILD_PACKS}


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
