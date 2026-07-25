# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The advertised schema has a fixed ceiling, and this is what holds it there.

Everything an MCP server advertises is sent to the model on every request, in
every session, by every caller. That cost is invisible in review — a tool added
to the list looks like one small function — so it is measured here instead, and
a change that grows the advertised payload past the bound fails rather than
merely being noticed later.

The bound is not aspirational. It is what this surface actually costs plus
headroom, so it fails on a real regression and not on a reworded description.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("fastmcp", reason="requires the 'mcp' extra")

from lionagi.mcp import server  # noqa: E402 — must follow the extra guard

# The surface costs a little over 2,100 bytes as one tool. The bound is a little
# under double that: enough headroom to reword the tool's own documentation,
# nowhere near enough to absorb a second advertised tool (the smallest tool on
# the previous per-operation surface was ~430 bytes and the submit tools were
# 6,400-9,200 each), nor to inline a verb's parameters into the tool schema,
# which is the failure this bound exists to catch.
MAX_ADVERTISED_BYTES = 4_096

# One tool. Not "few" — one. A second advertised tool reopens, per tool, the
# argument this shape settled once.
MAX_ADVERTISED_TOOLS = 1


def advertised() -> str:
    """Exactly what a `tools/list` puts on the wire, serialized compactly."""
    tools = asyncio.run(server.mcp.list_tools())
    payload = [t.to_mcp_tool().model_dump(mode="json", exclude_none=True) for t in tools]
    return json.dumps(payload, separators=(",", ":"))


def test_the_advertised_schema_stays_under_its_ceiling():
    blob = advertised()
    assert len(blob) <= MAX_ADVERTISED_BYTES, (
        f"the advertised tool schema is {len(blob)} bytes, over the "
        f"{MAX_ADVERTISED_BYTES} byte ceiling. Every byte here is sent on every "
        "request of every session. A verb's parameters belong behind `help`, "
        "not in the tool schema."
    )


def test_exactly_one_tool_is_advertised():
    tools = asyncio.run(server.mcp.list_tools())
    names = sorted(t.name for t in tools)
    assert len(names) == MAX_ADVERTISED_TOOLS, f"advertised tools: {names}"
    assert names == ["request"]


def test_the_advertised_schema_describes_only_ops_and_help():
    # The tool schema is not allowed to become the union of every verb's
    # parameters — that is the growth the ceiling is guarding against, and it
    # would pass the byte check for a while before it stopped.
    tool = asyncio.run(server.mcp.list_tools())[0].to_mcp_tool()
    assert set(tool.inputSchema.get("properties", {})) == {"ops", "help"}
