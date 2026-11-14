# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The khive server a profile's injection provider spawns acts as the child's own
actor -- the same ``agent:<profile>`` its forwarded khive servers get -- not as
whoever launched the run. The MCP pool is mocked, so no daemon is needed."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from lionagi.session.branch import Branch
from lionhub.agent.factory import register_profile_injection
from lionhub.agent.khive_actor import KHIVE_ACTOR_ENV
from lionhub.tools.khive_injection import KhiveInjectionProvider


def _registered_provider(branch: Branch) -> KhiveInjectionProvider:
    providers = [
        entry.provider
        for entry in branch.providers._entries
        if isinstance(entry.provider, KhiveInjectionProvider)
    ]
    assert len(providers) == 1
    return providers[0]


async def _config_sent_to_pool(provider: KhiveInjectionProvider) -> dict:
    """The server config the provider hands the MCP pool on its first turn."""
    client = AsyncMock()
    client.call_tool = AsyncMock(return_value=SimpleNamespace(content=[SimpleNamespace(text="{}")]))
    get_client = AsyncMock(return_value=client)
    branch = SimpleNamespace(name="child", msgs=SimpleNamespace(last_response=None))
    with patch(
        "lionagi.service.connections.mcp_wrapper.MCPConnectionPool.get_client",
        get_client,
    ):
        await provider.provide(branch, SimpleNamespace(rendered="task"))
    assert get_client.await_count >= 1
    return get_client.await_args_list[0].args[0]


@pytest.fixture(autouse=True)
def _injection_enabled(monkeypatch):
    monkeypatch.delenv("LIONAGI_KHIVE_INJECTION", raising=False)
    # The assertions are on the server config, not on truncation, so the
    # tokenizer (which may need to download its vocabulary) is bypassed.
    monkeypatch.setattr("lionhub.tools.khive_injection._truncate", lambda text, max_tokens: text)


@pytest.mark.asyncio
async def test_profile_provider_spawns_khive_as_the_child_actor():
    branch = Branch()
    profile = SimpleNamespace(name="implementer", khive_injection=True)

    register_profile_injection(branch, "implementer", profile)

    config = await _config_sent_to_pool(_registered_provider(branch))
    assert config["command"] == "kkernel"
    assert config["env"][KHIVE_ACTOR_ENV] == "agent:implementer"


@pytest.mark.asyncio
async def test_actor_follows_the_profile_name_not_the_role_name():
    branch = Branch()
    profile = SimpleNamespace(name="planner", khive_injection=True)

    register_profile_injection(branch, "orchestrator", profile)

    config = await _config_sent_to_pool(_registered_provider(branch))
    assert config["env"][KHIVE_ACTOR_ENV] == "agent:planner"


@pytest.mark.asyncio
async def test_profile_name_that_is_not_a_valid_actor_is_anonymous():
    branch = Branch()
    profile = SimpleNamespace(name="two words", khive_injection=True)

    register_profile_injection(branch, "implementer", profile)

    config = await _config_sent_to_pool(_registered_provider(branch))
    assert config["env"][KHIVE_ACTOR_ENV] == "agent:anonymous"
