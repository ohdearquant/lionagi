# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A turn whose final message carries call markup is a failed run.

A model served through the CLI can write a tool call into its reply as text, in
the model's own call markup, rather than making the call. The CLI accepts that
text as the turn's final message and ends the turn with ``turn.completed``, so
without this check the run is recorded as completed although the agent stopped
mid-task and the call never ran. The parser reads only the last ``agent_message``
of the turn: markup in an earlier message was superseded by a later one, and a
prose mention of the markup's word or a bare ``|`` is not a tag.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from lionagi.providers.openai.codex import CodexCodeRequest, stream_codex_cli
from lionagi.service.types.stream_chunk import StreamChunk

# U+FF5C FULLWIDTH VERTICAL LINE, written as an escape so the source stays
# ASCII: the separators the model emits are not the ASCII `|`.
BAR = "\uff5c"

NARRATION = "Now let me check the tests that build the table."

MARKUP_TAIL = f"</{BAR}DSML{BAR} parameter>\n</{BAR}DSML{BAR} invoke>\n</{BAR}DSML{BAR} calls>"

MARKUP_FIXTURE = f"{NARRATION}\n{MARKUP_TAIL}"
ASCII_MARKUP_FIXTURE = MARKUP_FIXTURE.replace(BAR, "|")


def _request() -> CodexCodeRequest:
    return CodexCodeRequest(prompt="test", verbose_output=False)


async def _parse(events: list[dict]) -> tuple[list[StreamChunk], object]:
    """Drive the parser with a scripted event stream; return chunks and the session."""

    async def fake_events(request):
        for event in events:
            yield event

    chunks: list[StreamChunk] = []
    session = None
    with patch(
        "lionagi.providers.openai.codex.stream_codex_cli_events",
        side_effect=fake_events,
    ):
        async for item in stream_codex_cli(_request()):
            if isinstance(item, StreamChunk):
                chunks.append(item)
            else:
                session = item
    return chunks, session


def _agent_message(text: str) -> dict:
    return {"type": "item.completed", "item": {"type": "agent_message", "text": text}}


def _turn_completed() -> dict:
    return {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}


@pytest.mark.asyncio
async def test_call_markup_in_the_final_message_fails_the_run():
    """The reported fixture: the narration, then the call markup as text."""
    chunks, session = await _parse([_agent_message(MARKUP_FIXTURE), _turn_completed()])

    errors = [c for c in chunks if c.type == "error"]
    assert session.is_error is True, (
        "the turn completed while the model's call markup sat in its final message "
        "unexecuted, and the run was recorded as a success"
    )
    assert len(errors) == 1, f"expected one error chunk, got {errors}"
    assert errors[0].is_error is True, f"the error chunk reads as success: {errors[0]}"
    assert errors[0].metadata.get("call_markup_in_text") is True, (
        f"the chunk does not say why the run failed: {errors[0].metadata}"
    )
    assert session.result and "tool call" in session.result, (
        f"the session result does not explain the failure: {session.result!r}"
    )


@pytest.mark.asyncio
async def test_the_usage_result_chunk_survives_the_failure():
    """The failed run still reports the usage turn.completed carried."""
    chunks, _ = await _parse([_agent_message(MARKUP_FIXTURE), _turn_completed()])

    results = [c for c in chunks if c.type == "result"]
    assert len(results) == 1, f"the usage result chunk was dropped: {chunks}"
    assert results[0].metadata.get("usage") == {"input_tokens": 10, "output_tokens": 5}, (
        f"the result chunk lost the turn's usage: {results[0].metadata}"
    )


@pytest.mark.asyncio
async def test_the_ascii_bar_form_fails_too():
    """A model that writes ASCII `|` separators is the same failure."""
    chunks, session = await _parse([_agent_message(ASCII_MARKUP_FIXTURE), _turn_completed()])

    assert session.is_error is True, "the ASCII `|` form of the markup went unnoticed"
    errors = [c for c in chunks if c.type == "error" and c.is_error]
    assert len(errors) == 1, f"expected one error chunk, got {errors}"


@pytest.mark.asyncio
async def test_an_opening_invoke_tag_fails():
    """An opening tag is as much a written call as the closing tail."""
    text = f'{NARRATION}\n<{BAR}DSML{BAR} invoke name="x">'
    _, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, f"the opening invoke tag went unnoticed: {text!r}"


@pytest.mark.asyncio
async def test_the_same_narration_without_markup_completes():
    """The control: the narration alone is an ordinary turn."""
    _, session = await _parse([_agent_message(NARRATION), _turn_completed()])

    assert session.is_error is False, "an ordinary final message was treated as a written tool call"


@pytest.mark.asyncio
async def test_prose_mentioning_dsml_or_a_bar_completes():
    """The word DSML and a bare `|` are not markup tags."""
    text = "The DSML adapter is unrelated here; the table has columns a | b in it."
    _, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is False, (
        f"prose mentioning DSML or containing a `|` was treated as a call: {text!r}"
    )


@pytest.mark.asyncio
async def test_markup_in_an_earlier_message_does_not_fail_the_turn():
    """Only the last agent_message of the turn is read."""
    events = [
        _agent_message(MARKUP_FIXTURE),
        _agent_message("Done — the call ran and the results are in."),
        _turn_completed(),
    ]
    chunks, session = await _parse(events)

    assert session.is_error is False, (
        "markup the model moved past with a later message still failed the run"
    )
    assert [c for c in chunks if c.type == "error"] == [], (
        f"a superseded message produced an error chunk: {chunks}"
    )
