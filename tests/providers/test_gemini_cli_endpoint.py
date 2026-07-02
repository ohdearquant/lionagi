# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the Antigravity (`agy`) CLI endpoint.

Covers argv construction (json output-format, model resolution, resume/yolo
flags), nonzero-exit error surfacing, endpoint _call session mapping, default
model gemini-3.5-flash, and the REST-vs-CLI helpful error.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from lionagi.providers.google.gemini_code import (
    GeminiCodeRequest,
    GeminiSession,
    stream_gemini_cli,
)

# ---------------------------------------------------------------------------
# argv construction
# ---------------------------------------------------------------------------


class TestCmdArgs:
    """as_cmd_args must emit json output-format + a resolved agy model name."""

    def test_default_argv_uses_json_and_resolved_model(self):
        args = GeminiCodeRequest(prompt="hello").as_cmd_args()
        assert args[:2] == ["-p", "hello"]
        assert "--output-format" in args and "json" in args
        i = args.index("--model")
        assert args[i + 1] == "Gemini 3.5 Flash (Medium)"

    def test_pro_model_resolves_to_high(self):
        args = GeminiCodeRequest(prompt="hi", model="gemini-3-pro-preview").as_cmd_args()
        i = args.index("--model")
        assert args[i + 1] == "Gemini 3.1 Pro (High)"

    def test_yolo_emits_skip_permissions(self):
        args = GeminiCodeRequest(prompt="hi", yolo=True).as_cmd_args()
        assert "--dangerously-skip-permissions" in args

    def test_no_yolo_no_skip_permissions(self):
        args = GeminiCodeRequest(prompt="hi", yolo=False).as_cmd_args()
        assert "--dangerously-skip-permissions" not in args

    def test_resume_emits_conversation_flag(self):
        args = GeminiCodeRequest(prompt="hi", resume="conv-1").as_cmd_args()
        i = args.index("--conversation")
        assert args[i + 1] == "conv-1"
        assert "--continue" not in args

    def test_continue_recent_emits_continue(self):
        args = GeminiCodeRequest(prompt="hi", continue_recent=True).as_cmd_args()
        assert "--continue" in args

    def test_system_prompt_folded_into_prompt(self):
        req = GeminiCodeRequest(prompt="ask", system_prompt="be terse")
        assert req.full_prompt() == "be terse\n\nask"
        args = req.as_cmd_args()
        assert args[1] == "be terse\n\nask"


# ---------------------------------------------------------------------------
# Subprocess error surfacing
# ---------------------------------------------------------------------------


class TestSubprocessErrorSurfacing:
    """When agy exits nonzero, ndjson_from_cli raises RuntimeError; it propagates."""

    @pytest.mark.asyncio
    async def test_nonzero_exit_propagates_runtime_error(self):
        async def fake_events(_request):
            raise RuntimeError("agy exited 1: authentication required")
            yield  # pragma: no cover — make it an async generator

        with patch(
            "lionagi.providers.google.gemini_code.stream_gemini_cli_events",
            side_effect=fake_events,
        ):
            with pytest.raises(RuntimeError, match="authentication required"):
                async for _ in stream_gemini_cli(GeminiCodeRequest(prompt="hi")):
                    pass


# ---------------------------------------------------------------------------
# Endpoint _call session mapping
# ---------------------------------------------------------------------------


class TestEndpointCall:
    """The endpoint _call must return a session dict carrying the conversation_id."""

    @pytest.mark.asyncio
    async def test_call_returns_session_dict_with_session_id(self):
        from lionagi.providers.google.gemini_code import GeminiCLIEndpoint

        async def fake_events(_request):
            yield {
                "conversation_id": "conv-xyz",
                "status": "SUCCESS",
                "response": "GEMINI-LIONAGI-OK",
                "duration_seconds": 0.5,
                "num_turns": 1,
                "usage": {"input_tokens": 3, "output_tokens": 4},
            }

        ep = GeminiCLIEndpoint()
        request = GeminiCodeRequest(prompt="hello")
        with patch(
            "lionagi.providers.google.gemini_code.stream_gemini_cli_events",
            side_effect=fake_events,
        ):
            result = await ep._call({"request": request}, {})

        assert result["result"] == "GEMINI-LIONAGI-OK"
        assert result["session_id"] == "conv-xyz", (
            "conversation_id must survive into the returned session dict for state.db persistence"
        )


# ---------------------------------------------------------------------------
# Default model
# ---------------------------------------------------------------------------


class TestDefaultModel:
    """GeminiCodeRequest default model must be the latest flash family."""

    def test_default_model_is_gemini_3_5_flash(self):
        req = GeminiCodeRequest(prompt="hello")
        assert req.model == "gemini-3.5-flash"

    def test_explicit_model_is_preserved(self):
        req = GeminiCodeRequest(prompt="hello", model="gemini-2.5-pro")
        assert req.model == "gemini-2.5-pro"


# ---------------------------------------------------------------------------
# BACKENDS default model
# ---------------------------------------------------------------------------


class TestBackendsDefaultModel:
    """BACKENDS entries for gemini-cli must point to the latest flash family."""

    def test_gemini_cli_backend_uses_3_5_flash(self):
        from lionagi.service.providers import BACKENDS

        assert "gemini-3.5-flash" in BACKENDS["gemini-cli"]
        assert "gemini-3.5-flash" in BACKENDS["gemini_cli"]
        assert "gemini-3.5-flash" in BACKENDS["gemini-code"]
        assert "gemini-3.5-flash" in BACKENDS["gemini_code"]


# ---------------------------------------------------------------------------
# CLI_PROVIDERS includes gemini variants
# ---------------------------------------------------------------------------


class TestCliProvidersSet:
    """gemini_code, gemini-cli, gemini_cli, gemini-code must be in CLI_PROVIDERS."""

    def test_gemini_cli_in_cli_providers(self):
        from lionagi.service.providers import CLI_PROVIDERS

        for name in ("gemini_code", "gemini-code", "gemini_cli", "gemini-cli"):
            assert name in CLI_PROVIDERS, f"{name!r} not in CLI_PROVIDERS"


# ---------------------------------------------------------------------------
# Run.py error message improvement
# ---------------------------------------------------------------------------


class TestRunErrorMessage:
    """ValueError from run.py when provider is not CLI must mention gemini-cli."""

    @pytest.mark.asyncio
    async def test_gemini_api_provider_gives_helpful_error(self):
        """Using 'gemini' (REST API) in run() must mention 'gemini-cli'."""
        from lionagi.session.branch import Branch

        branch = Branch(chat_model="gemini/gemini-2.5-flash")

        if branch.chat_model.is_cli:
            pytest.skip("gemini resolved to CLI endpoint — skip REST path test")

        from lionagi.operations.run.run import run
        from lionagi.operations.types import RunParam

        with pytest.raises(ValueError, match="gemini-cli"):
            async for _ in run(branch, "hello", RunParam()):
                pass
