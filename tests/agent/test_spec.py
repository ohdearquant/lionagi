# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

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


class TestResolvePermissions:
    def test_none(self):
        assert _resolve_permissions(None) is None

    def test_policy_passthrough(self):
        p = PermissionPolicy.safe()
        assert _resolve_permissions(p) is p

    def test_dict(self):
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
    def test_preset_string(self, preset, expected_mode):
        result = _resolve_permissions(preset)
        assert isinstance(result, PermissionPolicy)
        assert result.mode == expected_mode

    def test_invalid_preset(self):
        with pytest.raises(ValueError, match="Unknown permissions preset"):
            _resolve_permissions("super_safe")

    def test_invalid_type(self):
        with pytest.raises(TypeError):
            _resolve_permissions(42)


# ---------------------------------------------------------------------------
# AgentSpec.compose
# ---------------------------------------------------------------------------


class TestAgentSpecCompose:
    def test_basic(self):
        spec = AgentSpec.compose("analyst")
        assert isinstance(spec.profile, Profile)
        assert spec.profile.role.name == "analyst"
        assert spec.permissions is None

    def test_with_modes(self):
        spec = AgentSpec.compose("critic", modes=["adversarial"])
        assert len(spec.profile.modes) == 1
        assert spec.profile.modes[0].name == "adversarial"

    def test_resolves_permission_preset(self):
        spec = AgentSpec.compose("analyst", permissions="safe")
        assert isinstance(spec.permissions, PermissionPolicy)

    def test_tools_tuple(self):
        spec = AgentSpec.compose("implementer", tools=["coding", "reader"])
        assert spec.tools == ("coding", "reader")

    def test_model_effort(self):
        spec = AgentSpec.compose("analyst", model="openai/gpt-4.1", effort="high")
        assert spec.model == "openai/gpt-4.1"
        assert spec.effort == "high"


# ---------------------------------------------------------------------------
# AgentSpec.coding preset
# ---------------------------------------------------------------------------


class TestAgentSpecCoding:
    def test_coding_preset(self):
        spec = AgentSpec.coding()
        assert spec.profile.role.name == "implementer"
        assert "coding" in spec.tools
        assert spec.effort == "high"

    def test_coding_custom_model(self):
        spec = AgentSpec.coding(model="anthropic/claude-sonnet-4-6")
        assert spec.model == "anthropic/claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# AgentSpec.build_system_message
# ---------------------------------------------------------------------------


class TestAgentSpecSystemMessage:
    def test_contains_role_body(self):
        spec = AgentSpec.compose("analyst")
        msg = spec.build_system_message()
        assert spec.profile.role.body in msg

    def test_contains_mode_behaviors(self):
        spec = AgentSpec.compose("critic", modes=["adversarial"])
        msg = spec.build_system_message()
        from lionagi.casts.pattern import Mode

        adv = Mode.load("adversarial")
        assert adv.behaviors in msg

    def test_contains_policy_block(self):
        spec = AgentSpec.compose("analyst")
        msg = spec.build_system_message()
        assert "## Authority" in msg
        assert "## Escalation Conditions" in msg

    def test_no_pack(self):
        spec = AgentSpec.compose("analyst")
        spec2 = AgentSpec(profile=spec.profile, pack=None)
        msg = spec2.build_system_message()
        assert spec2.profile.role.body in msg
        assert "escalation_request" not in msg

    def test_extra_prompt(self):
        spec = AgentSpec(
            profile=Profile.compose("analyst"),
            extra_prompt="Be concise.",
        )
        msg = spec.build_system_message()
        assert "Be concise." in msg


# ---------------------------------------------------------------------------
# AgentSpec.emission_operable
# ---------------------------------------------------------------------------


class TestAgentSpecEmission:
    def test_delegates_to_role(self):
        spec = AgentSpec.compose("critic", grant_emissions=True)
        result = spec.emission_operable()
        expected = spec.profile.role.emission_operable()
        assert result == expected

    def test_false_returns_none(self):
        spec = AgentSpec.compose("critic", grant_emissions=False)
        assert spec.emission_operable() is None


# ---------------------------------------------------------------------------
# AgentSpec.from_legacy (AgentConfig bridge)
# ---------------------------------------------------------------------------


