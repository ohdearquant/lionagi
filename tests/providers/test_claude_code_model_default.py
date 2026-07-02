# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Claude CLI model default: bare 'sonnet' pins to Sonnet 5."""

from __future__ import annotations

from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest


def _model_arg(req: ClaudeCodeRequest) -> str:
    args = req.as_cmd_args()
    return args[args.index("--model") + 1]


class TestSonnetDefault:
    def test_default_model_resolves_to_sonnet_5(self):
        req = ClaudeCodeRequest(prompt="hi")
        assert _model_arg(req) == "claude-sonnet-5"

    def test_explicit_sonnet_alias_resolves_to_sonnet_5(self):
        req = ClaudeCodeRequest(prompt="hi", model="sonnet")
        assert _model_arg(req) == "claude-sonnet-5"

    def test_explicit_full_id_passes_through(self):
        req = ClaudeCodeRequest(prompt="hi", model="claude-sonnet-4-6")
        assert _model_arg(req) == "claude-sonnet-4-6"

    def test_other_aliases_untouched(self):
        assert _model_arg(ClaudeCodeRequest(prompt="hi", model="opus")) == "opus"
        assert _model_arg(ClaudeCodeRequest(prompt="hi", model="haiku")) == "haiku"
        assert _model_arg(ClaudeCodeRequest(prompt="hi", model="fable")) == "fable"
