# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A turn whose last agent text carries call markup is a failed run.

A model served through the CLI can write a tool call into its reply as text, in
the model's own call markup, rather than making the call. The CLI accepts that
text as the turn's final message and ends the turn with ``turn.completed``, so
without this check the run is recorded as completed although the agent stopped
mid-task and the call never ran. When the turn's last non-empty agent text
carries markup outside code, the session is marked failed (``is_error``, and
the result names the cause) and an error chunk is emitted — read when a turn
completes or the stream ends. A streamed run raises on that error chunk; a
non-streamed call returns the session marked failed, as it does for any failed
turn, and does not raise.

The parser reads only the turn's last text: markup in an earlier message was
superseded by a later one or by a call that ran after it, text inside a fenced
code block or an inline code span is a quotation of the markup rather than a
call, and a prose mention of the markup's word or a bare ``|`` is not a tag.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from lionagi.providers.openai.codex import (
    CodexCLIEndpoint,
    CodexCodeRequest,
    stream_codex_cli,
)
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


def _turn_failed(message: str) -> dict:
    return {"type": "turn.failed", "error": {"message": message}}


def _command_execution() -> dict:
    return {
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "id": "call-1",
            "command": "ls",
            "aggregated_output": "ok",
            "exit_code": 0,
            "status": "completed",
        },
    }


def _legacy_message(text: str) -> dict:
    return {"type": "message", "message": {"content": text}}


def _legacy_tool_block() -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "id": "call-2", "name": "Bash", "input": {"command": "ls"}}
            ]
        },
    }


def _benign_sentinel() -> dict:
    return {"type": "error", "error": {}}


def _markup_errors(chunks: list[StreamChunk]) -> list[StreamChunk]:
    return [c for c in chunks if c.metadata.get("call_markup_in_text")]


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


# --- the benign end-of-stream sentinel must not change the session's outcome ---


@pytest.mark.asyncio
async def test_a_benign_sentinel_leaves_a_failed_turn_failed():
    """The sentinel a resumed session ends with must not retract a real failure."""
    events = [
        _agent_message(MARKUP_FIXTURE),
        _turn_completed(),
        _benign_sentinel(),
    ]
    chunks, session = await _parse(events)

    assert session.is_error is True, (
        "the benign end-of-stream sentinel turned a failed turn back into a success"
    )
    assert session.result and "tool call" in session.result, (
        f"the sentinel replaced the failure result with its own message: {session.result!r}"
    )
    markup_errors = _markup_errors(chunks)
    assert len(markup_errors) == 1, f"expected the markup error chunk, got {markup_errors}"
    sentinels = [c for c in chunks if c.metadata.get("benign_eos")]
    assert len(sentinels) == 1, f"the sentinel's own chunk was dropped: {chunks}"
    assert sentinels[0].type == "error" and sentinels[0].is_error is False, (
        f"the sentinel chunk no longer reads as a non-error: {sentinels[0]}"
    )


@pytest.mark.asyncio
async def test_a_benign_sentinel_leaves_a_clean_session_clean():
    """On a clean resumed session the sentinel keeps the reply as the result."""
    events = [
        _agent_message("done"),
        _turn_completed(),
        _benign_sentinel(),
    ]
    chunks, session = await _parse(events)

    assert session.is_error is False, "a clean session was marked failed by its own sentinel"
    assert "empty error payload" not in session.result, (
        f"the sentinel replaced the reply text with its own message: {session.result!r}"
    )
    assert session.result == "done", f"the reply text was lost: {session.result!r}"
    assert [c for c in chunks if c.type == "error" and c.is_error] == [], (
        f"the sentinel was reported as a real failure: {chunks}"
    )


@pytest.mark.asyncio
async def test_a_benign_sentinel_leaves_the_returned_session_clean():
    """The same stream through _call: the returned dict agrees, without raising."""
    events = [
        _agent_message("done"),
        _turn_completed(),
        _benign_sentinel(),
    ]

    async def fake(request):
        for event in events:
            yield event

    with patch(
        "lionagi.providers.openai.codex.stream_codex_cli_events",
        side_effect=fake,
    ):
        out = await CodexCLIEndpoint()._call({"request": _request()}, {})

    assert out["is_error"] is False, (
        f"a clean resumed session came back marked failed: {out['is_error']!r}"
    )
    assert "empty error payload" not in out["result"], (
        f"the sentinel replaced the reply text with its own message: {out['result']!r}"
    )
    assert out["result"] == "done", f"the reply text was lost: {out['result']!r}"


