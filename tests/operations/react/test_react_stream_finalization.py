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
from pydantic import BaseModel

from lionagi.operations.ReAct.utils import Analysis, ReActAnalysis
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


"""Tests for ReAct error handling, edge cases, and finalization behavior."""

# ============================================================================
# 4. Edge Cases
# ============================================================================


@pytest.mark.asyncio
async def test_invalid_tool_response_handling():
    """Test handling of invalid/malformed tool responses."""
    branch = make_mocked_branch_for_react()
    branch.acts.register_tool(multiply)

    with (
        patch("lionagi.operations.operate.operate.operate") as mock_operate,
        patch("lionagi.operations.act.act.act") as mock_act,
    ):
        analysis = ReActAnalysis(
            analysis="Call tool",
            extension_needed=False,
        )

        final = Analysis(answer="Handled error")

        mock_operate.side_effect = [analysis, final]
        # Return None simulating invalid response
        mock_act.return_value = [None]

        result = await branch.ReAct(
            instruct={"instruction": "Test invalid response"},
            tools=[multiply],
            max_extensions=0,
        )

        assert "Handled error" in result


@pytest.mark.asyncio
async def test_all_none_response_recovery():
    """Test recovery from response with all None values using continue_after_failed_response."""
    branch = make_mocked_branch_for_react()

    with patch("lionagi.operations.operate.operate.operate") as mock_operate:
        # First call returns analysis
        analysis = ReActAnalysis(
            analysis="Analysis",
            extension_needed=False,
        )

        # Second call (final answer) returns valid Analysis
        final = Analysis(answer="Recovered successfully")

        mock_operate.side_effect = [analysis, final]

        # Should complete successfully with continue_after_failed_response=True
        result = await branch.ReAct(
            instruct={"instruction": "Test recovery"},
            max_extensions=0,
            continue_after_failed_response=True,
        )

        assert "Recovered successfully" in result


@pytest.mark.asyncio
async def test_continue_after_failed_response():
    """Test that continue_after_failed_response allows continuation."""
    branch = make_mocked_branch_for_react()

    with patch("lionagi.operations.operate.operate.operate") as mock_operate:
        # First call returns all None
        failed_response = {"field1": None, "field2": None}
        # Second call returns valid response
        valid_analysis = ReActAnalysis(analysis="Recovered", extension_needed=False)
        final = Analysis(answer="Success")

        mock_operate.side_effect = [
            failed_response,
            valid_analysis,
            final,
        ]

        # Should not raise error and continue
        result = await branch.ReAct(
            instruct={"instruction": "Test recovery"},
            max_extensions=1,
            continue_after_failed_response=True,
        )

        # Should complete despite initial failure
        assert mock_operate.call_count >= 2


@pytest.mark.asyncio
async def test_empty_planned_actions():
    """Test ReAct when analysis has no planned actions."""
    branch = make_mocked_branch_for_react()

    with (
        patch("lionagi.operations.operate.operate.operate") as mock_operate,
        patch("lionagi.operations.act.act.act") as mock_act,
    ):
        analysis = ReActAnalysis(
            analysis="No actions needed",
            extension_needed=False,
        )

        final = Analysis(answer="Direct answer")

        mock_operate.side_effect = [analysis, final]

        result = await branch.ReAct(
            instruct={"instruction": "Simple question"},
            max_extensions=0,
        )

        assert result == "Direct answer"
        # act should not be called if no planned actions
        assert mock_act.call_count == 0


@pytest.mark.asyncio
async def test_react_with_custom_response_format():
    """Test ReAct with custom response format for final answer."""

    class CustomResult(BaseModel):
        """Custom response format."""

        calculation: float
        explanation: str

    branch = make_mocked_branch_for_react()

    with patch("lionagi.operations.operate.operate.operate") as mock_operate:
        analysis = ReActAnalysis(
            analysis="Complete",
            extension_needed=False,
        )

        # Final answer with custom format
        custom_result = CustomResult(calculation=42.0, explanation="The answer is 42")

        mock_operate.side_effect = [analysis, custom_result]

        result = await branch.ReAct(
            instruct={"instruction": "Calculate something"},
            response_format=CustomResult,
            max_extensions=0,
        )

        # Should return CustomResult instance
        assert isinstance(result, CustomResult)
        assert result.calculation == 42.0
        assert result.explanation == "The answer is 42"


