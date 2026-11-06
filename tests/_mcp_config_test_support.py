# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Shared temporary MCP configuration data and writer for tests."""

from __future__ import annotations

import json

SERVERS = {"khive": {"command": "kkernel", "args": ["mcp"]}}


def _write_config(directory, servers=SERVERS):
    path = directory / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": servers}))
    return path
