# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the built-in playbook template endpoints.

Covers the bundled read-only templates (``lionagi/studio/builtin_playbooks/``)
that back the Studio Workflows page's "built-in templates" section: listing,
detail, and idempotent install into the user's own ``~/.lionagi/playbooks``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi", reason="studio extra not installed")

import lionagi.studio.services.playbooks as svc  # noqa: E402

# The real, shipped set — kept in sync with examples/playbooks/ (see
# builtin_playbooks/README.md). If this list ever drifts from what's actually
# bundled, the "matches examples/playbooks" test below will catch it too.
EXPECTED_BUILTIN_NAMES = {
    "audit",
    "chatgpt-orchestrate",
    "doc-alignment",
    "feature",
    "minimal",
    "persistent-chat",
    "pr-review",
    "research",
    "resolve-issues",
    "test-coverage",
}


# ---------------------------------------------------------------------------
# Bundled data integrity
# ---------------------------------------------------------------------------


def test_bundled_root_exists_and_has_expected_names():
    """The real package-data directory ships all 10 known built-ins."""
    assert svc._BUILTIN_PLAYBOOKS_ROOT.exists()
    names = {
        p.name.removesuffix(".playbook.yaml")
        for p in svc._BUILTIN_PLAYBOOKS_ROOT.glob("*.playbook.yaml")
    }
    assert names == EXPECTED_BUILTIN_NAMES


def test_bundled_files_match_examples_playbooks_byte_for_byte():
    """Catches drift between examples/playbooks/ (docs) and the bundled copy."""
    repo_root = Path(__file__).resolve().parents[2]
    examples_root = repo_root / "examples" / "playbooks"
    assert examples_root.exists()

    for name in EXPECTED_BUILTIN_NAMES:
        bundled = svc._BUILTIN_PLAYBOOKS_ROOT / f"{name}.playbook.yaml"
        example = examples_root / f"{name}.playbook.yaml"
        assert bundled.read_text() == example.read_text(), (
            f"{name}.playbook.yaml has drifted between builtin_playbooks/ and examples/playbooks/"
        )


# ---------------------------------------------------------------------------
# list_builtin_playbooks / get_builtin_playbook (service layer, real bundled data)
# ---------------------------------------------------------------------------


class TestListBuiltinPlaybooks:
    def test_returns_all_known_builtins(self):
        names = {p["name"] for p in svc.list_builtin_playbooks()}
        assert names == EXPECTED_BUILTIN_NAMES

    def test_each_entry_has_a_real_description(self):
        for pb in svc.list_builtin_playbooks():
            assert isinstance(pb["description"], str) and pb["description"].strip()

    def test_pr_review_has_explicit_args_schema(self):
        entries = {p["name"]: p for p in svc.list_builtin_playbooks()}
        pr_review = entries["pr-review"]
        assert "repo" in pr_review["args"]
        assert "focus" in pr_review["args"]

    def test_chatgpt_orchestrate_has_no_args_but_has_argument_hint(self):
        """chatgpt-orchestrate only declares argument-hint, no explicit args: dict."""
        entries = {p["name"]: p for p in svc.list_builtin_playbooks()}
        entry = entries["chatgpt-orchestrate"]
        assert entry["args"] == {}
        assert entry["argument_hint"]

    def test_installed_flag_false_when_user_playbooks_dir_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(svc, "_PLAYBOOKS_ROOT", tmp_path / "nonexistent")
        for pb in svc.list_builtin_playbooks():
            assert pb["installed"] is False

    def test_installed_flag_true_after_materializing_a_copy(self, tmp_path, monkeypatch):
        user_root = tmp_path / "playbooks"
        monkeypatch.setattr(svc, "_PLAYBOOKS_ROOT", user_root)
        user_root.mkdir()
        (user_root / "minimal.playbook.yaml").write_text("name: minimal\n")

        entries = {p["name"]: p for p in svc.list_builtin_playbooks()}
        assert entries["minimal"]["installed"] is True
        assert entries["audit"]["installed"] is False


class TestGetBuiltinPlaybook:
    def test_returns_full_data_and_raw_text(self):
        pb = svc.get_builtin_playbook("minimal")
        assert pb is not None
        assert pb["name"] == "minimal"
        assert pb["data"]["description"]
        assert "prompt" in pb["data"]
        assert isinstance(pb["raw"], str) and pb["raw"]

    def test_unknown_name_returns_none(self):
        assert svc.get_builtin_playbook("does-not-exist") is None

    @pytest.mark.parametrize("bad_name", ["../secrets", "a/b"])
    def test_path_traversal_with_separator_rejected(self, bad_name):
        """A component containing '/' is caught by safe_path_join (HTTPException)."""
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            svc.get_builtin_playbook(bad_name)

    @pytest.mark.parametrize("bad_name", ["..", ""])
    def test_bare_dots_or_empty_resolve_to_no_match(self, bad_name):
        """No literal '/' means the `.playbook.yaml` suffix always defangs it into
        a benign, nonexistent filename (matches the pre-existing get_playbook()
        behavior this function mirrors) — not found, not a raised error."""
        assert svc.get_builtin_playbook(bad_name) is None


