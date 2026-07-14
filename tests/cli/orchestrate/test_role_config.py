# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0043 wiring: pack-backed mode resolution + casts role/mode composition."""

from __future__ import annotations

import pytest

from lionagi.cli.orchestrate._orchestration import (
    build_worker_branch,
    casts_role_system,
    mode_roster,
    resolve_modes,
    role_config,
    setup_orchestration,
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


@pytest.mark.asyncio
async def test_selected_pack_reaches_worker_prompt(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from lionagi import iModel
    from lionagi.casts.pack import Pack, RoleConfig, RolePolicy

    class _Session:
        def __init__(self):
            self.branches = []

        def include_branches(self, branch):
            self.branches.append(branch)

    pack = Pack(
        name="selected",
        configs={"researcher": RoleConfig()},
        policies={"researcher": RolePolicy(boundaries=("selected-pack-worker-boundary",))},
    )
    env = SimpleNamespace(
        run=SimpleNamespace(agent_artifact_dir=lambda name: tmp_path / name),
        session=_Session(),
        default_model_spec="openai/gpt-4o-mini",
        bare=False,
        effort=None,
        theme=None,
        yolo=False,
        bypass=False,
        verbose=False,
        fast=False,
        cwd=str(tmp_path),
        team_data=None,
        exchange=None,
        messenger=None,
        roster=None,
        messenger_names=None,
        pack=pack,
        _live_persist=None,
        register_name=lambda _name: None,
    )
    monkeypatch.setattr(
        "lionagi.cli.orchestrate._orchestration.build_imodel_from_spec",
        lambda *_a, **_kw: iModel(provider="openai", model="gpt-4o-mini", api_key="dummy-key"),
    )

    def missing_profile(_name):
        raise FileNotFoundError

    monkeypatch.setattr(
        "lionagi.cli.orchestrate._orchestration.load_agent_profile", missing_profile
    )

    branch, *_ = await build_worker_branch(
        env, agent_id="researcher", role="researcher", explicit_name="researcher"
    )

    assert "selected-pack-worker-boundary" in branch.system.rendered


@pytest.mark.asyncio
async def test_selected_pack_reaches_orchestrator_prompt(tmp_path):
    pack_path = tmp_path / "selected.yaml"
    pack_path.write_text(
        """\
name: selected
roles:
  orchestrator:
    boundaries:
      - selected-pack-orchestrator-boundary
""",
        encoding="utf-8",
    )

    env = await setup_orchestration(
        pattern_name="PackContinuity",
        model_spec="openai/gpt-4o-mini",
        agent_name=None,
        save_dir=str(tmp_path / "run"),
        cwd=str(tmp_path),
        yolo=False,
        verbose=False,
        effort=None,
        theme=None,
        pack=str(pack_path),
    )

    assert env.pack is not None
    assert "selected-pack-orchestrator-boundary" in env.orc_branch.system.rendered
