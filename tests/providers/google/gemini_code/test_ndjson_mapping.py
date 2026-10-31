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

from lionagi._provider_errors import (
    ProviderTeardownError,
    classify_provider_error,
)
from lionagi.service.token_budget import lookup_context_window
from lionhub.providers.google.gemini_code import (
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
        "lionhub.providers.google.gemini_code.stream_gemini_cli_events",
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
async def test_empty_success_response_is_an_error():
    """A SUCCESS carrying no content must not read as a successful empty answer.

    Headless print mode cannot prompt for a tool permission, so it auto-denies
    the call and still reports SUCCESS with an empty response. Passing that
    through as a completed-but-empty turn fails open: a caller using this engine
    for a second opinion receives silence stamped success and reads it as assent.
    """
    session = await _run_objects([_success_obj(response="")])
    assert session.is_error is True
    assert session.result


@pytest.mark.asyncio
async def test_empty_success_error_names_the_permission_remedy():
    """The error has to be actionable — the operator needs the remedy, not just a flag."""
    chunks = await _run_chunks([_success_obj(response="")])
    error_chunks = [c for c in chunks if c.type == "error"]
    assert len(error_chunks) == 1
    content = error_chunks[0].content.lower()
    assert "no response content" in content
    assert "permission" in content


@pytest.mark.asyncio
async def test_empty_success_emits_no_text_or_result_chunk():
    """An empty text chunk downstream is exactly what made this look like a real answer."""
    chunks = await _run_chunks([_success_obj(response="")])
    types = [c.type for c in chunks]
    assert "text" not in types
    assert "result" not in types


@pytest.mark.asyncio
async def test_error_object_surfaces_its_error_field():
    """agy reports failures in `error` while leaving `response` empty; a bare
    status line drops the only text that says what actually went wrong."""
    chunks = await _run_chunks(
        [
            _success_obj(
                status="ERROR",
                response="",
                error='invalid model selection (--model "gemini-3.9-flash")',
            )
        ]
    )
    error_chunks = [c for c in chunks if c.type == "error"]
    assert len(error_chunks) == 1
    assert "invalid model selection" in error_chunks[0].content


@pytest.mark.asyncio
async def test_session_id_not_clobbered_by_later_null():
    """obj.get('conversation_id') or session.session_id — a later object with no
    conversation_id must not erase a previously captured session id."""
    session = await _run_objects(
        [_success_obj(conversation_id="conv-first"), _success_obj(conversation_id=None)]
    )
    assert session.session_id == "conv-first"


# state.db persistence chunks (system / result) — the channel run.py reads
# session_id and provider usage metadata from during `li agent` streaming.


async def _run_chunks(objects: list[dict], request: GeminiCodeRequest | None = None) -> list:
    """Drive stream_gemini_cli and return every yielded StreamChunk (final CLISession excluded)."""

    async def fake_events(_request):
        for obj in objects:
            yield obj

    chunks = []
    with patch(
        "lionhub.providers.google.gemini_code.stream_gemini_cli_events",
        side_effect=fake_events,
    ):
        async for item in stream_gemini_cli(request or _make_request()):
            if not isinstance(item, GeminiSession):
                chunks.append(item)
    return chunks


@pytest.mark.asyncio
async def test_system_chunk_carries_session_id_and_no_model_agy_did_not_report():
    """The system chunk is where a served model is read from, so it carries
    only a model agy itself reported, never the requested one."""
    request = _make_request(model="gemini-3.8-flash")
    chunks = await _run_chunks([_success_obj()], request)
    system_chunks = [c for c in chunks if c.type == "system"]
    assert len(system_chunks) == 1
    assert system_chunks[0].metadata["session_id"] == "conv-123"
    assert system_chunks[0].metadata["model"] is None
    session = await _run_objects([_success_obj()], request)
    assert session.model is None


@pytest.mark.asyncio
async def test_system_chunk_carries_the_model_agy_reported():
    chunks = await _run_chunks([_success_obj(model="gemini-served-x")])
    system_chunks = [c for c in chunks if c.type == "system"]
    assert system_chunks[0].metadata["model"] == "gemini-served-x"


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", [None, "", "   ", "\n\t", 42, ["gemini-x"], {"name": "x"}])
async def test_a_model_field_that_is_not_a_name_is_not_recorded(reported):
    request = _make_request(model="gemini-3.8-flash")
    chunks = await _run_chunks([_success_obj(model=reported)], request)
    system_chunks = [c for c in chunks if c.type == "system"]
    assert system_chunks[0].metadata["model"] is None
    result_chunks = [c for c in chunks if c.type == "result"]
    assert result_chunks[0].metadata["model"] == resolve_agy_model("gemini-3.8-flash")


@pytest.mark.asyncio
async def test_a_reported_name_is_recorded_without_surrounding_whitespace():
    chunks = await _run_chunks([_success_obj(model="  gemini-served-x\n")])
    system_chunks = [c for c in chunks if c.type == "system"]
    assert system_chunks[0].metadata["model"] == "gemini-served-x"
    session = await _run_objects([_success_obj(model="  gemini-served-x\n")])
    assert session.model == "gemini-served-x"


@pytest.mark.asyncio
async def test_a_model_reported_without_a_conversation_id_is_on_a_system_chunk():
    """The stream and the invoke result name the same model."""
    objects = [_success_obj(conversation_id=None, model="gemini-served-x")]
    chunks = await _run_chunks(objects)
    system_chunks = [c for c in chunks if c.type == "system"]
    assert [c.metadata["model"] for c in system_chunks] == ["gemini-served-x"]
    assert system_chunks[0].metadata["session_id"] is None
    session = await _run_objects(objects)
    assert session.model == "gemini-served-x"


@pytest.mark.asyncio
async def test_an_object_that_names_no_model_keeps_the_last_one_reported():
    objects = [_success_obj(model="gemini-A"), _success_obj()]
    chunks = await _run_chunks(objects)
    system_chunks = [c for c in chunks if c.type == "system"]
    assert [c.metadata["model"] for c in system_chunks] == ["gemini-A", "gemini-A"]
    session = await _run_objects(objects)
    assert session.model == "gemini-A"


@pytest.mark.asyncio
async def test_a_failed_turn_names_no_model():
    """The model a failed turn names produced no output, so it is not read as served."""
    objects = [_success_obj(status="FAILED", model="gemini-failed-x", error="quota")]
    chunks = await _run_chunks(objects)
    system_chunks = [c for c in chunks if c.type == "system"]
    assert system_chunks[0].metadata["session_id"] == "conv-123"
    assert system_chunks[0].metadata["model"] is None
    assert [c.type for c in chunks].count("error") == 1
    session = await _run_objects(objects)
    assert session.model is None


@pytest.mark.asyncio
async def test_a_failed_turn_clears_the_model_an_earlier_turn_reported():
    objects = [
        _success_obj(model="gemini-A"),
        _success_obj(status="FAILED", model="gemini-failed-x", error="quota"),
    ]
    chunks = await _run_chunks(objects)
    system_chunks = [c for c in chunks if c.type == "system"]
    assert [c.metadata["model"] for c in system_chunks] == ["gemini-A", None]
    session = await _run_objects(objects)
    assert session.is_error
    assert session.model is None


@pytest.mark.asyncio
async def test_result_record_keeps_the_requested_model_when_agy_reports_none():
    """The run record read from the result chunk still names a model; result
    chunks are never read as the served model."""
    request = _make_request(model="gemini-3.8-flash")
    chunks = await _run_chunks([_success_obj()], request)
    result_chunks = [c for c in chunks if c.type == "result"]
    assert result_chunks[0].metadata["model"] == resolve_agy_model("gemini-3.8-flash")


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


@pytest.mark.asyncio
async def test_loop_closed_during_teardown_preserves_text_and_is_retryable():
    async def events_then_teardown_failure(_request):
        yield _success_obj(response="completed review")
        raise RuntimeError("Event loop is closed")

    yielded = []
    with patch(
        "lionhub.providers.google.gemini_code.stream_gemini_cli_events",
        side_effect=events_then_teardown_failure,
    ):
        async for item in stream_gemini_cli(_make_request()):
            yielded.append(item)

    text_chunks = [item for item in yielded if getattr(item, "type", None) == "text"]
    error_chunks = [item for item in yielded if getattr(item, "type", None) == "error"]
    session = next(item for item in yielded if isinstance(item, GeminiSession))

    assert [chunk.content for chunk in text_chunks] == ["completed review"]
    assert session.result == "completed review"
    assert session.is_error is True
    assert len(error_chunks) == 1
    assert yielded.index(text_chunks[0]) < yielded.index(error_chunks[0])
    error = classify_provider_error(error_chunks[0].content)
    assert isinstance(error, ProviderTeardownError)
    assert error.retryable is True


@pytest.mark.asyncio
async def test_a_teardown_failure_after_a_reported_model_clears_it():
    async def events_then_teardown_failure(_request):
        yield _success_obj(model="gemini-A")
        raise RuntimeError("Event loop is closed")

    with patch(
        "lionhub.providers.google.gemini_code.stream_gemini_cli_events",
        side_effect=events_then_teardown_failure,
    ):
        yielded = [item async for item in stream_gemini_cli(_make_request())]

    session = next(item for item in yielded if isinstance(item, GeminiSession))
    assert session.is_error is True
    assert session.model is None


@pytest.mark.asyncio
async def test_loop_closed_before_output_emits_one_retryable_error():
    async def teardown_failure(_request):
        raise RuntimeError("Event loop is closed")
        yield

    yielded = []
    with patch(
        "lionhub.providers.google.gemini_code.stream_gemini_cli_events",
        side_effect=teardown_failure,
    ):
        async for item in stream_gemini_cli(_make_request()):
            yielded.append(item)

    assert [getattr(item, "type", None) for item in yielded] == ["error", None]
    error = classify_provider_error(yielded[0].content)
    assert isinstance(error, ProviderTeardownError)
    assert error.retryable is True
    assert isinstance(yielded[1], GeminiSession)
    assert yielded[1].is_error is True


@pytest.mark.asyncio
async def test_unrelated_runtime_error_is_not_reclassified_as_teardown():
    async def failing_events(_request):
        raise RuntimeError("parser invariant failed")
        yield

    with (
        patch(
            "lionhub.providers.google.gemini_code.stream_gemini_cli_events",
            side_effect=failing_events,
        ),
        pytest.raises(RuntimeError, match="parser invariant failed"),
    ):
        async for _ in stream_gemini_cli(_make_request()):
            pass


@pytest.mark.asyncio
async def test_on_text_and_on_final_fire():
    texts: list[str] = []
    finals: list[GeminiSession] = []

    async def fake_events(_request):
        yield _success_obj(response="hello world")

    with patch(
        "lionhub.providers.google.gemini_code.stream_gemini_cli_events",
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


@pytest.mark.asyncio
async def test_on_text_loop_closed_error_propagates():
    async def fake_events(_request):
        yield _success_obj(response="hello world")

    def failing_callback(_text):
        raise RuntimeError("Event loop is closed")

    with (
        patch(
            "lionhub.providers.google.gemini_code.stream_gemini_cli_events",
            side_effect=fake_events,
        ),
        pytest.raises(RuntimeError, match="Event loop is closed"),
    ):
        async for _ in stream_gemini_cli(
            _make_request(),
            on_text=failing_callback,
        ):
            pass


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("gemini-3-flash-preview", "Gemini 3.5 Flash (Medium)"),
        ("gemini-3-pro-preview", "Gemini 3.1 Pro (High)"),
        ("flash", "Gemini 3.8 Flash (Medium)"),
        ("pro", "Gemini 3.1 Pro (High)"),
        ("gemini-pro", "Gemini 3.1 Pro (High)"),
        ("Gemini 3.5 Flash (High)", "Gemini 3.5 Flash (High)"),  # exact passthrough
        (None, "Gemini 4 Argon (High)"),
        ("gemini-4-argon", "Gemini 4 Argon (High)"),
        ("gemini-4", "Gemini 4 Argon (High)"),
        ("gemini-4-argon-low", "Gemini 4 Argon (Low)"),
        ("gemini-4-argon-medium", "Gemini 4 Argon (Medium)"),
        ("Gemini 4 Argon (Low)", "Gemini 4 Argon (Low)"),  # exact passthrough
        ("argon", "Gemini 4 Argon (High)"),
        ("ARGON", "Gemini 4 Argon (High)"),
        ("argon-low", "Gemini 4 Argon (Low)"),
        ("argon-medium", "Gemini 4 Argon (Medium)"),
        ("argon-high", "Gemini 4 Argon (High)"),
        # a free-form name with argon in it is on the 4 family by its name
        ("gemini-4-argon-preview", "Gemini 4 Argon (Medium)"),
        ("gemini-argon-high", "Gemini 4 Argon (High)"),
        ("gemini-14-argon", "Gemini 4 Argon (Medium)"),
        # argon alone is enough, and a tier word in the name picks the tier
        ("argon high", "Gemini 4 Argon (High)"),
        ("Argon High", "Gemini 4 Argon (High)"),
        ("argon-max", "Gemini 4 Argon (High)"),
        ("argon-4-high", "Gemini 4 Argon (High)"),
        ("argon_high", "Gemini 4 Argon (High)"),
        ("argon-xhigh", "Gemini 4 Argon (High)"),
        # a tier inside a longer word is no tier
        ("argon-highlights", "Gemini 4 Argon (Medium)"),
        ("argon-maxim", "Gemini 4 Argon (Medium)"),
        ("argon-allow", "Gemini 4 Argon (Medium)"),
        ("argon-lowrider", "Gemini 4 Argon (Medium)"),
        # ...in any script, while a separator ends the word
        ("argon-lowé", "Gemini 4 Argon (Medium)"),
        ("argonélow", "Gemini 4 Argon (Medium)"),
        ("gemini-highé", "Gemini 3.5 Flash (Medium)"),
        ("argon-low-é", "Gemini 4 Argon (Low)"),
        # a decimal digit next to it is no letter; a numeral that is not one counts as a letter
        ("argon-low2", "Gemini 4 Argon (Low)"),
        ("argon-low\u00b2", "Gemini 4 Argon (Medium)"),
        # a combining mark is neither, so it ends the word; composed, the letter does not
        ("argon-low\u0301", "Gemini 4 Argon (Low)"),
        ("argon-lo\u1e83", "Gemini 4 Argon (Medium)"),
        ("gemini-flash-lowest", "Gemini 3.5 Flash (Medium)"),
        ("gemini-3.8-flash-maximum", "Gemini 3.8 Flash (Medium)"),
        ("gemini-allow-high", "Gemini 3.5 Flash (High)"),
        ("gemini-pro-highlights", "Gemini 3.1 Pro (Low)"),
        # with no tier word a free-form name takes Medium; only the aliases default to High
        ("Gemini 4 Argon", "Gemini 4 Argon (Medium)"),
        # ...unless it names Pro (3.1 Pro) or a dotted version, which it keeps, as before
        ("gemini-3.8-flash-argon", "Gemini 3.8 Flash (Medium)"),
        ("gemini-argon-3.8-flash", "Gemini 3.8 Flash (Medium)"),
        ("gemini-3.6-flash-argon", "Gemini 3.6 Flash (Medium)"),
        ("gemini-3.5-flash-argon", "Gemini 3.5 Flash (Medium)"),
        ("gemini-argon-2.5-flash", "Gemini 3.5 Flash (Medium)"),
        ("gemini-4.5-argon", "Gemini 3.5 Flash (Medium)"),
        ("gemini-3.5.1-flash-argon", "Gemini 3.5 Flash (Medium)"),
        ("gemini-1.5.2-argon", "Gemini 3.5 Flash (Medium)"),
        ("gemini-4-flash-3.5.1", "Gemini 3.5 Flash (Medium)"),
        ("gemini-4-flash-3.5.", "Gemini 3.5 Flash (Medium)"),
        ("argon 3.5.", "Gemini 3.5 Flash (Medium)"),
        ("gemini-argon-pro", "Gemini 3.1 Pro (Low)"),
        ("gemini-3.8-flash-pro-argon", "Gemini 3.1 Pro (Low)"),
        # an undotted major version 4 is the 4 family, unless a dotted version is named;
        # a date is not a version, and 4k is a size, not a version
        ("gemini-4-flash", "Gemini 4 Argon (Medium)"),
        ("gemini-v4-flash", "Gemini 4 Argon (Medium)"),
        ("gemini-4-flash-2026.10", "Gemini 4 Argon (Medium)"),
        ("gemini-4-flash-100.5", "Gemini 4 Argon (Medium)"),
        ("gemini-4k-flash", "Gemini 3.5 Flash (Medium)"),
        # ...and a numeral of any script after the 4 makes it part of a longer word
        ("gemini4\u00b2", "Gemini 3.5 Flash (Medium)"),
        ("gemini-3.8-flash-4", "Gemini 3.8 Flash (Medium)"),
        ("gemini-3.6-flash-4", "Gemini 3.6 Flash (Medium)"),
        ("gemini-3.5-flash-4", "Gemini 3.5 Flash (Medium)"),
        ("gemini-3.1-flash-4", "Gemini 3.5 Flash (Medium)"),
        # 4.0 / 4.5 / 4.x / 14 / 40 are not major version 4
        ("gemini-4.0-flash", "Gemini 3.5 Flash (Medium)"),
        ("gemini-4.5-flash", "Gemini 3.5 Flash (Medium)"),
        ("gemini-4.x-flash", "Gemini 3.5 Flash (Medium)"),
        ("gemini-14-flash", "Gemini 3.5 Flash (Medium)"),
        ("gemini-40-flash", "Gemini 3.5 Flash (Medium)"),
        # a dot after the 4 with no letter or numeral after it ends the 4, as a trailing dot
        # ends a dotted version
        ("gemini-4.", "Gemini 4 Argon (Medium)"),
        ("gemini-4-flash.", "Gemini 4 Argon (Medium)"),
        ("gemini-4.-flash", "Gemini 4 Argon (Medium)"),
        # a dot and a letter or numeral of any script after the 4 name a release the table
        # does not list
        ("gemini-4.\u00e9", "Gemini 3.5 Flash (Medium)"),
        ("gemini-4.\u00b2", "Gemini 3.5 Flash (Medium)"),
        ("gemini-3.8-flash", "Gemini 3.8 Flash (Medium)"),
        ("gemini-3.8-flash-low", "Gemini 3.8 Flash (Low)"),
        ("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)"),
        ("gemini-3.8-flash-preview", "Gemini 3.8 Flash (Medium)"),
        # gemini-3.6-flash defaults to High and must never silently resolve to a
        # 3.5 display name.
        ("gemini-3.6-flash", "Gemini 3.6 Flash (High)"),
        ("gemini-3.6", "Gemini 3.6 Flash (High)"),
        ("gemini-3.6-flash-medium", "Gemini 3.6 Flash (Medium)"),
        ("Gemini 3.6 Flash (Low)", "Gemini 3.6 Flash (Low)"),  # exact passthrough
        # A free-form 3.6 name not in the alias table still stays on the 3.6
        # family via the version-aware heuristic — no downgrade to 3.5.
        ("gemini-3.6-flash-preview", "Gemini 3.6 Flash (Medium)"),
        # ...but a number that merely CONTAINS "3.6" is not version 3.6: the
        # version match is a delimited token, so 13.6 / 3.60 do not upgrade.
        ("gemini-13.6-flash", "Gemini 3.5 Flash (Medium)"),
        ("gemini-3.60-flash", "Gemini 3.5 Flash (Medium)"),
        # ...nor is a 3.8 or 3.6 that follows a dot: it is part of a longer version
        ("gemini-1.3.8-flash", "Gemini 3.5 Flash (Medium)"),
        ("gemini-2.3.6-flash", "Gemini 3.5 Flash (Medium)"),
        ("2.3.6-argon", "Gemini 3.5 Flash (Medium)"),
        # ...and a date is no version, so the name's argon decides
        ("gemini-2026.3.8-argon", "Gemini 4 Argon (Medium)"),
        # ...but a dot after anything else separates the version, which it keeps
        ("gemini-4-flash.3.5", "Gemini 3.5 Flash (Medium)"),
        ("argon.3.5", "Gemini 3.5 Flash (Medium)"),
        ("gemini-4-flash.3.8", "Gemini 3.8 Flash (Medium)"),
        ("gemini-4-flash.3.6-x", "Gemini 3.6 Flash (Medium)"),
    ],
)
def test_resolve_agy_model(spec, expected):
    assert resolve_agy_model(spec) == expected


