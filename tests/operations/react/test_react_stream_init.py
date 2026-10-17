# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for ReAct tool execution flows and initial invocation."""

import asyncio

import pytest

from lionagi.protocols.messages import ActionResponse
from lionagi.testing import TestBranch as ScriptedBranch

from ._tool_trace import tool_branch


def _each_ran_once(trace, expected):
    """Fail outright unless every expected trace entry occurred exactly once.

    The ordering tests below are strict xfails that accept an AssertionError, so a
    tool that never ran, or ran twice, would also read as the expected failure. A
    pytest failure is not an AssertionError, so it reports a broken precondition
    as a failure instead.
    """
    if sorted(trace) != sorted(expected):
        pytest.fail(f"tools did not each run once: {trace!r}")


# 1. Tool Execution Flows


@pytest.mark.asyncio
async def test_single_tool_invocation():
    """A model action reaches the real tool and its result reaches the next turn."""
    calls = []

    def multiply(a: float, b: float) -> float:
        """Multiply two numbers."""
        calls.append((a, b))
        return a * b

    request = {"function": "multiply", "arguments": {"a": 6, "b": 7}}
    branch = tool_branch([{"action_requests": [request]}], [multiply], "42")
    result = await branch.ReAct(
        instruction="What is 6 times 7?", tools=[multiply], max_extensions=0, return_analysis=True
    )

    assert len(result) == 2
    assert result[-1].answer == "42"
    assert len(result[0].action_responses) == 1
    assert result[0].action_responses[0].function == "multiply"
    assert result[0].action_responses[0].arguments == {"a": 6, "b": 7}
    assert result[0].action_responses[0].output == 42
    assert calls == [(6, 7)]
    responses = [m for m in branch.messages if isinstance(m, ActionResponse)]
    assert len(responses) == 1
    assert responses[0].function == "multiply"
    assert responses[0].arguments == {"a": 6, "b": 7}
    assert responses[0].output == 42
    api_calls = ScriptedBranch.calls(branch)
    assert len(api_calls) == 2
    final_messages = str(api_calls[-1].payload["messages"])
    assert "42" in final_messages


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_round",
    [
        pytest.param({"action_strategy": "sequential"}, id="declared-on-both-rounds"),
        pytest.param(
            {},
            id="declared-on-the-second-round-only",
            marks=pytest.mark.xfail(
                strict=True,
                raises=AssertionError,
                reason=(
                    "an analysis turn's declared action strategy should govern its own "
                    "tool batch, but the batch takes the strategy of the analysis before it"
                ),
            ),
        ),
    ],
)
async def test_multiple_tool_calls_sequential_strategy(first_round):
    """The extension's real tool batch completes one action before starting the next."""
    trace = []

    async def multiply(a: float, b: float) -> float:
        """Multiply two numbers."""
        trace.append(("start", "multiply", a, b))
        await asyncio.sleep(0)
        trace.append(("end", "multiply", a, b))
        return a * b

    async def divide(a: float, b: float) -> float:
        """Divide two numbers."""
        trace.append(("start", "divide", a, b))
        await asyncio.sleep(0)
        trace.append(("end", "divide", a, b))
        return a / b

    branch = tool_branch(
        [
            {
                "extension_needed": True,
                "action_requests": [{"function": "multiply", "arguments": {"a": 100, "b": 5}}],
                **first_round,
            },
            {
                "action_strategy": "sequential",
                "action_requests": [
                    {"function": "divide", "arguments": {"a": 500, "b": 10}},
                    {"function": "multiply", "arguments": {"a": 2, "b": 3}},
                ],
            },
        ],
        [multiply, divide],
        "50",
    )
    assert (
        await branch.ReAct(instruction="Calculate (100 * 5) / 10", tools=True, max_extensions=1)
        == "50"
    )
    expected = [
        ("start", "multiply", 100, 5),
        ("end", "multiply", 100, 5),
        ("start", "divide", 500, 10),
        ("end", "divide", 500, 10),
        ("start", "multiply", 2, 3),
        ("end", "multiply", 2, 3),
    ]
    _each_ran_once(trace, expected)
    assert trace == expected
    responses = [m for m in branch.messages if isinstance(m, ActionResponse)]
    assert [(m.function, m.output) for m in responses] == [
        ("multiply", 500),
        ("divide", 50),
        ("multiply", 6),
    ]
    api_calls = ScriptedBranch.calls(branch)
    assert len(api_calls) == 3
    final_messages = str(api_calls[-1].payload["messages"])
    assert "output: 50.0" in final_messages
    assert "output: 6" in final_messages


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "an analysis turn's declared action strategy should govern its own tool batch, "
        "but the first batch always runs concurrently"
    ),
)
async def test_first_round_declared_sequential_runs_in_order():
    """Two actions declared sequential on the first turn complete one after the other."""
    trace = []

    async def first(a: float, b: float) -> float:
        """Record an in-order call."""
        trace.append(("start", "first", a, b))
        await asyncio.sleep(0)
        trace.append(("end", "first", a, b))
        return a + b

    async def second(a: float, b: float) -> float:
        """Record an in-order call."""
        trace.append(("start", "second", a, b))
        await asyncio.sleep(0)
        trace.append(("end", "second", a, b))
        return a + b

    branch = tool_branch(
        [
            {
                "action_strategy": "sequential",
                "action_requests": [
                    {"function": "first", "arguments": {"a": 1, "b": 2}},
                    {"function": "second", "arguments": {"a": 3, "b": 4}},
                ],
            }
        ],
        [first, second],
        "done",
    )
    assert await branch.ReAct(instruction="Run both", tools=True, max_extensions=0) == "done"
    expected = [
        ("start", "first", 1, 2),
        ("end", "first", 1, 2),
        ("start", "second", 3, 4),
        ("end", "second", 3, 4),
    ]
    _each_ran_once(trace, expected)
    assert trace == expected


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "an analysis turn's declared action strategy should govern its own tool batch, "
        "but a batch takes the strategy of the analysis before it"
    ),
)
async def test_later_round_declared_concurrent_overlaps():
    """A turn declared concurrent overlaps when the turn before it was sequential."""
    trace = []

    async def single(a: float, b: float) -> float:
        """Record a call with its arguments and return the sum."""
        trace.append(("single", a, b))
        return a + b

    async def slow(name: str) -> str:
        """Record a call that yields before finishing."""
        trace.append(("start", name))
        await asyncio.sleep(0)
        trace.append(("end", name))
        return name

    branch = tool_branch(
        [
            {
                "extension_needed": True,
                "action_strategy": "sequential",
                "action_requests": [{"function": "single", "arguments": {"a": 1, "b": 2}}],
            },
            {
                "action_strategy": "concurrent",
                "action_requests": [
                    {"function": "slow", "arguments": {"name": name}} for name in ("NYC", "SF")
                ],
            },
        ],
        [single, slow],
        "Both cities are sunny",
    )
    assert (
        await branch.ReAct(instruction="Compare weather", tools=True, max_extensions=1)
        == "Both cities are sunny"
    )
    expected = [
        ("single", 1, 2),
        ("start", "NYC"),
        ("start", "SF"),
        ("end", "NYC"),
        ("end", "SF"),
    ]
    _each_ran_once(trace, expected)
    assert trace == expected


