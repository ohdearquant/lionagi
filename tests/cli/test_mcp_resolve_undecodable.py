# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A config that is not valid UTF-8 is reported as unusable when it is
discovered, and refused by name when it is named."""

from __future__ import annotations

import pytest

from lionagi.cli._mcp_resolve import McpConfigError, resolve_spawn_mcp_servers


def test_undecodable_discovered_config_is_reported_as_unusable_naming_the_file(tmp_path):
    config = tmp_path / ".mcp.json"
    config.write_bytes(b'{"mcpServers": {"caf\xe9": {"command": "x"}}}')
    sub = tmp_path / "sub"
    sub.mkdir()

    resolution = resolve_spawn_mcp_servers(launch_dir=sub)

    assert resolution.servers is None
    assert resolution.reason.startswith(
        f"mcp_config_unusable:{config.resolve()} is not valid UTF-8:"
    )


def test_undecodable_explicit_config_is_an_error_naming_the_file(tmp_path):
    config = tmp_path / ".mcp.json"
    config.write_bytes(b'{"mcpServers": {"caf\xe9": {"command": "x"}}}')

    with pytest.raises(McpConfigError) as raised:
        resolve_spawn_mcp_servers(config, launch_dir=tmp_path)

    assert str(config) in str(raised.value)
