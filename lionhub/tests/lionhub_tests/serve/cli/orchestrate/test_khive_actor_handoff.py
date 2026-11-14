# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A khive server an orchestrated child is handed acts as the profile that child runs as.

The run resolves one server set. Each child gets its own copy, so the set the run
keeps stays as it was resolved while every child carries its own actor.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from lionagi import Branch
from lionhub.serve.cli.orchestrate import fanout as fanout_module
from lionhub.serve.cli.orchestrate._orchestration import build_worker_branch

from .test_no_mcp_config_handoff import (  # noqa: F401
    AgentProfile,
    _setup,
    orch_mod,
    tmp_run,
)

_KHIVE = {
    "command": "/opt/khive/bin/kkernel",
    "args": ["mcp"],
    "env": {
        "KHIVE_ACTOR": "someone-else",
        "KHIVE_PACKS": "kg, comm ,memory",
        "KEEP": "1",
    },
}
_OTHER = {
    "command": "/opt/other/bin/server",
    "args": ["serve"],
    "env": {"KHIVE_ACTOR": "someone-else", "KHIVE_PACKS": "kg,comm"},
}


def _write_config(launch_dir) -> None:
    (launch_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"khive": _KHIVE, "other": _OTHER}})
    )


def _handed(chat_model) -> dict:
    return chat_model.endpoint.config.kwargs["mcp_servers"]


def _assert_acts_as(servers: dict, actor: str) -> None:
    assert servers["khive"]["env"] == {
        "KHIVE_ACTOR": actor,
        "KHIVE_PACKS": "kg, comm ,memory",
        "KEEP": "1",
    }
    # A server that is not the knowledge kernel is handed over as it was resolved.
    assert servers["other"]["env"] == _OTHER["env"]


@pytest.mark.asyncio
async def test_orchestrator_without_a_profile_acts_as_the_anonymous_child(tmp_run):  # noqa: F811
    _write_config(tmp_run)
    env = await _setup(no_mcp_config=False)

    _assert_acts_as(_handed(env.orc_branch.chat_model), "agent:anonymous")
    assert env.khive_actors == {str(env.orc_branch.id): "agent:anonymous"}


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["implementer", "reviewer"])
async def test_worker_acts_as_its_own_profile_and_the_run_set_is_left_alone(
    tmp_run,  # noqa: F811
    monkeypatch,
    role,
):
    _write_config(tmp_run)
    env = await _setup(no_mcp_config=False)
    profile = AgentProfile(
        name=role,
        model="claude_code/sonnet",
        system_prompt="Do the work.",
        raw_body="Do the work.",
    )
    monkeypatch.setattr(orch_mod, "load_agent_profile", lambda name: profile)

    branch, _, _, _ = await build_worker_branch(env, agent_id="w1", role=role)

    _assert_acts_as(_handed(branch.chat_model), f"agent:{role}")
    # A node later cloned from this worker acts as the same profile.
    assert env.khive_actors[str(branch.id)] == f"agent:{role}"
    # Workers differ, so the set the run keeps is not attributed to any one of them.
    assert env.mcp_servers["khive"]["env"]["KHIVE_ACTOR"] == "someone-else"
    assert env.mcp_servers["khive"]["env"]["KHIVE_PACKS"] == "kg, comm ,memory"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("orc_profile_name", "actor"),
    [(None, "agent:anonymous"), ("reviewer", "agent:reviewer")],
)
async def test_synthesis_branch_acts_as_the_orchestrator_profile(
    tmp_run,  # noqa: F811
    monkeypatch,
    orc_profile_name,
    actor,
):
    import lionhub.agent as agent_pkg

    _write_config(tmp_run)
    env = await _setup(no_mcp_config=False)
    # Works whether or not the env class is frozen.
    object.__setattr__(env, "orc_profile_name", orc_profile_name)
    captured: dict = {}

    async def fake_create_agent(spec, **kwargs):
        captured.update(kwargs)
        return Branch(name="synthesis-stub")

    monkeypatch.setattr(
        agent_pkg,
        "AgentSpec",
        SimpleNamespace(compose=lambda *args, **kwargs: object()),
    )
    monkeypatch.setattr(agent_pkg, "create_agent", fake_create_agent)

    await fanout_module._fresh_synthesis_branch(env)

    _assert_acts_as(captured["resolved_mcp_servers"], actor)
    _assert_acts_as(_handed(captured["chat_model"]), actor)
