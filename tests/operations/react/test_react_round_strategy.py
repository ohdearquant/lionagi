# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A round declaring 'sequential' narrows its own batch; a caller's strategy is kept."""

import asyncio
import json
from typing import Literal

import pytest
from pydantic import BaseModel, Field

from lionagi.ln import AlcallParams
from lionagi.operations._defaults import make_parse_param
from lionagi.operations.act.act import _get_default_call_params
from lionagi.operations.ReAct.ReAct import ReActStream
from lionagi.operations.ReAct.utils import ReActAnalysis
from lionagi.operations.types import ActionParam, ChatParam
from lionagi.testing import TestBranch


def tool_branch(rounds, tools, answer):
    responses = [
        json.dumps({"analysis": "Use the requested tools", "extension_needed": False, **data})
        for data in rounds
    ]
    responses.append(json.dumps({"answer": answer}))
    return TestBranch.from_text(responses, tools=tools)


def traced_tools(trace):
    """Two tools that record a start and an end around a suspension point."""

    async def left(a: int) -> int:
        """Record a start and an end around a suspension point."""
        trace.append(("start", "left", a))
        await asyncio.sleep(0)
        trace.append(("end", "left", a))
        return a

    async def right(a: int) -> int:
        """Record a start and an end around a suspension point."""
        trace.append(("start", "right", a))
        await asyncio.sleep(0)
        trace.append(("end", "right", a))
        return a

    return [left, right]


def one_at_a_time(*calls):
    """The trace of calls that each finish before the next one starts."""
    return [step for name, a in calls for step in (("start", name, a), ("end", name, a))]


def in_flight_tool(state):
    """A tool that records the most calls in flight at once.

    Each call stays in flight until a second call has entered or a bounded number of
    suspension points has passed, so a one-at-a-time batch finishes with a peak of 1
    instead of waiting on a timer.
    """

    async def hold(name: str) -> str:
        """Stay in flight until another call has entered, or give up after a bound."""
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        for _ in range(50):
            if state["now"] >= 2:
                break
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        state["now"] -= 1
        return name

    return hold


def calls(*pairs):
    return [{"function": name, "arguments": {"a": a}} for name, a in pairs]


@pytest.mark.asyncio
async def test_a_first_round_declaring_sequential_runs_its_calls_one_at_a_time():
    """The round's own declaration orders its batch: each call finishes before the next starts."""
    trace = []
    branch = tool_branch(
        [{"action_strategy": "sequential", "action_requests": calls(("left", 1), ("right", 2))}],
        traced_tools(trace),
        "done",
    )

    assert await branch.ReAct(instruction="run both", tools=True, max_extensions=0) == "done"
    assert trace == one_at_a_time(("left", 1), ("right", 2))


@pytest.mark.asyncio
async def test_an_undeclared_first_round_keeps_the_concurrent_default():
    """With no declaration the batch stays concurrent: both calls are in flight together."""
    state = {"now": 0, "peak": 0}
    branch = tool_branch(
        [
            {
                "action_requests": [
                    {"function": "hold", "arguments": {"name": "left"}},
                    {"function": "hold", "arguments": {"name": "right"}},
                ]
            }
        ],
        [in_flight_tool(state)],
        "done",
    )

    assert await branch.ReAct(instruction="run both", tools=True, max_extensions=0) == "done"
    assert state["peak"] == 2


@pytest.mark.asyncio
async def test_a_second_round_declaring_sequential_orders_its_own_batch():
    """A round declaring 'sequential' orders its own batch even when the round before declared nothing."""
    trace = []
    branch = tool_branch(
        [
            {"extension_needed": True, "action_requests": calls(("left", 1))},
            {"action_strategy": "sequential", "action_requests": calls(("left", 3), ("right", 4))},
        ],
        traced_tools(trace),
        "done",
    )

    assert await branch.ReAct(instruction="run both", tools=True, max_extensions=1) == "done"
    assert trace == one_at_a_time(("left", 1), ("left", 3), ("right", 4))


@pytest.mark.asyncio
async def test_a_round_declaring_nothing_does_not_inherit_the_round_before_it():
    """A later round that declares no strategy runs concurrently, not under the previous round's."""
    trace = []
    branch = tool_branch(
        [
            {
                "extension_needed": True,
                "action_strategy": "sequential",
                "action_requests": calls(("left", 1)),
            },
            {"action_requests": calls(("left", 3), ("right", 4))},
        ],
        traced_tools(trace),
        "done",
    )

    assert await branch.ReAct(instruction="run both", tools=True, max_extensions=1) == "done"
    # Round 2 declares nothing, so its two calls enter together rather than one at a time.
    assert trace[:2] == [("start", "left", 1), ("end", "left", 1)]
    assert trace[2:4] == [("start", "left", 3), ("start", "right", 4)]
    assert sorted(trace[4:]) == [("end", "left", 3), ("end", "right", 4)]


