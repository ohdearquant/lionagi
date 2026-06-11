# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.casts._catalog.build_catalog."""

from __future__ import annotations

import pytest

from lionagi.casts._catalog import build_catalog
from lionagi.casts.pattern import list_modes, list_roles


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

    def test_role_entry_shape(self):
        cat = build_catalog()
        for role in cat["roles"]:
            assert "name" in role
            assert "description" in role
            assert "emits" in role
            assert "body" in role
            assert isinstance(role["name"], str)
            assert isinstance(role["description"], str)
            assert isinstance(role["emits"], list)
            assert isinstance(role["body"], str)

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

    def test_role_emits_are_class_name_strings(self):
        cat = build_catalog()
        analyst = next(r for r in cat["roles"] if r["name"] == "analyst")
        assert "AnalysisResult" in analyst["emits"]
        assert "Finding" in analyst["emits"]

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
        import json

        cat = build_catalog()
        # must not raise
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
