# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Emission events describe the failed reply, and a silent leg gets one fresh attempt on a fallback model."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from lionhub.orchestration.engines.engine import (
    RAW_RESPONSE_TEXT_CAP,
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
    run = _StubEngine(emission_reply_text=True).new_run(on_event=events.append)
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
    assert run.emission_failures == ["reviewer x2"]
    assert run.emission_fallbacks == []


@pytest.mark.asyncio
async def test_raw_text_is_capped_and_says_so() -> None:
    events: list[dict] = []
    run = _StubEngine(emission_reply_text=True).new_run(on_event=events.append)
    state = {"arrived": False}
    long = "x" * (RAW_RESPONSE_TEXT_CAP + 500)
    leg = _Leg("reviewer", [(_reply(long), False), (_reply("again"), False)], state)

    await run.operate_with_repair(leg, "go", arrived=lambda: state["arrived"], retries=1)

    raw = next(e for e in events if e["type"] == "emission_repair")["raw_response"]
    assert len(raw["text"]) == RAW_RESPONSE_TEXT_CAP
    assert raw["text_chars"] == RAW_RESPONSE_TEXT_CAP + 500
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
    assert run.emission_failures == ["bare x2"]


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
    assert run.emission_failures == []
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
    assert run.emission_failures == []


@pytest.mark.asyncio
async def test_a_silent_fallback_leg_counts_every_attempt_and_keeps_the_primary_name() -> None:
    events: list[dict] = []
    run = _StubEngine(emission_reply_text=True).new_run(on_event=events.append)
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
    assert run.emission_failures == ["p x4"]
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
    assert run.emission_failures == ["p x2"]


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

    assert made == [] and run.emission_fallbacks == [] and run.emission_failures == []


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
    assert run.emission_failures == []
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
    assert run.emission_failures == ["review-correctness x2"]
    assert run.emission_fallbacks == []


@pytest.mark.asyncio
async def test_reply_text_is_left_off_the_events_unless_the_engine_records_it() -> None:
    events: list[dict] = []
    run = _StubEngine().new_run(on_event=events.append)
    state = {"arrived": False}
    leg = _Leg("reviewer", [(_reply("a quoted diff line"), False), (_reply("more"), False)], state)

    await run.operate_with_repair(leg, "go", arrived=lambda: state["arrived"], retries=1)

    for kind in ("emission_repair", "emission_missing"):
        raw = next(e for e in events if e["type"] == kind)["raw_response"]
        assert "text" not in raw and "text_truncated" not in raw
        assert raw["stop_reason"] == "end_turn" and raw["model"] == "claude-haiku-5-5"
    assert next(e for e in events if e["type"] == "emission_repair")["raw_response"][
        "text_chars"
    ] == len("a quoted diff line")
    assert "a quoted diff line" not in repr(events)


@pytest.mark.asyncio
async def test_the_raw_reply_is_built_only_while_an_observer_is_attached(monkeypatch) -> None:
    import lionhub.orchestration.engines.engine as engine_module

    built: list[object] = []
    real = engine_module._raw_leg_response
    monkeypatch.setattr(
        engine_module, "_raw_leg_response", lambda leg, **kw: built.append(leg) or real(leg, **kw)
    )
    state = {"arrived": False}

    def legs() -> _Leg:
        return _Leg("reviewer", [(_reply("a"), False), (_reply("b"), False)], state)

    await (
        _StubEngine()
        .new_run()
        .operate_with_repair(legs(), "go", arrived=lambda: state["arrived"], retries=1)
    )
    assert built == []

    await (
        _StubEngine()
        .new_run(on_event=lambda event: None)
        .operate_with_repair(legs(), "go", arrived=lambda: state["arrived"], retries=1)
    )
    assert len(built) == 2


def _branch_with(model_response: dict, text: str = "t") -> Any:
    return SimpleNamespace(
        chat_model=SimpleNamespace(is_cli=False),
        msgs=SimpleNamespace(
            last_response=SimpleNamespace(response=text, model_response=model_response)
        ),
    )


