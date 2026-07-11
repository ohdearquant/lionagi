# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Pins the exclude=True trade-off on ClaudeCodeRequest.mcp_servers.

``mcp_servers`` carries the same shape as the raw ``mcpServers`` block of a
``.mcp.json`` file, i.e. server command lines that commonly embed secrets
(API keys/tokens in an ``env`` dict). It is marked ``exclude=True`` so those
values never make it into ``model_dump()``/``model_dump_json()`` output —
notably ``APICalling.to_dict()``, which feeds persisted event logs and
branch snapshots on disk (``~/.lionagi/runs/.../branches/{id}.json``).

Investigated whether this ever erases the value from the path that
actually matters (issuing the CLI subprocess call): it does not.
``ClaudeCodeCLIEndpoint._call`` executes ``payload["request"]`` — the live
``ClaudeCodeRequest`` instance built directly from kwargs each turn — never
a re-validated/reconstructed copy from a dumped dict. The ``auto_finish``
continuation turn uses ``request.model_copy(deep=True)`` (a field-preserving
clone, unaffected by ``exclude``), not ``model_dump``/``model_validate``.
There is no ``APICalling.from_dict`` construction path that replays a
persisted record back into a live call, so the one real round-trip
(``to_dict()`` for persistence) is one-directional: it is never validated
back into an executable request. Given that, keeping ``exclude=True`` is the
right trade-off (avoids leaking MCP server secrets into on-disk logs) and
costs nothing functionally — pinned here rather than "fixed" by removing it.
"""

from __future__ import annotations

import pytest

from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest
from lionagi.service.imodel import iModel


def test_mcp_servers_excluded_from_model_dump():
    """exclude=True hides mcp_servers from model_dump(), by design."""
    req = ClaudeCodeRequest(prompt="hi", mcp_servers={"khive": {"command": "khive-mcp"}})
    assert req.mcp_servers == {"khive": {"command": "khive-mcp"}}
    assert "mcp_servers" not in req.model_dump()
    assert "mcp_servers" not in req.model_dump(mode="json")


def test_mcp_servers_survives_auto_finish_model_copy():
    """The auto_finish continuation turn clones via model_copy (field-
    preserving), not model_dump/model_validate — mcp_servers must survive."""
    req = ClaudeCodeRequest(prompt="hi", mcp_servers={"khive": {"command": "khive-mcp"}})
    req2 = req.model_copy(deep=True)
    assert req2.mcp_servers == {"khive": {"command": "khive-mcp"}}


@pytest.mark.asyncio
async def test_mcp_servers_present_on_live_request_but_absent_from_persisted_dict():
    """The live request object used to actually issue the CLI call carries
    mcp_servers; the persisted-record view (APICalling.to_dict(), which
    feeds event logs / branch snapshots) intentionally does not."""
    m = iModel(provider="claude_code", model="sonnet", api_key="dummy")
    m.endpoint.config.kwargs["mcp_servers"] = {"khive": {"command": "khive-mcp"}}

    api_call = await m.create_event(prompt="hi")

    live_request = api_call.payload["request"]
    assert isinstance(live_request, ClaudeCodeRequest)
    assert live_request.mcp_servers == {"khive": {"command": "khive-mcp"}}

    persisted = api_call.to_dict()
    assert "mcp_servers" not in persisted["payload"]["request"]
