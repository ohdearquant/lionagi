# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for opt-in khive context-provider registration on agent spawn."""

import pytest

from lionagi.agent.factory import create_agent
from lionagi.agent.spec import AgentSpec
from lionagi.tools.khive_injection import KhiveInjectionPolicy, WritebackPolicy


def _registered_provider(branch):
    registry = branch._context_providers
    assert registry is not None
    assert len(registry) == 1
    return registry._entries[0].provider


@pytest.mark.asyncio
async def test_coding_spec_registers_default_khive_injection_provider():
    branch = await create_agent(
        AgentSpec.coding(khive_injection=True),
        load_settings=False,
    )

    provider = _registered_provider(branch)
    assert provider.name.startswith("khive_injection:")
    assert provider.policy.profile_id == "implementer-recall-v1"
    assert provider.policy.writeback.enabled is True
    assert provider.policy.compose.enabled is False


@pytest.mark.asyncio
async def test_empty_mapping_opt_in_applies_fleet_defaults():
    # An empty mapping is a valid opt-in (a dict without writeback) and must
    # receive the fleet defaults rather than being silently disabled as falsey.
    branch = await create_agent(
        AgentSpec.coding(khive_injection={}),
        load_settings=False,
    )

    provider = _registered_provider(branch)
    assert provider.policy.profile_id == "implementer-recall-v1"
    assert provider.policy.writeback.enabled is True
    assert provider.policy.compose.enabled is False


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["0", "false", "no", "off", " OFF "])
async def test_khive_injection_env_kill_switch_disables_registration(monkeypatch, value):
    monkeypatch.setenv("LIONAGI_KHIVE_INJECTION", value)

    branch = await create_agent(
        AgentSpec.coding(khive_injection=True),
        load_settings=False,
    )

    assert branch._context_providers is None


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "", "1", "on"])
async def test_khive_injection_env_allows_normal_registration(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("LIONAGI_KHIVE_INJECTION", raising=False)
    else:
        monkeypatch.setenv("LIONAGI_KHIVE_INJECTION", value)

    branch = await create_agent(
        AgentSpec.coding(khive_injection=True),
        load_settings=False,
    )

    assert _registered_provider(branch).policy.writeback.enabled is True


@pytest.mark.asyncio
async def test_default_spec_does_not_create_context_provider_registry():
    branch = await create_agent(AgentSpec.coding(), load_settings=False)

    assert branch._context_providers is None


@pytest.mark.asyncio
async def test_policy_mapping_registers_configured_provider():
    branch = await create_agent(
        AgentSpec.coding(
            khive_injection={
                "profile_id": "researcher-recall-v1",
                "compose": {"enabled": True},
            }
        ),
        load_settings=False,
    )

    provider = _registered_provider(branch)
    assert provider.policy.profile_id == "researcher-recall-v1"
    assert provider.policy.compose.enabled is True
    assert provider.policy.writeback.enabled is True


@pytest.mark.asyncio
async def test_policy_mapping_respects_explicit_writeback_disable():
    branch = await create_agent(
        AgentSpec.coding(khive_injection={"writeback": {"enabled": False}}),
        load_settings=False,
    )

    provider = _registered_provider(branch)
    assert provider.policy.writeback.enabled is False
    assert provider.policy.compose.enabled is False


@pytest.mark.asyncio
async def test_policy_instance_is_registered_unchanged():
    policy = KhiveInjectionPolicy(
        profile_id="reviewer-recall-v1",
        writeback=WritebackPolicy(enabled=False),
    )

    branch = await create_agent(
        AgentSpec.coding(khive_injection=policy),
        load_settings=False,
    )

    registered_policy = _registered_provider(branch).policy
    assert registered_policy is policy
    assert registered_policy.writeback.enabled is False


@pytest.mark.asyncio
async def test_invalid_policy_configuration_fails_at_spawn():
    with pytest.raises(TypeError, match="khive_injection must be"):
        await create_agent(
            AgentSpec.coding(khive_injection="enabled"),
            load_settings=False,
        )


@pytest.mark.parametrize(
    "configured",
    [
        True,
        {
            "profile_id": "researcher-recall-v1",
            "compose": {"enabled": True},
        },
    ],
)
def test_agent_spec_yaml_round_trip_preserves_serializable_injection(tmp_path, configured):
    spec = AgentSpec.coding(khive_injection=configured)
    path = tmp_path / "agent.yaml"

    spec.to_yaml(path)
    loaded = AgentSpec.from_yaml(path)

    assert loaded.khive_injection == configured


def test_agent_spec_yaml_skips_policy_instance(tmp_path):
    spec = AgentSpec.coding(
        khive_injection=KhiveInjectionPolicy(profile_id="implementer-recall-v1")
    )
    path = tmp_path / "agent.yaml"

    spec.to_yaml(path)
    loaded = AgentSpec.from_yaml(path)

    assert loaded.khive_injection is None
