# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for lionagi/providers/_provider_errors.py.

classify_provider_error matches quota/auth/context patterns case-insensitively; unmatched text returns base ProviderError; all subclasses are RuntimeError; EmissionError attrs preserved.
"""

from __future__ import annotations

import pytest

from lionagi.providers._provider_errors import (
    EmissionError,
    ProviderAuthError,
    ProviderContextError,
    ProviderError,
    ProviderQuotaError,
    classify_provider_error,
)

# ---------------------------------------------------------------------------
# Quota patterns
# ---------------------------------------------------------------------------


def test_classify_usage_limit_reached_returns_quota_error():
    err = classify_provider_error("You've hit your usage limit reached. Please wait.")
    assert isinstance(err, ProviderQuotaError)


def test_classify_rate_limit_exceeded_returns_quota_error():
    err = classify_provider_error("rate_limit_exceeded: too many requests")
    assert isinstance(err, ProviderQuotaError)


def test_classify_rate_limit_space_sep_returns_quota_error():
    err = classify_provider_error("Rate limit exceeded on this endpoint")
    assert isinstance(err, ProviderQuotaError)


def test_classify_try_again_at_hour_returns_quota_error():
    err = classify_provider_error("try again at 8:32 PM")
    assert isinstance(err, ProviderQuotaError)


def test_classify_quota_case_insensitive_upper():
    err = classify_provider_error("USAGE LIMIT REACHED")
    assert isinstance(err, ProviderQuotaError)


def test_classify_quota_in_stderr_tail():
    err = classify_provider_error(
        "CLI failure (empty error payload; event type='error')",
        stderr_tail="usage limit reached. try again at 9:00 PM",
    )
    assert isinstance(err, ProviderQuotaError)


# ---------------------------------------------------------------------------
# Auth patterns
# ---------------------------------------------------------------------------


def test_classify_invalid_api_key_returns_auth_error():
    err = classify_provider_error("Error: invalid_api_key provided")
    assert isinstance(err, ProviderAuthError)


def test_classify_invalid_api_key_space_sep():
    err = classify_provider_error("invalid api key: please check your credentials")
    assert isinstance(err, ProviderAuthError)


def test_classify_not_logged_in_returns_auth_error():
    err = classify_provider_error("not logged in — run `codex login`")
    assert isinstance(err, ProviderAuthError)


def test_classify_401_unauthorized_returns_auth_error():
    err = classify_provider_error("401: Unauthorized access denied")
    assert isinstance(err, ProviderAuthError)


def test_classify_auth_case_insensitive():
    err = classify_provider_error("INVALID API KEY")
    assert isinstance(err, ProviderAuthError)


# ---------------------------------------------------------------------------
# Context-length patterns
# ---------------------------------------------------------------------------


def test_classify_context_window_exceeded_returns_context_error():
    err = classify_provider_error("context window exceeded — please shorten your prompt")
    assert isinstance(err, ProviderContextError)


def test_classify_context_length_too_long_returns_context_error():
    err = classify_provider_error("context length too long for this model")
    assert isinstance(err, ProviderContextError)


def test_classify_context_case_insensitive():
    err = classify_provider_error("CONTEXT WINDOW EXCEEDED")
    assert isinstance(err, ProviderContextError)


# ---------------------------------------------------------------------------
# Unmatched → base ProviderError (not a subclass)
# ---------------------------------------------------------------------------


def test_classify_unknown_returns_base_provider_error():
    err = classify_provider_error("Some random failure with no known pattern")
    assert type(err) is ProviderError


def test_classify_empty_string_returns_base_provider_error():
    err = classify_provider_error("")
    assert type(err) is ProviderError


# ---------------------------------------------------------------------------
# All classes are RuntimeError-compatible
# ---------------------------------------------------------------------------


def test_provider_error_is_runtime_error():
    err = ProviderError("msg")
    assert isinstance(err, RuntimeError)


def test_quota_error_is_runtime_error():
    err = ProviderQuotaError("quota exceeded")
    assert isinstance(err, RuntimeError)


def test_auth_error_is_runtime_error():
    err = ProviderAuthError("bad key")
    assert isinstance(err, RuntimeError)


def test_context_error_is_runtime_error():
    err = ProviderContextError("too long")
    assert isinstance(err, RuntimeError)


def test_emission_error_is_runtime_error():
    err = EmissionError("missing", agent="planner", attempts=2, stage="synthesis")
    assert isinstance(err, RuntimeError)


def test_classify_result_is_runtime_error():
    err = classify_provider_error("usage limit reached")
    assert isinstance(err, RuntimeError)


# ---------------------------------------------------------------------------
# Attrs preserved correctly
# ---------------------------------------------------------------------------


def test_provider_error_attrs_stored():
    err = ProviderError("the message", stderr_tail="tail text", raw="raw text")
    assert str(err) == "the message"
    assert err.stderr_tail == "tail text"
    assert err.raw == "raw text"


def test_provider_error_raw_defaults_to_message():
    err = ProviderError("only a message")
    assert err.raw == "only a message"


def test_emission_error_attrs_stored():
    err = EmissionError("no emission", agent="synthesiser", attempts=3, stage="final")
    assert err.agent == "synthesiser"
    assert err.attempts == 3
    assert err.stage == "final"
    assert str(err) == "no emission"
