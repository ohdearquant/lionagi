# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A turn whose last agent text carries call markup is a failed run.

A model served through the CLI can write a tool call into its reply as text, in
the model's own call markup, rather than making the call. The CLI accepts that
text as the turn's final message and ends the turn with ``turn.completed``, so
without this check the run is recorded as completed although the agent stopped
mid-task and the call never ran. When the turn's last agent text with visible
content carries markup outside code, the session is marked failed (``is_error``,
and the result names the cause) and an error chunk is emitted. The pending
text is read at a turn end -- ``turn.completed``, or the end-of-stream
sentinel a resumed session closes with, which a consumer reads instead of
reading on to the stream's end -- and at the end of a stream that carried no
turn end. A streamed run raises on that error chunk; a non-streamed call
returns the session marked failed, as it does for any failed turn, and does
not raise.

The parser reads only the turn's last such text: markup in an earlier message is
superseded by a later one or by a call that ran after it, text inside a fenced
code block or a single-paragraph inline code span is a quotation of the markup
rather than a call, and a prose mention of the markup's word or a bare ``|`` is
not a tag.
"""

from __future__ import annotations

import contextlib
import time
from unittest.mock import patch

import pytest

from lionagi.providers.openai.codex import (
    _CALL_ITEM_TYPES,
    CodexCLIEndpoint,
    CodexCodeRequest,
    stream_codex_cli,
)
from lionagi.service.types.cli_session import CLISession
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


def _legacy_block(block_type: str, text: str) -> dict:
    return {"type": "message", "message": {"content": [{"type": block_type, "text": text}]}}


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
async def test_a_non_streamed_call_raises_for_the_failed_session():
    """The same turn through _call: the failed session raises its provider error."""
    from lionagi._provider_errors import ProviderError

    events = [_agent_message(MARKUP_FIXTURE), _turn_completed()]

    async def fake(request):
        for event in events:
            yield event

    with patch(
        "lionagi.providers.openai.codex.stream_codex_cli_events",
        side_effect=fake,
    ):
        with pytest.raises(ProviderError) as info:
            await CodexCLIEndpoint()._call({"request": _request()}, {})

    assert "tool call" in str(info.value)


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


def _call_item(item_type: str) -> dict:
    """A completed call or call result of ``item_type``, as the CLI reports it."""
    items = {
        "command_execution": _command_execution()["item"],
        "file_change": {
            "type": "file_change",
            "id": "call-4",
            "changes": [],
            "status": "completed",
        },
        "mcp_tool_call": {
            "type": "mcp_tool_call",
            "id": "call-3",
            "server": "docs",
            "tool": "search",
            "status": "completed",
        },
        "web_search": {"type": "web_search", "id": "call-5", "query": "x", "status": "completed"},
        "function_call": {
            "type": "function_call",
            "id": "call-6",
            "name": "Bash",
            "arguments": "{}",
        },
        "tool_call": {"type": "tool_call", "id": "call-7", "name": "Bash", "input": {}},
        "function_call_output": {
            "type": "function_call_output",
            "call_id": "call-8",
            "output": "ok",
        },
    }
    return {"type": "item.completed", "item": items[item_type]}


# The call item types the parser knows, written out here so a type dropped from
# the parser's own set leaves this list and its case behind rather than taking
# both away.
CALL_ITEM_TYPES = [
    "command_execution",
    "file_change",
    "function_call",
    "function_call_output",
    "mcp_tool_call",
    "tool_call",
    "web_search",
]


def test_the_call_item_types_are_the_ones_this_file_pins():
    """The parser's set is the list above, so a type removed there fails here."""
    assert sorted(_CALL_ITEM_TYPES) == CALL_ITEM_TYPES, (
        "the call item types the parser supersedes with have moved: "
        f"{sorted(_CALL_ITEM_TYPES)} != {CALL_ITEM_TYPES}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("call", CALL_ITEM_TYPES)
async def test_a_call_after_the_markup_supersedes_it(call):
    """A completed call is a real call: the turn ends on it, so the markup message
    before it is not the turn's last text and does not fail the turn."""
    events = [
        _agent_message(MARKUP_FIXTURE),
        _call_item(call),
        _turn_completed(),
    ]
    chunks, session = await _parse(events)

    assert session.is_error is False, (
        f"a turn that ended on a completed {call} failed over an earlier markup message"
    )
    assert _markup_errors(chunks) == [], f"the completed {call} did not clear the text: {chunks}"


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
    """turn.failed ends the turn like turn.completed: a later turn that writes no text
    does not read the failed turn's message."""
    events = [
        _agent_message(MARKUP_FIXTURE),
        _turn_failed("boom"),
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


# --- block structure ends an inline span and marks code ---


@pytest.mark.asyncio
async def test_a_list_item_line_ends_an_inline_span_before_the_markup():
    """An inline span stays inside one paragraph, and a list item is its own block."""
    text = f'- Use the `build command\n- <{BAR}DSML{BAR} invoke name="x">\n- Run `ls`.'
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, f"a list item line did not end the span: {text!r}"
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


@pytest.mark.asyncio
async def test_a_heading_line_ends_an_inline_span_before_the_markup():
    """An ATX heading starts a new block, so a stray backtick before it quotes nothing."""
    text = f'Stray ` backtick\n# Heading <{BAR}DSML{BAR} invoke name="x">\nAlso `x` here.'
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, f"a heading line did not end the span: {text!r}"
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


@pytest.mark.asyncio
async def test_a_blockquote_line_ends_an_inline_span_before_the_markup():
    """A blockquote line starts a new block too."""
    text = f'Stray ` backtick\n> Quote <{BAR}DSML{BAR} invoke name="x">\nAlso `x` here.'
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, f"a blockquote line did not end the span: {text!r}"
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


@pytest.mark.asyncio
async def test_a_fence_opener_indented_four_spaces_is_an_indented_code_block():
    """Four spaces make an indented code block, not a fence opener; the markup after
    it is prose and fails the turn."""
    text = f'Done.\n\n    ```\n\n<{BAR}DSML{BAR} invoke name="x">\n\n```python\nx = 1\n```'
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, f"a four-space line opened a fence: {text!r}"
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


@pytest.mark.asyncio
async def test_a_tab_indented_fence_line_is_an_indented_code_block():
    """A tab before the fence is an indented code block; the markup is prose."""
    text = f'\t```\n<{BAR}DSML{BAR} invoke name="x">\n\t```'
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, f"a tab-indented line opened a fence: {text!r}"
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


@pytest.mark.asyncio
async def test_an_indented_fence_line_does_not_close_a_fence_early():
    """Four spaces inside a fence is content, not a closer; the block still quotes."""
    text = f'```\n    ```\n<{BAR}DSML{BAR} invoke name="x">\n```'
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is False, f"an indented line closed the fence early: {text!r}"
    assert _markup_errors(chunks) == [], f"a quotation was reported as a call: {chunks}"


@pytest.mark.asyncio
async def test_a_fence_inside_a_list_item_quotes_the_markup():
    """A list marker before a fence puts the fence in that list item, so it is code."""
    text = f'- ```xml\n  <{BAR}DSML{BAR} invoke name="x">\n  ```'
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is False, f"a list-item fence was read as a call: {text!r}"
    assert _markup_errors(chunks) == [], f"a quotation was reported as a call: {chunks}"


@pytest.mark.asyncio
async def test_a_fence_inside_an_ordered_list_item_quotes_the_markup():
    """The ordered-list marker works the same way."""
    text = f'1. ```\n   <{BAR}DSML{BAR} invoke name="x">\n   ```'
    _, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is False, f"an ordered-list fence was read as a call: {text!r}"


@pytest.mark.asyncio
async def test_a_doubled_backslash_before_a_backtick_still_opens_a_span():
    """A backtick is escaped only after an odd run of backslashes, so two do not escape."""
    text = f'\\\\`<{BAR}DSML{BAR} invoke name="x">`'
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is False, f"a doubled backslash suppressed the span: {text!r}"
    assert _markup_errors(chunks) == [], f"a quotation was reported as a call: {chunks}"


@pytest.mark.asyncio
async def test_an_indented_code_block_quotes_the_markup():
    """Four spaces after a blank line start an indented code block, so the tag is code."""
    text = f'Example:\n\n    <{BAR}DSML{BAR} invoke name="x">\n'
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is False, f"an indented code block was read as a call: {text!r}"
    assert _markup_errors(chunks) == [], f"a quotation was reported as a call: {chunks}"


@pytest.mark.asyncio
async def test_an_html_escaped_tag_is_read_as_the_reader_sees_it():
    """An entity that spells the tag names it on screen, so it fails the turn too."""
    text = f"&lt;{BAR}DSML{BAR} invoke name=&quot;x&quot;&gt;"
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, f"an escaped tag was read as prose: {text!r}"
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


@pytest.mark.asyncio
async def test_a_longer_closer_line_hides_the_markup_it_contains():
    """A five-backtick line closes a three-backtick fence, so its contents are quoted."""
    text = f'```\n<{BAR}DSML{BAR} invoke name="x">\n`````\nDone.'
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is False, f"a longer closer did not hold the block open: {text!r}"
    assert _markup_errors(chunks) == [], f"a quotation was reported as a call: {chunks}"


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


# --- a consumer that stops at the end-of-stream sentinel, as a streamed run does ---


async def _stop_at_the_sentinel(events: list[dict]) -> tuple[list[StreamChunk], CLISession]:
    """Consume the parser as a streamed run does, stopping at the sentinel chunk.

    The generator is closed at that point, so the parser's stream-end check
    never runs and only what was yielded before the sentinel can be read.
    """

    async def fake_events(request):
        for event in events:
            yield event

    chunks: list[StreamChunk] = []
    session = CLISession()
    with patch(
        "lionagi.providers.openai.codex.stream_codex_cli_events",
        side_effect=fake_events,
    ):
        async with contextlib.aclosing(stream_codex_cli(_request(), session)) as gen:
            async for item in gen:
                if isinstance(item, StreamChunk):
                    chunks.append(item)
                    if item.metadata.get("benign_eos"):
                        break
    return chunks, session


@pytest.mark.asyncio
async def test_markup_before_the_sentinel_without_a_turn_end_fails():
    """The sentinel closes a turn the way turn.completed does, so pending markup fails."""
    chunks, session = await _stop_at_the_sentinel(
        [_agent_message(MARKUP_FIXTURE), _benign_sentinel()]
    )

    assert session.is_error is True, (
        "the sentinel ended the turn without reading the markup message it left pending"
    )
    assert session.result and "tool call" in session.result, (
        f"the session result does not explain the failure: {session.result!r}"
    )
    markup_errors = _markup_errors(chunks)
    assert len(markup_errors) == 1, f"expected one markup error chunk, got {chunks}"
    assert markup_errors[0].type == "error" and markup_errors[0].is_error is True, (
        f"the markup chunk reads as success: {markup_errors[0]}"
    )


@pytest.mark.asyncio
async def test_a_turn_end_before_the_sentinel_does_not_report_the_markup_twice():
    """The turn end already read and cleared the pending text; the sentinel reports nothing."""
    chunks, session = await _stop_at_the_sentinel(
        [_agent_message(MARKUP_FIXTURE), _turn_completed(), _benign_sentinel()]
    )

    assert session.is_error is True, "the sentinel turned a failed turn back into a success"
    markup_errors = _markup_errors(chunks)
    assert len(markup_errors) == 1, f"expected exactly one markup error chunk, got {markup_errors}"


@pytest.mark.asyncio
async def test_a_clean_message_before_the_sentinel_still_completes():
    """The sentinel's turn-end read leaves a turn with no markup text alone."""
    chunks, session = await _stop_at_the_sentinel(
        [_agent_message("done"), _turn_completed(), _benign_sentinel()]
    )

    assert session.is_error is False, "a clean turn was marked failed by its own sentinel"
    assert _markup_errors(chunks) == [], f"a clean turn produced a markup chunk: {chunks}"


@pytest.mark.asyncio
async def test_a_non_streamed_call_markup_then_sentinel_raises():
    """The non-streamed path reads the same sentinel turn end and raises."""
    from lionagi._provider_errors import ProviderError

    events = [_agent_message(MARKUP_FIXTURE), _benign_sentinel()]

    async def fake(request):
        for event in events:
            yield event

    with patch(
        "lionagi.providers.openai.codex.stream_codex_cli_events",
        side_effect=fake,
    ):
        with pytest.raises(ProviderError) as info:
            await CodexCLIEndpoint()._call({"request": _request()}, {})

    assert "tool call" in str(info.value)


# --- the failure reaches a real run, which stops at the sentinel ---


@pytest.mark.asyncio
async def test_a_real_run_raises_on_markup_before_the_sentinel():
    """A streamed run over a real endpoint raises on the markup chunk.

    This drives ``Branch.run`` itself rather than the parser's generator, so
    the failure is shown to reach the streamed path and not only the parser.
    """
    from unittest.mock import patch

    from lionagi.operations.run.run import RunParam, run
    from lionagi.service.imodel import iModel
    from lionagi.session.branch import Branch

    events = [_agent_message(MARKUP_FIXTURE), _benign_sentinel()]

    async def fake(request):
        for event in events:
            yield event

    model = iModel(provider="codex", model="gpt-5.1-codex", api_key="x")
    branch = Branch()
    branch.chat_model = model

    with patch(
        "lionagi.providers.openai.codex.stream_codex_cli_events",
        side_effect=fake,
    ):
        raised: Exception | None = None
        try:
            async for _ in run(branch, "go", RunParam()):
                pass
        except Exception as exc:  # noqa: BLE001 - the run's own failure is the subject
            raised = exc

    assert raised is not None, "a real run over a written call passed as a success"
    assert "tool call" in str(raised), f"the run failed without naming the markup: {raised!r}"


# --- the turn-end check itself ---


@pytest.mark.asyncio
async def test_the_turn_end_check_reports_the_markup_before_the_sentinel():
    """The failure is the turn end's own, read before the sentinel ends the stream.

    The chunk names the event it was read at, so a consumer that stops at the
    sentinel can tell the turn-end read from a later one.
    """
    events = [_agent_message(MARKUP_FIXTURE), _turn_completed(), _benign_sentinel()]
    chunks, session = await _stop_at_the_sentinel(events)

    markup_errors = _markup_errors(chunks)
    assert len(markup_errors) == 1, f"the turn end did not report the markup message: {chunks}"
    assert markup_errors[0].metadata.get("type") == "turn.completed", (
        f"the markup chunk was not read at the turn end: {markup_errors[0].metadata}"
    )
    sentinel_index = next(i for i, c in enumerate(chunks) if c.metadata.get("benign_eos"))
    assert chunks.index(markup_errors[0]) < sentinel_index, (
        f"the markup chunk arrived after the sentinel: {chunks}"
    )
    assert session.is_error is True, "the failure did not reach the session"


@pytest.mark.asyncio
async def test_two_turns_where_the_first_ends_with_markup_stay_failed():
    """The first turn's failure stands when a later clean turn ends the stream."""
    events = [
        _agent_message(MARKUP_FIXTURE),
        _turn_completed(),
        _agent_message("ok"),
        _turn_completed(),
    ]
    chunks, session = await _parse(events)

    assert session.is_error is True, "a clean second turn retracted the markup failure of the first"
    assert len(_markup_errors(chunks)) == 1, (
        f"expected one markup error chunk for the first turn, got {chunks}"
    )


# --- a whitespace-only message is not the turn's last visible text ---


@pytest.mark.asyncio
async def test_a_whitespace_only_message_does_not_supersede_markup():
    """A message with no visible text cannot take the markup message's place."""
    events = [
        _agent_message(MARKUP_FIXTURE),
        _agent_message("\n"),
        _turn_completed(),
    ]
    chunks, session = await _parse(events)

    assert session.is_error is True, (
        "a whitespace-only message replaced the markup message and the turn completed"
    )
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


# --- an inline code span stays inside one paragraph and needs a matching run ---


@pytest.mark.asyncio
async def test_a_stray_backtick_in_another_paragraph_does_not_hide_markup():
    """The span would cross a blank line, so it does not quote the markup between them."""
    text = f"I'll check the ` char.\n\n{MARKUP_FIXTURE}\n\nThen `x` is fine."
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, (
        f"two stray backticks in separate paragraphs quoted the markup: {text!r}"
    )
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


@pytest.mark.asyncio
async def test_three_backticks_closed_by_two_do_not_hide_markup():
    """A run closes only on a run of the same length, so the markup stays visible."""
    text = f'```<{BAR}DSML{BAR} invoke name="x">``'
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, (
        f"a three-backtick run closed on two backticks and quoted the markup: {text!r}"
    )
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


# --- the legacy terminal event reports an outcome, it does not retract one ---


@pytest.mark.asyncio
async def test_a_legacy_result_event_does_not_retract_a_markup_failure():
    """A success outcome arriving after the failure leaves the failure in place."""
    events = [
        _agent_message(MARKUP_FIXTURE),
        _turn_completed(),
        {"type": "result", "result": "done", "is_error": False},
    ]
    _, session = await _parse(events)

    assert session.is_error is True, (
        "the legacy result event turned the markup failure back into a success"
    )
    assert session.result and "tool call" in session.result, (
        f"the event's own text replaced the failure result: {session.result!r}"
    )


@pytest.mark.asyncio
async def test_a_legacy_result_event_failure_without_markup_is_reported():
    """The event's own failure still marks the session, and its text is the result."""
    events = [{"type": "result", "result": "boom", "is_error": True}]
    _, session = await _parse(events)

    assert session.is_error is True, "a failing legacy result event was read as a success"
    assert session.result == "boom", f"the failure text was lost: {session.result!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "fails"),
    [
        (f'    <{BAR}DSML{BAR}invoke name="x">', False),
        (f'    ```\n<{BAR}DSML{BAR}invoke name="x">', True),
    ],
    ids=["indented-code", "indented-fence-then-prose"],
)
async def test_a_legacy_result_event_text_is_read_before_it_is_trimmed(text, fails):
    """Four leading spaces make an indented code line, as they do in a message: the
    markup after them is code, and a fence after them is code that ends at the next
    line, which is prose."""
    chunks, session = await _parse([{"type": "result", "result": text, "is_error": False}])

    assert session.is_error is fails, f"the legacy result text was misread: {text!r}"
    assert len(_markup_errors(chunks)) == int(fails)
    if not fails:
        # Read untrimmed, kept trimmed: the result is the text without its indent.
        assert session.result == text.strip(), f"the result kept its indent: {session.result!r}"


# --- a legacy block list reads output_text blocks too ---


@pytest.mark.asyncio
async def test_markup_in_a_legacy_output_text_block_fails():
    """An output_text block is a text block, so its markup is the turn's last text."""
    events = [
        _legacy_block("output_text", MARKUP_FIXTURE),
        _turn_completed(),
    ]
    chunks, session = await _parse(events)

    assert session.is_error is True, (
        "markup in an output_text block went unnoticed, so a written call passed as a turn"
    )
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


# --- the endpoint's own fallback chunk for a failed session ---


@pytest.mark.asyncio
async def test_endpoint_stream_reports_a_failed_legacy_result_before_the_sentinel():
    """The non-error sentinel chunk does not stand in for the session's failure."""
    events = [
        {"type": "result", "result": "boom", "is_error": True},
        _benign_sentinel(),
    ]

    async def fake(request):
        for event in events:
            yield event

    with patch(
        "lionagi.providers.openai.codex.stream_codex_cli_events",
        side_effect=fake,
    ):
        chunks = [c async for c in CodexCLIEndpoint().stream({"request": _request()})]

    failed = [c for c in chunks if c.type == "error" and c.is_error]
    assert len(failed) == 1, (
        "no failure chunk beside the sentinel for a failed session: "
        f"{[(c.type, c.is_error, c.content) for c in chunks]}"
    )
    assert "boom" in failed[0].content, f"the failure text was lost: {failed[0].content!r}"


# --- line ends and fence rules ---


@pytest.mark.asyncio
async def test_a_crlf_blank_line_ends_an_inline_span():
    """A CRLF blank line is a paragraph break, so stray backticks do not quote the markup."""
    text = f"I'll check the ` char.\r\n\r\n{MARKUP_FIXTURE}\r\n\r\nThen `x` is fine."
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, f"a CRLF blank line did not end the span: {text!r}"
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


@pytest.mark.asyncio
async def test_a_crlf_tilde_fence_quotes_the_markup():
    """A fence written with CRLF line ends closes, so its contents are a quotation."""
    text = f"The model wrote:\r\n~~~\r\n{MARKUP_TAIL}\r\n~~~\r\nThat is the markup."
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is False, f"a CRLF fence did not close: {text!r}"
    assert _markup_errors(chunks) == [], f"a quotation was reported as a call: {chunks}"


@pytest.mark.asyncio
async def test_a_four_backtick_fence_holds_a_three_backtick_block():
    """An inner three-backtick line does not close a four-backtick fence."""
    text = f"Example:\n````markdown\n```\n{MARKUP_TAIL}\n```\n````\nDone."
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is False, f"the inner fence closed the outer one: {text!r}"
    assert _markup_errors(chunks) == [], f"a quotation was reported as a call: {chunks}"


@pytest.mark.asyncio
async def test_a_longer_closer_closes_a_fence_and_the_text_after_it_is_read():
    """A four-backtick line closes a three-backtick fence; markup after it is prose."""
    text = f"```\nquoted\n````\n{MARKUP_FIXTURE}\n```"
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, f"the markup after the closed fence was hidden: {text!r}"
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


@pytest.mark.asyncio
async def test_a_backtick_line_with_a_backtick_in_its_info_string_opens_no_fence():
    """A line like ```py `x` is prose with a code span, so it does not hide the call after it."""
    text = f"```see `<{BAR}DSML{BAR} invoke>` here\n{MARKUP_FIXTURE}\n```"
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, f"an info string with a backtick opened a fence: {text!r}"
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


@pytest.mark.asyncio
async def test_an_unclosed_fence_is_code_to_the_end_of_the_message():
    """A fence that never closes runs to the end of its container, as CommonMark
    reads it, so markup after its opener is code."""
    text = f"```\n{MARKUP_TAIL}"
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is False, f"markup inside an unclosed fence was read: {text!r}"
    assert _markup_errors(chunks) == []


@pytest.mark.asyncio
async def test_escaped_backticks_do_not_quote_the_markup():
    """A backslash-escaped backtick opens no span."""
    text = f'{NARRATION} \\`<{BAR}DSML{BAR} invoke name="x">\\`'
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, f"escaped backticks quoted the markup: {text!r}"
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


# --- a recorded failure, non-string text and the legacy event's own text ---


@pytest.mark.asyncio
async def test_markup_after_a_failure_keeps_the_failure_result():
    """The markup chunk is still emitted, but the earlier failure's result stays."""
    chunks, session = await _parse([_turn_failed("boom"), _agent_message(MARKUP_FIXTURE)])

    assert session.is_error is True, "the failed session read as a success"
    assert session.result == "boom", f"the markup replaced the failure result: {session.result!r}"
    errors = _markup_errors(chunks)
    assert len(errors) == 1, f"expected one markup error chunk, got {chunks}"
    assert "tool call" in errors[0].content, f"the chunk does not name the markup: {errors[0]}"


@pytest.mark.asyncio
async def test_non_string_message_text_is_not_read():
    """A message whose text is not a string neither raises nor replaces the pending text."""
    events = [
        _agent_message(MARKUP_FIXTURE),
        {"type": "item.completed", "item": {"type": "agent_message", "text": 7}},
        {"type": "message", "message": {"content": [{"type": "text", "text": ["x"]}]}},
        _turn_completed(),
    ]
    chunks, session = await _parse(events)

    assert session.is_error is True, "a non-string message superseded the markup"
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


@pytest.mark.parametrize(
    "event",
    [
        {"type": "result", "result": 7},
        {"type": "session.end", "response": None},
        {"type": "response", "text": ["x"]},
    ],
)
async def test_a_legacy_terminal_events_non_string_text_is_not_read(event):
    """A non-string result/response/text is not read and does not raise."""
    chunks, session = await _parse([_agent_message(MARKUP_FIXTURE), event, _turn_completed()])

    assert session.is_error is True, (
        f"a non-string event text cleared the pending markup: {event!r}"
    )
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


@pytest.mark.asyncio
async def test_markup_in_a_legacy_result_events_own_text_fails():
    """A legacy terminal event's text is read like a message."""
    events = [{"type": "result", "result": MARKUP_FIXTURE, "is_error": False}]
    chunks, session = await _parse(events)

    assert session.is_error is True, "markup in the legacy result text completed the run"
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


@pytest.mark.asyncio
async def test_clean_legacy_result_text_does_not_replace_pending_markup():
    """Clean text in the legacy terminal event leaves an earlier message's markup pending."""
    events = [
        _legacy_message(MARKUP_FIXTURE),
        {"type": "result", "result": "all done", "is_error": False},
    ]
    chunks, session = await _parse(events)

    assert session.is_error is True, "clean result text superseded the pending markup"
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


# --- an indented line cannot interrupt an open paragraph ---


@pytest.mark.asyncio
async def test_an_indented_line_after_a_paragraph_line_is_that_paragraphs_text():
    """Four spaces after a paragraph line continue it, so the markup is prose."""
    text = f'Let me run it:\n    <{BAR}DSML{BAR} invoke name="x">'
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, (
        f"an indented line after a paragraph line was read as code: {text!r}"
    )
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


@pytest.mark.asyncio
async def test_a_tab_indented_line_after_a_paragraph_line_is_that_paragraphs_text():
    """A tab is the same: it cannot interrupt an open paragraph either."""
    text = f'Let me run it:\n\t<{BAR}DSML{BAR} invoke name="x">'
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, f"a tab-indented line hid the markup: {text!r}"
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


@pytest.mark.asyncio
async def test_an_indented_line_under_a_list_item_line_is_that_items_text():
    """A list item's continuation at four spaces is the item's text, not code."""
    text = f'- Call:\n    <{BAR}DSML{BAR} invoke name="x">'
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, f"a list-item continuation was read as code: {text!r}"
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


@pytest.mark.asyncio
async def test_an_indented_line_under_a_quote_line_is_that_quotes_text():
    """A quoted paragraph's continuation at four spaces is its text, not code."""
    text = f'> Here:\n>     <{BAR}DSML{BAR} invoke name="x">'
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, f"a quoted continuation was read as code: {text!r}"
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


@pytest.mark.asyncio
async def test_an_indented_line_after_a_heading_is_an_indented_code_block():
    """A heading opens no paragraph, so an indented line under it is code."""
    text = f'# Title\n    <{BAR}DSML{BAR} invoke name="x">'
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is False, f"a heading did not end the paragraph above it: {text!r}"
    assert _markup_errors(chunks) == [], f"a quotation was reported as a call: {chunks}"


# --- a closer may carry trailing spaces or tabs ---


@pytest.mark.asyncio
async def test_a_closer_with_trailing_spaces_closes_the_fence():
    """A closer's trailing spaces are not content, so the fence closes."""
    text = f'```xml\n<{BAR}DSML{BAR} invoke name="x">\n```  '
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is False, f"a closer with trailing spaces did not close: {text!r}"
    assert _markup_errors(chunks) == [], f"a quotation was reported as a call: {chunks}"


@pytest.mark.asyncio
async def test_a_closer_with_a_trailing_tab_closes_the_fence():
    """A tab after a tilde closer is not content either."""
    text = f'~~~\n<{BAR}DSML{BAR} invoke name="x">\n~~~\t'
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is False, f"a closer with a trailing tab did not close: {text!r}"
    assert _markup_errors(chunks) == [], f"a quotation was reported as a call: {chunks}"


# --- a fence inside nested containers, and a closer outside them ---


@pytest.mark.asyncio
async def test_a_fence_inside_a_list_item_quote_quotes_the_markup():
    """A list marker and a quote marker before a fence are both containers of it."""
    text = f'- > ```\n  > <{BAR}DSML{BAR} invoke name="x">\n  > ```'
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is False, f"a fence in nested containers was read as a call: {text!r}"
    assert _markup_errors(chunks) == [], f"a quotation was reported as a call: {chunks}"


@pytest.mark.asyncio
async def test_a_fence_inside_an_ordered_list_item_quote_quotes_the_markup():
    """The ordered-list form works the same way."""
    text = f'1. > ```\n   > <{BAR}DSML{BAR} invoke name="x">\n   > ```'
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is False, f"an ordered nested fence was read as a call: {text!r}"
    assert _markup_errors(chunks) == [], f"a quotation was reported as a call: {chunks}"


@pytest.mark.asyncio
async def test_a_line_outside_the_openers_quote_ends_the_fence():
    """A body line that drops the quote marker leaves the quote, so the fence ends
    there and the markup after it is prose."""
    text = f'> ```xml\n<{BAR}DSML{BAR} invoke name="x">\n> ```'
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, f"a quoted fence held on to a line outside its quote: {text!r}"
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


@pytest.mark.asyncio
async def test_a_line_outside_the_openers_list_indent_ends_the_fence():
    """A body line indented less than the list item's content leaves the item, so
    the fence ends there and the markup on that line is prose."""
    text = f'- ```xml\n  code\n<{BAR}DSML{BAR} invoke name="x">'
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, (
        f"a list-item fence held on to a line outside the item: {text!r}"
    )
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


# --- an escaped backtick may close a span ---


@pytest.mark.asyncio
async def test_an_escaped_backtick_closes_an_open_span():
    """A backslash-escaped backtick opens no span, but it does close one."""
    text = f'`code\\` <{BAR}DSML{BAR} invoke name="x">`'
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, f"an escaped closer hid the markup: {text!r}"
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


# --- a setext underline ends a paragraph ---


@pytest.mark.asyncio
async def test_a_setext_underline_ends_the_paragraph_before_it():
    """A run of `=` under a paragraph line makes it a heading, so the markup after
    the underline is prose."""
    text = f'Stray `a\n===\n<{BAR}DSML{BAR} invoke name="x">\n`'
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, f"a setext underline did not end the paragraph: {text!r}"
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


# --- a numbered marker other than `1.` does not interrupt a paragraph ---


@pytest.mark.asyncio
async def test_a_numbered_marker_other_than_one_does_not_interrupt_a_paragraph():
    """`2.` cannot start a list inside an open paragraph, so the span stays open
    and the tag in it is code."""
    text = f'Use `<{BAR}DSML{BAR} invoke name="x">\n2. y`'
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is False, f"`2.` interrupted the paragraph: {text!r}"
    assert _markup_errors(chunks) == [], f"a quotation was reported as a call: {chunks}"


@pytest.mark.asyncio
async def test_a_numbered_marker_of_one_does_interrupt_a_paragraph():
    """`1.` does start a list inside an open paragraph, so the span before it ends
    and the tag is prose."""
    text = f'Use `<{BAR}DSML{BAR} invoke name="x">\n1. y`'
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, f"`1.` did not interrupt the paragraph: {text!r}"
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


# --- an entity needs its semicolon ---


@pytest.mark.asyncio
async def test_an_entity_without_a_semicolon_names_no_tag():
    """A bare `&lt` is literal text to a reader, so the tag it would spell is not
    on screen and the turn completes."""
    text = f'&lt{BAR}DSML{BAR} invoke name="x">'
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is False, f"a bare `&lt` was decoded as a tag: {text!r}"
    assert _markup_errors(chunks) == [], f"a quotation was reported as a call: {chunks}"


@pytest.mark.asyncio
async def test_a_thematic_break_ends_the_paragraph_before_it():
    """A run of `*` on its own line is a thematic break, so the backtick above
    it and the one after the markup pair with nothing and the markup is prose."""
    text = f'Stray ` backtick\n***\n<{BAR}DSML{BAR} invoke name="x"> `'
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, f"a thematic break did not end the paragraph: {text!r}"
    assert len(_markup_errors(chunks)) == 1, f"expected one markup error chunk, got {chunks}"


@pytest.mark.parametrize(
    "text",
    [
        # The outer span opens first, so the single backticks inside it are its text.
        f'`` `a` <{BAR}DSML{BAR} invoke name="x"> ``',
        f'``` `a` ``b`` <{BAR}DSML{BAR} invoke name="x"> ```',
    ],
)
@pytest.mark.asyncio
async def test_markup_inside_a_span_holding_shorter_runs_is_code(text):
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is False, f"markup inside an outer code span was read: {text!r}"
    assert _markup_errors(chunks) == []


@pytest.mark.parametrize(
    "text",
    [
        f'> - ```\n>   <{BAR}DSML{BAR} invoke name="x">\n>   ```',
        f'- - ```\n    <{BAR}DSML{BAR} invoke name="x">\n    ```',
    ],
)
@pytest.mark.asyncio
async def test_a_fence_in_a_nested_item_holds_lines_indented_to_that_item(text):
    """An item's content column counts from where its marker line starts inside
    the containers before it."""
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is False, f"a nested item's fence body was read: {text!r}"
    assert _markup_errors(chunks) == []


@pytest.mark.parametrize(
    "text",
    [
        f'# Heading `\n<{BAR}DSML{BAR} invoke name="x"> `',
        f'> ```xml\n<{BAR}DSML{BAR} invoke name="x"> ```',
    ],
)
@pytest.mark.asyncio
async def test_a_line_that_is_a_block_of_its_own_pairs_no_backtick_after_it(text):
    """A heading, and a fence opener that never closes, are one-line blocks, so
    their backticks do not hide markup on the next line."""
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, f"markup after a one-line block was hidden: {text!r}"
    assert len(_markup_errors(chunks)) == 1


@pytest.mark.parametrize(("padding", "fails"), [(0, True), (1, False)], ids=["at", "past"])
def test_a_message_past_the_length_limit_is_not_read(padding, fails):
    from lionagi.providers.openai.codex import _MARKUP_CHECK_MAX_CHARS, _has_tool_call_markup

    tail = f"\n\n{INVOKE}"
    text = "x" * (_MARKUP_CHECK_MAX_CHARS - len(tail) + padding) + tail

    assert _has_tool_call_markup(text) is fails


@pytest.mark.parametrize(
    "unit",
    [
        "[",
        "[a ",
        "*a _b ",
        "`a ``b ",
        "<a ",
        "```a\n",
        ">" * 1000 + " x\n",
        "x <?",
        "x <!--",
        "x <![CDATA[",
    ],
    ids=[
        "brackets",
        "bracket-words",
        "emphasis",
        "backticks",
        "lt",
        "fences",
        "quotes",
        "open-instructions",
        "open-comments",
        "open-cdata",
    ],
)
def test_a_message_at_the_length_limit_is_read_in_bounded_time(unit):
    from lionagi.providers.openai.codex import _MARKUP_CHECK_MAX_CHARS, _has_tool_call_markup

    text = (unit * (_MARKUP_CHECK_MAX_CHARS // len(unit) + 1))[:_MARKUP_CHECK_MAX_CHARS]
    started = time.perf_counter()
    _has_tool_call_markup(text)
    assert time.perf_counter() - started < 5.0


def test_code_spans_cost_stays_near_linear_in_the_runs():
    from lionagi.providers.openai.codex import _has_tool_call_markup

    segment = "\\`` " * 40_000
    started = time.perf_counter()
    _has_tool_call_markup(segment)
    assert time.perf_counter() - started < 2.0


INVOKE = f'<{BAR}DSML{BAR}invoke name="x">'


@pytest.mark.parametrize(
    ("text", "fails"),
    [
        # Fences inside lists and quotes close where their container puts the closer.
        (f"- ```\n  {INVOKE}\n    ```", False),
        (f"> - ```\n>   {INVOKE}\n>     ```", False),
        (f"- > ```\n  > {INVOKE}\n    > ```", False),
        (f"- > - ```\n  >   {INVOKE}\n  >     ```", False),
        (f"- - ```\n    {INVOKE}\n    ```", False),
        (f"- - ~~~ {INVOKE}\n    code\n    ~~~", False),
        (f"> - ```\n>   {INVOKE}\n>\n>   ```", False),
        (f"> - ```\n>   {INVOKE}\n> \n>   ```", False),
        (f"> -\t```\n>   {INVOKE}\n>   ```", False),
        (f"> - ```\n>   {INVOKE}\n>   ```", False),
        # A closer indented past its item is fence text; the line after the real closer is prose.
        (f"- ```xml\n\t   ```\n  ```\n  {INVOKE}\n  ```", True),
        (f"> - ```xml\n>       ```\n>   ```\n>   {INVOKE}\n>   ```", True),
        (f"> - ```\n>       ```\n>   ```\n>     {INVOKE}\n>     ```", True),
        (f"- ```xml\n    ````\n{INVOKE} ```", True),
        (f"- ```xml\n  ```\n    {INVOKE}", True),
        # A code span runs across the lines of one paragraph, quoted or not.
        (f"> `{INVOKE}\n> `", False),
        (f"> `` `a`\n> {INVOKE} ``", False),
        (f"`` `a` {INVOKE} ``", False),
        # Only a bullet, or `1.` / `1)` followed by text, interrupts a paragraph.
        (f"> The call `\n> 2. {INVOKE} `", False),
        (f"The call `\n2.\n{INVOKE}`", False),
        (f"The call `\n1.\n{INVOKE}`", False),
        (f"Here are the steps:\n2. ```\n   {INVOKE}\n   ```", True),
        (f"Paragraph\n2.\n    {INVOKE}", True),
        (f"1. `\n2. {INVOKE} `", True),
        (f"1) `\n2) {INVOKE} `", True),
        # A setext underline ends a heading; the paragraph after it is prose.
        (f"The call `\n-\n{INVOKE}`", True),
        (f"Stray ` backtick\n---\n{INVOKE}", True),
        (f"# Heading `\n{INVOKE} `", True),
        (f"> Heading\n> ===\n>     {INVOKE}", False),
        (f"> Heading\n> ===\n    {INVOKE}", False),
        (f"> Heading\n> ===\n    `\n{INVOKE} `", True),
        # An underline cannot continue a quoted or listed paragraph lazily, so it is text.
        (f"> Paragraph\n===\n    {INVOKE}", True),
        (f"- Paragraph\n===\n    {INVOKE}", True),
        # Indentation is counted in columns from the start of the line.
        (f"> \t{INVOKE}", True),
        (f"-\n    {INVOKE}", True),
        # A quote ends at a line without its marker, and a fence in it ends there too.
        (f"> ```xml\n{INVOKE} ```", True),
        (f"> ```xml\n\n> {INVOKE}\n> ```", True),
    ],
)
@pytest.mark.asyncio
async def test_markup_is_read_where_commonmark_puts_it(text, fails):
    """Markup fails the turn exactly when CommonMark reads it as prose."""
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is fails, f"read wrongly: {text!r}"
    assert len(_markup_errors(chunks)) == (1 if fails else 0)


@pytest.mark.parametrize(
    ("text", "fails"),
    [
        (f"```\n{INVOKE}", False),
        (f"```\n{INVOKE}\n", False),
        (f"```\n\n{INVOKE}\n\n", False),
        (f"```\n{INVOKE}\n```", False),
        (f"```\n{INVOKE}\n```\n", False),
        (f"> ```\n> {INVOKE}", False),
        (f"> ```\n> {INVOKE}\n> ```", False),
        (f"- ```\n  {INVOKE}", False),
        (f"- ```\n  {INVOKE}\n  ```", False),
        (f"~~~ {INVOKE}\ntext", False),
        (f"~~~ {INVOKE}\ntext\n~~~", False),
        (f"```\nUse `{INVOKE}` like this", False),
        (f"```xml\n    {INVOKE}", False),
        (f" ```xml\n    {INVOKE}", False),
        (f"Paragraph\n```xml\n    {INVOKE}", False),
        (f"> ```\n>\t{INVOKE}", False),
        (f"> ```\n> `{INVOKE}\n`", False),
        (f"- ~~~\n  ```\n  {INVOKE}\n  \t```", False),
        (f"~~~ `{INVOKE}`\ntext", False),
        ('``` &lt;\uff5cDSML\uff5cinvoke name="x"&gt;\ntext', False),
        # The container ends, and the fence in it ends with it.
        (f"> ```\n> code\n\n{INVOKE}", True),
        (f"> ```\n> code\n{INVOKE}", True),
        (f"- ```\n  code\n\n{INVOKE}", True),
    ],
)
@pytest.mark.asyncio
async def test_a_fence_that_never_closes_runs_to_the_end_of_its_container(text, fails):
    """A fence is code to its closer or, with none, to the end of its container;
    its information string is not shown, so it is not read either."""
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is fails, f"read wrongly: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        f'<`x`{BAR}DSML{BAR}invoke name="x">',
        f'Ends with <\n\n{BAR}DSML{BAR}invoke name="x">',
        f'<{BAR}DSML{BAR}\n\ninvoke name="x">',
        f'<{BAR}DSML{BAR}invoke `name="x"`>',
        f"Write <{BAR}DSML{BAR}invoke `attr` when x > 0.",
        f"<{BAR}DSML{BAR}invoke\n`x`>",
    ],
)
@pytest.mark.asyncio
async def test_no_tag_is_pieced_together_across_a_code_span_or_a_block(text):
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is False, f"a tag was pieced together: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        f'<{BAR}DSML{BAR}invoke\nname="x">',
        f'<{BAR}DSML{BAR}invoke  \nname="x">',
        f'> <{BAR}DSML{BAR}invoke\n> name="x">',
    ],
)
@pytest.mark.asyncio
async def test_a_tag_written_across_the_lines_of_one_paragraph_fails_the_turn(text):
    """A line break inside a paragraph reads as a space, so a tag split over two
    of its lines is still a tag."""
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is True, f"a tag over two lines was missed: {text!r}"


