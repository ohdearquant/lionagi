# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""With the prompt cache placed, the review dimensions share one cached copy of the
artifact: it is sent once, in a turn of its own, and every dimension's request starts
from that turn, with its own task after it. The Anthropic endpoint is patched, so no
request leaves the process."""

import copy
import json
import os
from unittest.mock import patch

import pytest

from lionagi.ln.concurrency import fail_after
from lionagi.providers._provider_errors import ProviderError
from lionagi.providers.anthropic.messages import AnthropicMessagesEndpoint
from lionhub.orchestration.engines import ReviewEngine
from lionhub.orchestration.engines.review import DimensionClean, IssueFound

pytestmark = pytest.mark.asyncio

HAIKU = "anthropic/claude-haiku-5-5"
SONNET = "anthropic/claude-sonnet-5-5"
FIVE_MINUTES = {"type": "ephemeral"}
DIMENSIONS = ("correctness", "security", "performance", "maintainability")
ARTIFACT = "def first(xs):  # ARTIFACT-7f3c\n    return xs[0]\n"
TOKEN = "ARTIFACT-7f3c"
TURN = "Your review task follows in the next message"
VERDICT = "Issue a single ProposedVerdict"
# A phrase from each dimension's mode, which belongs in its task, not in the system prompt.
MODE_PHRASE = {
    "correctness": "Partition the problem into its full space of cases",
    "security": "First reconstruct the target",
    "performance": "Before asserting any non-trivial claim, classify its support",
    "maintainability": "Continuously check whether the active reasoning path",
}


def _response(text: str, model: str = "claude-haiku-5-5") -> dict:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _emission(key: str, **fields) -> str:
    return f"Done.\n```json\n{json.dumps({key: fields})}\n```\n"


def _blocks(message: dict) -> list[dict]:
    content = message["content"]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content if isinstance(b, dict)]


def _message_text(message: dict) -> str:
    return "".join(block.get("text", "") for block in _blocks(message))


def _last_text(payload: dict) -> str:
    return _message_text(payload["messages"][-1])


def _unmarked(value):
    """*value* with every cache mark removed, for comparing what two requests send.

    A message whose content is a string is the API's shorthand for one text block, so it is
    expanded to that block: a marked message is sent as blocks, the same message unmarked
    as the string, and the provider reads both as the same prefix.
    """
    if isinstance(value, dict):
        if isinstance(value.get("content"), str):
            value = {**value, "content": [{"type": "text", "text": value["content"]}]}
        return {k: _unmarked(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [_unmarked(v) for v in value]
    return value


def _dimension_of(payload: dict) -> str | None:
    last = _last_text(payload)
    found = [d for d in DIMENSIONS if f"for **{d}** only" in last]
    return found[0] if len(found) == 1 else None


class _Provider:
    """Answers each request by what its last message asks for, and keeps every payload sent."""

    def __init__(self, *, turn_reply="ready", turn_error=None, silent=()):
        self.sent: list[dict] = []
        self.turn_reply = turn_reply
        self.turn_error = turn_error
        self.silent = set(silent)

    def patches(self):
        build = AnthropicMessagesEndpoint.create_payload
        provider = self

        def spy(self, *args, **kwargs):
            payload, headers = build(self, *args, **kwargs)
            provider.sent.append(copy.deepcopy(payload))
            return payload, headers

        async def call(self, request, *_args, **_kwargs):
            # The dimensions are in flight together, so the answer is read off this
            # request's own payload, never the one built last.
            return provider.answer(request)

        return (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-anthropic-key"}),
            patch.object(AnthropicMessagesEndpoint, "create_payload", spy),
            patch.object(AnthropicMessagesEndpoint, "call", call),
        )

    def answer(self, payload: dict) -> dict:
        last = _last_text(payload)
        if TURN in last:
            if self.turn_error is not None:
                raise self.turn_error
            return _response(self.turn_reply)
        dimension = _dimension_of(payload)
        if dimension is not None:
            if dimension in self.silent and payload["model"] != SONNET.split("/")[1]:
                return _response("I looked and it seems fine.")
            return _response(
                _emission("dimension_clean", dimension=dimension, rationale="checked"),
                model=payload["model"],
            )
        if VERDICT in last:
            return _response("APPROVE: every dimension is clean.")
        return _response("ok")

    def turns(self) -> list[int]:
        return [i for i, p in enumerate(self.sent) if TURN in _last_text(p)]

    def dimension_requests(self) -> dict[str, list[int]]:
        out: dict[str, list[int]] = {}
        for i, payload in enumerate(self.sent):
            dimension = _dimension_of(payload)
            if dimension is not None:
                out.setdefault(dimension, []).append(i)
        return out


async def _review(provider: _Provider, **options):
    engine = ReviewEngine(
        model=HAIKU,
        khive_injection=False,
        dimensions=DIMENSIONS,
        verify_clean=False,
        **options,
    )
    events: list[dict] = []
    env, spy, call = provider.patches()
    with env, spy, call, fail_after(30):
        run = engine.new_run(on_event=events.append)
        out = await engine._run(run, ARTIFACT)
    return run, out, events


async def test_the_dimensions_read_one_artifact_turn_written_before_any_of_them():
    provider = _Provider()
    run, out, events = await _review(provider, prompt_caching=True)

    assert "APPROVE" in out
    assert {c.dimension for c in run.by_type(DimensionClean)} == set(DIMENSIONS)
    (turn,) = provider.turns()
    requests = provider.dimension_requests()
    assert sorted(requests) == sorted(DIMENSIONS)
    assert all(len(indexes) == 1 for indexes in requests.values())
    # The turn is the only request carrying the artifact as its newest message, and every
    # dimension request is built after it, so each can read the entry it wrote.
    assert all(indexes[0] > turn for indexes in requests.values())
    turn_payload = provider.sent[turn]
    assert TOKEN in _last_text(turn_payload)
    assert FIVE_MINUTES in [b.get("cache_control") for b in _blocks(turn_payload["messages"][-1])]

    for dimension, (index,) in requests.items():
        payload = provider.sent[index]
        # One system prompt for the turn and every dimension: no mode text in it.
        assert _unmarked(payload["system"]) == _unmarked(turn_payload["system"])
        assert all(phrase not in json.dumps(payload["system"]) for phrase in MODE_PHRASE.values())
        assert [m["role"] for m in payload["messages"]] == ["user", "assistant", "user"]
        # The first message is the turn's, byte for byte, so its prefix is the cached one.
        assert _unmarked(payload["messages"][0]) == _unmarked(turn_payload["messages"][0])
        assert _message_text(payload["messages"][1]) == "ready"
        # The artifact is in the shared turn only; the task and its mode come after it.
        assert TOKEN not in _last_text(payload)
        assert MODE_PHRASE[dimension] in _last_text(payload)
        assert FIVE_MINUTES in [b.get("cache_control") for b in _blocks(payload["messages"][-1])]
    assert not [e for e in events if e["type"].startswith("artifact_turn")]


async def test_without_the_cache_each_dimension_sends_the_artifact_ahead_of_its_task():
    provider = _Provider()
    run, out, _events = await _review(provider)

    assert "APPROVE" in out
    assert provider.turns() == []
    requests = provider.dimension_requests()
    assert sorted(requests) == sorted(DIMENSIONS)
    prefixes = set()
    for dimension, (index,) in requests.items():
        (message,) = provider.sent[index]["messages"]
        text = _message_text(message)
        end = text.index(TOKEN) + len(TOKEN)
        assert end < text.index(f"for **{dimension}** only")
        assert end < text.index(MODE_PHRASE[dimension])
        prefixes.add(text[:end])
    # Everything up to the artifact is the same text for every dimension.
    assert len(prefixes) == 1


async def test_a_single_dimension_sends_no_artifact_turn():
    provider = _Provider()
    engine = ReviewEngine(
        model=HAIKU,
        prompt_caching=True,
        khive_injection=False,
        dimensions=("correctness",),
        verify_clean=False,
    )
    env, spy, call = provider.patches()
    with env, spy, call:
        run = engine.new_run()
        await engine._run(run, ARTIFACT)

    assert provider.turns() == []
    ((index,),) = provider.dimension_requests().values()
    assert TOKEN in _last_text(provider.sent[index])


@pytest.mark.parametrize(
    "error", [ProviderError("overloaded"), RuntimeError("a defect")], ids=lambda e: type(e).__name__
)
async def test_a_failed_artifact_turn_leaves_every_dimension_its_own_artifact(error):
    provider = _Provider(turn_error=error)
    run, out, events = await _review(provider, prompt_caching=True)

    assert "APPROVE" in out
    assert {c.dimension for c in run.by_type(DimensionClean)} == set(DIMENSIONS)
    (failed,) = [e for e in events if e["type"] == "artifact_turn_failed"]
    assert failed["agent"] == "review-correctness"
    assert len(provider.turns()) == 1
    for dimension, (index,) in provider.dimension_requests().items():
        # The leader included: its failed turn left nothing in its history.
        (message,) = provider.sent[index]["messages"]
        assert TOKEN in _message_text(message)
        assert f"for **{dimension}** only" in _message_text(message)


async def test_a_leader_that_fails_before_its_turn_releases_the_other_dimensions():
    provider = _Provider()
    engine = ReviewEngine(
        model=HAIKU,
        prompt_caching=True,
        khive_injection=False,
        dimensions=DIMENSIONS,
        verify_clean=False,
    )
    events: list[dict] = []
    env, spy, call = provider.patches()
    with env, spy, call, fail_after(10):
        run = engine.new_run(on_event=events.append)
        build = run.make_agent

        async def make_agent(role, *, name=None, **kwargs):
            if name == "review-correctness":
                raise ProviderError("spawn failed")
            return await build(role, name=name, **kwargs)

        run.make_agent = make_agent
        await engine._run(run, ARTIFACT)

    assert [e["dimension"] for e in events if e["type"] == "dimension_failed"] == ["correctness"]
    assert {c.dimension for c in run.by_type(DimensionClean)} == set(DIMENSIONS) - {"correctness"}
    assert provider.turns() == []
    for _dimension, (index,) in provider.dimension_requests().items():
        (message,) = provider.sent[index]["messages"]
        assert TOKEN in _message_text(message)


async def test_a_reply_that_reviewed_is_not_copied_into_the_other_dimensions():
    issue = {
        "dimension": "correctness",
        "description": "an empty list raises IndexError",
        "severity": "minor",
        "location": "first.py:2",
        "confidence": 0.6,
    }
    provider = _Provider(turn_reply=_emission("issue_found", **issue))
    run, out, events = await _review(provider, prompt_caching=True)

    assert "APPROVE" in out
    (unused,) = [e for e in events if e["type"] == "artifact_turn_unused"]
    assert unused["agent"] == "review-correctness"
    # The one emission the turn carried is counted once; no copy re-emits it.
    assert [i.description for i in run.by_type(IssueFound)] == [issue["description"]]
    for dimension, (index,) in provider.dimension_requests().items():
        last = _last_text(provider.sent[index])
        assert TOKEN in last and f"for **{dimension}** only" in last
        if dimension != "correctness":
            assert len(provider.sent[index]["messages"]) == 1


async def test_a_fallback_leg_starts_from_the_artifact_turn():
    provider = _Provider(silent={"security"})
    run, out, _events = await _review(
        provider, prompt_caching=True, models={"review_fallback": SONNET}
    )

    assert "APPROVE" in out
    assert run.emission_fallbacks[0]["agent"] == "review-security"
    (turn,) = provider.turns()
    fallback = [
        p
        for p in provider.sent
        if p["model"] == SONNET.split("/")[1] and _dimension_of(p) == "security"
    ]
    assert fallback
    payload = fallback[0]
    assert _unmarked(payload["messages"][0]) == _unmarked(provider.sent[turn]["messages"][0])
    assert TOKEN not in _last_text(payload)
    assert MODE_PHRASE["security"] in _last_text(payload)
