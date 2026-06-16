# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the benign_eos predicate in stream_codex_cli.

Only {"type":"error","error":{}} (empty dict) is the resume-EOF sentinel; non-empty error dicts and turn.failed are always real failures.
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
    """{"error": {"code": "rate_limit"}} has a truthy value so any(err.values()) is True — not benign."""
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
    """turn.failed signals an explicit model failure and must never be marked benign_eos, even with an empty error payload."""
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
    """{"error":{}} is the resume-EOF sentinel and must be tagged benign_eos=True so run() terminates cleanly."""
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
    """{"error": {"message": ""}} is not the bare {} sentinel; must surface as a real error."""
    events = [{"type": "error", "error": {"message": ""}}]
    chunks = await _chunks_from_events(events)

    assert len(chunks) == 1
    assert chunks[0].type == "error"
    assert not chunks[0].metadata.get("benign_eos")


@pytest.mark.asyncio
async def test_error_event_with_empty_message_and_toplevel_code_is_not_benign():
    """Falsy payload plus top-level "code" indicator must surface as a real error."""
    events = [{"type": "error", "error": {"message": ""}, "code": "rate_limit"}]
    chunks = await _chunks_from_events(events)

    assert len(chunks) == 1
    assert chunks[0].type == "error"
    assert not chunks[0].metadata.get("benign_eos")


@pytest.mark.asyncio
async def test_error_event_with_none_message_is_not_benign():
    """{"error": {"message": None}} is a non-empty dict (structured), not the {} sentinel."""
    events = [{"type": "error", "error": {"message": None}}]
    chunks = await _chunks_from_events(events)

    assert len(chunks) == 1
    assert chunks[0].type == "error"
    assert not chunks[0].metadata.get("benign_eos")


@pytest.mark.asyncio
async def test_error_event_with_toplevel_code_and_empty_error_is_not_benign():
    """Bare {} error payload plus a top-level "code" key is outside the EOF envelope; must surface as real error."""
    events = [{"type": "error", "error": {}, "code": "rate_limit"}]
    chunks = await _chunks_from_events(events)

    assert len(chunks) == 1
    assert chunks[0].type == "error"
    assert not chunks[0].metadata.get("benign_eos")


@pytest.mark.asyncio
async def test_error_event_without_error_key_is_not_benign():
    """{"type":"error"} with no error key is malformed, not the EOF sentinel (which requires an explicit empty {} object)."""
    events = [{"type": "error"}]
    chunks = await _chunks_from_events(events)

    assert len(chunks) == 1
    assert chunks[0].type == "error"
    assert not chunks[0].metadata.get("benign_eos")


@pytest.mark.asyncio
async def test_error_event_with_toplevel_message_surfaces_the_message():
    """Top-level "message" key (real codex CLI usage-limit shape) must surface as chunk content, not str({})."""
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
    """turn.failed uses nested error.message as canonical content even when a top-level "message" is also present."""
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


# ---------------------------------------------------------------------------
# Round-3 regression: "error": null must NOT be treated as benign EOS
# (fix/cli-worker-error-surfacing regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_event_null_payload_is_not_benign():
    """Regression: null→{} normalisation caused {"error":null} to match the empty-dict sentinel and be swallowed silently."""
    events = [{"type": "error", "error": None}]
    chunks = await _chunks_from_events(events)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.type == "error"
    assert not chunk.metadata.get("benign_eos"), (
        "error:null must NOT be tagged benign_eos — it is a malformed envelope, "
        f"not the resume-EOF sentinel; metadata={chunk.metadata}"
    )
    # The content must be self-describing (null normalised to {} hits the empty-
    # payload branch), not the raw string "None".
    assert chunk.content != "None", (
        f"null error payload must not render as the string 'None'; got: {chunk.content!r}"
    )
    assert "turn.failed" not in chunk.content or "error" in chunk.content, (
        "self-describing fallback message should name the event type"
    )


@pytest.mark.asyncio
async def test_error_event_empty_dict_is_still_benign_after_null_fix():
    """{"error":{}} must remain benign EOS after the null-normalisation fix — the original sentinel must not break."""
    events = [{"type": "error", "error": {}}]
    chunks = await _chunks_from_events(events)

    error_chunks = [c for c in chunks if c.type == "error"]
    assert len(error_chunks) == 1
    assert error_chunks[0].metadata.get("benign_eos") is True, (
        "explicit empty-dict error payload must still be tagged benign_eos=True; "
        f"metadata={error_chunks[0].metadata}"
    )
