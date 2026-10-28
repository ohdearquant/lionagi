# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Engine(max_tokens=...) and ReviewEngine(stage_max_tokens=...): the output-token limit a
stage's agent is built with, read off the payload its endpoint would send.

A reviewer that spends the whole default limit on thinking returns no text, and the
engine had no way to give one stage a larger limit.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from lionagi._errors import ConfigurationError
from lionhub.orchestration.engines import Engine, ReviewEngine
from lionhub.orchestration.engines.review import (
    DimensionClean,
    IssueFound,
    VerifyResult,
    _clean_ref,
    _verify_ref,
)

pytestmark = pytest.mark.asyncio

MODEL = "anthropic/claude-haiku-5-5"


def _sent_max_tokens(branch) -> int:
    request = {"messages": [{"role": "user", "content": "x"}], "model": "claude-haiku-5-5"}
    payload, _ = branch.chat_model.endpoint.create_payload(request)
    return payload["max_tokens"]


async def _agent(engine: Engine, name: str = "r1", model: str = MODEL, **kwargs):
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-anthropic-key"}):
        return await engine.new_run().make_agent("researcher", name=name, model=model, **kwargs)


# -- Engine: the run-wide limit and the per-call override -----------------------------------


async def test_the_engine_wide_limit_reaches_every_agent():
    engine = Engine(max_tokens=16384)
    for name in ("a", "b"):
        assert _sent_max_tokens(await _agent(engine, name)) == 16384


async def test_an_engine_without_it_builds_agents_on_the_providers_default():
    """Control: the default engine sends 8192, so the assertion above reads the limit."""
    assert _sent_max_tokens(await _agent(Engine())) == 8192


async def test_a_call_that_names_a_limit_outranks_the_engine():
    engine = Engine(max_tokens=16384)
    assert _sent_max_tokens(await _agent(engine, max_tokens=24000)) == 24000


async def test_a_cli_agent_under_an_engine_limit_is_refused():
    with pytest.raises(ConfigurationError, match="no output-token limit to set"):
        await _agent(Engine(max_tokens=16384), model="claude_code/haiku")


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "16384"])
async def test_a_limit_that_is_not_a_positive_integer_is_refused(bad):
    with pytest.raises(ConfigurationError, match="max_tokens must be a positive integer"):
        Engine(max_tokens=bad)


# -- ReviewEngine: the per-stage map ---------------------------------------------------------


async def test_the_stage_limit_outranks_the_engine_wide_one_and_unnamed_stages_fall_back():
    engine = ReviewEngine(max_tokens=9000, stage_max_tokens={"review": 16384})
    assert engine.max_tokens_for("review") == 16384
    assert engine.max_tokens_for("verify") == 9000
    assert ReviewEngine(stage_max_tokens={"review": 16384}).max_tokens_for("verify") is None


async def test_a_stage_the_engine_builds_no_agent_for_is_refused():
    with pytest.raises(ConfigurationError, match="builds no agent for"):
        ReviewEngine(stage_max_tokens={"reveiw": 16384})


@pytest.mark.parametrize("bad", [0, True, "16384"])
async def test_a_stage_limit_that_is_not_a_positive_integer_is_refused(bad):
    with pytest.raises(ConfigurationError, match=r"stage_max_tokens\['review'\] must be"):
        ReviewEngine(stage_max_tokens={"review": bad})


class _Emitter:
    """operate() emits the event registered for this agent's name, once."""

    def __init__(self, run, name: str, event):
        self.name = name
        self._run = run
        self._event = event
        self.calls = 0

    async def operate(self, *, instruction: str):
        self.calls += 1
        if self._event is not None and self.calls == 1:
            await self._run.emit(self._event)
        return None


class _Synth:
    async def operate(self, *, instruction: str):
        return "APPROVE"


def _recording_make(run, events_by_name: dict, seen: dict):
    async def make(role, *, name=None, **kw):
        seen[name] = kw.get("max_tokens")
        if name == "verdict":
            return _Synth()
        return _Emitter(run, name or role, events_by_name.get(name))

    return make