@pytest.mark.asyncio
async def test_a_non_streamed_call_returns_the_session_failed_without_raising():
    """The same turn through _call: the call returns, marked failed."""
    events = [_agent_message(MARKUP_FIXTURE), _turn_completed()]

    async def fake(request):
        for event in events:
            yield event

    with patch(
        "lionagi.providers.openai.codex.stream_codex_cli_events",
        side_effect=fake,
    ):
        out = await CodexCLIEndpoint()._call({"request": _request()}, {})

    assert out["is_error"] is True, (
        f"the returned session came back marked successful: {out['is_error']!r}"
    )
    assert out["result"] and "tool call" in out["result"], (
        f"the returned session result does not explain the failure: {out['result']!r}"
    )


# --- the turn's last non-empty agent text of any shape ---


@pytest.mark.asyncio
async def test_an_empty_message_does_not_supersede_markup():
    """An empty agent_message is not the turn's last text and cannot clear it."""
    events = [
        _agent_message(MARKUP_FIXTURE),
        _agent_message(""),
        _turn_completed(),
    ]
    chunks, session = await _parse(events)

    assert session.is_error is True, (
        "an empty message replaced the markup message and the turn completed"
    )
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


@pytest.mark.asyncio
async def test_markup_in_a_legacy_message_sets_the_turn_text():
    """A legacy message event's string content is read like an agent_message."""
    _, session = await _parse([_legacy_message(MARKUP_FIXTURE), _turn_completed()])

    assert session.is_error is True, (
        "markup in a legacy message event went unnoticed, so a written call passed as a turn"
    )


@pytest.mark.asyncio
async def test_markup_in_a_legacy_message_at_the_stream_end_fails():
    """The stream's end reads the same text a turn end would."""
    chunks, session = await _parse([_legacy_message(MARKUP_FIXTURE)])

    assert session.is_error is True, (
        "a stream that ended on markup in a legacy message passed as a success"
    )
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


@pytest.mark.asyncio
async def test_a_call_after_the_markup_supersedes_it():
    """A completed call is a real call, so an earlier message is not the last one."""
    events = [
        _agent_message(MARKUP_FIXTURE),
        _command_execution(),
        _agent_message("Ran it; here are the results."),
        _turn_completed(),
    ]
    chunks, session = await _parse(events)

    assert session.is_error is False, (
        "markup the model moved past by running a real call still failed the turn"
    )
    assert _markup_errors(chunks) == [], f"a superseded message produced an error chunk: {chunks}"


@pytest.mark.asyncio
async def test_a_call_after_the_markup_without_a_later_message_completes():
    """The turn ends on the call's own result; the markup is not the last text."""
    events = [
        _agent_message(MARKUP_FIXTURE),
        _command_execution(),
        _turn_completed(),
    ]
    chunks, session = await _parse(events)

    assert session.is_error is False, (
        "a turn that ended on a completed call failed over an earlier markup message"
    )
    assert _markup_errors(chunks) == [], f"the completed call did not clear the text: {chunks}"


@pytest.mark.asyncio
async def test_a_plan_update_after_the_markup_does_not_supersede_it():
    """A plan item is not a call: the markup message is still the turn's last text."""
    plan = {
        "type": "item.completed",
        "item": {"type": "todo_list", "id": "plan-1", "items": [{"text": "x", "completed": True}]},
    }
    chunks, session = await _parse([_agent_message(MARKUP_FIXTURE), plan, _turn_completed()])

    assert session.is_error is True, "a plan update after the markup let the turn complete"
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk: {chunks}"


@pytest.mark.asyncio
async def test_an_mcp_tool_call_after_the_markup_supersedes_it():
    """A completed MCP tool call is a real call, so the markup message is not the last text."""
    call = {
        "type": "item.completed",
        "item": {
            "type": "mcp_tool_call",
            "id": "call-3",
            "server": "docs",
            "tool": "search",
            "status": "completed",
        },
    }
    chunks, session = await _parse([_agent_message(MARKUP_FIXTURE), call, _turn_completed()])

    assert session.is_error is False, "a turn that ended on an MCP call failed over the markup"
    assert _markup_errors(chunks) == [], f"the MCP call did not clear the text: {chunks}"


@pytest.mark.asyncio
async def test_a_legacy_tool_block_after_the_markup_supersedes_it():
    """The legacy tool_use block is a real call, and it clears the text the same way."""
    events = [
        _legacy_message(MARKUP_FIXTURE),
        _legacy_tool_block(),
        _turn_completed(),
    ]
    chunks, session = await _parse(events)

    assert session.is_error is False, (
        "a legacy call block did not supersede the markup message before it"
    )
    assert _markup_errors(chunks) == [], f"the call block did not clear the text: {chunks}"


