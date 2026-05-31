# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest
import yaml

from lionagi.casts.pattern import Mode, Role, list_modes, list_roles
from lionagi.casts.profile import Profile


class TestProfileCompose:
    def test_compose_by_name(self):
        p = Profile.compose("analyst")
        assert p.name == "analyst"
        assert isinstance(p.role, Role)
        assert p.modes == ()

    def test_compose_by_role_object(self):
        role = Role.load("critic")
        p = Profile.compose(role)
        assert p.role is role
        assert p.name == "critic"

    def test_compose_modes_by_name(self):
        p = Profile.compose("researcher", modes=["evidential", "systematic"])
        assert len(p.modes) == 2
        assert p.modes[0].name == "evidential"
        assert p.modes[1].name == "systematic"

    def test_compose_modes_by_object(self):
        m = Mode.load("adversarial")
        p = Profile.compose("critic", modes=[m])
        assert p.modes[0] is m

    def test_compose_custom_name(self):
        p = Profile.compose("analyst", name="my-analyst")
        assert p.name == "my-analyst"


class TestProfileConflicts:
    def test_fast_slow_conflict(self):
        with pytest.raises(ValueError, match="conflict"):
            Profile.compose("researcher", modes=["fast", "slow"])

    def test_fast_systematic_conflict(self):
        with pytest.raises(ValueError, match="conflict"):
            Profile.compose("researcher", modes=["fast", "systematic"])

    def test_no_conflict_evidential_adversarial(self):
        p = Profile.compose("critic", modes=["evidential", "adversarial"])
        assert len(p.modes) == 2


class TestProfileBuildSystemMessage:
    def test_includes_role_body(self):
        p = Profile.compose("analyst")
        msg = p.build_system_message()
        assert p.role.body in msg

    def test_includes_mode_behaviors(self):
        p = Profile.compose("analyst", modes=["adversarial"])
        msg = p.build_system_message()
        adv = Mode.load("adversarial")
        assert adv.behaviors in msg

    def test_no_modes_returns_role_body(self):
        p = Profile.compose("analyst")
        msg = p.build_system_message()
        assert msg == p.role.body


class TestProfileEmission:
    def test_emission_operable_delegates_to_role(self):
        p = Profile.compose("analyst")
        op = p.emission_operable()
        assert op == p.role.emission_operable()

    def test_emission_operable_for_role_with_emits(self):
        p = Profile.compose("critic")
        op = p.emission_operable()
        assert op is not None
        assert "escalation_request" in op.allowed()


class TestProfileFrozen:
    def test_is_frozen(self):
        p = Profile.compose("analyst")
        with pytest.raises(AttributeError):
            p.name = "other"


class TestProfileFromYaml:
    def test_round_trip(self, tmp_path):
        data = {"name": "test-profile", "role": "analyst", "modes": ["evidential"]}
        yaml_file = tmp_path / "profile.yaml"
        yaml_file.write_text(yaml.dump(data))
        p = Profile.from_yaml(yaml_file)
        assert p.name == "test-profile"
        assert p.role.name == "analyst"
        assert len(p.modes) == 1
        assert p.modes[0].name == "evidential"

    def test_no_modes(self, tmp_path):
        data = {"name": "bare", "role": "critic"}
        yaml_file = tmp_path / "bare.yaml"
        yaml_file.write_text(yaml.dump(data))
        p = Profile.from_yaml(yaml_file)
        assert p.modes == ()


class TestListRolesAndModes:
    def test_list_roles_includes_known(self):
        roles = list_roles()
        for name in ("analyst", "critic", "implementer", "researcher", "reviewer"):
            assert name in roles

    def test_list_roles_is_sorted(self):
        roles = list_roles()
        assert roles == sorted(roles)

    def test_list_modes_includes_known(self):
        modes = list_modes()
        for name in ("adversarial", "fast", "slow", "systematic", "evidential"):
            assert name in modes

    def test_list_modes_is_sorted(self):
        modes = list_modes()
        assert modes == sorted(modes)
