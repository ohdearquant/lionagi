# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Submit-time MCP resolution: the leg's tool surface comes from the submission."""

from __future__ import annotations

import json

import pytest

from lionagi import _paths
from lionagi.cli._mcp_resolve import (
    SNAPSHOT_SEARCHED_FROM_KEY,
    SNAPSHOT_SOURCE_KEY,
    McpConfigError,
    discover_mcp_config,
    resolve_spawn_mcp_servers,
)

SERVERS = {"khive": {"command": "kkernel", "args": ["mcp"]}}


def _write_config(directory, servers=SERVERS):
    path = directory / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": servers}))
    return path


def test_discovery_walks_up_from_the_launch_directory(tmp_path):
    _write_config(tmp_path)
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert discover_mcp_config(deep) == tmp_path / ".mcp.json"


def test_account_home_bare_config_is_not_discovered(tmp_path, monkeypatch):
    account_home = tmp_path / "acct"
    launch_dir = account_home / "work" / "repo"
    launch_dir.mkdir(parents=True)
    safe_home = tmp_path / "safe"
    safe_home.mkdir()
    _write_config(account_home, {"bare": {"command": "bare"}})
    monkeypatch.setattr(_paths, "_account_home", lambda: account_home)
    monkeypatch.setenv("HOME", str(safe_home))

    resolution = resolve_spawn_mcp_servers(launch_dir=launch_dir)

    assert resolution.servers is None
    assert resolution.reason == "no_mcp_config_found"
    assert resolution.source is None


def test_servers_are_read_from_the_launch_dir_not_the_target_cwd(tmp_path):
    """The defect this exists for: a leg pointed at a checkout keeps the
    submitting directory's servers instead of finding none of its own."""
    caller = tmp_path / "caller"
    checkout = tmp_path / "checkout"
    caller.mkdir()
    checkout.mkdir()
    _write_config(caller)

    assert discover_mcp_config(checkout) is None
    resolution = resolve_spawn_mcp_servers(launch_dir=caller)
    assert resolution.servers == SERVERS
    assert resolution.source == caller / ".mcp.json"


def test_no_config_reports_a_reason_rather_than_an_empty_result(tmp_path):
    """Nothing-configured must stay distinguishable from configured-and-unusable;
    collapsing them is what made the original loss silent."""
    resolution = resolve_spawn_mcp_servers(launch_dir=tmp_path)
    assert resolution.servers is None
    assert resolution.reason == "no_mcp_config_found"
    assert not resolution.ok


def test_disabled_is_a_choice_and_carries_no_reason(tmp_path):
    _write_config(tmp_path)
    resolution = resolve_spawn_mcp_servers(launch_dir=tmp_path, disabled=True)
    assert resolution.servers is None
    assert resolution.reason is None


def test_unusable_discovered_config_reports_why(tmp_path):
    (tmp_path / ".mcp.json").write_text("{not json")
    resolution = resolve_spawn_mcp_servers(launch_dir=tmp_path)
    assert resolution.servers is None
    assert resolution.reason.startswith("mcp_config_unusable:")


def test_explicit_config_that_cannot_be_used_is_an_error(tmp_path):
    """A caller who named a file is not asking for a silent fallback."""
    with pytest.raises(McpConfigError, match="--mcp-config"):
        resolve_spawn_mcp_servers(str(tmp_path / "absent.json"), launch_dir=tmp_path)


def test_explicit_config_wins_over_discovery(tmp_path):
    _write_config(tmp_path, {"discovered": {"command": "x"}})
    named = tmp_path / "named.json"
    named.write_text(json.dumps({"mcpServers": SERVERS}))
    resolution = resolve_spawn_mcp_servers(str(named), launch_dir=tmp_path)
    assert resolution.servers == SERVERS


def test_snapshot_keeps_the_discovered_source_and_is_not_explicit(tmp_path):
    source = _write_config(tmp_path)
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"mcpServers": SERVERS, SNAPSHOT_SOURCE_KEY: str(source)}))

    resolution = resolve_spawn_mcp_servers(snapshot, launch_dir=tmp_path)

    assert resolution.servers == SERVERS
    assert resolution.source == source
    assert resolution.explicit is False


def test_a_snapshot_reports_the_directory_its_search_began_in(tmp_path):
    """A run whose submission named a cwd starts there; when that cwd is not where the
    search began, the pair the snapshot reports must still rediscover the same file."""
    from lionhub.serve.mcp.jobs import _write_mcp_server_snapshot

    server_dir = tmp_path / "server"
    checkout = tmp_path / "checkout"
    server_dir.mkdir()
    checkout.mkdir()
    source = _write_config(server_dir)
    _write_config(checkout, {"other": {"command": "x"}})
    snapshot = tmp_path / "job" / "mcp-servers.json"
    snapshot.parent.mkdir()
    _write_mcp_server_snapshot(snapshot, SERVERS, source=source, searched_from=server_dir)

    resolution = resolve_spawn_mcp_servers(snapshot, launch_dir=checkout)

    assert resolution.servers == SERVERS
    assert resolution.source == source
    assert resolution.searched_from == server_dir
    assert resolution.explicit is False
    assert discover_mcp_config(resolution.searched_from) == resolution.source


def test_a_snapshot_without_its_search_directory_reports_the_launch_directory(tmp_path):
    """A snapshot written before the search directory was recorded still resolves."""
    source = _write_config(tmp_path)
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"mcpServers": SERVERS, SNAPSHOT_SOURCE_KEY: str(source)}))

    resolution = resolve_spawn_mcp_servers(snapshot, launch_dir=tmp_path / ".")

    assert resolution.searched_from == tmp_path.resolve()


@pytest.mark.parametrize("key", [SNAPSHOT_SOURCE_KEY, SNAPSHOT_SEARCHED_FROM_KEY])
@pytest.mark.parametrize(
    "value", [None, 5, ["x"], "", "rel/.mcp.json"], ids=["null", "int", "list", "empty", "relative"]
)
def test_a_snapshot_path_that_is_not_absolute_makes_the_file_unusable(tmp_path, key, value):
    document = {
        "mcpServers": SERVERS,
        SNAPSHOT_SOURCE_KEY: str(tmp_path / ".mcp.json"),
        SNAPSHOT_SEARCHED_FROM_KEY: str(tmp_path),
    }
    document[key] = value
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(document))

    with pytest.raises(McpConfigError, match=f"{key}.*must be an absolute path"):
        resolve_spawn_mcp_servers(snapshot, launch_dir=tmp_path)


def test_config_without_servers_key_is_unusable_not_empty(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({"other": {}}))
    resolution = resolve_spawn_mcp_servers(launch_dir=tmp_path)
    assert resolution.reason.startswith("mcp_config_unusable:")


def test_only_the_claude_lane_receives_the_server_set():
    """The other CLI providers read a user-level config; handing them a server
    set here would drop it silently, so the builder does not."""
    from lionagi.cli._providers import build_chat_model

    claude = build_chat_model("claude_code", "opus", False, False, None, mcp_servers=SERVERS)
    assert claude.endpoint.config.kwargs["mcp_servers"] == SERVERS

    codex = build_chat_model("codex", "gpt-5.6", False, False, None, mcp_servers=SERVERS)
    kwargs = getattr(getattr(codex, "endpoint", None), "config", None)
    assert kwargs is None or "mcp_servers" not in kwargs.kwargs
