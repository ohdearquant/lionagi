# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.lndl.prompt.get_lndl_system_prompt."""

from __future__ import annotations

import hashlib

from lionagi.lndl.prompt import get_lndl_system_prompt

# SHA-256 of the prompt as of the tests being written. Any accidental edit to
# LNDL_SYSTEM_PROMPT (whitespace, typo, truncation, marker removal) will flip
# this hash and make test_prompt_snapshot fail immediately.
_EXPECTED_SHA256 = "d03ae99735fbfcbbf4ebd76bf0d9b86edaf5aa5c656ffbf796a9d96ce5e7adb8"


class TestGetLndlSystemPrompt:
    def test_returns_string(self):
        result = get_lndl_system_prompt()
        assert isinstance(result, str)

    def test_not_empty(self):
        result = get_lndl_system_prompt()
        assert len(result) > 0

    def test_stripped(self):
        result = get_lndl_system_prompt()
        assert result == result.strip()

    def test_consistent_on_repeated_calls(self):
        r1 = get_lndl_system_prompt()
        r2 = get_lndl_system_prompt()
        assert r1 == r2

    # ------------------------------------------------------------------
    # Regression guard: DSL marker presence
    # ------------------------------------------------------------------

    def test_prompt_contains_lvar_marker(self):
        """Prompt must contain the <lvar opening tag — lvar binding syntax."""
        prompt = get_lndl_system_prompt()
        assert "<lvar" in prompt, (
            "LNDL prompt is missing <lvar marker; "
            "every LNDL session will fail to parse variable bindings"
        )

    def test_prompt_contains_lact_marker(self):
        """Prompt must contain the <lact opening tag — action invocation syntax."""
        prompt = get_lndl_system_prompt()
        assert "<lact" in prompt, (
            "LNDL prompt is missing <lact marker; every LNDL session will fail to parse tool calls"
        )

    def test_prompt_contains_out_block_marker(self):
        """Prompt must contain the OUT{ output-spec syntax."""
        prompt = get_lndl_system_prompt()
        assert "OUT{" in prompt, (
            "LNDL prompt is missing OUT{ marker; every LNDL session will fail to commit outputs"
        )

    def test_prompt_minimum_length(self):
        """Prompt must be at least 500 chars; shorter means truncation."""
        prompt = get_lndl_system_prompt()
        assert len(prompt) >= 500, (
            f"LNDL prompt is suspiciously short ({len(prompt)} chars); "
            "the constant was likely truncated or replaced with a stub"
        )

    def test_prompt_is_deterministic(self):
        """Identical string on every call — no dynamic content injected."""
        calls = [get_lndl_system_prompt() for _ in range(5)]
        assert len(set(calls)) == 1, (
            "get_lndl_system_prompt() returned different values across calls; "
            "dynamic content must not be injected into the base system prompt"
        )

    def test_prompt_snapshot(self):
        """SHA-256 snapshot guard: any accidental edit trips this test.

        To update: python -c "import hashlib; from lionagi.lndl.prompt import get_lndl_system_prompt; print(hashlib.sha256(get_lndl_system_prompt().encode()).hexdigest())"
        """
        prompt = get_lndl_system_prompt()
        actual = hashlib.sha256(prompt.encode()).hexdigest()
        assert actual == _EXPECTED_SHA256, (
            f"LNDL system prompt has changed unexpectedly.\n"
            f"  expected SHA-256: {_EXPECTED_SHA256}\n"
            f"  actual   SHA-256: {actual}\n"
            "If the change is intentional, update _EXPECTED_SHA256 in this file."
        )