STAGES = {"review": 111, "verify": 222, "verdict": 333}


async def test_a_clean_review_builds_its_review_verify_and_verdict_agents_with_their_limits():
    dims = ("correctness",)
    engine = ReviewEngine(dimensions=dims, stage_max_tokens=STAGES)
    run = engine.new_run()
    seen: dict = {}
    run.make_agent = _recording_make(
        run,
        {
            "review-correctness": DimensionClean(dimension="correctness", rationale="checked"),
            "verify-clean": VerifyResult(
                issue="CLEAN: nothing", ref=_clean_ref(dims), holds=True, rationale="ran it"
            ),
        },
        seen,
    )
    await engine._run(run, "ARTIFACT")
    assert seen == {"review-correctness": 111, "verify-clean": 222, "verdict": 333}


async def test_an_issue_verifier_is_built_with_the_verify_limit():
    engine = ReviewEngine(dimensions=("security",), stage_max_tokens=STAGES)
    run = engine.new_run()
    issue = IssueFound(dimension="security", description="sqli", severity="critical")
    seen: dict = {}
    run.make_agent = _recording_make(
        run,
        {
            "review-security": issue,
            "verify-security": VerifyResult(
                issue="sqli", ref=_verify_ref(issue), holds=True, rationale="reproduced"
            ),
        },
        seen,
    )
    await engine._run(run, "ARTIFACT")
    assert seen["review-security"] == 111
    assert seen["verify-security"] == 222
    assert seen["verdict"] == 333


async def test_a_stage_without_a_limit_leaves_the_agent_on_the_default():
    """Control: unset stages pass None, so make_agent keeps the provider's default."""
    dims = ("correctness",)
    engine = ReviewEngine(dimensions=dims, stage_max_tokens={"review": 111})
    run = engine.new_run()
    seen: dict = {}
    run.make_agent = _recording_make(
        run,
        {
            "review-correctness": DimensionClean(dimension="correctness", rationale="checked"),
            "verify-clean": VerifyResult(
                issue="CLEAN: nothing", ref=_clean_ref(dims), holds=True, rationale="ran it"
            ),
        },
        seen,
    )
    await engine._run(run, "ARTIFACT")
    assert seen == {"review-correctness": 111, "verify-clean": None, "verdict": None}


def _reply(text: str):
    return SimpleNamespace(
        response=text,
        model_response={
            "id": "msg_test",
            "model": "claude-haiku-5-5",
            "stop_reason": "max_tokens",
            "usage": {"input_tokens": 10, "output_tokens": 8192},
            "content": [{"type": "thinking", "thinking": "..."}],
        },
    )


class _Leg:
    def __init__(self, steps: list, state: dict):
        self.chat_model = SimpleNamespace(is_cli=False)
        self.msgs = SimpleNamespace(last_response=None)
        self._steps = list(steps)
        self._state = state

    async def operate(self, *, instruction: str):
        reply, arrives = self._steps.pop(0)
        self.msgs.last_response = reply
        if arrives:
            self._state["arrived"] = True
        return reply.response


async def test_the_fallback_leg_is_built_with_the_review_fallback_limit():
    state = {"arrived": False}
    legs = {
        "review-correctness": _Leg([(_reply(""), False), (_reply(""), False)], state),
        "review-correctness-fallback": _Leg([(_reply("ok"), True)], state),
    }
    engine = ReviewEngine(
        dimensions=("correctness",),
        verify_clean=False,
        models={"review": "haiku-model", "review_fallback": "sonnet-model"},
        stage_max_tokens={"review": 16384, "review_fallback": 20000},
    )
    engine._reported_dimensions = lambda run: {"correctness"} if state["arrived"] else set()
    seen: dict = {}

    async def make_agent(role, *, name=None, model=None, **kwargs):
        seen[name] = (model, kwargs.get("max_tokens"))
        return legs[name]

    run = engine.new_run()
    run.make_agent = make_agent
    await engine._review_dimension(run, "the artifact", "correctness")

    assert seen == {
        "review-correctness": ("haiku-model", 16384),
        "review-correctness-fallback": ("sonnet-model", 20000),
    }