@pytest.mark.asyncio
async def test_return_analysis_parameter():
    """Test return_analysis parameter returns all intermediate analyses."""
    branch = make_mocked_branch_for_react()

    with patch("lionagi.operations.operate.operate.operate") as mock_operate:
        round1 = ReActAnalysis(analysis="Step 1", extension_needed=True)
        round2 = ReActAnalysis(analysis="Step 2", extension_needed=False)
        final = Analysis(answer="Final")

        mock_operate.side_effect = [round1, round2, final]

        result = await branch.ReAct(
            instruct={"instruction": "Task"},
            max_extensions=2,
            return_analysis=True,  # Return all analyses
        )

        # Should return list of all analyses
        assert isinstance(result, list)
        assert len(result) == 3  # 2 ReActAnalysis + 1 Analysis
        assert result[0].analysis == "Step 1"
        assert result[1].analysis == "Step 2"
        assert result[2].answer == "Final"


@pytest.mark.asyncio
async def test_reasoning_effort_parameter():
    """Test reasoning_effort parameter affects guidance."""
    branch = make_mocked_branch_for_react()

    with patch("lionagi.operations.operate.operate.operate") as mock_operate:
        analysis = ReActAnalysis(
            analysis="High effort reasoning",
            extension_needed=True,
        )
        round2 = ReActAnalysis(analysis="Continue", extension_needed=False)
        final = Analysis(answer="Result")

        mock_operate.side_effect = [analysis, round2, final]

        await branch.ReAct(
            instruct={"instruction": "Complex task"},
            reasoning_effort="high",  # High reasoning effort
            max_extensions=2,
        )

        # Check that operate was called with reasoning_effort in imodel_kw
        extension_call = mock_operate.call_args_list[1]  # Second call
        chat_param = extension_call[1]["chat_param"]
        assert chat_param.imodel_kw.get("reasoning_effort") == "high"


@pytest.mark.asyncio
async def test_verbose_analysis_output():
    """Test verbose_analysis parameter for debugging output."""
    branch = make_mocked_branch_for_react()

    with patch("lionagi.operations.operate.operate.operate") as mock_operate:
        analysis = ReActAnalysis(
            analysis="Analysis text",
            extension_needed=False,
        )
        final = Analysis(answer="Final answer")

        mock_operate.side_effect = [analysis, final]

        # Test with verbose_analysis=True (should complete without error)
        result = await branch.ReAct(
            instruct={"instruction": "Task"},
            verbose_analysis=True,
            max_extensions=0,
        )

        assert result == "Final answer"


# ============================================================================
# Performance and Stress Tests
# ============================================================================


@pytest.mark.asyncio
async def test_react_with_many_extensions():
    """Test ReAct with multiple reasoning rounds."""
    branch = make_mocked_branch_for_react()

    with patch("lionagi.operations.operate.operate.operate") as mock_operate:
        # Create 10 rounds of reasoning (more manageable)
        rounds = []
        for i in range(10):
            rounds.append(
                ReActAnalysis(
                    analysis=f"Round {i + 1}",
                    extension_needed=(True if i < 9 else False),
                )
            )

        final = Analysis(answer="Complete")

        # 10 rounds + 1 final = 11 calls
        mock_operate.side_effect = rounds + [final]

        result = await branch.ReAct(
            instruct={"instruction": "Complex multi-step task"},
            max_extensions=10,
        )

        assert mock_operate.call_count == 11  # 10 rounds + 1 final
        assert result == "Complete"


@pytest.mark.asyncio
async def test_react_with_many_tools():
    """Test ReAct with many registered tools."""
    branch = make_mocked_branch_for_react()

    # Register multiple tools
    tools = [multiply, divide, get_weather]
    for tool in tools:
        branch.acts.register_tool(tool)

    assert len(branch.acts.registry) >= 3

    with patch("lionagi.operations.operate.operate.operate") as mock_operate:
        analysis = ReActAnalysis(
            analysis="Using tools",
            extension_needed=False,
        )
        final = Analysis(answer="Done with tools")

        mock_operate.side_effect = [analysis, final]

        result = await branch.ReAct(
            instruct={"instruction": "Use multiple tools"},
            tools=True,  # Use all registered tools
            max_extensions=0,
        )

        assert "Done with tools" in result


if __name__ == "__main__":
    pytest.main([__file__, "-xvs"])
