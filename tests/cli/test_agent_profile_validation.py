# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for _validate_bare_name: path-traversal + separator rejection."""

from __future__ import annotations

import pytest

from lionagi.cli._providers import _validate_bare_name, load_agent_profile


class TestValidateBareName:
    """_validate_bare_name must reject anything that is not a safe identifier."""

    def test_accepts_plain_name(self):
        _validate_bare_name("orchestrator")

    def test_accepts_hyphenated_name(self):
        _validate_bare_name("my-agent")

    def test_accepts_underscore_name(self):
        _validate_bare_name("my_agent_v2")

    def test_accepts_alphanumeric(self):
        _validate_bare_name("Agent42")

    def test_rejects_path_separator_slash(self):
        with pytest.raises(ValueError, match="bare identifier"):
            _validate_bare_name("a/b")

    def test_rejects_parent_traversal(self):
        with pytest.raises(ValueError, match="bare identifier"):
            _validate_bare_name("../agents/orchestrator")

    def test_rejects_leading_dot(self):
        with pytest.raises(ValueError, match="bare identifier"):
            _validate_bare_name(".hidden")

    def test_rejects_dot_alone(self):
        with pytest.raises(ValueError, match="bare identifier"):
            _validate_bare_name(".")

    def test_rejects_double_dot(self):
        with pytest.raises(ValueError, match="bare identifier"):
            _validate_bare_name("..")

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError, match="bare identifier"):
            _validate_bare_name("")

    def test_rejects_backslash(self):
        with pytest.raises(ValueError, match="bare identifier"):
            _validate_bare_name("foo\\bar")

    def test_rejects_null_byte(self):
        with pytest.raises(ValueError, match="bare identifier"):
            _validate_bare_name("foo\x00bar")


class TestLoadAgentProfileValidation:
    """load_agent_profile must fail closed before touching the filesystem."""

    def test_rejects_path_traversal_name(self):
        """Traversal name raises ValueError before any filesystem probe."""
        with pytest.raises(ValueError, match="bare identifier"):
            load_agent_profile("../agents/orchestrator")

    def test_single_separator_name_is_a_plugin_token_not_rejected(self):
        """A single '/' is now a `<plugin>/<name>` token (each side still validated as a
        bare identifier) — it proceeds past validation to plugin/file lookup, raising
        FileNotFoundError (no matching plugin here), not the bare-identifier ValueError."""
        with pytest.raises(FileNotFoundError):
            load_agent_profile("a/b")

    def test_rejects_separator_name_with_invalid_component(self):
        """Each side of a `<plugin>/<name>` token is still validated — a component that
        fails the bare-identifier check (here, a second separator) still fails closed."""
        with pytest.raises(ValueError, match="bare identifier"):
            load_agent_profile("a/b/c")

    def test_rejects_hidden_name(self):
        with pytest.raises(ValueError, match="bare identifier"):
            load_agent_profile(".hidden")

    def test_valid_name_proceeds_to_file_lookup(self):
        """A valid name passes validation and raises FileNotFoundError (no
        .lionagi dir in the test environment), NOT ValueError."""
        with pytest.raises((FileNotFoundError, Exception)) as exc_info:
            load_agent_profile("valid-name")
        # Must NOT be the validation error.
        assert not isinstance(exc_info.value, ValueError)

    def test_broken_profile_symlink_reports_unreadable_target(self, monkeypatch, tmp_path):
        import lionagi.cli._providers as providers

        lionagi_dir = tmp_path / ".lionagi"
        agents_dir = lionagi_dir / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "broken.md").symlink_to("missing-profile.md")

        monkeypatch.setattr(providers, "_find_lionagi_dirs", lambda: [lionagi_dir])
        monkeypatch.setattr(providers, "_plugin_agent_profiles", lambda: {})

        with pytest.raises(FileNotFoundError) as exc_info:
            providers.load_agent_profile("broken")

        message = str(exc_info.value)
        assert "exists but its symlink target is unreadable: missing-profile.md" in message
        assert "broken" not in providers.list_agents()
