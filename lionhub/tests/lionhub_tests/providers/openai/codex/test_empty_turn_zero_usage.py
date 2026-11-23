# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""An empty codex turn whose usage counts are all zero and whose stream had no item says, before
the stderr tail, that the prompt did not reach the model. Every other empty turn keeps the plain
failure text, and the failure stays an empty turn either way.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from lionagi.service.types.cli_session import CLISession
from lionagi.service.types.stream_chunk import StreamChunk
from lionhub.providers._empty_turn import empty_turn_chunk, empty_turn_error
from lionhub.providers.openai.codex import _PROMPT_NOT_SENT, CodexCodeRequest, stream_codex_cli

_FAILURE = "The CLI ended the turn without assistant text, a tool call or an error (empty_turn)."

# Recorded: `codex exec --json` from codex 0.162.1, in a scratch codex home whose
# UserPromptSubmit hook writes a message to stderr and exits 2. This is the whole stream and the
# whole stderr; the hook's message is in neither. The 401 line comes from the scratch home having
# no sign-in, and the same home without the hook prints it too.
_ZERO_USAGE = {
    "input_tokens": 0,
    "cached_input_tokens": 0,
    "cache_write_input_tokens": 0,
    "output_tokens": 0,
    "reasoning_output_tokens": 0,
}
HOOK_BLOCKED_EVENTS = [
    {"type": "thread.started", "thread_id": "01a20717-4e08-7fc3-aed8-443389f35c38"},
    {"type": "turn.started"},
    {"type": "turn.completed", "usage": _ZERO_USAGE},
]
HOOK_BLOCKED_STDERR = (
    "Reading additional input from stdin...\n"
    "2026-11-23T06:28:17.915592Z ERROR codex_api::endpoint::responses_websocket: failed to "
    "connect to websocket: HTTP error: 401 Unauthorized, url: wss://api.openai.com/v1/responses\n"
)
_TAIL = HOOK_BLOCKED_STDERR.strip()


async def _empty_turn_failure(events: list[dict]) -> tuple[StreamChunk, CLISession]:
    """The one failure chunk of a stream that ends with the CLI's clean exit, and the session."""

    async def fake_events(request):
        for event in events:
            yield event
        yield {"type": "done", "stderr": HOOK_BLOCKED_STDERR}

    session = CLISession()
    chunks = []
    with patch("lionhub.providers.openai.codex.stream_codex_cli_events", side_effect=fake_events):
        async for item in stream_codex_cli(
            CodexCodeRequest(prompt="Reply with the single word: ok.", verbose_output=False),
            session=session,
        ):
            if isinstance(item, StreamChunk):
                chunks.append(item)
    failures = [c for c in chunks if c.is_error]
    assert len(failures) == 1, failures
    assert failures[0].metadata.get("empty_turn") is True
    assert failures[0].metadata.get("stderr_tail") == _TAIL
    return failures[0], session


@pytest.mark.asyncio
async def test_the_recorded_hook_blocked_turn_says_the_prompt_did_not_reach_the_model():
    failure, session = await _empty_turn_failure(HOOK_BLOCKED_EVENTS)

    expected = f"{_FAILURE} {_PROMPT_NOT_SENT} Its stderr ended with: {_TAIL}"
    assert failure.content == expected
    assert session.result == expected
    assert session.is_error is True
    # The sentence comes before the tail, which here is about a missing sign-in.
    assert failure.content.index("did not reach the model") < failure.content.index("401")
    error = empty_turn_error(session)
    assert error is not None and str(error).startswith(f"{_FAILURE} {_PROMPT_NOT_SENT}")


# Authored from the recorded stream, each changed in one fact.
_NO_SENTENCE_CASES = {
    # The turn reached the model and wrote nothing.
    "input_counted": [
        HOOK_BLOCKED_EVENTS[0],
        HOOK_BLOCKED_EVENTS[1],
        {"type": "turn.completed", "usage": {**_ZERO_USAGE, "input_tokens": 12}},
    ],
    "output_counted": [
        HOOK_BLOCKED_EVENTS[0],
        HOOK_BLOCKED_EVENTS[1],
        {"type": "turn.completed", "usage": {**_ZERO_USAGE, "reasoning_output_tokens": 4}},
    ],
    # An item that is neither text nor a call leaves the turn empty but shows the model ran.
    "an_item": [
        HOOK_BLOCKED_EVENTS[0],
        HOOK_BLOCKED_EVENTS[1],
        {"type": "item.completed", "item": {"type": "reasoning", "text": "thinking"}},
        {"type": "turn.completed", "usage": _ZERO_USAGE},
    ],
    "an_item_started": [
        HOOK_BLOCKED_EVENTS[0],
        HOOK_BLOCKED_EVENTS[1],
        {"type": "item.started", "item": {"type": "reasoning"}},
        {"type": "turn.completed", "usage": _ZERO_USAGE},
    ],
    # A usage value that is not a count is not read as a zero count.
    "usage_not_all_counts": [
        HOOK_BLOCKED_EVENTS[0],
        HOOK_BLOCKED_EVENTS[1],
        {"type": "turn.completed", "usage": {**_ZERO_USAGE, "details": {}}},
    ],
    "usage_empty": [
        HOOK_BLOCKED_EVENTS[0],
        HOOK_BLOCKED_EVENTS[1],
        {"type": "turn.completed", "usage": {}},
    ],
    # No turn end: nothing reported usage at all.
    "no_turn_completed": HOOK_BLOCKED_EVENTS[:2],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(_NO_SENTENCE_CASES))
async def test_any_other_empty_turn_keeps_the_plain_failure_text(case):
    failure, session = await _empty_turn_failure(_NO_SENTENCE_CASES[case])

    expected = f"{_FAILURE} Its stderr ended with: {_TAIL}"
    assert failure.content == expected
    assert session.result == expected


def test_a_cause_sentence_goes_between_the_failure_and_the_stderr_sentence():
    session = CLISession()
    chunk = empty_turn_chunk(session, "last line\n", cause="Why it was empty.")
    assert chunk.content == f"{_FAILURE} Why it was empty. Its stderr ended with: last line"
    assert chunk.metadata == {"empty_turn": True, "stderr_tail": "last line"}

    plain = empty_turn_chunk(CLISession(), "last line\n")
    assert plain.content == f"{_FAILURE} Its stderr ended with: last line"
    assert empty_turn_chunk(CLISession(), None, cause="").content == (
        f"{_FAILURE} No stderr was captured."
    )
