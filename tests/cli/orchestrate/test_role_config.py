# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0074 wiring: pack-backed mode resolution + casts role/mode composition."""

from __future__ import annotations

from lionagi.cli.orchestrate._orchestration import (
    casts_role_system,
    mode_roster,
    resolve_modes,
    role_config,
)


class TestModeRoster:
    def test_lists_all_mode_names(self):
        from lionagi.casts.pattern import list_modes

        text = mode_roster()
        for m in list_modes():
            assert m in text

    def test_surfaces_role_allowlists(self):
        # analyst restricts modes in the default pack; the planner prompt must
        # advertise that restriction so it never assigns a mode resolve_modes
        # would drop.
        cfg = role_config("analyst")
        assert cfg is not None and cfg.modes_allow
        text = mode_roster()
        assert f"analyst accepts only {', '.join(sorted(cfg.modes_allow))}" in text

    def test_allowlists_match_enforcement(self):
        # Every advertised allowlist must accept its own modes at execution.
        cfg = role_config("critic")
        assert cfg is not None and cfg.modes_allow
        for m in cfg.modes_allow:
            assert resolve_modes("critic", [m]) == [m]

    def test_custom_pack_unknown_mode_not_advertised(self):
        # A custom pack may allowlist a name the mode catalog doesn't know;
        # resolve_modes drops it, so the roster must never advertise it.
        from lionagi.casts.pack import Pack, RoleConfig

        pack = Pack(
            name="custom",
            configs={
                "critic": RoleConfig(modes_allow=("not_a_mode", "premortem")),
                "analyst": RoleConfig(modes_allow=("also_fake",)),
            },
        )
        text = mode_roster(pack)
        assert "not_a_mode" not in text
        assert "also_fake" not in text
        assert "critic accepts only premortem" in text
        assert "analyst accepts no per-task modes (leave empty)" in text
        # Coherence: every advertised entry survives enforcement on the SAME pack.
        assert resolve_modes("critic", ["premortem"], pack) == ["premortem"]
        assert resolve_modes("critic", ["not_a_mode"], pack) == []


class TestResolveModes:
    def test_pack_defaults(self):
        # critic ships with default_modes: [adversarial] in the default pack
        assert resolve_modes("critic") == ["adversarial"]

    def test_override_within_allowlist(self):
        assert resolve_modes("critic", ["premortem"]) == ["premortem"]

    def test_override_outside_allowlist_dropped(self):
        # 'fast' is not in critic's modes_allow
        assert resolve_modes("critic", ["fast"]) == []

    def test_unknown_mode_dropped(self):
        assert resolve_modes("critic", ["not_a_mode"]) == []

    def test_role_with_no_default_modes(self):
        # writer is in the pack but declares no default_modes
        assert resolve_modes("writer") == []

    def test_role_absent_from_pack(self):
        # a name with no pack config resolves to no modes (no crash)
        assert resolve_modes("definitely_not_a_role") == []


class TestCastsRoleSystem:
    def test_modes_extend_the_prompt(self):
        base = casts_role_system("critic")
        with_mode = casts_role_system("critic", modes=["adversarial"])
        assert base is not None and with_mode is not None
        assert len(with_mode) > len(base)  # adversarial behaviors appended

    def test_unknown_role_is_none(self):
        assert casts_role_system("not_a_role") is None

    def test_lion_system_prepended(self):
        sys = casts_role_system("critic")
        assert "LION" in sys[:200] or "lionagi" in sys[:200].lower()


def test_role_config_model_unset_in_default_pack():
    # the shipped pack must not pin a provider
    assert role_config("critic").model is None
