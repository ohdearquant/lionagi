# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A codex turn that ends cleanly with nothing in it fails, quoting what its stderr said.

The CLI can exit 0 after ``turn.completed`` having written no assistant text and called no
tool. Read as a completed run, that is an empty answer with no reason attached, and a caller
reading only the status takes it for success. These arms hold the parser, the endpoint's call,
the run loop and the run's reason to one answer: the run failed as ``run.failed.empty_turn``,
and the failure quotes the end of the CLI's stderr.
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

import pytest

from lionagi._provider_errors import ProviderEmptyTurnError
from lionagi.service.types.cli_session import CLISession
from lionagi.service.types.stream_chunk import StreamChunk
from lionagi.state.reasons import RunReasons
from lionhub.orchestration.runs import resolve_run_reason
from lionhub.providers._empty_turn import STDERR_TAIL_CHARS, is_empty_turn
from lionhub.providers.openai.codex import CodexCLIEndpoint, CodexCodeRequest, stream_codex_cli

TURN_COMPLETED = {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 0}}
BENIGN_SENTINEL = {"type": "error", "error": {}}


def _agent_message(text: str) -> dict:
    return {"type": "item.completed", "item": {"type": "agent_message", "text": text}}


def _done(stderr: str, unread: str | None = None) -> dict:
    """The closing event the events function adds after a clean exit."""
    return {"type": "done", "stderr": stderr, "stderr_unread": unread}


def _request() -> CodexCodeRequest:
    return CodexCodeRequest(prompt="test", verbose_output=False)


def _fake_events(events: list[dict]):
    async def fake(request):
        for event in events:
            yield event

    return patch("lionhub.providers.openai.codex.stream_codex_cli_events", side_effect=fake)


async def _parse(events: list[dict]) -> tuple[list[StreamChunk], CLISession]:
    chunks: list[StreamChunk] = []
    session = None
    with _fake_events(events):
        async for item in stream_codex_cli(_request()):
            if isinstance(item, StreamChunk):
                chunks.append(item)
            elif isinstance(item, CLISession):
                session = item
    assert session is not None
    return chunks, session


def _empty_turn(chunks: list[StreamChunk]) -> list[StreamChunk]:
    return [c for c in chunks if c.metadata.get("empty_turn")]


class TestTheParser:
    async def test_a_turn_with_no_text_and_no_tool_call_fails_once(self):
        chunks, session = await _parse([TURN_COMPLETED, _done("")])

        errors = [c for c in chunks if c.type == "error"]
        assert len(errors) == 1, errors
        assert errors[0].is_error is True
        assert errors[0].metadata.get("empty_turn") is True
        assert session.is_error is True
        assert "(empty_turn)" in session.result

    async def test_the_same_turn_with_text_does_not(self):
        chunks, session = await _parse([_agent_message("done"), TURN_COMPLETED, _done("")])

        assert [c for c in chunks if c.type == "error"] == []
        assert session.is_error is False

    async def test_whitespace_is_not_text(self):
        chunks, _ = await _parse([_agent_message("  \n\t "), TURN_COMPLETED, _done("")])

        assert len(_empty_turn(chunks)) == 1

    async def test_a_real_failure_is_not_relabelled_as_empty(self):
        chunks, _ = await _parse([{"type": "turn.failed", "error": {"message": "boom"}}])

        assert _empty_turn(chunks) == []
        assert [c.content for c in chunks if c.is_error] == ["boom"]

    def test_a_tool_call_alone_is_not_an_empty_turn(self):
        session = CLISession()
        session.tool_uses.append({"id": "call-1", "name": "Bash", "input": {"command": "ls"}})
        assert is_empty_turn(session) is False

    def test_a_tool_result_alone_is_not_an_empty_turn(self):
        session = CLISession()
        session.tool_results.append({"tool_use_id": "call-1", "content": "ok"})
        assert is_empty_turn(session) is False


class TestTheBenignEndOfStream:
    async def test_after_text_it_keeps_its_benign_classification(self):
        chunks, session = await _parse([_agent_message("done"), BENIGN_SENTINEL])

        errors = [c for c in chunks if c.type == "error"]
        assert len(errors) == 1
        assert errors[0].metadata.get("benign_eos") is True
        assert errors[0].is_error is False
        assert session.is_error is False

    async def test_with_no_text_before_it_the_turn_is_empty(self):
        """The sentinel is not yielded then: the stream's end decides, and the one
        error chunk a consumer sees is the empty turn's."""
        chunks, session = await _parse([BENIGN_SENTINEL])

        errors = [c for c in chunks if c.type == "error"]
        assert len(errors) == 1, errors
        assert errors[0].metadata.get("empty_turn") is True
        assert not errors[0].metadata.get("benign_eos")
        assert session.is_error is True


