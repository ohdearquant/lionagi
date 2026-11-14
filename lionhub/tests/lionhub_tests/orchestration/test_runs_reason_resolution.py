# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"Run teardown reason classification."

from __future__ import annotations

import pytest

from lionagi.state.reasons import VALID_REASON_CODES, RunReasons


class TestTeardownReasonResolution:
    """Smoke tests for the cli/agent.py _resolve_run_reason() helper.

    The full CLI path is tested in lionhub/tests/lionhub_tests/serve/cli/test_agent_*; here we only
    verify the mapping each terminal status produces is in the namespace.
    """

    def test_each_terminal_status_maps_to_a_valid_reason_code(self):
        from lionhub.orchestration.runs import resolve_run_reason as _resolve_run_reason

        cases = [
            ("completed", None),
            ("failed", RuntimeError("boom")),
            ("failed", None),  # bare failed with no exception
            ("timed_out", None),
            ("aborted", None),
            ("cancelled", None),
        ]
        for status, exc in cases:
            code, summary, evidence = _resolve_run_reason(
                status=status,
                exception=exc,
            )
            assert code in VALID_REASON_CODES, (
                f"({status}, {type(exc).__name__ if exc else 'None'}) -> "
                f"{code!r} not in VALID_REASON_CODES"
            )
            assert isinstance(summary, str) and summary, "summary must be non-empty"

    def test_failed_with_exception_embeds_class_name(self):
        from lionhub.orchestration.runs import resolve_run_reason as _resolve_run_reason

        _, summary, _ = _resolve_run_reason(
            status="failed",
            exception=ValueError("bad input"),
        )
        assert "ValueError" in summary
        assert "bad input" in summary

    def test_non_provider_exception_stays_generic_failed_exception(self):
        """A plain RuntimeError (not a classified ProviderError) must keep the
        generic bucket — only provider-classified failures get the retryable
        split below."""
        from lionhub.orchestration.runs import resolve_run_reason as _resolve_run_reason

        code, _, _ = _resolve_run_reason(status="failed", exception=RuntimeError("boom"))
        assert code == RunReasons.FAILED_EXCEPTION

    @pytest.mark.parametrize(
        "cls_name,message,expected",
        [
            ("ProviderQuotaError", "usage limit reached", "retryable"),
            ("ProviderCapacityError", "model is at capacity", "retryable"),
            ("ProviderStreamDisconnectError", "stream disconnected before completion", "retryable"),
            ("ProviderAdapterError", "agy returned status=ERROR", "retryable"),
            ("ProviderAuthError", "not logged in", "nonretryable"),
            ("ProviderContextError", "context window exceeded", "nonretryable"),
            ("ProviderUnsupportedModelError", "model is not supported", "nonretryable"),
            ("ProviderSafetyError", "flagged for cybersecurity risk", "nonretryable"),
            # The unclassified base class defaults to non-retryable.
            ("ProviderError", "something unrecognized went wrong", "nonretryable"),
        ],
    )
    def test_classified_provider_error_maps_to_retryable_reason_code(
        self, cls_name, message, expected
    ):
        """A terminal ProviderError subclass must carry a reason code that
        distinguishes retryable from non-retryable — the rank-2 reliability
        cohort's classification requirement (ADR-0057 vocabulary)."""
        import lionagi._provider_errors as provider_errors
        from lionhub.orchestration.runs import resolve_run_reason as _resolve_run_reason

        error_cls = getattr(provider_errors, cls_name)
        code, summary, _ = _resolve_run_reason(status="failed", exception=error_cls(message))

        expected_code = (
            RunReasons.FAILED_PROVIDER_RETRYABLE
            if expected == "retryable"
            else RunReasons.FAILED_PROVIDER_NONRETRYABLE
        )
        assert code == expected_code
        assert code in VALID_REASON_CODES
        assert cls_name in summary

    def test_a_provider_error_wrapped_by_the_model_stream_keeps_its_reason(self):
        """iModel re-raises every stream failure as a ValueError; the provider's typed error
        rides as its cause and decides the reason."""
        from lionagi._provider_errors import ProviderQuotaError
        from lionhub.orchestration.runs import resolve_run_reason as _resolve_run_reason

        try:
            try:
                raise ProviderQuotaError("Claude Code said: You've hit your session limit")
            except ProviderQuotaError as quota:
                raise ValueError(f"Failed to stream API call: {quota}") from quota
        except ValueError as wrapped:
            code, summary, _ = _resolve_run_reason(status="failed", exception=wrapped)

        assert code == RunReasons.FAILED_PROVIDER_RETRYABLE
        assert summary == "ProviderQuotaError: Claude Code said: You've hit your session limit"

        plain = ValueError("Failed to stream API call: boom")
        plain.__cause__ = RuntimeError("boom")
        assert (
            _resolve_run_reason(status="failed", exception=plain)[0] == RunReasons.FAILED_EXCEPTION
        )
