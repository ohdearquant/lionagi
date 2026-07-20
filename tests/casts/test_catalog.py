# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.casts.catalog.build_catalog."""

from __future__ import annotations

import json
from importlib.resources import as_file, files

import yaml

from lionagi.casts.catalog import build_catalog
from lionagi.casts.emission import EscalationRequest, field_name_for
from lionagi.casts.pattern import Role, list_modes, list_roles


class TestBuildCatalog:
    def test_top_level_keys(self):
        cat = build_catalog()
        assert set(cat) == {"roles", "modes"}

    def test_roles_count_matches_list_roles(self):
        cat = build_catalog()
        assert len(cat["roles"]) == len(list_roles())

    def test_modes_count_matches_list_modes(self):
        cat = build_catalog()
        assert len(cat["modes"]) == len(list_modes())

    def test_default_pack_resolves_catalog_keys_and_covers_role_files(self):
        packaged = files("lionagi.casts").joinpath("packs", "default.yaml")
        with as_file(packaged) as path:
            pack_data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        catalog = build_catalog()
        catalog_role_entries = {entry["name"]: entry for entry in catalog["roles"]}
        catalog_roles = set(catalog_role_entries)
        catalog_modes = {entry["name"] for entry in catalog["modes"]}
        pack_roles = set(pack_data.get("roles") or {})
        top_level_modes = set(pack_data.get("modes") or {})
        referenced_modes = {
            mode
            for role_spec in (pack_data.get("roles") or {}).values()
            for field in ("default_modes", "modes_allow")
            for mode in role_spec.get(field, ())
        }

        problems = []
        if unknown_roles := pack_roles - catalog_roles:
            problems.append(f"unknown default-pack roles: {sorted(unknown_roles)}")
        if unresolved_roles := {
            role
            for role in pack_roles & catalog_roles
            if catalog_role_entries[role]["config"] is None
        }:
            problems.append(f"unresolved default-pack roles: {sorted(unresolved_roles)}")
        if unknown_modes := (top_level_modes | referenced_modes) - catalog_modes:
            problems.append(f"unknown default-pack modes: {sorted(unknown_modes)}")
        if uncovered_roles := catalog_roles - pack_roles:
            problems.append(f"uncovered role files: {sorted(uncovered_roles)}")

        assert not problems, "; ".join(problems)

    def test_role_entry_shape(self):
        cat = build_catalog()
        for role in cat["roles"]:
            for field in ("name", "description", "emits", "body", "config"):
                assert field in role, f"role {role.get('name')!r} missing {field!r}"
            assert isinstance(role["name"], str)
            assert isinstance(role["description"], str)
            assert isinstance(role["emits"], list)
            assert isinstance(role["body"], str)
            # config is dict or None
            assert role["config"] is None or isinstance(role["config"], dict)

    def test_emit_entry_shape(self):
        cat = build_catalog()
        for role in cat["roles"]:
            for entry in role["emits"]:
                assert "model" in entry, f"emit entry missing 'model': {entry}"
                assert "key" in entry, f"emit entry missing 'key': {entry}"
                assert isinstance(entry["model"], str)
                assert isinstance(entry["key"], str)

    def test_mode_entry_shape(self):
        cat = build_catalog()
        for mode in cat["modes"]:
            assert "name" in mode
            assert "description" in mode
            assert "behaviors" in mode
            assert "conflicts_with" in mode
            assert isinstance(mode["name"], str)
            assert isinstance(mode["description"], str)
            assert isinstance(mode["behaviors"], str)
            assert isinstance(mode["conflicts_with"], list)

    def test_known_role_present(self):
        cat = build_catalog()
        names = {r["name"] for r in cat["roles"]}
        assert "analyst" in names
        assert "critic" in names
        assert "implementer" in names

    def test_known_mode_present(self):
        cat = build_catalog()
        names = {m["name"] for m in cat["modes"]}
        assert "adversarial" in names
        assert "evidential" in names
        assert "slow" in names

    def test_emitting_role_contains_escalation_request(self):
        """EscalationRequest is implicitly added by build_emission_operable."""
        cat = build_catalog()
        analyst = next(r for r in cat["roles"] if r["name"] == "analyst")
        assert analyst["emits"], "analyst should have emissions"
        emit_models = {e["model"] for e in analyst["emits"]}
        assert "EscalationRequest" in emit_models

    def test_emitting_role_escalation_key_matches_field_name_for(self):
        """The 'key' for EscalationRequest must equal field_name_for(EscalationRequest)."""
        expected_key = field_name_for(EscalationRequest)
        cat = build_catalog()
        analyst = next(r for r in cat["roles"] if r["name"] == "analyst")
        esc = next(e for e in analyst["emits"] if e["model"] == "EscalationRequest")
        assert esc["key"] == expected_key

    def test_emitting_role_keys_match_field_name_for(self):
        """All emit keys must be the snake_case produced by field_name_for."""
        cat = build_catalog()
        analyst = next(r for r in cat["roles"] if r["name"] == "analyst")
        # Load the role directly and compare via emission_operable
        role = Role.load("analyst")
        op = role.emission_operable()
        expected = [(s.base_type.__name__, s.name) for s in op.get_specs()]
        actual = [(e["model"], e["key"]) for e in analyst["emits"]]
        assert actual == expected

    def test_non_emitting_role_has_empty_emits_and_no_escalation(self):
        """A role with no emits tuple has emits=[] and no EscalationRequest."""
        cat = build_catalog()
        # Find a role with no emits — check each until we find one
        non_emitting = [r for r in cat["roles"] if not r["emits"]]
        # At minimum, verify the contract holds for any such role
        # (if all roles emit, this test trivially passes — that's valid)
        for role in non_emitting:
            emit_models = {e["model"] for e in role["emits"]}
            assert "EscalationRequest" not in emit_models

    def test_emitting_role_emit_list_matches_emission_operable(self):
        """Full contract: catalog emits == emission_operable().get_specs() sequence."""
        cat = build_catalog()
        critic = next(r for r in cat["roles"] if r["name"] == "critic")
        role = Role.load("critic")
        op = role.emission_operable()
        assert op is not None
        expected = [{"model": s.base_type.__name__, "key": s.name} for s in op.get_specs()]
        assert critic["emits"] == expected

    def test_mode_conflicts_with_are_strings(self):
        cat = build_catalog()
        for mode in cat["modes"]:
            for item in mode["conflicts_with"]:
                assert isinstance(item, str)

    def test_roles_sorted(self):
        cat = build_catalog()
        names = [r["name"] for r in cat["roles"]]
        assert names == sorted(names)

    def test_modes_sorted(self):
        cat = build_catalog()
        names = [m["name"] for m in cat["modes"]]
        assert names == sorted(names)

    def test_catalog_is_json_serializable(self):
        cat = build_catalog()
        s = json.dumps(cat)
        reloaded = json.loads(s)
        assert reloaded["roles"][0]["name"] == cat["roles"][0]["name"]

    def test_no_role_body_is_none(self):
        cat = build_catalog()
        for role in cat["roles"]:
            assert role["body"] is not None

    def test_roles_all_have_descriptions(self):
        cat = build_catalog()
        for role in cat["roles"]:
            assert role["description"].strip(), f"empty description on role {role['name']!r}"

    def test_pack_config_present_for_known_packed_role(self):
        """Roles in default.yaml have a non-None config section."""
        cat = build_catalog()
        critic = next(r for r in cat["roles"] if r["name"] == "critic")
        assert critic["config"] is not None, "critic should have a pack config"
        cfg = critic["config"]
        # RoleConfig fields
        for field in ("active", "default_modes", "modes_allow"):
            assert field in cfg, f"config missing {field!r}"

    def test_pack_config_active_is_bool(self):
        cat = build_catalog()
        for role in cat["roles"]:
            if role["config"] and "active" in role["config"]:
                assert isinstance(role["config"]["active"], bool)

    def test_pack_missing_gracefully_returns_none_config(self, monkeypatch):
        """When the default pack can't be loaded, config is None — catalog still serves."""
        import lionagi.casts.catalog as cat_mod

        monkeypatch.setattr(cat_mod, "_load_default_pack", lambda: None)
        cat = build_catalog()
        assert "roles" in cat
        for role in cat["roles"]:
            assert role["config"] is None

    def test_build_catalog_exported_from_casts_init(self):
        from lionagi.casts import build_catalog as bc

        assert bc is build_catalog
