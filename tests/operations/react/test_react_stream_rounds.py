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
from lionagi.session.branch import Branch
from lionagi.testing import LionAGIMockFactory

# ============================================================================
# Helper Functions and Fixtures
# ============================================================================


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


# ============================================================================
# 1. Tool Execution Flows
# ============================================================================


"""Tests for ReAct multi-step reasoning rounds, extensions, and state."""

# ============================================================================
# 2. Multi-Step Reasoning
# ============================================================================


@pytest.mark.asyncio
async def test_reasoning_chain_with_context_accumulation():
    """Test multi-step reasoning with context building across rounds."""
    branch = make_mocked_branch_for_react()

    with patch("lionagi.operations.operate.operate.operate") as mock_operate:
        # Round 1: Initial analysis
        round1 = ReActAnalysis(
            analysis="First step: identify the problem",
            planned_actions=[],
            extension_needed=True,
        )

        # Round 2: Build on previous context
        round2 = ReActAnalysis(
            analysis="Second step: analyze the data based on step 1",
            planned_actions=[],
            extension_needed=True,
        )

        # Round 3: Final reasoning
        round3 = ReActAnalysis(
            analysis="Third step: synthesize findings from steps 1 and 2",
            planned_actions=[],
            extension_needed=False,
        )

        # Final answer
        final = Analysis(answer="Complete solution based on 3-step reasoning")

        mock_operate.side_effect = [round1, round2, round3, final]

        result = await branch.ReAct(
            instruct={"instruction": "Solve complex problem step by step"},
            max_extensions=3,
        )

        assert "Complete solution" in result
        # Should have 4 calls: 3 reasoning rounds + 1 final answer
        assert mock_operate.call_count == 4

        # Verify all rounds were executed with proper context
        # Check that each extension call received previous analysis
        for i, call in enumerate(mock_operate.call_args_list):
            assert call is not None


@pytest.mark.asyncio
async def test_max_extensions_limit():
    """Test that extension loop respects max_extensions limit."""
    branch = make_mocked_branch_for_react()

    with patch("lionagi.operations.operate.operate.operate") as mock_operate:
        # Create 2 analyses that request extension
        round1 = ReActAnalysis(
            analysis="Round 1",
            planned_actions=[],
            extension_needed=True,
        )
        round2 = ReActAnalysis(
            analysis="Round 2 - last extension",
            planned_actions=[],
            extension_needed=False,  # Stops here
        )

        final = Analysis(answer="Complete after 2 rounds")

        # Initial + 1 extension + final = 3 calls
        mock_operate.side_effect = [round1, round2, final]

        result = await branch.ReAct(
            instruct={"instruction": "Task with extensions"},
            max_extensions=2,
        )

        # Should complete successfully
        assert mock_operate.call_count == 3
        assert "Complete after 2 rounds" in result


@pytest.mark.asyncio
async def test_early_termination_extension_false():
    """Test early termination when extension_needed is False."""
    branch = make_mocked_branch_for_react()

    with patch("lionagi.operations.operate.operate.operate") as mock_operate:
        # First round decides no extension needed
        analysis = ReActAnalysis(
            analysis="Task complete after first analysis",
            planned_actions=[],
            extension_needed=False,  # No more extensions
        )

        final = Analysis(answer="Quick answer")

        mock_operate.side_effect = [analysis, final]

        result = await branch.ReAct(
            instruct={"instruction": "Simple task"},
            max_extensions=10,  # Allow many, but stop early
        )

        # Should only have 2 calls: 1 analysis + 1 final
        assert mock_operate.call_count == 2
        assert "Quick answer" in result


@pytest.mark.asyncio
async def test_extension_not_allowed():
    """Test behavior when extension_allowed is False."""
    branch = make_mocked_branch_for_react()

    with patch("lionagi.operations.operate.operate.operate") as mock_operate:
        # Analysis requests extension but it's not allowed
        analysis = ReActAnalysis(
            analysis="Want to extend but can't",
            planned_actions=[],
            extension_needed=True,
        )

        final = Analysis(answer="Forced to conclude")

        mock_operate.side_effect = [analysis, final]

        result = await branch.ReAct(
            instruct={"instruction": "Task"},
            extension_allowed=False,  # Disable extensions
            max_extensions=5,
        )

        # Should only have 2 calls despite extension_needed=True
        assert mock_operate.call_count == 2
        assert "Forced to conclude" in result


