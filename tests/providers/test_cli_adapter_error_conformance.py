# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""What every CLI adapter must show a stream consumer when a session fails.

Four CLI adapters each decide, independently, what reaches a consumer when a
run ends in failure. Two of them agree, two diverge. Each has its own tests, and
several of those go into more detail on their own adapter than anything here
does; what none of them does is compare the four, so a fifth adapter, or a
refactor of an existing one, reopens the class in silence and the next reader
rebuilds the comparison by hand.

The contract, per adapter, in three separate assertions:

1. a session finishing with ``is_error`` set yields EXACTLY ONE chunk of type
   ``error``,
2. that chunk carries ``is_error``, and
3. a session finishing WITHOUT ``is_error`` yields ZERO chunks of type
   ``error``.

"Exactly one" is the contract and not a stylistic preference. "At least one"
cannot express "this failure was not reported twice", so the already-reported
guard some adapters carry would have no expression here at all. Assertion 3 is
the other direction: without it, an adapter that reported errors on healthy
sessions would pass cleanly, and "emits on failure" and "emits unconditionally"
are different claims that only a healthy fixture can tell apart.

Where the tests patch, and why not one level up
-----------------------------------------------
Each test patches ``<module>._ndjson_from_cli``, the module-private wrapper
around the shared NDJSON reader. All four adapters call it by bare module-global
name through two levels of real code (the parser calls an events function, which
calls the seam), so the fixture is fed to the real parser, the real parser builds
the real session, and the real ``stream()`` decides what to yield. Nothing here
hand-builds a session and hands it to an endpoint: that tests the endpoint
against a state the parser never produces, which is mutation-sensitive and
unreachable at the same time.

The CLI-binary guard (``if not CLAUDE_CLI``, and its three siblings) lives in
that events function, ABOVE the seam, so patching the seam alone still raises on
a machine where that CLI is not installed. Each test therefore also points the
binary constant at a stub path. That keeps the real events function in the loop.
Three of the four append a terminal ``{"type": "done"}`` themselves, which is why
no fixture below carries one; gemini's appends nothing and its parser needs no
sentinel, so the same fixture shape is right for all four for two different
reasons.

Fixture fidelity
----------------
Every fixture is labelled ``RECORDED`` or ``AUTHORED`` and the label is
load-bearing. An authored event dict is written from what our own parser reads,
so it agrees with the parser by construction and inherits its blind spots
exactly -- and the blind spot is the failure mode under test, since a declared
failure disappears precisely when nobody reads the field. An authored fixture
therefore cannot contain a field the parser ignores, and cannot reveal a CLI
that signals failure through a channel we never consult. It would pass, cleanly,
forever. Only a recording is evidence about the world; an authored dict is
evidence about our model of it. Replacing the authored ones with captured
transcripts is tracked separately.

Landing red, on purpose
-----------------------
This suite lands with the remaining known divergence marked ``xfail(strict=True)``.
Strict matters: non-strict would let the suite stay green on the day a
divergence is fixed while still claiming the gap is open, and a stale
expectation reads as current state. It has already earned that once: codex's
marks were removed because the divergence they named was fixed, and strict is
what turned that into a failing run rather than a quiet pass.

AN EXPECTED FAILURE IS UNMARKED BY THE DIVERGENCE IT NAMES BEING FIXED, NEVER BY
A PASSING RUN. An xfail here that starts passing is a signal to investigate, not
to delete the mark: either the divergence was fixed and this was not updated with
it, or the test stopped testing what it names. Every mark names what it waits on.

Who emits the error chunk differs, and it decides whether a guard is reachable
-----------------------------------------------------------------------------
The four adapters do not satisfy (or miss) this contract the same way, and the
difference is in which layer constructs the chunk:

- ``claude_code`` constructs one only in the endpoint, inside an
  already-reported guard. Its parser never constructs one, so that guard cannot
  fire on any real event sequence today. It is pinned intent, not live cover.
- ``gemini_code`` constructs one in the parser on the failing path, and its
  endpoint guard is what stops a second. Here the guard IS reachable, which
  makes gemini the one adapter where "exactly one" is non-vacuous today.
- ``codex`` constructs one in the parser AND one in the endpoint, and until
  recently there was no guard between them, so a real ``turn.failed`` event was
  reported twice. That is why "at least one" would have been the wrong
  contract: it passed on codex while the defect was live. The guard now exists
  and codex satisfies all three assertions unmarked.
- ``pi`` constructs none at all.

Codex also yields an ``error``-type chunk when a resumed session ends normally,
which looks like a violation of the third assertion and is not one. That chunk
carries ``is_error=False`` and ``benign_eos=True``, both set deliberately, and
the classification behind them is pinned by its own tests. The fixtures here use
``turn.completed`` for the healthy codex case, so the two do not collide. A
consumer keying on chunk type alone still cannot tell that case from a failure,
but the discriminators exist and removing the chunk would break a decision that
was made on purpose.

What this suite does not cover
------------------------------
- The non-streaming path. ``_call()`` drives the same generator and returns the
  session as a dict with nothing branching on the flag.
- ReAct's final-answer turn, which catches broadly and substitutes the last
  response.
- Per-tool error carriers, which are a separate signal and not the same
  contract. Three adapters emit ``tool_result`` chunks carrying their own
  ``is_error``; gemini emits none, because agy's json format has no per-tool
  events for it to read.
- Passing says the adapters agree with each other and with the fixtures. Where a
  fixture is AUTHORED it says nothing about the real CLI.
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
#
# Codex used to be marked here for the double report and for its error chunks
# not setting is_error. That divergence is fixed and closed out, so the mark is
# gone and the codex parameters are held to the contract like any other. This
# is the only reason a mark comes off.
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


# --------------------------------------------------------------------------- fixtures
#
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

ADAPTERS: list[Adapter] = [
    Adapter(
        id="claude_code",
        module="lionagi.providers.anthropic.claude_code",
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
        module="lionagi.providers.google.gemini_code",
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
        module="lionagi.providers.openai.codex",
        endpoint_cls="CodexCLIEndpoint",
        request_cls="CodexCodeRequest",
        binary_attr="CODEX_CLI",
        failing=Fixture(
            [{"type": "turn.failed", "error": {"message": "sandbox denied the write"}}],
            AUTHORED,
        ),
        healthy=Fixture(
            [{"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 3}}],
            AUTHORED,
        ),
    ),
    Adapter(
        id="pi",
        module="lionagi.providers.pi.cli",
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

    async def fake_ndjson(_request):
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


# --------------------------------------------------------------------------- the contract


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


# --------------------------------------------------------------------------- per-adapter


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
    """
    adapter = next(a for a in ADAPTERS if a.id == "codex")
    benign_eos = Fixture([{"type": "error", "error": {}}], AUTHORED)
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


def test_every_fixture_declares_where_it_came_from():
    """The labels are the only thing separating evidence about the world from
    evidence about our own model of it, so a missing one is a defect."""
    for adapter in ADAPTERS:
        for name, fixture in (("failing", adapter.failing), ("healthy", adapter.healthy)):
            assert fixture.origin in (RECORDED, AUTHORED), (
                f"{adapter.id}.{name} has no origin label"
            )
            assert fixture.events, f"{adapter.id}.{name} is empty"
