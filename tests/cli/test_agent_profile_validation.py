# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for agent profile name validation (LIONAGI-AUDIT-002).

Verify that _validate_bare_name rejects path traversal and separators
so that load_agent_profile() fails closed on malicious names.
"""

from __future__ import annotations

import pytest

from lionagi.cli._agents import _validate_bare_name, load_agent_profile


class TestValidateBareName:
    """_validate_bare_name must reject anything that is not a safe identifier."""

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

    def test_rejects_separator_name(self):
        with pytest.raises(ValueError, match="bare identifier"):
            load_agent_profile("a/b")

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