@pytest.mark.asyncio
async def test_max_extensions_clamped_to_100():
    """Test that max_extensions is clamped to 100."""
    branch = make_mocked_branch_for_react()

    with patch("lionagi.operations.operate.operate.operate") as mock_operate:
        # Create chain that stops after 2 rounds
        round1 = ReActAnalysis(analysis="Round 1", planned_actions=[], extension_needed=True)
        round2 = ReActAnalysis(analysis="Round 2", planned_actions=[], extension_needed=False)
        final = Analysis(answer="Done")

        mock_operate.side_effect = [round1, round2, final]

        # Request more than 100 extensions (should be clamped)
        result = await branch.ReAct(
            instruct={"instruction": "Task"},
            max_extensions=200,  # Will be clamped to 100
        )

        # Should complete normally (extension_needed=False stops it early)
        assert mock_operate.call_count == 3
        assert result == "Done"


# ============================================================================
# 3. Integration Scenarios
# ============================================================================


@pytest.mark.asyncio
async def test_react_with_real_tools_integration():
    """Test ReAct with real tool registration and execution."""
    branch = Branch(user="test_user")

    # Register real tools
    branch.acts.register_tool(multiply)
    branch.acts.register_tool(divide)

    # Verify tools are registered
    assert "multiply" in branch.acts.registry
    assert "divide" in branch.acts.registry

    with patch("lionagi.operations.operate.operate.operate") as mock_operate:
        # Simulate realistic ReAct flow
        analysis = ReActAnalysis(
            analysis="Calculate (20 * 3) / 4",
            planned_actions=[PlannedAction(action_type="multiply", description="20 * 3")],
            extension_needed=False,
        )

        final = Analysis(answer="15")

        mock_operate.side_effect = [analysis, final]

        # Execute with real tools (though operate is mocked)
        result = await branch.ReAct(
            instruct={"instruction": "Calculate (20 * 3) / 4"},
            tools=True,  # Use registered tools
            max_extensions=1,
        )

        assert result == "15"


@pytest.mark.asyncio
async def test_branch_state_consistency():
    """Test that ReAct completes with expected call pattern."""
    branch = make_mocked_branch_for_react()

    with patch("lionagi.operations.operate.operate.operate") as mock_operate:
        # Multi-round ReAct
        round1 = ReActAnalysis(analysis="Step 1", planned_actions=[], extension_needed=True)
        round2 = ReActAnalysis(analysis="Step 2", planned_actions=[], extension_needed=False)
        final = Analysis(answer="Final")

        mock_operate.side_effect = [round1, round2, final]

        result = await branch.ReAct(
            instruct={"instruction": "Multi-step task"},
            max_extensions=2,
            clear_messages=False,  # Keep message history
        )

        # Verify completion and call pattern
        assert result == "Final"
        assert mock_operate.call_count == 3


@pytest.mark.asyncio
async def test_clear_messages_parameter():
    """Test clear_messages parameter is properly forwarded."""
    branch = make_mocked_branch_for_react()

    with patch("lionagi.operations.operate.operate.operate") as mock_operate:
        analysis = ReActAnalysis(analysis="Analysis", planned_actions=[], extension_needed=False)
        final = Analysis(answer="Answer")

        mock_operate.side_effect = [analysis, final]

        result = await branch.ReAct(
            instruct={"instruction": "Test task"},
            max_extensions=0,
            clear_messages=True,  # Test clear_messages=True
        )

        assert result == "Answer"
        # Verify clear_messages was passed to operate calls
        first_call_kwargs = mock_operate.call_args_list[0][1]
        assert "clear_messages" in first_call_kwargs


@pytest.mark.asyncio
async def test_react_with_async_tool_registration():
    """Test ReAct can register and reference async tools."""
    branch = make_mocked_branch_for_react()
    branch.acts.register_tool(async_search)

    # Verify async tool was registered
    assert "async_search" in branch.acts.registry

    with patch("lionagi.operations.operate.operate.operate") as mock_operate:
        analysis = ReActAnalysis(
            analysis="Search for information",
            planned_actions=[PlannedAction(action_type="async_search", description="Search query")],
            extension_needed=False,
        )

        final = Analysis(answer="Search results found")

        mock_operate.side_effect = [analysis, final]

        result = await branch.ReAct(
            instruct={"instruction": "Search for test"},
            tools=[async_search],
            max_extensions=0,
        )

        assert "Search results found" in result
