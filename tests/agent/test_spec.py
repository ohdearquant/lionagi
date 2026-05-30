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


# ---------------------------------------------------------------------------
# MAJ-2: AgentSpec new fields (hook_handlers, cwd, yolo) + from_config round-trip
# ---------------------------------------------------------------------------


def test_agentspec_default_new_fields():
    """New fields default to safe values: empty dict, None, False."""
    spec = AgentSpec.compose("analyst")
    assert spec.hook_handlers == {}
    assert spec.cwd is None
    assert spec.yolo is False


def test_from_config_preserves_hook_handlers():
    """from_config must copy hook_handlers so guards survive the round-trip."""
    config = AgentConfig(role="analyst")
    calls = []

    async def my_guard(tool_name, action, args):
        calls.append(tool_name)

    config.pre("bash", my_guard)
    spec = AgentSpec.from_config(config)
    assert "pre:bash" in spec.hook_handlers
    assert spec.hook_handlers["pre:bash"] == [my_guard]


def test_from_config_preserves_cwd():
    """from_config must copy cwd so workspace root survives the round-trip."""
    config = AgentConfig(role="analyst", cwd="/tmp/workspace")
    spec = AgentSpec.from_config(config)
    assert spec.cwd == "/tmp/workspace"


def test_from_config_preserves_yolo():
    """from_config must copy yolo flag."""
    config = AgentConfig(role="analyst", yolo=True)
    spec = AgentSpec.from_config(config)
    assert spec.yolo is True


def test_from_config_hook_handlers_is_a_copy():
    """from_config must copy hook_handlers (shallow), not share the reference."""
    config = AgentConfig(role="analyst")

    async def hook(tool_name, action, args):
        pass

    config.pre("bash", hook)
    spec = AgentSpec.from_config(config)
    # Mutating the copy must not affect the original and vice-versa
    spec.hook_handlers["pre:bash"].clear()
    assert len(config.hook_handlers["pre:bash"]) == 1


# ---------------------------------------------------------------------------
# MAJ-2: _create_agent_from_spec threads cwd into bridge → CodingToolkit
# ---------------------------------------------------------------------------


async def test_create_agent_agentspec_cwd_threads_to_bridge(tmp_path, monkeypatch):
    """cwd on AgentSpec must reach CodingToolkit's workspace_root."""
    from pathlib import Path

    import lionagi.tools.coding as coding_mod

    captured_roots: list[Path] = []
    real_init = coding_mod.CodingToolkit.__init__

    def spy_init(self, workspace_root=None, **kw):
        captured_roots.append(workspace_root)
        real_init(self, workspace_root=workspace_root, **kw)

    monkeypatch.setattr(coding_mod.CodingToolkit, "__init__", spy_init)

    spec = AgentSpec.compose("implementer", tools=["coding"])
    import dataclasses

    spec = dataclasses.replace(spec, cwd=str(tmp_path))
    branch = await create_agent(spec, load_settings=False)
    assert isinstance(branch, Branch)
    assert captured_roots and captured_roots[0] == tmp_path


# ---------------------------------------------------------------------------
# MAJ-1: spec path honours load_settings / trust_project_settings
# ---------------------------------------------------------------------------


async def test_create_agent_agentspec_load_settings_false_no_call(monkeypatch):
    """load_settings=False on AgentSpec path must not call apply_hooks_from_settings."""
    import lionagi.agent.settings as settings_mod

    calls = []
    real = settings_mod.load_settings

    def spy(project_dir=None, *, include_project=True):
        calls.append(include_project)
        return real(project_dir, include_project=include_project)

    monkeypatch.setattr(settings_mod, "load_settings", spy)

    spec = AgentSpec.compose("analyst")
    await create_agent(spec, load_settings=False)
    assert calls == []


async def test_create_agent_agentspec_load_settings_true_passes_trust(monkeypatch):
    """load_settings=True on AgentSpec path must forward trust_project_settings."""
    import lionagi.agent.settings as settings_mod

    calls = []
    real = settings_mod.load_settings

    def spy(project_dir=None, *, include_project=True):
        calls.append(include_project)
        return real(project_dir, include_project=include_project)

    monkeypatch.setattr(settings_mod, "load_settings", spy)

    spec = AgentSpec.compose("analyst")
    await create_agent(spec, load_settings=True, trust_project_settings=False)
    assert calls == [False]


async def test_create_agent_agentspec_mcp_loaded_when_trusted(tmp_path, monkeypatch):
    """MAJ-1: _load_mcp is now reached on the spec path when trusted."""
    from lionagi.protocols.action.manager import ActionManager

    project = tmp_path / "project"
    project.mkdir()
    mcp_file = project / ".mcp.json"
    mcp_file.write_text('{"mcpServers": {"demo": {"command": "true"}}}')
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    calls = []

    async def fake_load_mcp(self, config_path, server_names=None, update=False):
        calls.append(config_path)
        return {}

    monkeypatch.setattr(ActionManager, "load_mcp_config", fake_load_mcp)

    import dataclasses

    spec = AgentSpec.compose("analyst")
    spec = dataclasses.replace(spec, cwd=str(project))
    await create_agent(spec, load_settings=False, trust_project_settings=True)

    assert calls == [str(mcp_file)]


# ---------------------------------------------------------------------------
# MIN-2: yolo kwarg flows through spec path
# ---------------------------------------------------------------------------


async def test_create_agent_agentspec_yolo_kwargs_applied(monkeypatch):
    """MIN-2: yolo=True on AgentSpec must pass PROVIDER_YOLO_KWARGS to iModel."""
    import lionagi.cli._providers as providers_mod
    import lionagi.service.imodel as imodel_mod

    monkeypatch.setitem(providers_mod.PROVIDER_EFFORT_KWARG, "openai", "reasoning_effort")
    monkeypatch.setitem(providers_mod.PROVIDER_YOLO_KWARGS, "openai", {"stream": True})

    captured: dict = {}
    real_init = imodel_mod.iModel.__init__

    def spy_init(self, *args, **kwargs):
        captured.update(kwargs)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(imodel_mod.iModel, "__init__", spy_init)

    spec = AgentSpec.compose("analyst", model="openai/gpt-4.1-mini")
    import dataclasses

    spec = dataclasses.replace(spec, yolo=True)
    branch = await create_agent(spec, load_settings=False)
    assert isinstance(branch, Branch)
    assert captured.get("stream") is True
