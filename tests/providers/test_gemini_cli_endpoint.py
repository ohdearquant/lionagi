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
    derive_print_timeout,
    format_print_timeout,
    stream_gemini_cli,
)


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

    def test_caller_timeout_emits_derived_print_timeout(self):
        caller_timeout = 1200
        derived = derive_print_timeout(caller_timeout)
        args = GeminiCodeRequest(prompt="hi", print_timeout=derived).as_cmd_args()

        i = args.index("--print-timeout")
        assert args[i + 1] == derived
        assert int(derived.removesuffix("s")) > caller_timeout

    def test_explicit_print_timeout_is_preserved(self):
        explicit = "45m"
        args = GeminiCodeRequest(prompt="hi", print_timeout=explicit).as_cmd_args()

        i = args.index("--print-timeout")
        assert args[i + 1] == explicit

    @pytest.mark.parametrize(
        "explicit",
        [
            "not-a-duration",
            "0s",
            "-1s",
            "999ms",
            "9223372036854775808ns",
            # Digits outside ASCII: a duration grammar of ASCII decimal digits
            # does not accept these, so letting them through would send agy a
            # value it cannot parse.
            "١h",
            "１h",
        ],
    )
    def test_explicit_print_timeout_rejects_unusable_go_durations(self, explicit):
        request = GeminiCodeRequest(prompt="hi", print_timeout=explicit)

        with pytest.raises(ValueError, match="print_timeout"):
            request.as_cmd_args()

    def test_explicit_print_timeout_accepts_subnanosecond_fraction_at_go_max(self):
        """Go's time.ParseDuration truncates fractions smaller than a nanosecond,
        so a `.1ns` excess sitting on top of int64's max is still parseable by
        `agy` and must not be rejected here."""
        explicit = "9223372036854775807.1ns"
        args = GeminiCodeRequest(prompt="hi", print_timeout=explicit).as_cmd_args()

        i = args.index("--print-timeout")
        assert args[i + 1] == explicit

    def test_endpoint_config_print_timeout_is_checked_at_argv_boundary(self):
        endpoint_kwargs = {"print_timeout": "0s"}
        request = GeminiCodeRequest(prompt="hi", **endpoint_kwargs)

        with pytest.raises(ValueError, match="print_timeout"):
            request.as_cmd_args()

    def test_no_caller_timeout_omits_print_timeout(self):
        args = GeminiCodeRequest(prompt="hi").as_cmd_args()

        assert "--print-timeout" not in args

    @pytest.mark.parametrize(
        "unbounded",
        [float("inf"), 1e300, 1e10],
        ids=["infinite", "astronomically-large", "just-over-go-max"],
    )
    def test_unbounded_caps_stay_parseable_instead_of_raising(self, unbounded):
        """Clamp configured caps to Go's int64 duration limit."""
        emitted = format_print_timeout(unbounded)
        assert emitted.endswith("s")
        # int() rejects "inf", "1e+300" and every other non-integer
        # spelling, so this asserts parseability rather than restating the
        # clamping expression.
        seconds = int(emitted.removesuffix("s"))
        assert 0 < seconds <= (2**63 - 1) // 10**9

    @pytest.mark.parametrize(
        "caller_timeout",
        [(2**63 - 1) // 10**9, 1e10, float("inf"), 10**1000],
        ids=["at-go-max", "over-go-max", "infinite", "huge-integer"],
    )
    def test_unrepresentable_caller_timeout_is_rejected(self, caller_timeout):
        with pytest.raises(ValueError, match="caller deadline"):
            derive_print_timeout(caller_timeout)

    @pytest.mark.parametrize("seconds", [float("-inf"), -1e300, -1, 0, 0.001])
    def test_numeric_caps_have_a_useful_minimum(self, seconds):
        emitted = format_print_timeout(seconds)

        # Whatever the numeric paths emit has to survive the same guard an
        # explicitly supplied value passes through. Asserting the number alone
        # would not notice a dropped unit suffix, because int("1") is also 1.
        args = GeminiCodeRequest(prompt="hi", print_timeout=emitted).as_cmd_args()
        assert args[args.index("--print-timeout") + 1] == emitted
        assert emitted.endswith("s")
        assert int(emitted.removesuffix("s")) >= 1


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


class TestDefaultModel:
    """GeminiCodeRequest default model must be the latest flash family."""

    def test_default_model_is_gemini_3_5_flash(self):
        req = GeminiCodeRequest(prompt="hello")
        assert req.model == "gemini-3.5-flash"

    def test_explicit_model_is_preserved(self):
        req = GeminiCodeRequest(prompt="hello", model="gemini-2.5-pro")
        assert req.model == "gemini-2.5-pro"


class TestBackendsDefaultModel:
    """BACKENDS entries for gemini-cli must point to the latest flash family."""

    def test_gemini_cli_backend_uses_3_5_flash(self):
        from lionagi.service.providers import BACKENDS

        assert "gemini-3.5-flash" in BACKENDS["gemini-cli"]
        assert "gemini-3.5-flash" in BACKENDS["gemini_cli"]
        assert "gemini-3.5-flash" in BACKENDS["gemini-code"]
        assert "gemini-3.5-flash" in BACKENDS["gemini_code"]


class TestCliProvidersSet:
    """gemini_code, gemini-cli, gemini_cli, gemini-code must be in CLI_PROVIDERS."""

    def test_gemini_cli_in_cli_providers(self):
        from lionagi.service.providers import CLI_PROVIDERS

        for name in ("gemini_code", "gemini-code", "gemini_cli", "gemini-cli"):
            assert name in CLI_PROVIDERS, f"{name!r} not in CLI_PROVIDERS"


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
