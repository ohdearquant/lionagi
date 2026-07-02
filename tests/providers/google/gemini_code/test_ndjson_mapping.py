# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Antigravity (`agy`) json-object result mapping.

The `agy --output-format json` transport emits one terminal object
(conversation_id, status, response, usage). These tests verify that object is
projected correctly onto a CLISession — response -> result, conversation_id ->
session_id (so it persists to state.db and native --conversation resume works),
usage/turns captured, and non-SUCCESS status flagged as an error.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from lionagi.providers.google.gemini_code import (
    GeminiCodeRequest,
    GeminiSession,
    resolve_agy_model,
    stream_gemini_cli,
)


def _make_request(**kw) -> GeminiCodeRequest:
    kw.setdefault("prompt", "test")
    kw.setdefault("verbose_output", False)
    return GeminiCodeRequest(**kw)


async def _run_objects(
    objects: list[dict], request: GeminiCodeRequest | None = None
) -> GeminiSession:
    """Drive stream_gemini_cli with a mocked agy object stream; return the final session."""

    async def fake_events(_request):
        for obj in objects:
            yield obj

    session = None
    with patch(
        "lionagi.providers.google.gemini_code.stream_gemini_cli_events",
        side_effect=fake_events,
    ):
        async for item in stream_gemini_cli(request or _make_request()):
            if isinstance(item, GeminiSession):
                session = item

    assert session is not None, "stream_gemini_cli did not yield a CLISession"
    return session


def _success_obj(**over) -> dict:
    obj = {
        "conversation_id": "conv-123",
        "status": "SUCCESS",
        "response": "The capital of France is Paris.\n",
        "duration_seconds": 1.5,
        "num_turns": 2,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "thinking_tokens": 5,
            "total_tokens": 125,
        },
    }
    obj.update(over)
    return obj


# ---------------------------------------------------------------------------
# Result projection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_response_becomes_result_stripped():
    session = await _run_objects([_success_obj()])
    assert session.result == "The capital of France is Paris."
    assert session.is_error is False


@pytest.mark.asyncio
async def test_conversation_id_persisted_as_session_id():
    """conversation_id must land on session.session_id so it reaches state.db and enables resume."""
    session = await _run_objects([_success_obj(conversation_id="abc-789")])
    assert session.session_id == "abc-789", (
        f"agy conversation_id must be stored as session_id; got {session.session_id!r}"
    )


@pytest.mark.asyncio
async def test_usage_and_turns_captured():
    session = await _run_objects([_success_obj()])
    assert session.usage["input_tokens"] == 100
    assert session.usage["output_tokens"] == 20
    assert session.usage["thinking_tokens"] == 5
    assert session.num_turns == 2


@pytest.mark.asyncio
async def test_duration_seconds_becomes_ms():
    session = await _run_objects([_success_obj(duration_seconds=2.0)])
    assert session.duration_ms == 2000


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_success_status_flagged_error():
    session = await _run_objects([_success_obj(status="ERROR", response="quota exceeded")])
    assert session.is_error is True
    assert "quota exceeded" in session.result


@pytest.mark.asyncio
async def test_no_object_flagged_error():
    """rc==0 but no parseable json object must not silently look like success."""
    session = await _run_objects([])
    assert session.is_error is True
    assert "no parseable" in session.result.lower()


@pytest.mark.asyncio
async def test_empty_success_response_not_error():
    session = await _run_objects([_success_obj(response="")])
    assert session.is_error is False
    assert session.result == ""


@pytest.mark.asyncio
async def test_session_id_not_clobbered_by_later_null():
    """obj.get('conversation_id') or session.session_id — a later object with no
    conversation_id must not erase a previously captured session id."""
    session = await _run_objects(
        [_success_obj(conversation_id="conv-first"), _success_obj(conversation_id=None)]
    )
    assert session.session_id == "conv-first"


# ---------------------------------------------------------------------------
# state.db persistence chunks (system / result) — the channel run.py reads
# session_id and provider usage metadata from during `li agent` streaming.
# ---------------------------------------------------------------------------


async def _run_chunks(objects: list[dict], request: GeminiCodeRequest | None = None) -> list:
    """Drive stream_gemini_cli and return every yielded StreamChunk (final CLISession excluded)."""

    async def fake_events(_request):
        for obj in objects:
            yield obj

    chunks = []
    with patch(
        "lionagi.providers.google.gemini_code.stream_gemini_cli_events",
        side_effect=fake_events,
    ):
        async for item in stream_gemini_cli(request or _make_request()):
            if not isinstance(item, GeminiSession):
                chunks.append(item)
    return chunks


@pytest.mark.asyncio
async def test_system_chunk_carries_session_id_and_model():
    chunks = await _run_chunks([_success_obj()])
    system_chunks = [c for c in chunks if c.type == "system"]
    assert len(system_chunks) == 1
    assert system_chunks[0].metadata["session_id"] == "conv-123"
    assert system_chunks[0].metadata["model"]


@pytest.mark.asyncio
async def test_result_chunk_carries_usage_turns_duration():
    chunks = await _run_chunks([_success_obj()])
    result_chunks = [c for c in chunks if c.type == "result"]
    assert len(result_chunks) == 1
    meta = result_chunks[0].metadata
    assert meta["conversation_id"] == "conv-123"
    assert meta["status"] == "SUCCESS"
    assert meta["usage"]["input_tokens"] == 100
    assert meta["num_turns"] == 2
    assert meta["duration_ms"] == 1500


@pytest.mark.asyncio
async def test_system_chunk_precedes_result_and_text_chunks():
    chunks = await _run_chunks([_success_obj()])
    types = [c.type for c in chunks]
    assert types.index("system") < types.index("text") < types.index("result")