# Model resolution — effort folding (agy has no effort flag/kwarg; effort is
# expressed only as the Low/Medium/High suffix on --model)


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
        # gemini-3.6-flash: effort folds onto the 3.6 family (not a 3.5
        # downgrade); bare default is High per the alias.
        ("gemini-3.6-flash", "low", "Gemini 3.6 Flash (Low)"),
        ("gemini-3.6-flash", "high", "Gemini 3.6 Flash (High)"),
        ("gemini-3.6-flash", "xhigh", "Gemini 3.6 Flash (High)"),
        ("gemini-3.6-flash", None, "Gemini 3.6 Flash (High)"),
        # Gemini 4 Argon: effort folds onto its three tiers, whatever tier the
        # id or alias carried.
        ("gemini-4-argon", "minimal", "Gemini 4 Argon (Low)"),
        ("gemini-4-argon", "medium", "Gemini 4 Argon (Medium)"),
        ("gemini-4-argon", "max", "Gemini 4 Argon (High)"),
        ("gemini-4-argon-high", "low", "Gemini 4 Argon (Low)"),
        ("argon", "medium", "Gemini 4 Argon (Medium)"),
        ("gemini-4", "xhigh", "Gemini 4 Argon (High)"),
        ("gemini-argon-preview", "low", "Gemini 4 Argon (Low)"),
        ("argon high", "low", "Gemini 4 Argon (Low)"),
        ("argon 4", "high", "Gemini 4 Argon (High)"),
        ("argon-3.5", "high", "Gemini 3.5 Flash (High)"),
        ("Gemini 4 Argon (Low)", "high", "Gemini 4 Argon (Low)"),
        # an effort is matched as a lowercase word, and a blank one is no effort
        ("gemini-4-argon", "High", "Gemini 4 Argon (High)"),
        ("gemini-4-argon", " low ", "Gemini 4 Argon (Low)"),
        ("gemini-4-argon", "", "Gemini 4 Argon (High)"),
        ("gemini-4-argon", "  ", "Gemini 4 Argon (High)"),
        ("gemini-3.8-flash", "LOW", "Gemini 3.8 Flash (Low)"),
    ],
)
def test_resolve_agy_model_effort_folding(spec, effort, expected):
    assert resolve_agy_model(spec, effort=effort) == expected


