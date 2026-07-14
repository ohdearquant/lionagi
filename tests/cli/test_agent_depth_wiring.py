# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Wiring of LIONAGI_AGENT_DEPTH stamping into `_run_agent`, `_run_fanout`,
`_run_flow`, and the engine subprocess spawn (`ndjson_from_cli`).

The stamp must land in os.environ BEFORE any provider/engine spawn — the
mechanism (see docs/internals/cli.md) relies on `ndjson_from_cli` passing
env=None to create_subprocess_exec so the spawned engine inherits this
process's os.environ verbatim.
"""

from __future__ import annotations

import os

import pytest

from lionagi.cli import _agent_depth as depth_mod
from lionagi.cli._agent_depth import DEPTH_ENV


@pytest.fixture(autouse=True)
def _clean_depth_env(monkeypatch):
    monkeypatch.delenv(DEPTH_ENV, raising=False)


class _StopEarly(Exception):
    """Sentinel raised to abort a function right after the stamp point."""


@pytest.mark.asyncio
async def test_run_agent_stamps_depth_before_any_spawn(monkeypatch):
    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(depth_mod, "_INHERITED_DEPTH", 0)

    def _boom_build_chat_model(*a, **kw):
        raise _StopEarly("build_chat_model must not be reached before the stamp")

    monkeypatch.setattr(agent_mod, "build_chat_model", _boom_build_chat_model)

    with pytest.raises(_StopEarly):
        await agent_mod._run_agent("claude", "do the thing", agent_name=None)

    # No seat set configured, no -a profile -> non-seat -> parent(0) + 1.
    assert os.environ[DEPTH_ENV] == "1"


@pytest.mark.asyncio
async def test_run_fanout_stamps_depth_before_setup(monkeypatch):
    import lionagi.cli.orchestrate.fanout as fanout_mod

    monkeypatch.setattr(depth_mod, "_INHERITED_DEPTH", 2)

    async def _boom_setup_orchestration(*a, **kw):
        raise _StopEarly("setup_orchestration must not be reached before the stamp")

    monkeypatch.setattr(fanout_mod, "setup_orchestration", _boom_setup_orchestration)

    with pytest.raises(_StopEarly):
        await fanout_mod._run_fanout("claude", "do the thing")

    # Fanout workers are never seats: unconditional parent(2) + 1.
    assert os.environ[DEPTH_ENV] == "3"


@pytest.mark.asyncio
async def test_run_flow_stamps_depth_before_setup(monkeypatch):
    import lionagi.cli.orchestrate.flow as flow_mod

    monkeypatch.setattr(depth_mod, "_INHERITED_DEPTH", 1)

    async def _boom_setup_orchestration(*a, **kw):
        raise _StopEarly("setup_orchestration must not be reached before the stamp")

    monkeypatch.setattr(flow_mod, "setup_orchestration", _boom_setup_orchestration)

    with pytest.raises(_StopEarly):
        await flow_mod._run_flow("claude", "do the thing")

    # li o flow / li play workers are never seats: unconditional parent(1) + 1.
    assert os.environ[DEPTH_ENV] == "2"


@pytest.mark.asyncio
async def test_ndjson_from_cli_inherits_stamped_depth(monkeypatch):
    """The CLI engine spawn (shared by claude_code/codex/gemini_code) must
    pass env=None to create_subprocess_exec while LIONAGI_AGENT_DEPTH is
    already set in this process's os.environ — that combination is what
    makes the child inherit the stamp with zero endpoint changes."""
    import asyncio

    from lionagi.providers._cli_subprocess import ndjson_from_cli

    monkeypatch.setattr(depth_mod, "_INHERITED_DEPTH", 0)
    depth_mod.stamp_agent_depth("implementer")
    assert os.environ[DEPTH_ENV] == "1"

    captured_kwargs: dict = {}
    real_create_subprocess_exec = asyncio.create_subprocess_exec

    async def _spy_create_subprocess_exec(*args, **kwargs):
        captured_kwargs.update(kwargs)
        # os.environ must still carry the stamp at the moment of spawn.
        captured_kwargs["_depth_at_spawn"] = os.environ.get(DEPTH_ENV)
        return await real_create_subprocess_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spy_create_subprocess_exec)

    chunks = []
    async for obj in ndjson_from_cli(["true"]):
        chunks.append(obj)

    assert captured_kwargs["env"] is None
    assert captured_kwargs["_depth_at_spawn"] == "1"