@pytest.mark.asyncio
async def test_concurrent_action_strategy():
    """Both real tools enter before either is released by the concurrent batch."""
    entered = []
    both_entered = asyncio.Event()

    async def weather(city: str) -> str:
        """Read a city's local weather fixture."""
        entered.append(city)
        if len(entered) == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), timeout=2)
        return f"{city}: sunny"

    branch = tool_branch(
        [
            {
                "action_requests": [
                    {"function": "weather", "arguments": {"city": city}} for city in ("NYC", "SF")
                ]
            }
        ],
        [weather],
        "Both cities are sunny",
    )
    result = await branch.ReAct(instruction="Compare weather", tools=True, max_extensions=0)
    assert result == "Both cities are sunny"
    assert sorted(entered) == ["NYC", "SF"]
    responses = [m for m in branch.messages if isinstance(m, ActionResponse)]
    assert len(responses) == 2
    assert {response.arguments["city"]: response.output for response in responses} == {
        "NYC": "NYC: sunny",
        "SF": "SF: sunny",
    }
    api_calls = ScriptedBranch.calls(branch)
    assert len(api_calls) == 2
    final_messages = str(api_calls[-1].payload["messages"])
    assert "NYC: sunny" in final_messages
    assert "SF: sunny" in final_messages


@pytest.mark.asyncio
async def test_tools_parameter_variations():
    """Implicit and explicit registered-tool selection both execute the model request."""
    for tools in (None, True):
        calls = []

        def multiply(a: float, b: float) -> float:
            """Multiply two numbers."""
            calls.append((a, b))
            return a * b

        branch = tool_branch(
            [{"action_requests": [{"function": "multiply", "arguments": {"a": 6, "b": 7}}]}],
            [multiply],
            "42",
        )
        assert await branch.ReAct(instruction="Multiply", tools=tools, max_extensions=0) == "42"
        assert calls == [(6, 7)]
        responses = [m for m in branch.messages if isinstance(m, ActionResponse)]
        assert [(m.function, m.arguments, m.output) for m in responses] == [
            ("multiply", {"a": 6, "b": 7}, 42)
        ]
        api_calls = ScriptedBranch.calls(branch)
        assert len(api_calls) == 2
        assert "output: 42" in str(api_calls[-1].payload["messages"])


@pytest.mark.asyncio
async def test_a_scripted_tool_run_registers_no_exit_log_dump(monkeypatch):
    """Running a real tool through a scripted branch arms no exit-time log dump."""
    import atexit

    registered = []
    monkeypatch.setattr(atexit, "register", lambda func, *a, **k: registered.append(func))
    calls = []

    def add(a: int, b: int) -> int:
        """Add two numbers."""
        calls.append((a, b))
        return a + b

    request = {"function": "add", "arguments": {"a": 1, "b": 2}}
    branch = tool_branch([{"action_requests": [request]}], [add], "3")
    result = await branch.ReAct(instruction="Add", tools=True, max_extensions=0)
    api_calls = ScriptedBranch.calls(branch)
    assert len(api_calls) == 2
    assert result == "3"
    assert calls == [(1, 2)]
    assert [m.output for m in branch.messages if isinstance(m, ActionResponse)] == [3]
    assert "output: 3" in str(api_calls[-1].payload["messages"])
    assert registered == []