@pytest.mark.asyncio
async def test_a_failed_turn_clears_the_text_for_the_next_turn():
    """turn.failed ends the turn like turn.completed, and a later turn is read afresh."""
    events = [
        _agent_message(MARKUP_FIXTURE),
        _turn_failed("boom"),
        _agent_message("Recovered and finished."),
        _turn_completed(),
    ]
    chunks, session = await _parse(events)

    assert _markup_errors(chunks) == [], f"no chunk may blame call markup here: {chunks}"
    errors = [c for c in chunks if c.type == "error" and c.is_error]
    assert len(errors) == 1, f"expected only the turn.failed error chunk, got {errors}"
    assert errors[0].content == "boom", f"the turn.failed message was lost: {errors[0].content!r}"


@pytest.mark.asyncio
async def test_the_turn_end_resets_the_text_between_turns():
    """Without the reset at turn.completed the second turn re-reports the first's markup."""
    events = [
        _agent_message(MARKUP_FIXTURE),
        _turn_completed(),
        _turn_completed(),
    ]
    chunks, _ = await _parse(events)

    assert len(_markup_errors(chunks)) == 1, (
        "the markup message stayed pending across the turn end and was reported again: "
        f"{_markup_errors(chunks)}"
    )


# --- a stream that ends without a turn end ---


@pytest.mark.asyncio
async def test_markup_at_the_stream_end_fails_the_run():
    """The CLI exiting reads the pending text the way a turn end would."""
    chunks, session = await _parse([_agent_message(MARKUP_FIXTURE)])

    assert session.is_error is True, "a stream that ended on markup passed as a success"
    assert session.result and "tool call" in session.result, (
        f"the session result does not explain the failure: {session.result!r}"
    )
    markup_errors = _markup_errors(chunks)
    assert len(markup_errors) == 1, f"expected one markup error chunk, got {chunks}"
    assert markup_errors[0].type == "error" and markup_errors[0].is_error is True, (
        f"the end-of-stream error chunk reads as success: {markup_errors[0]}"
    )


# --- markup inside code is a quotation, not a call ---


@pytest.mark.asyncio
async def test_markup_inside_a_fenced_block_completes():
    """A message that shows a tag inside a fenced block is not writing a call."""
    shown = f'<{BAR}DSML{BAR} invoke name="x">'
    text = f"Here is the shape it wrote:\n```\n{shown}\n```\nThat is all."
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is False, (
        f"a tag shown inside a fenced code block failed the turn: {text!r}"
    )
    assert _markup_errors(chunks) == [], f"quoted markup produced an error chunk: {chunks}"


@pytest.mark.asyncio
async def test_markup_inside_a_tilde_fence_completes():
    """The other fence marker is a code block too."""
    text = f"Shown here:\n~~~\n<{BAR}DSML{BAR} calls>\n~~~\nDone."
    _, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is False, f"a tilde-fenced block was read as a call: {text!r}"


@pytest.mark.asyncio
async def test_markup_inside_an_inline_code_span_completes():
    """A tag between backticks is quoted the same way."""
    text = f"Use `<{BAR}DSML{BAR} invoke>` in the prompt instead, it is just documentation."
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is False, f"a tag inside an inline code span failed the turn: {text!r}"
    assert _markup_errors(chunks) == [], f"quoted markup produced an error chunk: {chunks}"


@pytest.mark.asyncio
async def test_markup_outside_code_still_fails_alongside_quoted_markup():
    """Only the tag the message actually wrote as a call fails the turn."""
    quoted = f"`<{BAR}DSML{BAR} invoke>`"
    written = f'<{BAR}DSML{BAR} invoke name="x">'
    text = f"I showed {quoted} and then wrote {written}"
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, (
        f"the unquoted tag went unnoticed beside a quoted one: {text!r}"
    )
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


# --- the markup pattern ---


@pytest.mark.asyncio
async def test_a_function_calls_tag_alone_fails():
    """The opening function_calls tag is part of the model's call markup."""
    text = f"<{BAR}DSML{BAR} function_calls>"
    _, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, f"the function_calls tag went unnoticed: {text!r}"


@pytest.mark.asyncio
async def test_a_parameter_tag_alone_fails():
    """A parameter tag alone is the same written call."""
    text = f'<{BAR}DSML{BAR} parameter name="x">'
    _, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, f"the parameter tag went unnoticed: {text!r}"


@pytest.mark.asyncio
async def test_the_double_bar_form_fails():
    """A model that writes two bars on each side is writing the same tag."""
    text = f'<{BAR}{BAR}DSML{BAR}{BAR} invoke name="x">'
    _, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, f"the double-bar form went unnoticed: {text!r}"