@pytest.mark.parametrize("effort", [0, 1.5, ["high"], {"high": 1}])
@pytest.mark.parametrize(
    "spec,expected",
    [
        # each name's own tier differs from the one an unknown effort clamps to (Medium,
        # or High on Pro)
        ("gemini-argon-high", "Gemini 4 Argon (High)"),
        ("gemini-4-argon-low", "Gemini 4 Argon (Low)"),
        ("gemini-4-argon", "Gemini 4 Argon (High)"),
        ("gemini-3.6-flash", "Gemini 3.6 Flash (High)"),
        ("Gemini 4 Argon (Low)", "Gemini 4 Argon (Low)"),
        ("gemini-argon-pro", "Gemini 3.1 Pro (Low)"),
    ],
)
def test_resolve_agy_model_ignores_an_effort_that_is_not_a_string(spec, expected, effort):
    assert resolve_agy_model(spec, effort=effort) == expected
    assert resolve_agy_model(spec, effort=effort, reapply_effort=True) == expected


def test_resolve_agy_model_effort_ignored_for_cross_family_alias():
    """Claude/GPT-OSS routed through agy have no Low/Medium/High tiers —
    effort is accepted but has no suffix to fold into."""
    assert resolve_agy_model("opus", effort="high") == "Claude Opus 4.6 (Thinking)"


