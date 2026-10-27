# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Emission events carry the failed reply, and a silent leg gets one fresh attempt on a fallback model."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from lionhub.orchestration.engines.engine import (
    _RAW_RESPONSE_TEXT_CAP,
    Engine,
    EngineBudgetError,
)
from lionhub.orchestration.engines.review import ReviewEngine


class _StubEngine(Engine):
    async def _run(self, run, *args, **kwargs):  # pragma: no cover
        return ""


def _reply(text: str, *, stop_reason: str = "end_turn", content: list | None = None) -> Any:
    """A recorded assistant message shaped like AssistantResponse: .response and .model_response."""
    return SimpleNamespace(
        response=text,
        model_response={
            "id": "msg_test",
            "model": "claude-haiku-5-5",
            "stop_reason": stop_reason,
            "usage": {"input_tokens": 1200, "output_tokens": 64},
            "content": content if content is not None else [{"type": "text", "text": text}],
        },
    )


class _Leg:
    """Each operate() consumes the next (reply, arrives) step; arrives flips the shared flag."""

    def __init__(self, name: str, steps: list, state: dict, *, is_cli: bool = False) -> None:
        self.name = name
        self.chat_model = SimpleNamespace(is_cli=is_cli)
        self.msgs = SimpleNamespace(last_response=None)
        self.calls: list[str] = []
        self._steps = list(steps)
        self._state = state

    async def operate(self, *, instruction: str) -> str:
        self.calls.append(instruction)
        reply, arrives = self._steps.pop(0)
        self.msgs.last_response = reply
        if arrives:
            self._state["arrived"] = True
        return reply.response


