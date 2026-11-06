# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"Claude effort normalization."

from __future__ import annotations

from lionhub.orchestration.spawn import _clamp_claude_effort


class TestClampClaudeEffort:
    def test_xhigh_preserved_on_opus_4_7(self):
        assert _clamp_claude_effort("xhigh", "claude/claude-opus-4-7") == "xhigh"

    def test_xhigh_preserved_on_bare_opus_4_7(self):
        assert _clamp_claude_effort("xhigh", "opus-4-7") == "xhigh"

    def test_xhigh_preserved_on_bare_opus(self):
        assert _clamp_claude_effort("xhigh", "opus") == "xhigh"

    def test_xhigh_clamped_on_sonnet(self):
        assert _clamp_claude_effort("xhigh", "claude/claude-sonnet-4-6") == "high"

    def test_xhigh_clamped_on_haiku(self):
        assert _clamp_claude_effort("xhigh", "claude/claude-haiku-4-5") == "high"

    def test_non_xhigh_untouched_on_any_model(self):
        for effort in ("none", "minimal", "low", "medium", "high", "max"):
            assert _clamp_claude_effort(effort, "claude/claude-sonnet-4-6") == effort
            assert _clamp_claude_effort(effort, "claude/claude-opus-4-7") == effort
