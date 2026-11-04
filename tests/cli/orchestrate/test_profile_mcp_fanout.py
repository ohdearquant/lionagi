# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A planned fanout worker takes its profile's MCP restriction."""

from __future__ import annotations

import json

import pytest

import lionagi.cli.orchestrate._orchestration as orch_mod
from lionagi.casts.emission import TaskAssignment
from lionagi.cli._providers import AgentProfile
from lionagi.cli.orchestrate import fanout as fanout_mod

from .test_no_mcp_config_handoff import _codex_cli_args, _setup
from .test_no_mcp_config_handoff import tmp_run as tmp_run


class _WorkerCaptured(Exception):
    pass


def _plan_one_worker(monkeypatch, planned):
    async def plan(*args, **kwargs):
        planned.append(True)
        return [TaskAssignment(assignee="isolated", task="do work")]

    monkeypatch.setattr(fanout_mod, "plan", plan)
    monkeypatch.setattr(fanout_mod, "available_roles", lambda: ["isolated"])
    monkeypatch.setattr(fanout_mod, "role_roster", lambda *_: "")
    monkeypatch.setattr(fanout_mod, "mode_roster", lambda *_: "")
    profile = AgentProfile(name="isolated", model="codex/gpt-5", mcp="none")
    monkeypatch.setattr(orch_mod, "load_agent_profile", lambda _name: profile)


async def test_planned_fanout_worker_uses_empty_mcp_set(tmp_run, monkeypatch, codex_home):
    (tmp_run / ".mcp.json").write_text(json.dumps({"mcpServers": {"example": {"command": "x"}}}))
    codex_home.write_config({"hidden": {"command": "x"}})
    env = await _setup(no_mcp_config=False)
    planned = []
    _plan_one_worker(monkeypatch, planned)
    branches = []

    def capture(*args, branch, **kwargs):
        branches.append(branch)
        raise _WorkerCaptured

    monkeypatch.setattr(fanout_mod, "_build_worker_operate_node", capture)
    with pytest.raises(_WorkerCaptured):
        await fanout_mod._run_fanout_inner("claude_code/sonnet", "work", env=env)

    assert planned == [True]
    assert len(branches) == 1
    assert branches[0].metadata["mcp_launch"]["no_mcp_config_source"] == "profile"
    args = await _codex_cli_args(branches[0].chat_model)
    assert "--ignore-user-config" in args
    assert not any("example" in arg or "hidden" in arg for arg in args)


async def test_planned_conflicting_worker_refused_before_its_model_is_built(tmp_run, monkeypatch):
    named = tmp_run / "named.json"
    named.write_text(json.dumps({"mcpServers": {"example": {"command": "x"}}}))
    env = await _setup(no_mcp_config=False, mcp_config=str(named))
    planned = []
    built = []
    _plan_one_worker(monkeypatch, planned)
    monkeypatch.setattr(orch_mod, "build_imodel_from_spec", lambda *a, **kw: built.append(True))

    with pytest.raises(orch_mod.WorkerBuildError, match="mcp: none.*--mcp-config"):
        await fanout_mod._run_fanout_inner("claude_code/sonnet", "work", env=env)
    assert planned == [True]
    assert built == []
