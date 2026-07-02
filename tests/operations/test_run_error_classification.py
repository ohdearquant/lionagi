# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for run.py error-classification: StreamChunk type='error' raises typed ProviderError subclasses, all RuntimeError-compatible."""

from __future__ import annotations

import types
from unittest.mock import AsyncMock

import pytest

from lionagi.operations.run.run import RunParam, run
from lionagi.providers._provider_errors import (
    ProviderAuthError,
    ProviderContextError,
    ProviderError,
    ProviderQuotaError,
)
from lionagi.service.imodel import iModel
from lionagi.service.types.stream_chunk import StreamChunk
from lionagi.session.branch import Branch


def _make_fake_cli_model(chunks: list[StreamChunk]):
    """Return an iModel patched to behave as a CLI endpoint yielding *chunks*."""
    m = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")
    endpoint_ns = types.SimpleNamespace(
        is_cli=True,
        session_id=None,
        to_dict=lambda: {"type": "fake_cli", "session_id": None},
    )
    m.endpoint = endpoint_ns
    m.streaming_process_func = None

    async def create_event(**kw):
        return object()

    m.create_event = create_event
    m.executor = types.SimpleNamespace(append=AsyncMock(), config={})

    async def stream(api_call=None):
        for chunk in chunks:
            yield chunk

    m.stream = stream
    return m


async def _drain(gen) -> list:
    return [item async for item in gen]


# ---------------------------------------------------------------------------
# Quota error chunk → ProviderQuotaError raised (still a RuntimeError)
# ---------------------------------------------------------------------------


async def test_run_raises_quota_error_for_usage_limit_chunk():
    """A StreamChunk with quota-limit text must raise ProviderQuotaError."""
    quota_msg = "usage limit reached. try again at 9:00 PM"
    branch = Branch()
    branch.chat_model = _make_fake_cli_model([StreamChunk(type="error", content=quota_msg)])

    with pytest.raises(ProviderQuotaError, match="usage limit reached"):
        await _drain(run(branch, "do something", RunParam()))


async def test_run_quota_error_is_runtime_error():
    """ProviderQuotaError from run() must also be RuntimeError-catchable."""
    quota_msg = "rate_limit_exceeded: too many requests"
    branch = Branch()
    branch.chat_model = _make_fake_cli_model([StreamChunk(type="error", content=quota_msg)])

    with pytest.raises(RuntimeError):
        await _drain(run(branch, "do something", RunParam()))


# ---------------------------------------------------------------------------
# Auth error chunk → ProviderAuthError
# ---------------------------------------------------------------------------


async def test_run_raises_auth_error_for_invalid_key_chunk():
    auth_msg = "Error: invalid_api_key — check your credentials"
    branch = Branch()
    branch.chat_model = _make_fake_cli_model([StreamChunk(type="error", content=auth_msg)])

    with pytest.raises(ProviderAuthError):
        await _drain(run(branch, "do something", RunParam()))


# ---------------------------------------------------------------------------
# Context error chunk → ProviderContextError
# ---------------------------------------------------------------------------


async def test_run_raises_context_error_for_window_exceeded_chunk():
    ctx_msg = "context window exceeded — please shorten your prompt"
    branch = Branch()
    branch.chat_model = _make_fake_cli_model([StreamChunk(type="error", content=ctx_msg)])

    with pytest.raises(ProviderContextError):
        await _drain(run(branch, "do something", RunParam()))


# ---------------------------------------------------------------------------
# Unknown error chunk → base ProviderError (still RuntimeError)
# ---------------------------------------------------------------------------


async def test_run_raises_base_provider_error_for_unknown_chunk():
    unknown_msg = "An unexpected internal server error occurred"
    branch = Branch()
    branch.chat_model = _make_fake_cli_model([StreamChunk(type="error", content=unknown_msg)])

    with pytest.raises(ProviderError):
        await _drain(run(branch, "do something", RunParam()))


async def test_run_base_provider_error_is_runtime_error():
    branch = Branch()
    branch.chat_model = _make_fake_cli_model(
        [StreamChunk(type="error", content="some unknown provider failure")]
    )

    with pytest.raises(RuntimeError):
        await _drain(run(branch, "do something", RunParam()))


# ---------------------------------------------------------------------------
# Empty content → "(empty error)" guard, base ProviderError
# ---------------------------------------------------------------------------


async def test_run_empty_error_chunk_uses_fallback_string():
    """A StreamChunk(type='error', content=None) must not raise with a None message."""
    branch = Branch()
    branch.chat_model = _make_fake_cli_model([StreamChunk(type="error", content=None)])

    # The '(empty error)' guard in run.py means content becomes '(empty error)'
    with pytest.raises(ProviderError, match="empty error"):
        await _drain(run(branch, "do something", RunParam()))


# ---------------------------------------------------------------------------
# subprocess RuntimeError path → classified ProviderError
# ---------------------------------------------------------------------------


