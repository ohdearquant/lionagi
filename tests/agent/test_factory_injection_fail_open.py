# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""khive-injection provider CONSTRUCTION failures must fail open (warn +
continue without the provider), matching KhiveInjectionProvider.provide()'s
own transport-failure fail-open — a bad policy must not kill agent creation
or an engine run built on it."""

from __future__ import annotations

import pytest

from lionagi.agent.factory import _register_providers, create_agent
from lionagi.agent.spec import AgentSpec
from lionagi.session.branch import Branch
from lionagi.tools.khive_injection import KhiveInjectionProvider


@pytest.mark.asyncio
async def test_unsupported_snapshot_id_does_not_raise_and_registers_nothing():
    branch = Branch()
    spec = AgentSpec.compose(
        "researcher",
        khive_injection={"snapshot_id": "unsupported"},
    )

    _register_providers(branch, spec)  # must not raise

    khive_entries = [
        e for e in branch.providers._entries if isinstance(e.provider, KhiveInjectionProvider)
    ]
    assert khive_entries == []


@pytest.mark.asyncio
async def test_create_agent_with_bad_injection_config_still_returns_a_usable_branch():
    spec = AgentSpec.compose(
        "researcher",
        khive_injection={"snapshot_id": "unsupported"},
    )

    branch = await create_agent(spec, load_settings=False)  # must not raise

    khive_entries = [
        e for e in branch.providers._entries if isinstance(e.provider, KhiveInjectionProvider)
    ]
    assert khive_entries == []


@pytest.mark.asyncio
async def test_valid_injection_config_still_registers_normally():
    """Fail-open must not swallow a perfectly valid config."""
    branch = Branch()
    spec = AgentSpec.compose("researcher", khive_injection=True)

    _register_providers(branch, spec)

    khive_entries = [
        e for e in branch.providers._entries if isinstance(e.provider, KhiveInjectionProvider)
    ]
    assert len(khive_entries) == 1