def test_resolve_agy_model_36_flash_defaults_high_never_downgrades():
    """gemini-3.6-flash defaults to the High tier: gemini bakes effort into the
    model id, so a bare 3.6 spec must land on the High variant, and no 3.6 spec
    may ever silently resolve to a 3.5 name."""
    assert resolve_agy_model("gemini-3.6-flash") == "Gemini 3.6 Flash (High)"
    # every 3.6 form stays on the 3.6 family, aliased or free-form
    for spec in ("gemini-3.6-flash", "gemini-3.6", "gemini-3.6-flash-preview"):
        got = resolve_agy_model(spec)
        assert got.startswith("Gemini 3.6 Flash"), (spec, got)
    # explicit effort folds onto the 3.6 family, not a 3.5 downgrade
    assert resolve_agy_model("gemini-3.6-flash", effort="low") == "Gemini 3.6 Flash (Low)"
    # a number that merely contains "3.6" is NOT version 3.6 — no false upgrade
    for spec in ("gemini-13.6-flash", "gemini-3.60-flash"):
        assert not resolve_agy_model(spec).startswith("Gemini 3.6 Flash"), spec


def test_resolve_agy_model_4_argon_is_the_default_and_stays_on_family():
    assert resolve_agy_model(None) == "Gemini 4 Argon (High)"
    assert resolve_agy_model("gemini-4-argon", effort="low") == "Gemini 4 Argon (Low)"
    for spec in ("gemini-4-argon", "gemini-4", "gemini-4-argon-preview"):
        assert resolve_agy_model(spec).startswith("Gemini 4 Argon"), spec
    for spec in ("gemini-14-flash", "gemini-4.5-flash"):
        assert not resolve_agy_model(spec).startswith("Gemini 4 Argon"), spec