@pytest.mark.asyncio
async def test_a_callers_sequential_strategy_holds_on_every_round():
    """A caller's own sequential ActionParam governs extension rounds as well as the first."""
    trace = []
    branch = TestBranch.from_text(
        [
            json.dumps(
                {
                    "analysis": "x",
                    "extension_needed": True,
                    "action_requests": calls(("left", 1), ("right", 2)),
                }
            ),
            json.dumps(
                {
                    "analysis": "y",
                    "extension_needed": False,
                    "action_requests": calls(("left", 3), ("right", 4)),
                }
            ),
            json.dumps({"answer": "done"}),
        ],
        tools=traced_tools(trace),
    )
    action_param = ActionParam(
        action_call_params=_get_default_call_params(),
        tools=True,
        strategy="sequential",
        suppress_errors=True,
        verbose_action=False,
    )

    async for _ in ReActStream(
        branch=branch,
        instruction="run",
        chat_param=ChatParam.from_branch(branch),
        action_param=action_param,
        parse_param=make_parse_param(
            ReActAnalysis, branch.chat_model, handle_validation="return_value"
        ),
        max_extensions=1,
        handle_validation="return_value",
    ):
        pass

    assert trace == one_at_a_time(("left", 1), ("right", 2), ("left", 3), ("right", 4))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "declared", [{}, {"action_strategy": "concurrent"}], ids=["omitted", "concurrent"]
)
async def test_a_turn_cannot_loosen_a_callers_sequential_strategy(declared):
    """A caller that asks operate for 'sequential' keeps it when the turn says otherwise."""
    trace = []
    turn = {
        "analysis": "Use the requested tools",
        "extension_needed": False,
        "action_requests": calls(("left", 1), ("right", 2)),
        **declared,
    }
    branch = TestBranch.from_text([json.dumps(turn)], tools=traced_tools(trace))

    await branch.operate(
        instruction="run both",
        response_format=ReActAnalysis,
        actions=True,
        action_strategy="sequential",
    )
    assert trace == one_at_a_time(("left", 1), ("right", 2))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "declared", [{}, {"action_strategy": "sequential"}], ids=["undeclared", "sequential"]
)
async def test_a_declared_sequential_turn_keeps_the_callers_call_timeout(declared):
    """A turn the model runs one call at a time is still bounded by the caller's retry_timeout."""
    release = asyncio.Event()

    async def stall(a: int) -> int:
        """Wait for a release that never comes."""
        await release.wait()
        return a

    turn = {
        "analysis": "Use the requested tools",
        "extension_needed": False,
        "action_requests": calls(("stall", 1)),
        **declared,
    }
    branch = TestBranch.from_text([json.dumps(turn)], tools=[stall])
    task = asyncio.ensure_future(
        branch.operate(
            instruction="go",
            response_format=ReActAnalysis,
            actions=True,
            call_params=AlcallParams(retry_timeout=0.05),
        )
    )
    try:
        done, _ = await asyncio.wait({task}, timeout=5)
        assert done, "the call outlived the caller's retry_timeout"
        with pytest.raises(TimeoutError):
            task.result()
    finally:
        task.cancel()


class SequentialByDefault(BaseModel):
    """A response model whose own default for the field is 'sequential'."""

    analysis: str = ""
    extension_needed: bool = False
    action_strategy: Literal["sequential", "concurrent"] = "sequential"


@pytest.mark.asyncio
async def test_a_response_models_default_is_not_a_declaration():
    """An omitted field filled from the model's default leaves the caller's concurrent strategy."""
    state = {"now": 0, "peak": 0}
    turn = {
        "analysis": "Use the requested tools",
        "action_requests": [
            {"function": "hold", "arguments": {"name": "left"}},
            {"function": "hold", "arguments": {"name": "right"}},
        ],
    }
    branch = TestBranch.from_text([json.dumps(turn)], tools=[in_flight_tool(state)])

    await branch.operate(instruction="run both", response_format=SequentialByDefault, actions=True)
    assert state["peak"] == 2


class SequentialByFactoryDefault(BaseModel):
    """A response model whose own default for the field comes from a factory."""

    analysis: str = ""
    extension_needed: bool = False
    action_strategy: Literal["sequential", "concurrent"] = Field(
        default_factory=lambda: "sequential"
    )


@pytest.mark.asyncio
async def test_a_factory_default_is_not_a_declaration():
    """An omitted field whose default is a factory leaves the caller's concurrent strategy."""
    state = {"now": 0, "peak": 0}
    turn = {
        "analysis": "Use the requested tools",
        "action_requests": [
            {"function": "hold", "arguments": {"name": "left"}},
            {"function": "hold", "arguments": {"name": "right"}},
        ],
    }
    branch = TestBranch.from_text([json.dumps(turn)], tools=[in_flight_tool(state)])

    await branch.operate(
        instruction="run both",
        response_format=SequentialByFactoryDefault,
        actions=True,
    )
    assert state["peak"] == 2


@pytest.mark.asyncio
async def test_a_turn_declaring_the_models_own_default_still_narrows():
    """A response that sets the field to the model's default narrows; only an omitted field does not."""
    trace = []
    turn = {
        "analysis": "Use the requested tools",
        "extension_needed": False,
        "action_strategy": "sequential",
        "action_requests": calls(("left", 1), ("right", 2)),
    }
    branch = TestBranch.from_text([json.dumps(turn)], tools=traced_tools(trace))

    await branch.operate(
        instruction="run both",
        response_format=SequentialByDefault,
        actions=True,
    )
    assert trace == one_at_a_time(("left", 1), ("right", 2))


@pytest.mark.asyncio
async def test_a_max_concurrent_kw_entry_does_not_widen_a_narrowed_batch():
    """The caller's own max_concurrent cannot undo a narrowing the turn declares."""
    state = {"now": 0, "peak": 0}
    turn = {
        "analysis": "Use the requested tools",
        "extension_needed": False,
        "action_strategy": "sequential",
        "action_requests": [
            {"function": "hold", "arguments": {"name": "left"}},
            {"function": "hold", "arguments": {"name": "right"}},
        ],
    }
    branch = TestBranch.from_text([json.dumps(turn)], tools=[in_flight_tool(state)])

    await branch.operate(
        instruction="run both",
        response_format=ReActAnalysis,
        actions=True,
        call_params=AlcallParams(kw={"max_concurrent": 3}),
    )
    assert state["peak"] == 1
