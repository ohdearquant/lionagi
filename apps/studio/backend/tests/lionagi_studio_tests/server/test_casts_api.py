# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for GET /api/casts."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")


class TestGetCasts:
    def test_status_ok(self, studio_client):
        r = studio_client.get("/api/casts/")
        assert r.status_code == 200

    def test_top_level_keys(self, studio_client):
        data = studio_client.get("/api/casts/").json()
        assert "roles" in data
        assert "modes" in data

    def test_roles_is_list(self, studio_client):
        assert isinstance(studio_client.get("/api/casts/").json()["roles"], list)

    def test_modes_is_list(self, studio_client):
        assert isinstance(studio_client.get("/api/casts/").json()["modes"], list)

    def test_role_entry_shape(self, studio_client):
        data = studio_client.get("/api/casts/").json()
        for role in data["roles"]:
            for field in ("name", "description", "emits", "body", "config"):
                assert field in role, f"role {role.get('name')!r} missing {field!r}"
            assert isinstance(role["emits"], list)
            assert role["config"] is None or isinstance(role["config"], dict)

    def test_emit_entry_shape(self, studio_client):
        data = studio_client.get("/api/casts/").json()
        for role in data["roles"]:
            for entry in role["emits"]:
                assert "model" in entry
                assert "key" in entry

    def test_mode_entry_shape(self, studio_client):
        data = studio_client.get("/api/casts/").json()
        for mode in data["modes"]:
            for field in ("name", "description", "behaviors", "conflicts_with"):
                assert field in mode, f"mode {mode.get('name')!r} missing {field!r}"
            assert isinstance(mode["conflicts_with"], list)

    def test_known_roles_present(self, studio_client):
        data = studio_client.get("/api/casts/").json()
        names = {r["name"] for r in data["roles"]}
        assert "analyst" in names
        assert "critic" in names

    def test_known_modes_present(self, studio_client):
        data = studio_client.get("/api/casts/").json()
        names = {m["name"] for m in data["modes"]}
        assert "adversarial" in names
        assert "evidential" in names

    def test_non_empty(self, studio_client):
        data = studio_client.get("/api/casts/").json()
        assert len(data["roles"]) > 0
        assert len(data["modes"]) > 0

    def test_analyst_emits_contains_escalation_request(self, studio_client):
        """EscalationRequest is implicitly added to every emitting role."""
        data = studio_client.get("/api/casts/").json()
        analyst = next(r for r in data["roles"] if r["name"] == "analyst")
        emit_models = {e["model"] for e in analyst["emits"]}
        assert "EscalationRequest" in emit_models

    def test_analyst_emits_contains_analysis_result(self, studio_client):
        data = studio_client.get("/api/casts/").json()
        analyst = next(r for r in data["roles"] if r["name"] == "analyst")
        emit_models = {e["model"] for e in analyst["emits"]}
        assert "AnalysisResult" in emit_models

    def test_emit_keys_are_snake_case(self, studio_client):
        """key field is snake_case (from field_name_for)."""
        data = studio_client.get("/api/casts/").json()
        analyst = next(r for r in data["roles"] if r["name"] == "analyst")
        esc = next(e for e in analyst["emits"] if e["model"] == "EscalationRequest")
        assert esc["key"] == "escalation_request"

    def test_pack_config_present_for_critic(self, studio_client):
        data = studio_client.get("/api/casts/").json()
        critic = next(r for r in data["roles"] if r["name"] == "critic")
        assert critic["config"] is not None
        cfg = critic["config"]
        assert "active" in cfg
        assert "default_modes" in cfg
        assert "modes_allow" in cfg