@pytest.mark.parametrize(
    ("text", "fails"),
    [
        (f"<div>\n{INVOKE}\n</div>", True),
        (f"<pre>\n{INVOKE}\n</pre>", True),
        (f'<div>\n&lt;{BAR}DSML{BAR}invoke name="x"&gt;\n</div>', True),
        (f'<div>\n&lt{BAR}DSML{BAR}invoke name="x"&gt;\n</div>', False),
        (f"![{INVOKE}](x.png)", True),
        (f"![`{INVOKE}`](x.png)", False),
        (f"[{INVOKE}](https://example.com)", True),
    ],
)
@pytest.mark.asyncio
async def test_raw_html_and_link_text_are_read_as_prose(text, fails):
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is fails, f"read wrongly: {text!r}"


@pytest.mark.parametrize(
    ("text", "fails"),
    [
        ("> " * 19 + INVOKE, True),
        ("> " * 20 + INVOKE, False),
        ("\n".join("  " * i + "-" for i in range(9)) + " " + INVOKE, True),
        ("\n".join("  " * i + "-" for i in range(10)) + " " + INVOKE, False),
    ],
    ids=["19-quotes", "20-quotes", "9-lists", "10-lists"],
)
@pytest.mark.asyncio
async def test_text_at_the_nesting_limit_is_not_read(text, fails):
    """Text at the parser's nesting limit is not read, which completes the turn
    as it completed before this check."""
    chunks, session = await _parse([_agent_message(text), _turn_completed()])

    assert session.is_error is fails
