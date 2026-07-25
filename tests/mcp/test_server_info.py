# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`server_info` is how a caller tells a current server from a stale one."""

from __future__ import annotations

import asyncio

from lionagi.cli.machine import CONTRACT_VERSION
from lionagi.mcp import server
from lionagi.version import __version__


def _info() -> dict:
    return asyncio.run(server.server_info())


def test_reports_the_build_it_is_serving():
    info = _info()
    assert info["lionagi_version"] == __version__
    assert info["contract_version"] == CONTRACT_VERSION
    assert info["server_name"] == server.SERVER_NAME


def test_lists_every_registered_tool_by_name():
    # A caller checks for the capability it came for. A count alone cannot answer
    # "is job_wait here?", which is the question a stale server makes them ask.
    info = _info()
    registered = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert set(info["tools"]) == registered
    assert info["tool_count"] == len(registered)
    assert info["tools"] == sorted(info["tools"])
    assert "server_info" in info["tools"]


def test_reports_the_process_it_is_answering_from():
    import os

    info = _info()
    assert info["pid"] == os.getpid()
    assert info["uptime_seconds"] >= 0
    # started_at is when this process loaded the code, which is the fact that
    # makes staleness legible: a long uptime on an old version is the signature.
    assert info["started_at"].endswith("+00:00")
