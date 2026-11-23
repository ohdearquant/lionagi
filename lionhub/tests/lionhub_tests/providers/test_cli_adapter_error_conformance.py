# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""What every CLI adapter must show a stream consumer when a session fails.

Full design rationale, the per-adapter comparison, and what this suite does
not cover: see docs/internals/providers.md#cli-adapter-error-chunk-conformance.

The contract, per adapter, in three separate assertions:

1. a session finishing with ``is_error`` set yields EXACTLY ONE chunk of type
   ``error``,
2. that chunk carries ``is_error``, and
3. a session finishing WITHOUT ``is_error`` yields ZERO chunks of type
   ``error``.

Each test patches ``<module>._ndjson_from_cli``, the module-private wrapper
around the shared NDJSON reader, so the fixture is fed to the real parser and
the real ``stream()`` decides what to yield -- nothing here hand-builds a
session and hands it to an endpoint. The CLI-binary guard lives above that
seam, so each test also points the binary constant at a stub path to keep
the real events function in the loop.

This suite lands with the remaining known divergence (pi) marked
``xfail(strict=True)``. An expected failure is unmarked by the divergence it
names being fixed, never by a passing run: a passing xfail here means either
the divergence was fixed without updating this test, or the test stopped
testing what it names.
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

RECORDED = "recorded"
AUTHORED = "authored"

_UNMARK_RULE = (
    "Unmark this ONLY when {gap} is fixed and that work is closed out, never because "
    "the run went green. A passing xfail here means either the divergence was fixed "
    "without closing it out, or this test stopped testing what it names."
)

# Each mark names the specific divergence it is waiting on, so it cannot be
# removed by anything less than that divergence going away.
_PI_GAP = "pi emitting no error chunk and delivering the failure as a result chunk"


def _recorded_events(name: str) -> list[dict]:
    """Load a captured CLI transcript shipped with the library."""
    from lionagi import testing as _lt

    path = Path(_lt.__file__).resolve().parent / "data" / name
    return [json.loads(line) for line in path.read_text().splitlines() if line]


@dataclass(frozen=True)
class Fixture:
    """One NDJSON event stream, carrying where it came from."""

    events: list[dict]
    origin: str
    note: str = ""


@dataclass(frozen=True)
class Adapter:
    """One CLI adapter and the two event streams that exercise its contract."""

    id: str
    module: str
    endpoint_cls: str
    request_cls: str
    binary_attr: str
    failing: Fixture
    healthy: Fixture
    request_kwargs: dict[str, Any] = field(default_factory=lambda: {"prompt": "test"})


# Each failing stream is the terminal event shape that sets ``is_error`` on that
# adapter's session, read off the parser rather than guessed. Each healthy
# stream is the same event without the failure, so the pair differs in one fact.

_CLAUDE_RESULT = {
    "type": "result",
    "result": "the model refused",
    "usage": {"input_tokens": 10, "output_tokens": 2},
    "total_cost_usd": 0.001,
    "num_turns": 1,
    "duration_ms": 100,
    "duration_api_ms": 90,
    "is_error": True,
}

_GEMINI_TERMINAL = {
    "status": "ERROR",
    "response": "",
    "error": "quota exceeded",
    "conversation_id": "conv-1",
    "num_turns": 1,
    "duration_seconds": 1.0,
}

_CODEX_TEXT = {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}
_CODEX_TURN_COMPLETED = {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 3}}

ADAPTERS: list[Adapter] = [
    Adapter(
        id="claude_code",
        module="lionhub.providers.anthropic.claude_code",
        endpoint_cls="ClaudeCodeCLIEndpoint",
        request_cls="ClaudeCodeRequest",
        binary_attr="CLAUDE_CLI",
        request_kwargs={"prompt": "test", "verbose_output": False},
        failing=Fixture([dict(_CLAUDE_RESULT)], AUTHORED),
        healthy=Fixture(
            [{**_CLAUDE_RESULT, "is_error": False, "result": "done"}],
            AUTHORED,
        ),
    ),
    Adapter(
        id="gemini_code",
        module="lionhub.providers.google.gemini_code",
        endpoint_cls="GeminiCLIEndpoint",
        request_cls="GeminiCodeRequest",
        binary_attr="AGY_CLI",
        failing=Fixture([dict(_GEMINI_TERMINAL)], AUTHORED),
        # A SUCCESS with empty content is treated as an error by this parser on
        # purpose (an auto-denied tool call looks like that), so the healthy
        # fixture must carry content or it would not be healthy.
        healthy=Fixture(
            [{**_GEMINI_TERMINAL, "status": "SUCCESS", "response": "hello", "error": ""}],
            AUTHORED,
        ),
    ),
    Adapter(
        id="codex",
        module="lionhub.providers.openai.codex",
        endpoint_cls="CodexCLIEndpoint",
        request_cls="CodexCodeRequest",
        binary_attr="CODEX_CLI",
        failing=Fixture(
            [{"type": "turn.failed", "error": {"message": "sandbox denied the write"}}],
            AUTHORED,
        ),
        # A turn that writes no text fails as an empty turn (the pairs below), so the healthy
        # stream carries the turn's text.
        healthy=Fixture([_CODEX_TEXT, _CODEX_TURN_COMPLETED], AUTHORED),
    ),
    Adapter(
        id="pi",
        module="lionhub.providers.pi.cli",
        endpoint_cls="PiCLIEndpoint",
        request_cls="PiCodeRequest",
        binary_attr="PI_CLI",
        failing=Fixture(
            [{"type": "error", "error": {"message": "provider returned 500"}}],
            AUTHORED,
        ),
        healthy=Fixture(
            _recorded_events("pi_cli_events.jsonl"),
            RECORDED,
            note="a real pi run that succeeded, captured from the CLI",
        ),
    ),
]