@pytest.mark.parametrize(
    "spec",
    ["gemini-4-argon", "gemini-4", "argon", "Gemini 4 Argon (High)", "Gemini 4 Argon (Low)"],
)
def test_an_argon_name_has_the_argon_context_window(spec):
    assert lookup_context_window(spec, "gemini_code") == 1_048_576


def test_a_longer_window_row_still_wins_over_the_argon_rows():
    assert lookup_context_window("gemini-2.5-pro-argon", "gemini_code") == 2_097_152


def test_resolve_agy_model_38_flash_defaults_medium_and_stays_on_family():
    assert resolve_agy_model("flash") == "Gemini 3.8 Flash (Medium)"
    assert resolve_agy_model("gemini-3.8-flash") == "Gemini 3.8 Flash (Medium)"
    assert resolve_agy_model("gemini-3.8-flash", effort="high") == "Gemini 3.8 Flash (High)"
    for spec in ("gemini-3.8-flash", "gemini-3.8-flash-preview"):
        assert resolve_agy_model(spec).startswith("Gemini 3.8 Flash"), spec
    for spec in ("gemini-13.8-flash", "gemini-3.80-flash"):
        assert not resolve_agy_model(spec).startswith("Gemini 3.8 Flash"), spec


# Model resolution — reapply_effort (li agent -r --effort re-applying effort
# to an already-resolved persisted model)


