# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for Profile (lionagi/casts/profile.py) and list_roles/list_modes."""

from pathlib import Path

import pytest
import yaml

from lionagi.casts.pattern import Mode, Role, list_modes, list_roles
from lionagi.casts.profile import Profile

ROLES_DIR = Path(__file__).parent.parent.parent / "lionagi" / "casts" / "roles"
MODES_DIR = ROLES_DIR / "modes"


# ---------------------------------------------------------------------------
# list_roles
# ---------------------------------------------------------------------------


def test_list_roles_excludes_template():
    roles = list_roles()
    assert "TEMPLATE" not in roles


def test_list_roles_excludes_modes_dir():
    roles = list_roles()
    assert "modes" not in roles


def test_list_roles_includes_known_roles():
    roles = list_roles()
    for expected in ("analyst", "critic", "implementer", "researcher", "reviewer"):
        assert expected in roles, f"{expected!r} not in list_roles()"


def test_list_roles_is_sorted():
    roles = list_roles()
    assert roles == sorted(roles)


def test_list_roles_matches_disk_stems():
    disk = sorted(p.stem for p in ROLES_DIR.glob("*.md") if p.stem != "TEMPLATE")
    assert list_roles() == disk


def test_list_roles_user_dir_merge(tmp_path, monkeypatch):
    user_roles = tmp_path / ".lionagi" / "roles"
    user_roles.mkdir(parents=True)
    (user_roles / "my_custom_role.md").write_text("custom")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    roles = list_roles()
    assert "my_custom_role" in roles


# ---------------------------------------------------------------------------
# list_modes
# ---------------------------------------------------------------------------


def test_list_modes_includes_known_modes():
    modes = list_modes()
    for expected in ("adversarial", "fast", "slow", "systematic", "evidential"):
        assert expected in modes, f"{expected!r} not in list_modes()"


def test_list_modes_is_sorted():
    modes = list_modes()
    assert modes == sorted(modes)


def test_list_modes_matches_disk_stems():
    disk = sorted(p.stem for p in MODES_DIR.glob("*.md"))
    assert list_modes() == disk


def test_list_modes_user_dir_merge(tmp_path, monkeypatch):
    user_modes = tmp_path / ".lionagi" / "modes"
    user_modes.mkdir(parents=True)
    (user_modes / "my_mode.md").write_text("mode")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    modes = list_modes()
    assert "my_mode" in modes


# ---------------------------------------------------------------------------
# Profile.compose
# ---------------------------------------------------------------------------


def test_profile_compose_by_name():
    p = Profile.compose("analyst")
    assert p.name == "analyst"
    assert isinstance(p.role, Role)
    assert p.modes == ()


def test_profile_compose_by_role_object():
    role = Role.load("critic")
    p = Profile.compose(role)
    assert p.role is role
    assert p.name == "critic"


def test_profile_compose_modes_by_name():
    p = Profile.compose("researcher", modes=["evidential", "systematic"])
    assert len(p.modes) == 2
    assert p.modes[0].name == "evidential"
    assert p.modes[1].name == "systematic"


def test_profile_compose_modes_by_object():
    m = Mode.load("adversarial")
    p = Profile.compose("critic", modes=[m])
    assert p.modes[0] is m


def test_profile_compose_custom_name():
    p = Profile.compose("analyst", name="my-analyst")
    assert p.name == "my-analyst"


# ---------------------------------------------------------------------------
# Profile conflict rejection
# ---------------------------------------------------------------------------


def test_profile_conflict_fast_slow_raises():
    with pytest.raises(ValueError, match="conflict"):
        Profile.compose("researcher", modes=["fast", "slow"])


def test_profile_conflict_fast_systematic_raises():
    with pytest.raises(ValueError, match="conflict"):
        Profile.compose("researcher", modes=["fast", "systematic"])


def test_profile_no_conflict_evidential_adversarial():
    p = Profile.compose("critic", modes=["evidential", "adversarial"])
    assert len(p.modes) == 2


# ---------------------------------------------------------------------------
# Profile.capabilities
# ---------------------------------------------------------------------------


def test_profile_capabilities_delegates():
    from lionagi.casts.capabilities import capability_models

    p = Profile.compose("critic")
    assert p.capabilities == capability_models("critic")


def test_profile_capabilities_unknown_role_returns_tuple():
    p = Profile.compose("analyst")
    caps = p.capabilities
    assert isinstance(caps, tuple)
    assert len(caps) >= 1


# ---------------------------------------------------------------------------
# Profile.build_system_message
# ---------------------------------------------------------------------------


def test_profile_build_system_message_includes_role_body():
    p = Profile.compose("analyst")
    msg = p.build_system_message()
    assert p.role.body in msg


def test_profile_build_system_message_includes_mode_behaviors():
    p = Profile.compose("analyst", modes=["adversarial"])
    msg = p.build_system_message()
    adv = Mode.load("adversarial")
    assert adv.behaviors in msg


def test_profile_build_system_message_blank_line_separator():
    p = Profile.compose("analyst", modes=["adversarial"])
    msg = p.build_system_message()
    assert "\n\n" in msg


def test_profile_build_system_message_no_modes():
    p = Profile.compose("analyst")
    msg = p.build_system_message()
    assert msg == p.role.body


# ---------------------------------------------------------------------------
# Profile.from_yaml
# ---------------------------------------------------------------------------


def test_profile_from_yaml_round_trip(tmp_path):
    data = {"name": "test-profile", "role": "analyst", "modes": ["evidential"]}
    yaml_file = tmp_path / "profile.yaml"
    yaml_file.write_text(yaml.dump(data))

    p = Profile.from_yaml(yaml_file)
    assert p.name == "test-profile"
    assert p.role.name == "analyst"
    assert len(p.modes) == 1
    assert p.modes[0].name == "evidential"


def test_profile_from_yaml_no_modes(tmp_path):
    data = {"name": "bare", "role": "critic"}
    yaml_file = tmp_path / "bare.yaml"
    yaml_file.write_text(yaml.dump(data))

    p = Profile.from_yaml(yaml_file)
    assert p.modes == ()


# ---------------------------------------------------------------------------
# Profile is frozen
# ---------------------------------------------------------------------------


def test_profile_is_frozen():
    p = Profile.compose("analyst")
    with pytest.raises(AttributeError):
        p.name = "other"  # type: ignore[misc]