class TestAgentSpecFromLegacy:
    def test_maps_role_and_model(self):
        config = AgentConfig(
            model="anthropic/claude-sonnet-4-6",
            role="analyst",
            effort="high",
        )
        spec = AgentSpec.from_legacy(config)
        assert spec.profile.role.name == "analyst"
        assert spec.model == "anthropic/claude-sonnet-4-6"
        assert spec.effort == "high"

    def test_preserves_system_prompt(self):
        config = AgentConfig(role="analyst", system_prompt="Custom.")
        spec = AgentSpec.from_legacy(config)
        assert spec.extra_prompt == "Custom."
        assert "Custom." in spec.build_system_message()

    def test_empty_system_prompt_gives_none(self):
        config = AgentConfig(role="analyst", system_prompt="")
        spec = AgentSpec.from_legacy(config)
        assert spec.extra_prompt is None

    def test_tools(self):
        config = AgentConfig(role="analyst", tools=["coding"])
        spec = AgentSpec.from_legacy(config)
        assert spec.tools == ("coding",)

    def test_permissions_dict(self):
        config = AgentConfig(role="analyst", permissions={"mode": "deny_all"})
        spec = AgentSpec.from_legacy(config)
        assert isinstance(spec.permissions, PermissionPolicy)
        assert spec.permissions.mode == "deny_all"

    def test_no_role_defaults_to_implementer(self):
        config = AgentConfig()
        spec = AgentSpec.from_legacy(config)
        assert spec.profile.role.name == "implementer"

    def test_lion_system_preserved(self):
        config = AgentConfig(role="analyst", lion_system=False)
        spec = AgentSpec.from_legacy(config)
        assert spec.lion_system is False

    def test_preserves_hook_handlers(self):
        config = AgentConfig(role="analyst")

        async def my_guard(tool_name, action, args):
            pass

        config.pre("bash", my_guard)
        spec = AgentSpec.from_legacy(config)
        assert "pre:bash" in spec.hook_handlers
        assert spec.hook_handlers["pre:bash"] == [my_guard]

    def test_preserves_cwd(self):
        config = AgentConfig(role="analyst", cwd="/tmp/workspace")
        spec = AgentSpec.from_legacy(config)
        assert spec.cwd == "/tmp/workspace"

    def test_preserves_yolo(self):
        config = AgentConfig(role="analyst", yolo=True)
        spec = AgentSpec.from_legacy(config)
        assert spec.yolo is True

    def test_hook_handlers_is_a_copy(self):
        config = AgentConfig(role="analyst")

        async def hook(tool_name, action, args):
            pass

        config.pre("bash", hook)
        spec = AgentSpec.from_legacy(config)
        spec.hook_handlers["pre:bash"].clear()
        assert len(config.hook_handlers["pre:bash"]) == 1


# ---------------------------------------------------------------------------
# Hook methods
# ---------------------------------------------------------------------------


class TestAgentSpecHooks:
    def test_pre(self):
        spec = AgentSpec.compose("analyst")

        async def h(t, a, args):
            pass

        spec.pre("bash", h)
        assert spec.hook_handlers["pre:bash"] == [h]

    def test_post(self):
        spec = AgentSpec.compose("analyst")

        async def h(t, a, args, result):
            pass

        spec.post("editor", h)
        assert spec.hook_handlers["post:editor"] == [h]

    def test_on_error(self):
        spec = AgentSpec.compose("analyst")

        async def h(t, a, args):
            pass

        spec.on_error("bash", h)
        assert spec.hook_handlers["error:bash"] == [h]

    def test_chaining(self):
        spec = AgentSpec.compose("analyst")

        async def h(t, a, args):
            pass

        result = spec.pre("bash", h)
        assert result is spec


# ---------------------------------------------------------------------------
# Factory: create_agent with AgentSpec
# ---------------------------------------------------------------------------


