# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for AgentSpec.coding() context_management wiring: tool activation + system one-liner."""

from __future__ import annotations

from lionagi.agent.factory import create_agent
from lionagi.agent.spec import AgentSpec
from lionagi.tools.coding import DEFAULT_CODING_TOOLS


def test_context_in_default_coding_tools():
    assert "context" in DEFAULT_CODING_TOOLS


def test_coding_preset_defaults_context_management_true():
    spec = AgentSpec.coding()
    assert spec.context_management is True


def test_coding_preset_context_management_false():
    spec = AgentSpec.coding(context_management=False)
    assert spec.context_management is False


async def test_context_tool_registered_by_default():
    spec = AgentSpec.coding()
    branch = await create_agent(spec, load_settings=False)
    assert "context" in branch.acts.registry


async def test_context_management_false_removes_context_tool():
    spec = AgentSpec.coding(context_management=False)
    branch = await create_agent(spec, load_settings=False)
    assert "context" not in branch.acts.registry
    # the rest of the default coding toolset is untouched
    assert "reader" in branch.acts.registry
    assert "editor" in branch.acts.registry
    assert "bash" in branch.acts.registry


async def test_system_message_contains_one_liner_when_enabled():
    spec = AgentSpec.coding()
    branch = await create_agent(spec, load_settings=False)
    assert "context tool" in branch.msgs.system.rendered.lower()


async def test_system_message_omits_one_liner_when_disabled():
    spec = AgentSpec.coding(context_management=False)
    branch = await create_agent(spec, load_settings=False)
    assert "curate your own context" not in branch.msgs.system.rendered.lower()


async def test_one_liner_absent_for_non_coding_specs():
    """context_management only matters for the 'coding' toolset — plain specs must not get it."""
    spec = AgentSpec.compose("analyst", tools=["reader"])
    branch = await create_agent(spec, load_settings=False)
    assert "curate your own context" not in branch.msgs.system.rendered.lower()