async def _stream_chunks(adapter: Adapter, fixture: Fixture, monkeypatch) -> list:
    mod = importlib.import_module(adapter.module)

    async def fake_ndjson(_request, **_kwargs):
        for event in fixture.events:
            yield event

    # The binary guard sits above the seam, so the seam patch alone is not
    # enough where the CLI is absent. Nothing here executes the path that would
    # use this value.
    monkeypatch.setattr(mod, adapter.binary_attr, "/nonexistent/bin/stub-for-tests")
    monkeypatch.setattr(mod, "_ndjson_from_cli", fake_ndjson)

    endpoint = getattr(mod, adapter.endpoint_cls)()
    request = getattr(mod, adapter.request_cls)(**adapter.request_kwargs)
    return [chunk async for chunk in endpoint.stream({"request": request})]


def _params(diverging: dict[str, str]):
    """Parameter list where *diverging* maps an adapter id to the divergence that
    must be gone before its mark comes off."""
    out = []
    for adapter in ADAPTERS:
        gap = diverging.get(adapter.id)
        marks = [pytest.mark.xfail(strict=True, reason=_UNMARK_RULE.format(gap=gap))] if gap else []
        out.append(pytest.param(adapter, marks=marks, id=adapter.id))
    return out


@pytest.mark.parametrize("adapter", _params({"pi": _PI_GAP}))
async def test_a_failed_session_yields_exactly_one_error_chunk(adapter, monkeypatch):
    chunks = await _stream_chunks(adapter, adapter.failing, monkeypatch)
    errors = [c for c in chunks if c.type == "error"]

    assert len(errors) == 1, (
        f"{adapter.id}: a failed session produced {len(errors)} error chunks; a consumer "
        f"branching on chunk type sees {'nothing' if not errors else 'the failure twice'}"
    )


@pytest.mark.parametrize("adapter", _params({"pi": _PI_GAP}))
async def test_the_error_chunk_carries_the_error_flag(adapter, monkeypatch):
    chunks = await _stream_chunks(adapter, adapter.failing, monkeypatch)
    errors = [c for c in chunks if c.type == "error"]

    assert errors, f"{adapter.id}: no error chunk to carry the flag"
    assert all(c.is_error for c in errors), (
        f"{adapter.id}: the error chunk does not set is_error, so the one consumer "
        "that reads the flag rather than the type cannot see this failure"
    )


@pytest.mark.parametrize("adapter", _params({}))
async def test_a_healthy_session_yields_no_error_chunk(adapter, monkeypatch):
    chunks = await _stream_chunks(adapter, adapter.healthy, monkeypatch)

    assert [c for c in chunks if c.type == "error"] == [], (
        f"{adapter.id}: a session that did not fail reported an error, which is worse "
        "than the defect this contract exists to fix"
    )
    assert chunks, (
        f"{adapter.id}: the healthy fixture produced no chunks at all, so this asserts "
        "nothing about the error path"
    )


@pytest.mark.xfail(strict=True, reason=_UNMARK_RULE.format(gap=_PI_GAP))
async def test_pi_does_not_deliver_a_failure_wearing_the_type_that_means_success(monkeypatch):
    """pi's divergence is sharper than "no error chunk", and the sharper form is
    what has to be asserted.

    On a failed session pi yields one chunk of type ``result`` whose content is
    the error message: the error text survives while wearing the type that means
    success. A consumer keying on type sees a clean result, a consumer reading
    content sees an error string, and neither can tell it from success by the
    contract. Asserting only the missing error chunk would let a partial fix add
    one while leaving this in place.
    """
    adapter = next(a for a in ADAPTERS if a.id == "pi")
    chunks = await _stream_chunks(adapter, adapter.failing, monkeypatch)

    results = [c for c in chunks if c.type == "result"]
    assert not any("500" in (c.content or "") for c in results), (
        "the failure was delivered as a result chunk carrying the error message"
    )


