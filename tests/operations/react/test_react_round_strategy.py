# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Each ReAct round's batch runs under the action strategy that round declares."""

import asyncio
import json

import pytest

from lionagi.operations.ReAct.utils import ReActAnalysis
from lionagi.testing import TestBranch


def tool_branch(rounds, tools, answer):
    responses = [
        json.dumps({"analysis": "Use the requested tools", "extension_needed": False, **data})
        for data in rounds
    ]
    responses.append(json.dumps({"answer": answer}))
    return TestBranch.from_text(responses, tools=tools)


@pytest.mark.asyncio
async def test_a_first_round_declaring_sequential_runs_its_calls_one_at_a_time():
    """The round's own declaration orders its batch: each call finishes before the next starts."""
    trace = []

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

    branch = tool_branch(
        [
            {
                "action_strategy": "sequential",
                "action_requests": [
                    {"function": "left", "arguments": {"a": 1}},
                    {"function": "right", "arguments": {"a": 2}},
                ],
            }
        ],
        [left, right],
        "done",
    )

    assert await branch.ReAct(instruction="run both", tools=True, max_extensions=0) == "done"
    assert trace == [
        ("start", "left", 1),
        ("end", "left", 1),
        ("start", "right", 2),
        ("end", "right", 2),
    ]


@pytest.mark.asyncio
async def test_an_undeclared_first_round_keeps_the_concurrent_default():
    """With no declaration the batch stays concurrent: both calls are in flight together."""
    entered = []
    both_entered = asyncio.Event()

    async def wait_for_the_other(name: str) -> str:
        """Enter, then wait (bounded) for the other call to have entered too."""
        entered.append(name)
        if len(entered) == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), timeout=2)
        return name

    branch = tool_branch(
        [
            {
                "action_requests": [
                    {"function": "wait_for_the_other", "arguments": {"name": "left"}},
                    {"function": "wait_for_the_other", "arguments": {"name": "right"}},
                ]
            }
        ],
        [wait_for_the_other],
        "done",
    )

    assert await branch.ReAct(instruction="run both", tools=True, max_extensions=0) == "done"
    assert sorted(entered) == ["left", "right"]


@pytest.mark.asyncio
async def test_a_second_round_declaring_sequential_orders_its_own_batch():
    """A round declaring 'sequential' orders its own batch even when the round before declared nothing."""
    trace = []

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

    branch = tool_branch(
        [
            {
                "extension_needed": True,
                "action_requests": [{"function": "left", "arguments": {"a": 1}}],
            },
            {
                "action_strategy": "sequential",
                "action_requests": [
                    {"function": "left", "arguments": {"a": 3}},
                    {"function": "right", "arguments": {"a": 4}},
                ],
            },
        ],
        [left, right],
        "done",
    )

    assert await branch.ReAct(instruction="run both", tools=True, max_extensions=1) == "done"
    assert trace == [
        ("start", "left", 1),
        ("end", "left", 1),
        ("start", "left", 3),
        ("end", "left", 3),
        ("start", "right", 4),
        ("end", "right", 4),
    ]


@pytest.mark.asyncio
async def test_a_round_declaring_nothing_does_not_inherit_the_round_before_it():
    """A later round that declares no strategy runs concurrently, not under the previous round's."""
    trace = []

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

    branch = tool_branch(
        [
            {
                "extension_needed": True,
                "action_strategy": "sequential",
                "action_requests": [{"function": "left", "arguments": {"a": 1}}],
            },
            {
                "action_requests": [
                    {"function": "left", "arguments": {"a": 3}},
                    {"function": "right", "arguments": {"a": 4}},
                ]
            },
        ],
        [left, right],
        "done",
    )

    assert await branch.ReAct(instruction="run both", tools=True, max_extensions=1) == "done"
    # Round 2 declares nothing, so its two calls enter together rather than one at a time.
    assert trace[:2] == [("start", "left", 1), ("end", "left", 1)]
    assert trace[2:4] == [("start", "left", 3), ("start", "right", 4)]
    assert sorted(trace[4:]) == [("end", "left", 3), ("end", "right", 4)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "declared", [{}, {"action_strategy": "concurrent"}], ids=["omitted", "concurrent"]
)
async def test_a_turn_cannot_loosen_a_callers_sequential_strategy(declared):
    """A caller that asks operate for 'sequential' keeps it when the turn says otherwise."""
    trace = []

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

    turn = {
        "analysis": "Use the requested tools",
        "extension_needed": False,
        "action_requests": [
            {"function": "left", "arguments": {"a": 1}},
            {"function": "right", "arguments": {"a": 2}},
        ],
        **declared,
    }
    branch = TestBranch.from_text([json.dumps(turn)], tools=[left, right])

    await branch.operate(
        instruction="run both",
        response_format=ReActAnalysis,
        actions=True,
        action_strategy="sequential",
    )
    assert trace == [
        ("start", "left", 1),
        ("end", "left", 1),
        ("start", "right", 2),
        ("end", "right", 2),
    ]