# ---------------------------------------------------------------------------
# install_builtin_playbook — idempotent materialize into ~/.lionagi/playbooks
# ---------------------------------------------------------------------------


class TestInstallBuiltinPlaybook:
    def test_first_install_creates_file_and_returns_installed_true(self, tmp_path, monkeypatch):
        monkeypatch.setattr(svc, "_PLAYBOOKS_ROOT", tmp_path / "playbooks")

        result = svc.install_builtin_playbook("minimal")
        assert result["installed"] is True
        assert result["playbook"]["name"] == "minimal"

        dest = tmp_path / "playbooks" / "minimal.playbook.yaml"
        assert dest.exists()
        source_text = (svc._BUILTIN_PLAYBOOKS_ROOT / "minimal.playbook.yaml").read_text()
        assert dest.read_text() == source_text

    def test_second_install_is_a_noop_and_does_not_clobber_edits(self, tmp_path, monkeypatch):
        monkeypatch.setattr(svc, "_PLAYBOOKS_ROOT", tmp_path / "playbooks")

        svc.install_builtin_playbook("minimal")
        dest = tmp_path / "playbooks" / "minimal.playbook.yaml"
        # Simulate the user customizing their copy after cloning it.
        customized = yaml.safe_load(dest.read_text())
        customized["description"] = "my customized version"
        dest.write_text(yaml.dump(customized))

        result = svc.install_builtin_playbook("minimal")
        assert result["installed"] is False
        assert dest.read_text() == yaml.dump(customized)

    def test_unknown_builtin_raises_file_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(svc, "_PLAYBOOKS_ROOT", tmp_path / "playbooks")
        with pytest.raises(FileNotFoundError):
            svc.install_builtin_playbook("does-not-exist")

    @pytest.mark.parametrize("bad_name", ["../secrets", "a/b"])
    def test_path_traversal_with_separator_rejected(self, tmp_path, monkeypatch, bad_name):
        from fastapi import HTTPException

        monkeypatch.setattr(svc, "_PLAYBOOKS_ROOT", tmp_path / "playbooks")
        with pytest.raises(HTTPException):
            svc.install_builtin_playbook(bad_name)

    def test_bare_dots_resolve_to_unknown_builtin(self, tmp_path, monkeypatch):
        """No literal '/' means '..' becomes a benign nonexistent source filename
        (the .playbook.yaml suffix defangs it) — reported as FileNotFoundError,
        same as any other unknown builtin name, not a path-safety exception."""
        monkeypatch.setattr(svc, "_PLAYBOOKS_ROOT", tmp_path / "playbooks")
        with pytest.raises(FileNotFoundError):
            svc.install_builtin_playbook("..")


# ---------------------------------------------------------------------------
# Route-level smoke tests (real app, real bundled data, isolated user dir)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestBuiltinPlaybookRoutes:
    def test_list_route_returns_all_builtins(self, studio_client, tmp_path, monkeypatch):
        monkeypatch.setattr(svc, "_PLAYBOOKS_ROOT", tmp_path / "playbooks")
        resp = studio_client.get("/api/playbook-templates/")
        assert resp.status_code == 200, resp.text
        names = {p["name"] for p in resp.json()["playbooks"]}
        assert names == EXPECTED_BUILTIN_NAMES

    def test_get_route_returns_detail(self, studio_client, tmp_path, monkeypatch):
        monkeypatch.setattr(svc, "_PLAYBOOKS_ROOT", tmp_path / "playbooks")
        resp = studio_client.get("/api/playbook-templates/research")
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "research"

    def test_get_route_unknown_name_404(self, studio_client, tmp_path, monkeypatch):
        monkeypatch.setattr(svc, "_PLAYBOOKS_ROOT", tmp_path / "playbooks")
        resp = studio_client.get("/api/playbook-templates/does-not-exist")
        assert resp.status_code == 404

    def test_install_route_materializes_and_is_idempotent(
        self, studio_client, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(svc, "_PLAYBOOKS_ROOT", tmp_path / "playbooks")

        first = studio_client.post("/api/playbook-templates/minimal/install")
        assert first.status_code == 200, first.text
        assert first.json()["installed"] is True

        second = studio_client.post("/api/playbook-templates/minimal/install")
        assert second.status_code == 200, second.text
        assert second.json()["installed"] is False

        # And the ordinary (pre-existing) user-playbooks route now sees it.
        listed = studio_client.get("/api/playbooks/")
        assert listed.status_code == 200, listed.text
        assert any(p["name"] == "minimal" for p in listed.json()["playbooks"])

    def test_playbooks_name_route_unaffected_by_builtin_prefix(
        self, studio_client, tmp_path, monkeypatch
    ):
        """/api/playbooks/{name} must not be shadowed by /api/playbook-templates/."""
        monkeypatch.setattr(svc, "_PLAYBOOKS_ROOT", tmp_path / "playbooks")
        resp = studio_client.get("/api/playbooks/builtin")
        assert resp.status_code == 404