@pytest.mark.parametrize(
    "spec,effort,expected",
    [
        ("Gemini 3.5 Flash (Low)", "high", "Gemini 3.5 Flash (High)"),
        ("Gemini 3.5 Flash (High)", "low", "Gemini 3.5 Flash (Low)"),
        ("Gemini 3.1 Pro (Low)", "high", "Gemini 3.1 Pro (High)"),
        # medium clamps to High for the Pro family, same as fresh resolution.
        ("Gemini 3.1 Pro (Low)", "medium", "Gemini 3.1 Pro (High)"),
        # reapply on a persisted 3.6 model stays on the 3.6 family.
        ("Gemini 3.6 Flash (Low)", "high", "Gemini 3.6 Flash (High)"),
        ("Gemini 3.6 Flash (High)", "low", "Gemini 3.6 Flash (Low)"),
        ("Gemini 4 Argon (High)", "low", "Gemini 4 Argon (Low)"),
        ("Gemini 4 Argon (Low)", "max", "Gemini 4 Argon (High)"),
        ("Gemini 4 Argon (Low)", "High", "Gemini 4 Argon (High)"),
        ("Gemini 4 Argon (Low)", "", "Gemini 4 Argon (Low)"),
    ],
)
def test_resolve_agy_model_reapply_effort_overrides_persisted_suffix(spec, effort, expected):
    assert resolve_agy_model(spec, effort=effort, reapply_effort=True) == expected


def test_resolve_agy_model_reapply_effort_default_false_preserves_pin():
    """Without reapply_effort, an exact qualified model still wins over effort
    (unchanged default — this is the semantics for a caller-typed pin)."""
    assert resolve_agy_model("Gemini 3.5 Flash (Low)", effort="high") == "Gemini 3.5 Flash (Low)"


def test_resolve_agy_model_reapply_effort_no_new_effort_keeps_persisted():
    """reapply_effort=True with no new effort given must not touch the
    persisted model (nothing to reapply)."""
    assert (
        resolve_agy_model("Gemini 3.5 Flash (Low)", effort=None, reapply_effort=True)
        == "Gemini 3.5 Flash (Low)"
    )


def test_resolve_agy_model_reapply_effort_ignored_for_cross_family_alias():
    """Cross-family agy models (Claude/GPT-OSS) have no effort tier to
    reapply — reapply_effort has nothing to do and the model passes through."""
    assert (
        resolve_agy_model("Claude Opus 4.6 (Thinking)", effort="high", reapply_effort=True)
        == "Claude Opus 4.6 (Thinking)"
    )
