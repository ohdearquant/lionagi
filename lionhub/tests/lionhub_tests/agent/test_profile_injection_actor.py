# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The khive server a profile's injection provider spawns acts as the child's own
actor -- the same ``agent:<profile>`` its forwarded khive servers get -- not as
whoever launched the run. The MCP pool is mocked, so no daemon is needed."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from lionagi import iModel
from lionagi.session.branch import Branch
from lionhub.agent.factory import register_profile_injection
from lionhub.agent.khive_actor import KHIVE_ACTOR_ENV, attribute_khive_servers
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


@pytest.mark.asyncio
async def test_create_agent_runs_the_injection_client_as_the_named_actor():
    from lionhub.agent.factory import create_agent
    from lionhub.agent.spec import AgentSpec

    branch = await create_agent(
        AgentSpec.coding(khive_injection=True),
        load_settings=False,
        khive_actor="agent:reviewer",
    )

    config = await _config_sent_to_pool(_registered_provider(branch))
    assert config["env"][KHIVE_ACTOR_ENV] == "agent:reviewer"


@pytest.mark.asyncio
async def test_create_agent_without_an_actor_leaves_the_injection_server_as_configured():
    from lionhub.agent.factory import create_agent
    from lionhub.agent.spec import AgentSpec

    branch = await create_agent(AgentSpec.coding(khive_injection=True), load_settings=False)

    config = await _config_sent_to_pool(_registered_provider(branch))
    assert KHIVE_ACTOR_ENV not in (config.get("env") or {})


def _actor_config(directory, actor):
    (directory / ".khive").mkdir(parents=True)
    (directory / ".khive" / "config.toml").write_text(f'[actor]\nid = "{actor}"\n')
    return directory


@pytest.mark.asyncio
async def test_the_injection_server_starts_where_a_cli_childs_khive_servers_start(
    tmp_path, monkeypatch
):
    """The launching directory and the child's directory each name a different actor. The
    injection server and the child's forwarded khive server get the same directory and the
    same arguments, so khive resolves both from the same configuration."""
    launch = _actor_config(tmp_path / "launch", "team:a")
    work = _actor_config(tmp_path / "work", "team:b")
    monkeypatch.chdir(launch)
    model = iModel(provider="claude_code", model="sonnet", repo=str(work))
    branch = Branch(chat_model=model)
    profile = SimpleNamespace(name="implementer", khive_injection=True)

    register_profile_injection(branch, "implementer", profile)

    injection = await _config_sent_to_pool(_registered_provider(branch))
    forwarded = attribute_khive_servers(
        {"khive": {"command": "kkernel", "args": ["mcp"]}}, "agent:implementer", cwd=work
    )["khive"]
    assert injection["cwd"] == str(work)
    assert injection["args"] == forwarded["args"]
    assert injection["env"][KHIVE_ACTOR_ENV] == forwarded["env"][KHIVE_ACTOR_ENV]


@pytest.mark.asyncio
async def test_a_directory_the_caller_names_wins_over_the_models(tmp_path):
    model = iModel(provider="claude_code", model="sonnet", repo=str(tmp_path / "model"))
    branch = Branch(chat_model=model)
    profile = SimpleNamespace(name="implementer", khive_injection=True)

    register_profile_injection(branch, "implementer", profile, cwd=tmp_path / "agent")

    config = await _config_sent_to_pool(_registered_provider(branch))
    assert config["cwd"] == str(tmp_path / "agent")


@pytest.mark.asyncio
async def test_an_api_models_injection_server_starts_in_the_process_directory():
    branch = Branch()
    profile = SimpleNamespace(name="implementer", khive_injection=True)

    register_profile_injection(branch, "implementer", profile)

    config = await _config_sent_to_pool(_registered_provider(branch))
    assert "cwd" not in config


@pytest.mark.asyncio
async def test_create_agent_starts_the_injection_server_in_the_cli_models_directory(tmp_path):
    from lionhub.agent.factory import create_agent
    from lionhub.agent.spec import AgentSpec

    spec = AgentSpec.coding(khive_injection=True, model="claude_code/sonnet")
    spec.cwd = str(tmp_path)
    branch = await create_agent(spec, load_settings=False, khive_actor="agent:reviewer")

    config = await _config_sent_to_pool(_registered_provider(branch))
    assert config["cwd"] == str(tmp_path)
