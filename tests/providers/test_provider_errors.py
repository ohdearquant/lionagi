# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for lionagi/providers/_provider_errors.py.

classify_provider_error matches quota/auth/context/capacity/unsupported-model/
safety/stream-disconnect/adapter patterns case-insensitively; unmatched text
returns base ProviderError; all subclasses are RuntimeError; EmissionError
attrs preserved; the retryable classification is grounded in real
`li agent`/`li o flow` failure-message signatures.
"""

from __future__ import annotations

import pytest

from lionagi.providers._provider_errors import (
    EmissionError,
    ProviderAdapterError,
    ProviderAuthError,
    ProviderCapacityError,
    ProviderContextError,
    ProviderError,
    ProviderQuotaError,
    ProviderSafetyError,
    ProviderStreamDisconnectError,
    ProviderUnsupportedModelError,
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


def test_classify_codex_ran_out_of_room_returns_context_error():
    """Real cohort signature: Codex's own context-overflow message never uses
    the exceeded/too-long wording — grounded in `li agent` telemetry."""
    err = classify_provider_error(
        "Codex ran out of room in the model's context window. Start a new "
        "thread or clear earlier history before retrying."
    )
    assert isinstance(err, ProviderContextError)
    assert err.retryable is False


# ---------------------------------------------------------------------------
# Quota — date-formatted retry time (real cohort gap: no digit after "at")
# ---------------------------------------------------------------------------


def test_classify_usage_limit_with_date_formatted_retry_time():
    err = classify_provider_error(
        "You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
        "to purchase more credits or try again at Jul 6th, 2026 11:08 PM."
    )
    assert isinstance(err, ProviderQuotaError)
    assert err.retryable is True


def test_classify_hit_your_usage_limit_returns_quota_error():
    err = classify_provider_error(
        "You've hit your usage limit for GPT-5.3-Codex-Spark. Switch to another model now."
    )
    assert isinstance(err, ProviderQuotaError)


# ---------------------------------------------------------------------------
# Capacity patterns
# ---------------------------------------------------------------------------


def test_classify_model_at_capacity_returns_capacity_error():
    err = classify_provider_error("Selected model is at capacity. Please try a different model.")
    assert isinstance(err, ProviderCapacityError)
    assert err.retryable is True


# ---------------------------------------------------------------------------
# Unsupported model/tool patterns
# ---------------------------------------------------------------------------


def test_classify_unsupported_model_for_account_returns_unsupported_error():
    err = classify_provider_error(
        '{"type":"error","status":400,"error":{"type":"invalid_request_error",'
        '"message":"The \'gpt-5.6\' model is not supported when using Codex '
        'with a ChatGPT account."}}'
    )
    assert isinstance(err, ProviderUnsupportedModelError)
    assert err.retryable is False


def test_classify_unsupported_tool_returns_unsupported_error():
    err = classify_provider_error(
        "Tool 'image_generation' is not supported with gpt-5.3-codex-spark-1p."
    )
    assert isinstance(err, ProviderUnsupportedModelError)


def test_stream_disconnect_mentioning_unsupported_reconnect_stays_retryable():
    err = classify_provider_error(
        "stream disconnected before completion; automatic reconnect is not supported by the proxy"
    )
    assert isinstance(err, ProviderStreamDisconnectError)
    assert err.retryable is True


def test_agy_wrapped_stream_disconnect_stays_retryable():
    err = classify_provider_error(
        "agy returned status=ERROR: stream disconnected before completion; "
        "automatic reconnect is not supported by the proxy"
    )
    assert isinstance(err, ProviderStreamDisconnectError)
    assert err.retryable is True


# ---------------------------------------------------------------------------
# Safety patterns
# ---------------------------------------------------------------------------


def test_classify_cybersecurity_safety_block_returns_safety_error():
    err = classify_provider_error(
        "This content was flagged for possible cybersecurity risk. If this "
        "seems wrong, try rephrasing your request."
    )
    assert isinstance(err, ProviderSafetyError)
    assert err.retryable is False


# ---------------------------------------------------------------------------
# Stream-disconnect patterns
# ---------------------------------------------------------------------------


def test_classify_stream_disconnected_returns_stream_disconnect_error():
    err = classify_provider_error(
        "Reconnecting... 2/5 (stream disconnected before completion: idle "
        "timeout waiting for websocket)"
    )
    assert isinstance(err, ProviderStreamDisconnectError)
    assert err.retryable is True


# ---------------------------------------------------------------------------
# Adapter catch-all (agy) — only fires when nothing more specific matches
# ---------------------------------------------------------------------------


def test_classify_agy_generic_error_status_returns_adapter_error():
    err = classify_provider_error("agy returned status=ERROR")
    assert isinstance(err, ProviderAdapterError)
    assert err.retryable is True


def test_classify_agy_status_with_substantial_content_returns_adapter_error():
    err = classify_provider_error(
        "agy returned status=ERROR: I will start by checking the git show of "
        "the specified commit to understand the changes."
    )
    assert isinstance(err, ProviderAdapterError)


def test_classify_agy_quota_message_still_wins_over_adapter_catchall():
    """A more specific pattern embedded in an agy payload must classify as
    that specific error, not fall through to the generic adapter bucket."""
    err = classify_provider_error("agy returned status=ERROR: hit your usage limit")
    assert isinstance(err, ProviderQuotaError)


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
# retryable classification — the machine-readable retry/non-retry split
# ---------------------------------------------------------------------------


def test_base_provider_error_defaults_to_non_retryable():
    assert ProviderError.retryable is False
    assert ProviderError("unclassified").retryable is False


@pytest.mark.parametrize(
    "cls",
    [
        ProviderQuotaError,
        ProviderCapacityError,
        ProviderStreamDisconnectError,
        ProviderAdapterError,
    ],
)
def test_transient_classes_are_retryable(cls):
    assert cls.retryable is True
    assert cls("msg").retryable is True


@pytest.mark.parametrize(
    "cls",
    [
        ProviderAuthError,
        ProviderContextError,
        ProviderUnsupportedModelError,
        ProviderSafetyError,
    ],
)
def test_permanent_classes_are_non_retryable(cls):
    assert cls.retryable is False
    assert cls("msg").retryable is False


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
