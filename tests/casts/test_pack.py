# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Pack parsing — RolePolicy (prompt envelope) + RoleConfig (runtime, ADR-0043)."""

from __future__ import annotations

from lionagi.casts.pack import Pack


def test_from_file_parses_policy_and_config(tmp_path):
    p = tmp_path / "pack.yaml"
    p.write_text(
        "name: test\n"
        "roles:\n"
        "  critic:\n"
        "    authority: [decide]\n"
        "    default_modes: [adversarial]\n"
        "    modes_allow: [adversarial, premortem]\n"
        "    model: codex/x\n"
        "    effort: high\n"
        "    active: false\n"
    )
    pack = Pack.from_file(p)
    assert pack.name == "test"

    pol = pack.policy("critic")
    assert pol.authority == ("decide",)

    cfg = pack.config("critic")
    assert cfg.default_modes == ("adversarial",)
    assert cfg.modes_allow == ("adversarial", "premortem")
    assert cfg.model == "codex/x"
    assert cfg.effort == "high"
    assert cfg.active is False


def test_config_defaults_when_absent(tmp_path):
    p = tmp_path / "pack.yaml"
    p.write_text("name: t\nroles:\n  x:\n    authority: [a]\n")
    cfg = Pack.from_file(p).config("x")
    assert cfg.model is None
    assert cfg.effort is None
    assert cfg.default_modes == ()
    assert cfg.modes_allow == ()
    assert cfg.active is True


def test_missing_role_returns_none(tmp_path):
    p = tmp_path / "pack.yaml"
    p.write_text("name: t\nroles: {}\n")
    pack = Pack.from_file(p)
    assert pack.config("ghost") is None
    assert pack.policy("ghost") is None


def test_shipped_default_pack_has_critic_config():
    from importlib.resources import as_file, files

    packaged = files("lionagi.casts").joinpath("packs", "default.yaml")
    with as_file(packaged) as fp:
        pack = Pack.from_file(fp)
    cfg = pack.config("critic")
    assert cfg is not None
    assert "adversarial" in cfg.default_modes
    # model stays unset in the shipped pack (no hardcoded provider)
    assert cfg.model is None
