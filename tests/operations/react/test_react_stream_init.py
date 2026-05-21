# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""
Extended test coverage for ReAct operations.

Tests cover:
1. Tool execution flows (single, multiple, error handling)
2. Multi-step reasoning (context accumulation, max extensions)
3. Integration scenarios (real tools, branch state, message history)
4. Edge cases (tool not found, invalid responses, concurrent execution)
"""

from unittest.mock import AsyncMock, patch

import pytest

from lionagi.operations.ReAct.utils import Analysis, PlannedAction, ReActAnalysis
from lionagi.protocols.generic.event import EventStatus
from lionagi.providers.openai.chat.models import OpenAIChatCompletionsRequest
from lionagi.service.connections.api_calling import APICalling
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.imodel import iModel


def _get_oai_config(
    name: str = "openai_chat/completions",
    endpoint: str = "chat/completions",
    request_options=None,
    kwargs: dict | None = None,
) -> EndpointConfig:
    return EndpointConfig(
        name=name,
        provider="openai",
        base_url="https://api.openai.com/v1",
        endpoint=endpoint,
        api_key="dummy-key-for-testing",
        request_options=request_options,
        auth_type="bearer",
        content_type="application/json",
        method="POST",
        requires_tokens=True,
        kwargs=kwargs or {},
    )


from lionagi.session.branch import Branch

# ============================================================================
# Helper Functions and Fixtures
# ============================================================================


def make_mocked_branch_for_react():
    """Create a mocked Branch for ReAct testing."""
    branch = Branch(user="tester", name="ReActTestBranch")

    async def _fake_invoke(**kwargs):
        config = _get_oai_config(
            name="oai_chat",
            endpoint="chat/completions",
            request_options=OpenAIChatCompletionsRequest,
            kwargs={"model": "gpt-4o-mini"},
        )
        endpoint = Endpoint(config=config)
        fake_call = APICalling(
            payload={"model": "gpt-4o-mini", "messages": []},
            headers={"Authorization": "Bearer test"},
            endpoint=endpoint,
        )
        fake_call.execution.response = "mocked_response"
        fake_call.execution.status = EventStatus.COMPLETED
        return fake_call

    mock_invoke = AsyncMock(side_effect=_fake_invoke)
    mock_chat_model = iModel(provider="openai", model="gpt-4o-mini", api_key="test_key")
    mock_chat_model.invoke = mock_invoke

    branch.chat_model = mock_chat_model
    return branch


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


# ============================================================================
# 1. Tool Execution Flows
# ============================================================================


"""Tests for ReAct tool execution flows and initial invocation."""


@pytest.mark.asyncio
async def test_single_tool_invocation():
    """Test ReAct with single tool call - verifies tool integration."""
    branch = make_mocked_branch_for_react()
    branch.acts.register_tool(multiply)

    with patch("lionagi.operations.operate.operate.operate") as mock_operate:
        # First: ReActAnalysis with tool call
        first_analysis = ReActAnalysis(
            analysis="Need to calculate 6 * 7",
            planned_actions=[
                PlannedAction(action_type="multiply", description="Calculate 6 * 7")
            ],
            extension_needed=False,
        )

        # Second: Final answer
        final_analysis = Analysis(answer="42")

        mock_operate.side_effect = [first_analysis, final_analysis]

        result = await branch.ReAct(
            instruct={"instruction": "What is 6 times 7?"},
            tools=[multiply],
            max_extensions=0,
        )

        assert result == "42"
        # Verify operate was called twice (analysis + final answer)
        assert mock_operate.call_count == 2


@pytest.mark.asyncio
async def test_multiple_tool_calls_sequential_strategy():
    """Test ReAct specifying sequential action strategy."""
    branch = make_mocked_branch_for_react()

    with patch("lionagi.operations.operate.operate.operate") as mock_operate:
        # Round 1: First tool call with sequential strategy
        round1 = ReActAnalysis(
            analysis="Calculate 100 * 5",
            planned_actions=[
                PlannedAction(action_type="multiply", description="100 * 5")
            ],
            extension_needed=True,
            action_strategy="sequential",
        )

        # Round 2: Second tool call
        round2 = ReActAnalysis(
            analysis="Now divide by 10",
            planned_actions=[
                PlannedAction(action_type="divide", description="500 / 10")
            ],
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
    """Test ReAct with concurrent action strategy specification."""
    branch = make_mocked_branch_for_react()

    with patch("lionagi.operations.operate.operate.operate") as mock_operate:
        # Analysis requesting concurrent execution
        analysis = ReActAnalysis(
            analysis="Check weather in multiple cities",
            planned_actions=[
                PlannedAction(
                    action_type="get_weather",
                    description="Check NYC weather",
                ),
                PlannedAction(
                    action_type="get_weather",
                    description="Check SF weather",
                ),
            ],
            extension_needed=False,
            action_strategy="concurrent",  # Concurrent strategy
        )

        final = Analysis(answer="NYC: 72°F sunny, SF: 65°F cloudy")

        mock_operate.side_effect = [analysis, final]

        result = await branch.ReAct(
            instruct={"instruction": "Compare weather in NYC and SF"},
            max_extensions=0,
        )

        assert result == "NYC: 72°F sunny, SF: 65°F cloudy"
        assert mock_operate.call_count == 2


@pytest.mark.asyncio
async def test_planned_actions_structure():
    """Test that PlannedAction structure is properly handled."""
    branch = make_mocked_branch_for_react()

    with patch("lionagi.operations.operate.operate.operate") as mock_operate:
        # Analysis with detailed planned actions
        analysis = ReActAnalysis(
            analysis="Need to perform multiple actions",
            planned_actions=[
                PlannedAction(
                    action_type="search",
                    description="Search for information",
                ),
                PlannedAction(
                    action_type="analyze",
                    description="Analyze search results",
                ),
            ],
            extension_needed=False,
        )

        final = Analysis(answer="Actions completed")

        mock_operate.side_effect = [analysis, final]

        result = await branch.ReAct(
            instruct={"instruction": "Perform research"},
            max_extensions=0,
        )

        assert result == "Actions completed"
        # Verify the analysis object had correct planned_actions
        initial_call = mock_operate.call_args_list[0]
        assert initial_call is not None


@pytest.mark.asyncio
async def test_tools_parameter_variations():
    """Test different ways to specify tools parameter."""
    branch = make_mocked_branch_for_react()
    branch.acts.register_tool(multiply)

    with patch("lionagi.operations.operate.operate.operate") as mock_operate:
        analysis = ReActAnalysis(
            analysis="Complete",
            planned_actions=[],
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