async def test_codex_benign_end_of_stream_keeps_its_discriminators(monkeypatch):
    """The one healthy-session error chunk this contract tolerates, and why.

    Some Codex CLI versions emit ``{"type": "error", "error": {}}`` when a
    resumed session ends normally. The parser classifies that at some length:
    three conditions, the raw payload captured before null-normalisation
    specifically so an explicit ``null`` cannot be mistaken for the bare ``{}``
    sentinel. Having decided it is benign it retracts ``session.is_error`` and
    tags the metadata, then yields an error-type chunk anyway.

    That reads like a violation of the third assertion, and the first version of
    this file asserted the chunk away. It should not be asserted away: the tag
    is the contract, pinned by the parser's own tests, and a consumer reading
    either discriminator gets the right answer. What this test pins is that both
    discriminators survive, because it is their presence, not the chunk's
    absence, that keeps the case distinguishable from a real failure.

    The sentinel follows the turn's text here. After a turn with none, the
    stream's end reports an empty turn instead (the empty-turn pairs below).
    """
    adapter = next(a for a in ADAPTERS if a.id == "codex")
    benign_eos = Fixture([_CODEX_TEXT, {"type": "error", "error": {}}], AUTHORED)
    chunks = await _stream_chunks(adapter, benign_eos, monkeypatch)

    errors = [c for c in chunks if c.type == "error"]
    assert len(errors) == 1, (
        "the benign end-of-stream chunk moved; this test no longer describes the path"
    )
    assert errors[0].is_error is False, (
        "a benign end-of-stream set the failure flag, so a consumer reading the flag "
        "now sees a failure on a session that succeeded"
    )
    assert errors[0].metadata.get("benign_eos") is True, (
        "the benign classification stopped reaching the chunk, leaving an error-type "
        "chunk on a healthy session with nothing on it to say so"
    )


@dataclass(frozen=True)
class EmptyTurnPair:
    """One provider's two streams for the empty-turn rule.

    The two differ in one fact, whether the turn wrote assistant text. A turn
    that ended the way a finished turn ends (no error event, the CLI exiting 0)
    with no text and no tool call must fail, and the same turn with its text
    must not.
    """

    adapter_id: str
    with_text: Fixture
    empty: Fixture


# A provider joins the empty-turn rule with one pair here; its row in ADAPTERS
# supplies the rest.
EMPTY_TURN_PAIRS: list[EmptyTurnPair] = [
    EmptyTurnPair(
        adapter_id="codex",
        with_text=Fixture([_CODEX_TEXT, _CODEX_TURN_COMPLETED], AUTHORED),
        empty=Fixture([_CODEX_TURN_COMPLETED], AUTHORED),
    ),
]


def _empty_turn_params():
    by_id = {adapter.id: adapter for adapter in ADAPTERS}
    return [
        pytest.param(by_id[pair.adapter_id], pair, id=pair.adapter_id) for pair in EMPTY_TURN_PAIRS
    ]


@pytest.mark.parametrize(("adapter", "pair"), _empty_turn_params())
async def test_an_empty_turn_yields_exactly_one_error_chunk_naming_it(adapter, pair, monkeypatch):
    chunks = await _stream_chunks(adapter, pair.empty, monkeypatch)
    errors = [c for c in chunks if c.type == "error"]

    assert len(errors) == 1, (
        f"{adapter.id}: a turn with nothing in it produced {len(errors)} error chunks; read as "
        "a completed run it is an empty answer with no reason attached"
    )
    assert errors[0].is_error is True
    assert errors[0].metadata.get("empty_turn") is True


@pytest.mark.parametrize(("adapter", "pair"), _empty_turn_params())
async def test_the_same_turn_with_its_text_yields_no_error_chunk(adapter, pair, monkeypatch):
    chunks = await _stream_chunks(adapter, pair.with_text, monkeypatch)

    assert [c for c in chunks if c.type == "error"] == [], (
        f"{adapter.id}: a turn that wrote text was failed as empty"
    )
    assert any(c.type == "text" for c in chunks), (
        f"{adapter.id}: the with-text stream yielded no text, so the pair differs in nothing"
    )


def test_every_fixture_declares_where_it_came_from():
    """The labels are the only thing separating evidence about the world from
    evidence about our own model of it, so a missing one is a defect."""
    for adapter in ADAPTERS:
        for name, fixture in (("failing", adapter.failing), ("healthy", adapter.healthy)):
            assert fixture.origin in (RECORDED, AUTHORED), (
                f"{adapter.id}.{name} has no origin label"
            )
            assert fixture.events, f"{adapter.id}.{name} is empty"
    for pair in EMPTY_TURN_PAIRS:
        for name, fixture in (("with_text", pair.with_text), ("empty", pair.empty)):
            assert fixture.origin in (RECORDED, AUTHORED), (
                f"{pair.adapter_id}.{name} has no origin label"
            )
            assert fixture.events, f"{pair.adapter_id}.{name} is empty"