def _types(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


@pytest.mark.asyncio
async def test_emission_events_carry_the_failed_reply() -> None:
    events: list[dict] = []
    run = _StubEngine().new_run(on_event=events.append)
    state = {"arrived": False}
    leg = _Leg(
        "reviewer",
        [
            (_reply("", stop_reason="max_tokens", content=[{"type": "thinking"}]), False),
            (_reply("still prose, no fence"), False),
        ],
        state,
    )

    await run.operate_with_repair(leg, "review this", arrived=lambda: state["arrived"], retries=1)

    repair = next(e for e in events if e["type"] == "emission_repair")
    first = repair["raw_response"]
    assert first["stop_reason"] == "max_tokens"
    assert first["block_types"] == ["thinking"]
    assert first["usage"] == {"input_tokens": 1200, "output_tokens": 64}
    assert first["model"] == "claude-haiku-5-5"
    assert first["http_status"] == 200
    assert first["text"] == "" and first["text_truncated"] is False
    missing = next(e for e in events if e["type"] == "emission_missing")
    assert missing["raw_response"]["text"] == "still prose, no fence"
    assert missing["attempts"] == 2
    assert run._emission_failures == ["reviewer x2"]
    assert run.emission_fallbacks == []


@pytest.mark.asyncio
async def test_raw_text_is_capped_and_says_so() -> None:
    events: list[dict] = []
    run = _StubEngine().new_run(on_event=events.append)
    state = {"arrived": False}
    long = "x" * (_RAW_RESPONSE_TEXT_CAP + 500)
    leg = _Leg("reviewer", [(_reply(long), False), (_reply("again"), False)], state)

    await run.operate_with_repair(leg, "go", arrived=lambda: state["arrived"], retries=1)

    raw = next(e for e in events if e["type"] == "emission_repair")["raw_response"]
    assert len(raw["text"]) == _RAW_RESPONSE_TEXT_CAP
    assert raw["text_chars"] == _RAW_RESPONSE_TEXT_CAP + 500
    assert raw["text_truncated"] is True


@pytest.mark.asyncio
async def test_cli_worker_reply_has_no_http_status() -> None:
    events: list[dict] = []
    run = _StubEngine().new_run(on_event=events.append)
    state = {"arrived": False}
    leg = _Leg("worker", [(_reply("prose"), False), (_reply("prose"), False)], state, is_cli=True)

    await run.operate_with_repair(leg, "go", arrived=lambda: state["arrived"], retries=1)

    assert (
        next(e for e in events if e["type"] == "emission_repair")["raw_response"]["http_status"]
        is None
    )


@pytest.mark.asyncio
async def test_a_leg_with_no_recorded_reply_adds_no_raw_response() -> None:
    events: list[dict] = []
    run = _StubEngine().new_run(on_event=events.append)

    class _NoMessages:
        name = "bare"
        chat_model = SimpleNamespace(is_cli=False)

        async def operate(self, *, instruction: str) -> str:
            return "prose"

    await run.operate_with_repair(_NoMessages(), "go", arrived=lambda: False, retries=1)

    assert all("raw_response" not in e for e in events)
    assert run._emission_failures == ["bare x2"]


@pytest.mark.asyncio
async def test_fallback_leg_that_emits_clears_the_missing_record() -> None:
    events: list[dict] = []
    run = _StubEngine().new_run(on_event=events.append)
    state = {"arrived": False}
    primary = _Leg("review-correctness", [(_reply("a"), False), (_reply("b"), False)], state)
    fallback_leg = _Leg("review-correctness-fallback", [(_reply("ok"), True)], state)
    made: list[str] = []

    async def factory() -> _Leg:
        made.append("fb")
        return fallback_leg

    await run.operate_with_repair(
        primary,
        "review this",
        arrived=lambda: state["arrived"],
        retries=1,
        fallback=factory,
        fallback_model="anthropic/claude-sonnet-5-5",
    )

    assert made == ["fb"]
    assert fallback_leg.calls == ["review this"]
    assert "emission_missing" not in _types(events)
    assert _types(events).count("emission_fallback") == 1
    recovered = next(e for e in events if e["type"] == "emission_recovered")
    assert (recovered["agent"], recovered["attempt"], recovered["after_attempts"]) == (
        "review-correctness",
        1,
        2,
    )
    assert run._emission_failures == []
    assert run.emission_fallbacks == [
        {
            "agent": "review-correctness",
            "model": "anthropic/claude-sonnet-5-5",
            "attempt": 1,
            "after_attempts": 2,
        }
    ]


@pytest.mark.asyncio
async def test_fallback_that_emits_only_after_its_repair_reports_attempt_two() -> None:
    events: list[dict] = []
    run = _StubEngine().new_run(on_event=events.append)
    state = {"arrived": False}
    primary = _Leg("p", [(_reply("a"), False), (_reply("b"), False)], state)
    fallback_leg = _Leg("p-fb", [(_reply("c"), False), (_reply("d"), True)], state)

    async def factory() -> _Leg:
        return fallback_leg

    await run.operate_with_repair(
        primary,
        "go",
        arrived=lambda: state["arrived"],
        retries=1,
        fallback=factory,
        fallback_model="m2",
    )

    assert run.emission_fallbacks[0]["attempt"] == 2
    assert "emission_missing" not in _types(events)
    assert run._emission_failures == []


@pytest.mark.asyncio
async def test_a_silent_fallback_leg_counts_every_attempt_and_keeps_the_primary_name() -> None:
    events: list[dict] = []
    run = _StubEngine().new_run(on_event=events.append)
    state = {"arrived": False}
    primary = _Leg("p", [(_reply("a"), False), (_reply("b"), False)], state)
    fallback_leg = _Leg("p-fb", [(_reply("c"), False), (_reply("last words"), False)], state)

    async def factory() -> _Leg:
        return fallback_leg

    await run.operate_with_repair(
        primary,
        "go",
        arrived=lambda: state["arrived"],
        retries=1,
        fallback=factory,
        fallback_model="m2",
    )

    missing = next(e for e in events if e["type"] == "emission_missing")
    assert missing["agent"] == "p" and missing["attempts"] == 4
    assert missing["raw_response"]["text"] == "last words"
    assert run._emission_failures == ["p x4"]
    assert run.emission_fallbacks == []
    assert "emission_recovered" not in _types(events)


@pytest.mark.asyncio
async def test_a_fallback_the_budget_cannot_afford_is_skipped_not_fatal() -> None:
    events: list[dict] = []
    run = _StubEngine().new_run(on_event=events.append)
    state = {"arrived": False}
    primary = _Leg("p", [(_reply("a"), False), (_reply("b"), False)], state)

    async def factory() -> _Leg:
        raise EngineBudgetError("agent budget exhausted (12/12)")

    await run.operate_with_repair(
        primary,
        "go",
        arrived=lambda: state["arrived"],
        retries=1,
        fallback=factory,
        fallback_model="m2",
    )

    skipped = next(e for e in events if e["type"] == "emission_fallback_skipped")
    assert skipped["reason"] == "agent_budget"
    assert next(e for e in events if e["type"] == "emission_missing")["attempts"] == 2
    assert run._emission_failures == ["p x2"]


@pytest.mark.asyncio
async def test_no_fallback_is_made_when_the_first_leg_emits() -> None:
    run = _StubEngine().new_run()
    state = {"arrived": False}
    primary = _Leg("p", [(_reply("ok"), True)], state)
    made: list[str] = []

    async def factory() -> _Leg:
        made.append("fb")
        raise AssertionError("fallback factory must not run")

    await run.operate_with_repair(
        primary,
        "go",
        arrived=lambda: state["arrived"],
        retries=1,
        fallback=factory,
        fallback_model="m2",
    )

    assert made == [] and run.emission_fallbacks == [] and run._emission_failures == []


def _review_with_scripted_legs(models: dict[str, str], legs: dict[str, _Leg]):
    """A ReviewEngine whose run builds the scripted leg for each (name) and reports the dimension once a leg arrives."""
    state = legs["review-correctness"]._state
    # A base model is set on purpose: model_for("review_fallback") answers with it when the
    # stage is unset, which is exactly the "retry on the same model" a fallback must not become.
    engine = ReviewEngine(
        dimensions=("correctness",), verify_clean=False, models=models, model="base-model"
    )
    engine._reported_dimensions = lambda run: {"correctness"} if state["arrived"] else set()
    built: list[dict] = []

    async def make_agent(role, *, name=None, model=None, **kwargs):
        built.append({"name": name, "model": model})
        return legs[name]

    return engine, built, make_agent


@pytest.mark.asyncio
async def test_review_dimension_retries_on_the_review_fallback_stage_model() -> None:
    state = {"arrived": False}
    legs = {
        "review-correctness": _Leg(
            "review-correctness", [(_reply("a"), False), (_reply("b"), False)], state
        ),
        "review-correctness-fallback": _Leg(
            "review-correctness-fallback", [(_reply("ok"), True)], state
        ),
    }
    engine, built, make_agent = _review_with_scripted_legs(
        {"review": "haiku-model", "review_fallback": "sonnet-model"}, legs
    )
    run = engine.new_run()
    run.make_agent = make_agent

    await engine._review_dimension(run, "the artifact", "correctness")

    assert built == [
        {"name": "review-correctness", "model": "haiku-model"},
        {"name": "review-correctness-fallback", "model": "sonnet-model"},
    ]
    assert run._emission_failures == []
    assert run.emission_fallbacks[0]["model"] == "sonnet-model"


@pytest.mark.asyncio
async def test_review_dimension_without_a_fallback_model_keeps_the_old_behaviour() -> None:
    state = {"arrived": False}
    legs = {
        "review-correctness": _Leg(
            "review-correctness", [(_reply("a"), False), (_reply("b"), False)], state
        ),
    }
    # Only the base model is set: model_for("review_fallback") would answer with it, so a
    # fallback read through model_for would retry on the SAME model and build a second leg.
    engine, built, make_agent = _review_with_scripted_legs({"review": "haiku-model"}, legs)
    run = engine.new_run()
    run.make_agent = make_agent

    await engine._review_dimension(run, "the artifact", "correctness")

    assert [b["name"] for b in built] == ["review-correctness"]
    assert run._emission_failures == ["review-correctness x2"]
    assert run.emission_fallbacks == []
