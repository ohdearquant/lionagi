# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.lndl.prompt.get_lndl_system_prompt."""

from __future__ import annotations

from lionagi.lndl.prompt import get_lndl_system_prompt


class TestGetLndlSystemPrompt:
    def test_returns_string(self):
        """Line 87: get_lndl_system_prompt() returns a string."""
        result = get_lndl_system_prompt()
        assert isinstance(result, str)

    def test_not_empty(self):
        """Returned prompt has content."""
        result = get_lndl_system_prompt()
        assert len(result) > 0

    def test_stripped(self):
        """Result is stripped (no leading/trailing whitespace)."""
        result = get_lndl_system_prompt()
        assert result == result.strip()

    def test_consistent_on_repeated_calls(self):
        """Repeated calls return the same value."""
        r1 = get_lndl_system_prompt()
        r2 = get_lndl_system_prompt()
        assert r1 == r2
