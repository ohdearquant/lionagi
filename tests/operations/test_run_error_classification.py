# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the run.py error-classification path (issue #1397).

Verifies that a StreamChunk with type="error" whose content contains a
provider-recognisable pattern raises the typed ProviderError subclass rather
than a plain RuntimeError.  All subclasses remain RuntimeError-compatible so
existing ``except RuntimeError`` callers are unaffected.
"""

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