@pytest.mark.asyncio
async def test_system_chunk_emitted_on_error_but_no_result_chunk():
    """A failed turn can still report a live conversation id — capture it — but
    a result chunk (usage/turns) only makes sense for a completed turn."""
    chunks = await _run_chunks(
        [_success_obj(status="ERROR", response="quota exceeded", conversation_id="conv-err")]
    )
    types = [c.type for c in chunks]
    assert "system" in types
    assert "result" not in types
    system_chunks = [c for c in chunks if c.type == "system"]
    assert system_chunks[0].metadata["session_id"] == "conv-err"


@pytest.mark.asyncio
async def test_no_system_chunk_when_no_session_id():
    chunks = await _run_chunks([_success_obj(conversation_id=None)])
    assert "system" not in [c.type for c in chunks]


@pytest.mark.asyncio
async def test_error_chunk_leads_with_status_not_response():
    """The error chunk must not impersonate the response: a degraded
    termination can carry a complete final message in ``response``, and
    surfacing that text bare as the error inverts success into failure."""
    chunks = await _run_chunks(
        [_success_obj(status="TIMEOUT", response="the complete final answer")]
    )
    error_chunks = [c for c in chunks if c.type == "error"]
    assert len(error_chunks) == 1
    assert error_chunks[0].content.startswith("agy returned status=TIMEOUT")
    # bounded detail retained so quota/auth patterns still classify
    assert "the complete final answer" in error_chunks[0].content


@pytest.mark.asyncio
async def test_error_chunk_without_response_names_status():
    chunks = await _run_chunks([_success_obj(status="FAILURE", response="")])
    error_chunks = [c for c in chunks if c.type == "error"]
    assert len(error_chunks) == 1
    assert error_chunks[0].content == "agy returned status=FAILURE"


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_text_and_on_final_fire():
    texts: list[str] = []
    finals: list[GeminiSession] = []

    async def fake_events(_request):
        yield _success_obj(response="hello world")

    with patch(
        "lionagi.providers.google.gemini_code.stream_gemini_cli_events",
        side_effect=fake_events,
    ):
        async for _ in stream_gemini_cli(
            _make_request(),
            on_text=lambda t: texts.append(t),
            on_final=lambda s: finals.append(s),
        ):
            pass

    assert texts == ["hello world"]
    assert len(finals) == 1
    assert finals[0].result == "hello world"


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("gemini-3-flash-preview", "Gemini 3.5 Flash (Medium)"),
        ("gemini-3-pro-preview", "Gemini 3.1 Pro (High)"),
        ("flash", "Gemini 3.5 Flash (Medium)"),
        ("pro", "Gemini 3.1 Pro (High)"),
        ("Gemini 3.5 Flash (High)", "Gemini 3.5 Flash (High)"),  # exact passthrough
        (None, "Gemini 3.5 Flash (Medium)"),
    ],
)
def test_resolve_agy_model(spec, expected):
    assert resolve_agy_model(spec) == expected


# ---------------------------------------------------------------------------
# Model resolution — effort folding (agy has no effort flag/kwarg; effort is
# expressed only as the Low/Medium/High suffix on --model)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec,effort,expected",
    [
        # lionagi effort clamps onto agy's Low/Medium/High tier for flash.
        ("gemini-3.5-flash", "low", "Gemini 3.5 Flash (Low)"),
        ("gemini-3.5-flash", "medium", "Gemini 3.5 Flash (Medium)"),
        ("gemini-3.5-flash", "high", "Gemini 3.5 Flash (High)"),
        # xhigh/max both clamp to High — agy has no tier above High.
        ("gemini-3.5-flash", "xhigh", "Gemini 3.5 Flash (High)"),
        ("gemini-3.5-flash", "max", "Gemini 3.5 Flash (High)"),
        # none/minimal both clamp to Low.
        ("gemini-3.5-flash", "none", "Gemini 3.5 Flash (Low)"),
        ("gemini-3.5-flash", "minimal", "Gemini 3.5 Flash (Low)"),
        # Gemini 3.1 Pro has no Medium tier — medium (and anything clamping to
        # Medium) bumps to High; Low and High pass through unchanged.
        ("gemini-3.1-pro", "low", "Gemini 3.1 Pro (Low)"),
        ("gemini-3.1-pro", "medium", "Gemini 3.1 Pro (High)"),
        ("gemini-3.1-pro", "high", "Gemini 3.1 Pro (High)"),
        ("gemini-3.1-pro", "max", "Gemini 3.1 Pro (High)"),
        # An exact (...)-qualified model is already a concrete agy display
        # name — it wins over effort rather than being reinterpreted.
        ("Gemini 3.5 Flash (Low)", "high", "Gemini 3.5 Flash (Low)"),
        ("Gemini 3.1 Pro (Low)", "high", "Gemini 3.1 Pro (Low)"),
        # No effort given: family default from _MODEL_ALIASES, unaffected.
        ("gemini-3.5-flash", None, "Gemini 3.5 Flash (Medium)"),
        ("gemini-3.1-pro", None, "Gemini 3.1 Pro (High)"),
    ],
)
def test_resolve_agy_model_effort_folding(spec, effort, expected):
    assert resolve_agy_model(spec, effort=effort) == expected


def test_resolve_agy_model_effort_ignored_for_cross_family_alias():
    """Claude/GPT-OSS routed through agy have no Low/Medium/High tiers —
    effort is accepted but has no suffix to fold into."""
    assert resolve_agy_model("opus", effort="high") == "Claude Opus 4.6 (Thinking)"