class TestCreateAgentWithSpec:
    async def test_returns_branch(self):
        spec = AgentSpec.compose("analyst")
        branch = await create_agent(spec, load_settings=False)
        assert isinstance(branch, Branch)

    async def test_system_message_contains_role(self):
        spec = AgentSpec.compose("analyst")
        branch = await create_agent(spec, load_settings=False)
        assert spec.profile.role.body in branch.msgs.system.rendered

    async def test_with_tools(self):
        spec = AgentSpec.compose("analyst", tools=["reader"])
        branch = await create_agent(spec, load_settings=False)
        assert "reader_tool" in branch.acts.registry

    async def test_with_permissions_wires_preprocessor(self):
        spec = AgentSpec.compose("analyst", tools=["reader"], permissions="deny_all")
        branch = await create_agent(spec, load_settings=False)
        reader_tool = branch.acts.registry.get("reader_tool")
        assert reader_tool is not None
        assert reader_tool.preprocessor is not None

    async def test_deny_all_preprocessor_raises(self):
        spec = AgentSpec.compose("analyst", tools=["reader"], permissions="deny_all")
        branch = await create_agent(spec, load_settings=False)
        reader_tool = branch.acts.registry["reader_tool"]
        with pytest.raises(PermissionError):
            await reader_tool.preprocessor({"action": "read", "path": "/tmp/x.py"})

    async def test_agentconfig_still_works(self):
        config = AgentConfig()
        branch = await create_agent(config, load_settings=False)
        assert isinstance(branch, Branch)

    async def test_emission_grant(self):
        spec = AgentSpec.compose("critic", grant_emissions=True)
        branch = await create_agent(spec, load_settings=False)
        assert isinstance(branch, Branch)

    async def test_no_emission_grant(self):
        spec = AgentSpec.compose("critic", grant_emissions=False)
        branch = await create_agent(spec, load_settings=False)
        assert isinstance(branch, Branch)

    async def test_load_settings_false_no_call(self, monkeypatch):
        import lionagi.agent.settings as settings_mod

        calls = []

        def spy(project_dir=None, *, include_project=True):
            calls.append(True)
            return {}

        monkeypatch.setattr(settings_mod, "load_settings", spy)
        spec = AgentSpec.compose("analyst")
        await create_agent(spec, load_settings=False)
        assert calls == []


# ---------------------------------------------------------------------------
# YAML round-trip
# ---------------------------------------------------------------------------


class TestAgentSpecYaml:
    def test_from_yaml(self, tmp_path):
        data = {
            "role": "analyst",
            "modes": ["adversarial"],
            "model": "openai/gpt-4.1",
            "effort": "high",
            "tools": ["reader"],
        }
        import yaml

        p = tmp_path / "spec.yaml"
        p.write_text(yaml.dump(data))
        spec = AgentSpec.from_yaml(p)
        assert spec.profile.role.name == "analyst"
        assert spec.model == "openai/gpt-4.1"
        assert spec.tools == ("reader",)

    def test_to_yaml_round_trip(self, tmp_path):
        spec = AgentSpec.compose("analyst", model="openai/gpt-4.1", tools=["reader"])
        p = tmp_path / "out.yaml"
        spec.to_yaml(p)
        loaded = AgentSpec.from_yaml(p)
        assert loaded.profile.role.name == "analyst"
        assert loaded.model == "openai/gpt-4.1"

    def test_lion_system_false_round_trips(self, tmp_path):
        """lion_system=False must survive a to_yaml/from_yaml round-trip.

        Regression for LIONAGI-AUDIT-005 (agent-standards 2026-06-06): the
        original from_yaml() never read lion_system, so a saved False was
        silently reloaded as True (the compose() default).
        """
        import yaml

        spec = AgentSpec.compose("analyst")
        spec.lion_system = False
        p = tmp_path / "no_lion.yaml"
        spec.to_yaml(p)

        loaded = AgentSpec.from_yaml(p)
        assert loaded.lion_system is False, (
            "lion_system=False was not preserved across the YAML round-trip"
        )

    def test_lion_system_true_preserved(self, tmp_path):
        """lion_system=True (the default) still round-trips correctly."""
        spec = AgentSpec.compose("analyst")
        assert spec.lion_system is True
        p = tmp_path / "lion.yaml"
        spec.to_yaml(p)

        loaded = AgentSpec.from_yaml(p)
        assert loaded.lion_system is True

    def test_from_yaml_without_lion_system_key_defaults_true(self, tmp_path):
        """YAML files without lion_system key keep the default (True)."""
        import yaml

        p = tmp_path / "minimal.yaml"
        p.write_text(yaml.dump({"role": "analyst"}))
        loaded = AgentSpec.from_yaml(p)
        assert loaded.lion_system is True