class TestWhatTheFailureQuotes:
    async def test_the_end_of_stderr(self):
        chunks, _ = await _parse([TURN_COMPLETED, _done("starting\na hook refused the prompt\n")])

        (chunk,) = _empty_turn(chunks)
        assert chunk.content.endswith("Its stderr ended with: starting\na hook refused the prompt")
        assert chunk.metadata["stderr_tail"] == "starting\na hook refused the prompt"

    async def test_only_the_end_of_a_long_stderr_and_says_so(self):
        stderr = "x" * (STDERR_TAIL_CHARS + 50) + "END"
        chunks, _ = await _parse([TURN_COMPLETED, _done(stderr)])

        (chunk,) = _empty_turn(chunks)
        tail = chunk.metadata["stderr_tail"]
        assert len(tail) == STDERR_TAIL_CHARS
        assert tail.endswith("END")
        assert "[earlier stderr omitted]" in chunk.content

    async def test_that_the_cli_wrote_nothing_to_stderr(self):
        chunks, _ = await _parse([TURN_COMPLETED, _done("")])

        (chunk,) = _empty_turn(chunks)
        assert chunk.content.endswith("It wrote nothing to stderr.")

    async def test_why_stderr_could_not_be_read(self):
        chunks, _ = await _parse([TURN_COMPLETED, _done("", "its stderr drain did not finish")])

        (chunk,) = _empty_turn(chunks)
        assert chunk.content.endswith(
            "Its stderr could not be read: its stderr drain did not finish."
        )

    async def test_that_no_stderr_was_captured(self):
        """A stream with no closing event (the events function did not see a clean
        exit) has no stderr to quote, and says that rather than that it was empty."""
        chunks, _ = await _parse([TURN_COMPLETED])

        (chunk,) = _empty_turn(chunks)
        assert chunk.content.endswith("No stderr was captured.")


class TestTheEndpoint:
    async def test_the_call_raises_the_empty_turn_error(self):
        endpoint = CodexCLIEndpoint()
        with _fake_events([TURN_COMPLETED, _done("a hook refused the prompt")]):
            with pytest.raises(ProviderEmptyTurnError) as caught:
                await endpoint._call({"request": _request()}, {})

        assert caught.value.stderr_tail == "a hook refused the prompt"
        assert "a hook refused the prompt" in str(caught.value)
        assert type(caught.value).retryable is False

    async def test_the_stream_reports_it_exactly_once(self):
        endpoint = CodexCLIEndpoint()
        with _fake_events([TURN_COMPLETED, _done("")]):
            chunks = [c async for c in endpoint.stream({"request": _request()})]

        errors = [c for c in chunks if c.type == "error"]
        assert len(errors) == 1, errors
        assert errors[0].metadata.get("empty_turn") is True


async def test_the_run_loop_raises_it():
    """run() reads the error chunk's marker and raises the typed error rather than
    classifying the text, so the run ends with this class whatever stderr said."""
    from lionagi.operations.run.run import RunParam, run
    from lionagi.service.imodel import iModel
    from lionagi.session.branch import Branch

    chunks, _ = await _parse(
        [{"type": "thread.started", "thread_id": "t-1"}, TURN_COMPLETED, _done("quota: 429")]
    )

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")
    model.endpoint = types.SimpleNamespace(
        is_cli=True, session_id=None, to_dict=lambda: {"type": "fake_cli"}
    )
    model.streaming_process_func = None

    async def create_event(**kw):
        return object()

    async def stream(api_call=None):
        for chunk in chunks:
            yield chunk

    model.create_event = create_event
    model.executor = types.SimpleNamespace(append=AsyncMock(), config={})
    model.stream = stream
    branch = Branch()
    branch.chat_model = model

    with pytest.raises(ProviderEmptyTurnError) as caught:
        async for _ in run(branch, "hi", RunParam()):
            pass
    assert caught.value.stderr_tail == "quota: 429"


def test_the_run_reason_names_it():
    """Reached through the stream wrapper's ValueError, as a real run reaches it."""
    try:
        try:
            raise ProviderEmptyTurnError("empty (empty_turn). It wrote nothing to stderr.")
        except ProviderEmptyTurnError as inner:
            raise ValueError(f"Failed to stream API call: {inner}") from inner
    except ValueError as wrapped:
        code, summary, evidence = resolve_run_reason(status="failed", exception=wrapped)

    assert code == RunReasons.FAILED_EMPTY_TURN == "run.failed.empty_turn"
    assert summary.startswith("ProviderEmptyTurnError: ")
    assert evidence is None
