# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""One session failure produces one error chunk, and the chunk says it is one.

Two things reach a consumer when a codex session ends badly, and until now they
disagreed. The parser yields an error chunk as the failing event arrives. The
endpoint yields a second one as the session closes, unconditionally, so a real
``turn.failed`` was reported twice by two objects describing the same failure.
Neither carried ``is_error``, so a consumer reading the flag rather than the
type saw no failure at all.

The benign end-of-stream case is why the flag cannot simply be set on every
error-typed chunk. A resumed session that ends normally emits ``{"type":
"error"}`` with an empty payload, and that chunk is classified as *not* a
failure on purpose. It keeps ``is_error`` false, and the test below pins that
alongside the failure cases rather than in a separate file, because the two
decisions are one decision and a change to either belongs beside the other.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from lionagi.providers.openai.codex import CodexCLIEndpoint, CodexCodeRequest, stream_codex_cli
from lionagi.service.types.stream_chunk import StreamChunk


def _request() -> CodexCodeRequest:
    return CodexCodeRequest(prompt="test", verbose_output=False)


def _fake_events(events: list[dict]):
    async def fake(request):
        for event in events:
            yield event

    return patch(
        "lionagi.providers.openai.codex.stream_codex_cli_events",
        side_effect=fake,
    )


async def _parser_chunks(events: list[dict]) -> list[StreamChunk]:
    """What the parser alone yields, which is one half of what a consumer sees."""
    collected: list[StreamChunk] = []
    with _fake_events(events):
        async for item in stream_codex_cli(_request()):
            if isinstance(item, StreamChunk):
                collected.append(item)
    return collected


async def _endpoint_chunks(events: list[dict]) -> list[StreamChunk]:
    """What a consumer of the endpoint actually receives: parser chunks plus
    whatever the session-terminal branch adds on top."""
    endpoint = CodexCLIEndpoint()
    collected: list[StreamChunk] = []
    with _fake_events(events):
        async for chunk in endpoint.stream({"request": _request()}):
            collected.append(chunk)
    return collected


FAILURES = [
    pytest.param([{"type": "turn.failed", "error": {}}], id="turn-failed"),
    pytest.param(
        [{"type": "error", "error": {"code": "rate_limit"}}],
        id="error-with-payload",
    ),
]


@pytest.mark.anyio
@pytest.mark.parametrize("events", FAILURES)
async def test_a_failed_session_is_reported_exactly_once(events):
    chunks = await _endpoint_chunks(events)
    errors = [c for c in chunks if c.type == "error"]

    assert len(errors) == 1, (
        "one failure, one error chunk; a second describes the same failure again and a "
        f"consumer counting failures counts two: {errors}"
    )


@pytest.mark.anyio
@pytest.mark.parametrize("events", FAILURES)
async def test_a_failed_session_says_so_on_the_flag_as_well_as_the_type(events):
    """A consumer keying on ``is_error`` must not have to know the type vocabulary."""
    errors = [c for c in await _endpoint_chunks(events) if c.type == "error"]

    assert errors[0].is_error is True, (
        f"the failure chunk does not carry is_error, so the flag reads as success: {errors[0]}"
    )


@pytest.mark.anyio
@pytest.mark.parametrize("events", FAILURES)
async def test_the_parser_chunk_carries_the_flag_too(events):
    """The endpoint's guard means the parser's chunk is the one that survives, so
    the flag has to be right on that one, not only on the one it replaced."""
    errors = [c for c in await _parser_chunks(events) if c.type == "error"]

    assert len(errors) == 1
    assert errors[0].is_error is True, f"the surviving chunk reads as success: {errors[0]}"


@pytest.mark.anyio
async def test_a_benign_end_of_stream_still_reads_as_success():
    """The bare empty-error sentinel a resumed session ends with.

    Its type is "error" because that is the event the CLI sends. It is not a
    failure, the classification says so in the metadata, and the flag must
    agree with the classification rather than with the type.
    """
    chunks = await _endpoint_chunks([{"type": "error", "error": {}}])
    errors = [c for c in chunks if c.type == "error"]

    assert len(errors) == 1, f"the benign sentinel moved; this test describes nothing: {chunks}"
    assert errors[0].metadata.get("benign_eos") is True, (
        f"this arm is meant to exercise the benign path: {errors[0]}"
    )
    assert errors[0].is_error is False, (
        "a normal end of stream now reports itself as a failure, which is the whole "
        f"reason the flag could not be set on chunk type alone: {errors[0]}"
    )


@pytest.mark.anyio
async def test_a_terminal_result_that_reports_failure_is_reported_by_the_endpoint():
    """The path where the endpoint's chunk is the only one there is.

    A terminal ``result`` event carrying ``is_error`` marks the session failed
    without any error event having arrived, so the parser appends nothing and
    the guard's condition is true. This is what stops the guard from being a
    way to emit nothing at all, and it is the only arm that exercises the flag
    on the endpoint's own chunk.
    """
    events = [{"type": "result", "result": "boom", "is_error": True}]

    parser_errors = [c for c in await _parser_chunks(events) if c.type == "error"]
    assert parser_errors == [], (
        "the parser now reports this itself, so this arm no longer reaches the endpoint's "
        f"branch and stops testing what it says it tests: {parser_errors}"
    )

    errors = [c for c in await _endpoint_chunks(events) if c.type == "error"]
    assert len(errors) == 1, f"a failed session went unreported: {errors}"
    assert errors[0].is_error is True, f"the endpoint's own chunk reads as success: {errors[0]}"
    assert errors[0].content == "boom"


@pytest.mark.anyio
async def test_a_healthy_session_yields_no_error_chunk_at_all():
    """The must-not-match arm: the guard must not be satisfiable by a version
    that stops emitting, and a healthy session is where that would show."""
    chunks = await _endpoint_chunks([{"type": "turn.completed"}])

    assert [c for c in chunks if c.type == "error"] == [], (
        f"a healthy session reported a failure: {chunks}"
    )
