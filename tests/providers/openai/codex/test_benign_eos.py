# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Adapter-level tests for the benign_eos predicate in stream_codex_cli.

Covers the narrowing criteria from MAJOR 2 fix:
  1. error event with err={"code": "rate_limit"} (no "message") → NOT benign.
  2. turn.failed with empty error payload → NOT benign (turn.failed is never benign).
  3. error event with err={} (empty dict) → benign (resume-EOF sentinel).
"""

from __future__ import annotations

import pytest

from lionagi.providers.openai.codex.models import CodexCodeRequest, stream_codex_cli
from lionagi.service.types.stream_chunk import StreamChunk


def _make_request() -> CodexCodeRequest:
    return CodexCodeRequest(prompt="test", verbose_output=False)


async def _chunks_from_events(events: list[dict]) -> list[StreamChunk]:
    """Drive stream_codex_cli with a mocked event stream, collect StreamChunks."""
    from unittest.mock import patch

    async def fake_events(request):
        for ev in events:
            yield ev

    collected = []
    with patch(
        "lionagi.providers.openai.codex.models.stream_codex_cli_events",
        side_effect=fake_events,
    ):
        async for item in stream_codex_cli(_make_request()):
            if isinstance(item, StreamChunk):
                collected.append(item)
    return collected


# ---------------------------------------------------------------------------
# Test 1: error event with non-empty error (rate_limit) → NOT benign
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_event_with_rate_limit_code_is_not_benign():
    """An 'error' event whose error dict has a code but no message must NOT be
    tagged benign — only a completely empty error payload qualifies.

    Shape: {"type": "error", "error": {"code": "rate_limit"}}
    The error dict has a truthy "code" value, so any(err.values()) is True → not benign.
    """
    events = [{"type": "error", "error": {"code": "rate_limit"}}]
    chunks = await _chunks_from_events(events)

    assert len(chunks) == 1, f"expected 1 error chunk, got {len(chunks)}"
    error_chunk = chunks[0]
    assert error_chunk.type == "error"
    assert not error_chunk.metadata.get("benign_eos"), (
        f"rate_limit error must NOT be marked benign_eos; metadata={error_chunk.metadata}"
    )


# ---------------------------------------------------------------------------
# Test 2: turn.failed with empty error payload → NOT benign
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_failed_with_empty_error_is_not_benign():
    """A 'turn.failed' event is NEVER benign, even with an empty error payload.

    turn.failed signals an explicit model-side failure; it must always surface
    as an error so the caller can handle it (RunFailed signal, retry logic, etc.)
    """
    events = [{"type": "turn.failed", "error": {}}]
    chunks = await _chunks_from_events(events)

    assert len(chunks) == 1, f"expected 1 error chunk, got {len(chunks)}"
    error_chunk = chunks[0]
    assert error_chunk.type == "error"
    assert not error_chunk.metadata.get("benign_eos"), (
        "turn.failed must NEVER be marked benign_eos regardless of error payload; "
        f"metadata={error_chunk.metadata}"
    )


# ---------------------------------------------------------------------------
# Test 3: error event with completely empty error dict → benign EOS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_event_with_empty_error_dict_is_benign_eos():
    """An 'error' event with err={} is the true resume-EOF sentinel shape.

    This must be tagged benign_eos=True so run() terminates cleanly rather
    than propagating a spurious RunFailed signal.
    """
    # A bare error event with an empty error payload — the resume-EOF sentinel.
    events = [{"type": "error", "error": {}}]
    chunks = await _chunks_from_events(events)

    error_chunks = [c for c in chunks if c.type == "error"]
    assert len(error_chunks) == 1, f"expected 1 error chunk, got {error_chunks}"
    assert error_chunks[0].metadata.get("benign_eos") is True, (
        "empty-error 'error' event must be tagged benign_eos=True; "
        f"metadata={error_chunks[0].metadata}"
    )


# ---------------------------------------------------------------------------
# Round-3 shapes: structured-but-falsy error payloads are REAL failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_event_with_empty_message_is_not_benign():
    """{"error": {"message": ""}} is a real failure whose detail happens to be
    empty — the payload is not the bare {} sentinel, so it must surface."""
    events = [{"type": "error", "error": {"message": ""}}]
    chunks = await _chunks_from_events(events)

    assert len(chunks) == 1
    assert chunks[0].type == "error"
    assert not chunks[0].metadata.get("benign_eos")


@pytest.mark.asyncio
async def test_error_event_with_empty_message_and_toplevel_code_is_not_benign():
    """{"error": {"message": ""}, "code": "rate_limit"} — falsy payload plus a
    top-level failure indicator must surface as a real error (codex round-3
    repro: this shape was previously tagged benign_eos)."""
    events = [{"type": "error", "error": {"message": ""}, "code": "rate_limit"}]
    chunks = await _chunks_from_events(events)

    assert len(chunks) == 1
    assert chunks[0].type == "error"
    assert not chunks[0].metadata.get("benign_eos")


@pytest.mark.asyncio
async def test_error_event_with_none_message_is_not_benign():
    """{"error": {"message": None}} is structured (non-empty dict) and must
    surface as a real error."""
    events = [{"type": "error", "error": {"message": None}}]
    chunks = await _chunks_from_events(events)

    assert len(chunks) == 1
    assert chunks[0].type == "error"
    assert not chunks[0].metadata.get("benign_eos")


@pytest.mark.asyncio
async def test_error_event_with_toplevel_code_and_empty_error_is_not_benign():
    """Bare {} error payload but a top-level failure indicator ("code") —
    outside the known EOF envelope, must surface as a real error."""
    events = [{"type": "error", "error": {}, "code": "rate_limit"}]
    chunks = await _chunks_from_events(events)

    assert len(chunks) == 1
    assert chunks[0].type == "error"
    assert not chunks[0].metadata.get("benign_eos")


@pytest.mark.asyncio
async def test_error_event_without_error_key_is_not_benign():
    """A bare {"type": "error"} with no error key at all is malformed, not the
    EOF sentinel — the sentinel is an explicit empty error object (codex
    round-4 suggestion)."""
    events = [{"type": "error"}]
    chunks = await _chunks_from_events(events)

    assert len(chunks) == 1
    assert chunks[0].type == "error"
    assert not chunks[0].metadata.get("benign_eos")


@pytest.mark.asyncio
async def test_error_event_with_toplevel_message_surfaces_the_message():
    """An "error" event carrying its message at the TOP LEVEL (no "error" key)
    — the shape the codex CLI emits for usage-limit errors — must surface the
    actual message as chunk content, not the str() of an empty error dict.

    Real-world shape:
        {"type": "error", "message": "You've hit your usage limit. ..."}
    """
    msg = (
        "You've hit your usage limit. Visit "
        "https://chatgpt.com/codex/settings/usage to purchase more credits."
    )
    events = [{"type": "error", "message": msg}]
    chunks = await _chunks_from_events(events)

    assert len(chunks) == 1
    error_chunk = chunks[0]
    assert error_chunk.type == "error"
    assert not error_chunk.metadata.get("benign_eos")
    assert error_chunk.content == msg, (
        f"actionable message discarded: content={error_chunk.content!r}"
    )


@pytest.mark.asyncio
async def test_turn_failed_with_nested_message_still_preferred_over_toplevel():
    """turn.failed keeps its nested error.message as the surfaced content even
    when a top-level "message" is also present — the nested one is the
    provider's canonical detail for that event type."""
    events = [
        {
            "type": "turn.failed",
            "message": "outer",
            "error": {"message": "inner detail"},
        }
    ]
    chunks = await _chunks_from_events(events)

    assert len(chunks) == 1
    assert chunks[0].type == "error"
    assert not chunks[0].metadata.get("benign_eos")
    assert chunks[0].content == "inner detail"
