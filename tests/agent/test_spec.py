# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for AgentSpec (lionagi/agent/spec.py) and factory AgentSpec path."""

import pytest

from lionagi.agent.config import AgentConfig
from lionagi.agent.factory import create_agent
from lionagi.agent.permissions import PermissionPolicy
from lionagi.agent.spec import AgentSpec, _resolve_permissions
from lionagi.casts.profile import Profile
from lionagi.session.branch import Branch

# ---------------------------------------------------------------------------
# _resolve_permissions
# ---------------------------------------------------------------------------


def test_resolve_permissions_none():
    assert _resolve_permissions(None) is None


def test_resolve_permissions_policy_passthrough():
    p = PermissionPolicy.safe()
    assert _resolve_permissions(p) is p


def test_resolve_permissions_dict():
    result = _resolve_permissions({"mode": "deny_all"})
    assert isinstance(result, PermissionPolicy)
    assert result.mode == "deny_all"


@pytest.mark.parametrize(
    "preset,expected_mode",
    [
        ("safe", "rules"),
        ("read_only", "rules"),
        ("allow_all", "allow_all"),
        ("deny_all", "deny_all"),
    ],
)
def test_resolve_permissions_preset_string(preset, expected_mode):
    result = _resolve_permissions(preset)
    assert isinstance(result, PermissionPolicy)
    assert result.mode == expected_mode


def test_resolve_permissions_invalid_preset():
    with pytest.raises(ValueError, match="Unknown permissions preset"):
        _resolve_permissions("super_safe")


def test_resolve_permissions_invalid_type():
    with pytest.raises(TypeError):
        _resolve_permissions(42)


# ---------------------------------------------------------------------------
# AgentSpec.compose
# ---------------------------------------------------------------------------


def test_agentspec_compose_basic():
    spec = AgentSpec.compose("analyst")
    assert isinstance(spec.profile, Profile)
    assert spec.profile.role.name == "analyst"
    assert spec.permissions is None


def test_agentspec_compose_with_modes():
    spec = AgentSpec.compose("critic", modes=["adversarial"])
    assert len(spec.profile.modes) == 1
    assert spec.profile.modes[0].name == "adversarial"


def test_agentspec_compose_resolves_permission_preset():
    spec = AgentSpec.compose("analyst", permissions="safe")
    assert isinstance(spec.permissions, PermissionPolicy)
    assert spec.permissions.mode == "rules"


def test_agentspec_compose_tools_tuple():
    spec = AgentSpec.compose("implementer", tools=["coding", "reader"])
    assert spec.tools == ("coding", "reader")


def test_agentspec_compose_model_effort():
    spec = AgentSpec.compose("analyst", model="openai/gpt-4.1", effort="high")
    assert spec.model == "openai/gpt-4.1"
    assert spec.effort == "high"


# ---------------------------------------------------------------------------
# AgentSpec.build_system_message
# ---------------------------------------------------------------------------


def test_agentspec_build_system_message_contains_role_body():
    spec = AgentSpec.compose("analyst")
    msg = spec.build_system_message()
    assert spec.profile.role.body in msg


def test_agentspec_build_system_message_contains_mode_behaviors():
    spec = AgentSpec.compose("critic", modes=["adversarial"])
    msg = spec.build_system_message()
    from lionagi.casts.pattern import Mode

    adv = Mode.load("adversarial")
    assert adv.behaviors in msg


def test_agentspec_build_system_message_contains_policy_block():
    spec = AgentSpec.compose("analyst")
    msg = spec.build_system_message()
    # analyst has authority + boundaries + escalations in default pack
    assert "## Authority" in msg
    assert "## Escalation Conditions" in msg


def test_agentspec_build_system_message_policy_escalation_exact_wording():
    """Escalation block must use the exact STOP + escalation_request wording from ADR."""
    spec = AgentSpec.compose("analyst")
    msg = spec.build_system_message()
    assert "STOP and emit an `escalation_request` capability" in msg


def test_agentspec_build_system_message_no_pack():
    spec = AgentSpec.compose("analyst")
    spec2 = AgentSpec(profile=spec.profile, pack=None)
    msg = spec2.build_system_message()
    # No pack means no policy block; role body still present
    assert spec2.profile.role.body in msg
    assert "escalation_request" not in msg