async def test_run_subprocess_runtime_error_is_classified():
    """A plain RuntimeError raised by the stream iterator (e.g. from
    ndjson_from_cli on nonzero exit) must be caught, classified, and
    re-raised as the appropriate ProviderError subclass with the original
    exception as __cause__."""
    quota_stderr = "usage limit reached. try again at 9:00 PM"
    branch = Branch()

    # Patch stream to raise RuntimeError instead of yielding chunks
    m = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")
    import types

    endpoint_ns = types.SimpleNamespace(
        is_cli=True,
        session_id=None,
        to_dict=lambda: {"type": "fake_cli", "session_id": None},
    )
    m.endpoint = endpoint_ns
    m.streaming_process_func = None

    async def create_event(**kw):
        return object()

    m.create_event = create_event
    m.executor = types.SimpleNamespace(append=AsyncMock(), config={})

    original_exc = RuntimeError(quota_stderr)

    async def stream_raises(api_call=None):
        raise original_exc
        # make it an async generator
        yield  # pragma: no cover

    m.stream = stream_raises
    branch.chat_model = m

    with pytest.raises(ProviderQuotaError, match="usage limit reached") as exc_info:
        await _drain(run(branch, "do something", RunParam()))

    # Original exception must be chained as __cause__
    assert exc_info.value.__cause__ is original_exc, (
        "classified ProviderError must chain the original RuntimeError as __cause__"
    )


async def test_run_already_classified_provider_error_not_double_wrapped():
    """A ProviderError raised by the stream iterator must NOT be re-wrapped
    — it must propagate unchanged (no double classification)."""
    original_exc = ProviderQuotaError("usage limit reached. try again at 9:00 PM")
    branch = Branch()

    import types

    m = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")
    endpoint_ns = types.SimpleNamespace(
        is_cli=True,
        session_id=None,
        to_dict=lambda: {"type": "fake_cli", "session_id": None},
    )
    m.endpoint = endpoint_ns
    m.streaming_process_func = None

    async def create_event(**kw):
        return object()

    m.create_event = create_event
    m.executor = types.SimpleNamespace(append=AsyncMock(), config={})

    async def stream_raises(api_call=None):
        raise original_exc
        yield  # pragma: no cover

    m.stream = stream_raises
    branch.chat_model = m

    with pytest.raises(ProviderQuotaError) as exc_info:
        await _drain(run(branch, "do something", RunParam()))

    # Must be the same object — no wrapping
    assert exc_info.value is original_exc, (
        "already-classified ProviderError must not be double-wrapped"
    )


# ---------------------------------------------------------------------------
# Late error must not destroy already-streamed text
# ---------------------------------------------------------------------------


async def test_run_error_after_text_persists_delivered_content():
    """Text the provider streamed before failing is flushed into the branch
    (and yielded) before the classified error is raised — a timeout after a
    complete final message must not destroy the delivered response."""
    branch = Branch()
    branch.chat_model = _make_fake_cli_model(
        [
            StreamChunk(type="text", content="the complete final answer"),
            StreamChunk(type="error", content="agy returned status=TIMEOUT"),
        ]
    )

    with pytest.raises(ProviderError, match="status=TIMEOUT"):
        await _drain(run(branch, "do something", RunParam()))

    responses = [m for m in branch.msgs.messages if type(m).__name__ == "AssistantResponse"]
    assert len(responses) == 1
    assert responses[0].response == "the complete final answer"


async def test_run_error_without_text_persists_nothing():
    """The flush-before-raise path is a no-op when no text was streamed."""
    branch = Branch()
    branch.chat_model = _make_fake_cli_model(
        [StreamChunk(type="error", content="hard failure, no content")]
    )

    with pytest.raises(ProviderError):
        await _drain(run(branch, "do something", RunParam()))

    responses = [m for m in branch.msgs.messages if type(m).__name__ == "AssistantResponse"]
    assert responses == []


# ---------------------------------------------------------------------------
# Regression: error:null chunk must NOT be swallowed as benign EOS
# (null normalised to {} matched the benign predicate before the fix)
# ---------------------------------------------------------------------------


async def test_run_raises_provider_error_for_null_error_payload_chunk():
    """StreamChunk(type='error', error=null) must raise ProviderError, not complete silently."""
    # This is the chunk that the adapter emits for {"type":"error","error":null}.
    # After the fix it is NOT benign_eos, so run() must raise ProviderError.
    # The self-describing content from the empty-payload branch matches the base
    # ProviderError classifier (no quota/auth/context pattern).
    null_error_chunk = StreamChunk(
        type="error",
        content="CLI failure (empty error payload; event type='error')",
        metadata={"type": "error", "error": None},
        # Crucially: benign_eos is NOT set
    )
    branch = Branch()
    branch.chat_model = _make_fake_cli_model([null_error_chunk])

    with pytest.raises(ProviderError):
        await _drain(run(branch, "do something", RunParam()))