@pytest.mark.parametrize(
    ("body", "stop_reason", "block_types"),
    [
        pytest.param(
            {"stop_reason": "max_tokens", "content": [{"type": "thinking"}, {"type": "text"}]},
            "max_tokens",
            ["thinking", "text"],
            id="anthropic-messages",
        ),
        pytest.param(
            {"choices": [{"finish_reason": "length", "message": {"content": "x"}}]},
            "length",
            None,
            id="chat-completions",
        ),
        pytest.param(
            {
                "object": "response",
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [{"type": "reasoning"}],
            },
            "max_output_tokens",
            ["reasoning"],
            id="responses-incomplete",
        ),
        pytest.param(
            {
                "object": "response",
                "status": "completed",
                "incomplete_details": None,
                "output": [{"type": "reasoning"}, {"type": "message"}],
            },
            "completed",
            ["reasoning", "message"],
            id="responses-completed",
        ),
        pytest.param(
            {"object": "response", "status": "completed"},
            "completed",
            None,
            id="responses-object-only",
        ),
        pytest.param(
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "content_filter"},
                "output": [{"type": "message"}],
            },
            "content_filter",
            ["message"],
            id="responses-without-object-key",
        ),
        pytest.param({"result": "done"}, None, None, id="unknown-shape"),
    ],
)
def test_the_reply_reader_follows_the_provider_shape(body, stop_reason, block_types) -> None:
    from lionhub.orchestration.engines.engine import _raw_leg_response

    raw = _raw_leg_response(_branch_with({"id": "r1", "model": "m", "usage": {"n": 1}, **body}))

    assert (raw["stop_reason"], raw["block_types"]) == (stop_reason, block_types)
    assert (raw["response_id"], raw["model"], raw["usage"]) == ("r1", "m", {"n": 1})


@pytest.mark.asyncio
async def test_repair_and_recovery_events_name_what_their_numbers_count() -> None:
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

    repair = [e for e in events if e["type"] == "emission_repair"]
    assert [(e["agent"], e["repair_index"], e["attempt"]) for e in repair] == [
        ("p", 1, 1),
        ("p-fb", 1, 1),
    ]
    recovered = next(e for e in events if e["type"] == "emission_recovered")
    assert (recovered["operate_call"], recovered["attempt"]) == (2, 2)


class _SlowLeg(_Leg):
    async def operate(self, *, instruction: str) -> str:
        import asyncio

        self.calls.append(instruction)
        await asyncio.sleep(30)
        return ""


@pytest.mark.asyncio
async def test_a_fallback_leg_past_its_deadline_is_abandoned_and_the_emission_stays_missing() -> (
    None
):
    events: list[dict] = []
    run = _StubEngine(fallback_deadline_s=0.05).new_run(on_event=events.append)
    state = {"arrived": False}
    primary = _Leg("p", [(_reply("a"), False), (_reply("first leg words"), False)], state)
    slow = _SlowLeg("p-fb", [], state)

    async def factory() -> _Leg:
        return slow

    result = await run.operate_with_repair(
        primary,
        "go",
        arrived=lambda: state["arrived"],
        retries=1,
        fallback=factory,
        fallback_model="m2",
    )

    assert result == "first leg words"
    assert slow.calls == ["go"]
    timeout = next(e for e in events if e["type"] == "emission_fallback_timeout")
    assert (timeout["agent"], timeout["model"], timeout["limit_s"]) == ("p", "m2", 0.05)
    missing = next(e for e in events if e["type"] == "emission_missing")
    assert missing["attempts"] == 3
    assert missing["raw_response"]["stop_reason"] == "end_turn"
    assert run.emission_failures == ["p x3"]
    assert run.emission_fallbacks == [] and "emission_recovered" not in _types(events)


@pytest.mark.asyncio
async def test_a_fallback_inside_its_deadline_recovers_as_before() -> None:
    events: list[dict] = []
    run = _StubEngine(fallback_deadline_s=30).new_run(on_event=events.append)
    state = {"arrived": False}
    primary = _Leg("p", [(_reply("a"), False), (_reply("b"), False)], state)
    fallback_leg = _Leg("p-fb", [(_reply("ok"), True)], state)

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

    assert "emission_fallback_timeout" not in _types(events)
    assert _types(events).count("emission_recovered") == 1


def test_an_optional_stage_model_never_falls_back_to_the_base_model() -> None:
    engine = _StubEngine(
        model="base-model", models={"review": "r", "review_fallback": "fb", "x": ""}
    )

    assert engine.optional_model_for("review_fallback") == "fb"
    assert engine.optional_model_for("missing") is None
    assert engine.optional_model_for("x") is None
    assert engine.model_for("missing") == "base-model"


def test_emission_failures_are_recorded_once_and_read_as_a_copy() -> None:
    run = _StubEngine().new_run()

    run.record_emission_failure("review-security (McpError)")
    run.record_emission_failure("review-security (McpError)")
    run.record_emission_failure("verify-clean (McpError)")
    read = run.emission_failures
    read.append("not recorded")

    assert run.emission_failures == ["review-security (McpError)", "verify-clean (McpError)"]


def test_the_engine_reads_empty_before_any_run_and_review_engine_forwards_the_options() -> None:
    assert _StubEngine().emission_failures == []
    review = ReviewEngine(emission_reply_text=True, fallback_deadline_s=45.0)
    assert (review.emission_reply_text, review.fallback_deadline_s) == (True, 45.0)
