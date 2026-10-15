# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for ReAct tool execution flows and initial invocation."""

import asyncio
from unittest.mock import patch

import pytest

from lionagi.operations.ReAct.utils import Analysis, ReActAnalysis
from lionagi.protocols.messages import ActionResponse
from lionagi.testing import LionAGIMockFactory
from lionagi.testing import TestBranch as ScriptedBranch

from ._tool_trace import tool_branch

# Helper Functions and Fixtures


def make_mocked_branch_for_react():
    """Create a mocked Branch for ReAct testing."""
    return LionAGIMockFactory.create_mocked_branch(
        name="ReActTestBranch",
        user="tester",
        response="mocked_response",
        model="gpt-4o-mini",
    )


# Test tools
def multiply(a: float, b: float) -> float:
    """Multiply two numbers."""
    return a * b


def divide(a: float, b: float) -> float:
    """Divide two numbers."""
    if b == 0:
        raise ValueError("Division by zero")
    return a / b


def get_weather(city: str) -> dict:
    """Get weather for a city."""
    return {"city": city, "temp": 72, "condition": "sunny"}


async def async_search(query: str) -> str:
    """Async search tool."""
    return f"Search results for: {query}"


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
        instruction="What is 6 times 7?", tools=[multiply], max_extensions=0
    )

    assert result == "42"
    assert calls == [(6, 7)]
    responses = [m for m in branch.messages if isinstance(m, ActionResponse)]
    assert len(responses) == 1
    assert responses[0].function == "multiply"
    assert responses[0].arguments == {"a": 6, "b": 7}
    assert responses[0].output == 42
    api_calls = ScriptedBranch.calls(branch)
    assert len(api_calls) == 2
    final_messages = str(api_calls[-1].payload["messages"])
    assert "multiply" in final_messages and "42" in final_messages


@pytest.mark.asyncio
async def test_multiple_tool_calls_sequential_strategy():
    """Test ReAct specifying sequential action strategy."""
    branch = make_mocked_branch_for_react()

    with patch("lionagi.operations.operate.operate.operate") as mock_operate:
        # Round 1: First tool call with sequential strategy
        round1 = ReActAnalysis(
            analysis="Calculate 100 * 5",
            extension_needed=True,
            action_strategy="sequential",
        )

        # Round 2: Second tool call
        round2 = ReActAnalysis(
            analysis="Now divide by 10",
            extension_needed=False,
            action_strategy="sequential",
        )

        # Final answer
        final = Analysis(answer="50")

        mock_operate.side_effect = [round1, round2, final]

        result = await branch.ReAct(
            instruct={"instruction": "Calculate (100 * 5) / 10 using tools"},
            max_extensions=2,
        )

        assert result == "50"
        assert mock_operate.call_count == 3

        # Verify sequential strategy was passed to operate calls
        extension_call = mock_operate.call_args_list[1]
        action_param = extension_call[1]["action_param"]
        assert action_param.strategy == "sequential"


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


@pytest.mark.asyncio
async def test_tools_parameter_variations():
    """Test different ways to specify tools parameter."""
    branch = make_mocked_branch_for_react()
    branch.acts.register_tool(multiply)

    with patch("lionagi.operations.operate.operate.operate") as mock_operate:
        analysis = ReActAnalysis(
            analysis="Complete",
            extension_needed=False,
        )
        final = Analysis(answer="Done")

        # Test with tools=None
        mock_operate.side_effect = [analysis, final]
        result = await branch.ReAct(
            instruct={"instruction": "Task"},
            tools=None,
            max_extensions=0,
        )
        assert result == "Done"

        # Test with tools=True (use all registered)
        mock_operate.side_effect = [analysis, final]
        result = await branch.ReAct(
            instruct={"instruction": "Task"},
            tools=True,
            max_extensions=0,
        )
        assert result == "Done"