def test_agentspec_build_system_message_extra_prompt():
    spec = AgentSpec(
        profile=Profile.compose("analyst"),
        extra_prompt="Be concise.",
    )
    msg = spec.build_system_message()
    assert "Be concise." in msg


# ---------------------------------------------------------------------------
# AgentSpec.capability_operable
# ---------------------------------------------------------------------------


def test_agentspec_capability_operable_delegates():
    from lionagi.casts.capabilities import capability_operable

    spec = AgentSpec.compose("critic", grant_capabilities=True)
    result = spec.capability_operable()
    expected = capability_operable("critic")
    assert result == expected


def test_agentspec_capability_operable_false_returns_none():
    spec = AgentSpec.compose("critic", grant_capabilities=False)
    assert spec.capability_operable() is None


# ---------------------------------------------------------------------------
# AgentSpec.from_config
# ---------------------------------------------------------------------------


def test_from_config_maps_role_and_model():
    config = AgentConfig(
        name="my-agent",
        model="anthropic/claude-sonnet-4-6",
        role="analyst",
        effort="high",
    )
    spec = AgentSpec.from_config(config)
    assert spec.profile.role.name == "analyst"
    assert spec.model == "anthropic/claude-sonnet-4-6"
    assert spec.effort == "high"


def test_from_config_preserves_system_prompt():
    config = AgentConfig(role="analyst", system_prompt="Custom instructions.")
    spec = AgentSpec.from_config(config)
    assert spec.extra_prompt == "Custom instructions."
    msg = spec.build_system_message()
    assert "Custom instructions." in msg


def test_from_config_empty_system_prompt_gives_none():
    config = AgentConfig(role="analyst", system_prompt="")
    spec = AgentSpec.from_config(config)
    assert spec.extra_prompt is None


def test_from_config_tools():
    config = AgentConfig(role="analyst", tools=["coding"])
    spec = AgentSpec.from_config(config)
    assert spec.tools == ("coding",)


def test_from_config_permissions_dict():
    config = AgentConfig(role="analyst", permissions={"mode": "deny_all"})
    spec = AgentSpec.from_config(config)
    assert isinstance(spec.permissions, PermissionPolicy)
    assert spec.permissions.mode == "deny_all"


def test_from_config_modes():
    config = AgentConfig(role="analyst", modes=["adversarial"])
    spec = AgentSpec.from_config(config)
    assert len(spec.profile.modes) == 1
    assert spec.profile.modes[0].name == "adversarial"


def test_from_config_no_role_defaults_to_implementer():
    config = AgentConfig()
    spec = AgentSpec.from_config(config)
    assert spec.profile.role.name == "implementer"


def test_from_config_lion_system_preserved():
    config = AgentConfig(role="analyst", lion_system=False)
    spec = AgentSpec.from_config(config)
    assert spec.lion_system is False


# ---------------------------------------------------------------------------
# factory: create_agent with AgentSpec
# ---------------------------------------------------------------------------


async def test_create_agent_agentspec_returns_branch():
    spec = AgentSpec.compose("analyst")
    branch = await create_agent(spec, load_settings=False)
    assert isinstance(branch, Branch)


async def test_create_agent_agentspec_sets_system_message():
    spec = AgentSpec.compose("analyst")
    branch = await create_agent(spec, load_settings=False)
    system_text = branch.msgs.system.rendered
    assert spec.profile.role.body in system_text


async def test_create_agent_agentspec_no_model():
    spec = AgentSpec.compose("analyst")
    branch = await create_agent(spec, load_settings=False)
    assert isinstance(branch, Branch)


async def test_create_agent_agentspec_capability_grant_smoke():
    spec = AgentSpec.compose("critic", grant_capabilities=True)
    op = spec.capability_operable()
    if op is not None:
        branch = await create_agent(spec, load_settings=False)
        assert isinstance(branch, Branch)
    else:
        pytest.skip("No Operable for critic")


async def test_create_agent_agentspec_grant_capabilities_false():
    spec = AgentSpec.compose("critic", grant_capabilities=False)
    branch = await create_agent(spec, load_settings=False)
    assert isinstance(branch, Branch)


async def test_create_agent_agentconfig_still_works():
    config = AgentConfig()
    branch = await create_agent(config, load_settings=False)
    assert isinstance(branch, Branch)
