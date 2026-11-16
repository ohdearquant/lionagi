# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Submission and provider integration for resolved MCP configuration."""

from __future__ import annotations

from lionhub.orchestration.mcp_config import discover_mcp_config, resolve_spawn_mcp_servers
from tests._mcp_config_test_support import SERVERS, _write_config


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

    resolution = resolve_spawn_mcp_servers(submit_snapshot=snapshot, launch_dir=checkout)

    assert resolution.servers == SERVERS
    assert resolution.source == source
    assert resolution.searched_from == server_dir
    assert resolution.explicit is False
    assert discover_mcp_config(resolution.searched_from) == resolution.source


def test_only_the_claude_lane_receives_the_server_set():
    """The other CLI providers read a user-level config; handing them a server
    set here would drop it silently, so the builder does not."""
    from lionhub.orchestration.spawn import build_chat_model

    claude = build_chat_model("claude_code", "opus", False, False, None, mcp_servers=SERVERS)
    assert claude.endpoint.config.kwargs["mcp_servers"] == SERVERS

    codex = build_chat_model("codex", "gpt-5.6", False, False, None, mcp_servers=SERVERS)
    kwargs = getattr(getattr(codex, "endpoint", None), "config", None)
    assert kwargs is None or "mcp_servers" not in kwargs.kwargs
