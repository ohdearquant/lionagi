# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for ReAct error handling, edge cases, and finalization behavior."""

from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel

from lionagi._provider_errors import ProviderContextError
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


# 4. Edge Cases


@pytest.mark.asyncio
async def test_invalid_tool_response_handling():
    """A malformed action result reaches operate's filter before finalization."""
    request = {"function": "multiply", "arguments": {"a": 6, "b": 7}}
    branch = tool_branch([{"action_requests": [request]}], [multiply], "Handled error")
    with patch("lionagi.operations.act.act.act", new=AsyncMock(return_value=[None])) as mock_act:
        result = await branch.ReAct(
            instruction="Test invalid action response",
            tools=True,
            max_extensions=0,
            return_analysis=True,
        )
    assert len(result) == 2
    assert isinstance(result[0], ReActAnalysis)
    assert not getattr(result[0], "action_responses", None)
    assert result[-1].answer == "Handled error"
    mock_act.assert_awaited_once()
    requests = mock_act.await_args.args[1]
    assert len(requests) == 1
    assert requests[0].function == "multiply"
    assert requests[0].arguments == {"a": 6, "b": 7}
    assert len(ScriptedBranch.calls(branch)) == 2


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
async def test_cli_provider_error_on_final_answer_is_raised():
    """A CLI provider refusal of the final turn is raised, not replaced by an earlier reply."""
    branch = make_mocked_branch_for_react()
    branch.msgs.add_message(assistant_response="an earlier reply")

    with patch("lionagi.operations.operate.operate.operate") as mock_operate:
        mock_operate.side_effect = [
            ReActAnalysis(analysis="Analysis", extension_needed=False),
            ProviderContextError("prompt is too long"),
        ]

        with pytest.raises(ProviderContextError):
            await branch.ReAct(instruct={"instruction": "Test refusal"}, max_extensions=0)

        assert mock_operate.call_count == 2


@pytest.mark.asyncio
async def test_empty_planned_actions():
    """An explicit empty request list does not invoke a registered tool."""
    calls = []

    def unused() -> str:
        """A tool that should not run."""
        calls.append(True)
        return "unexpected"

    branch = tool_branch([{"action_requests": []}], [unused], "Direct answer")
    assert (
        await branch.ReAct(instruction="Simple question", tools=True, max_extensions=0)
        == "Direct answer"
    )
    assert calls == []
    assert not any(isinstance(m, ActionResponse) for m in branch.messages)
    assert len(ScriptedBranch.calls(branch)) == 2


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


# Performance and Stress Tests


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
    """All registered tools receive their requested arguments and persist actual results."""
    calls = []

    def multiply(a: float, b: float) -> float:
        """Multiply two numbers."""
        calls.append(("multiply", a, b))
        return a * b

    def divide(a: float, b: float) -> float:
        """Divide two numbers."""
        calls.append(("divide", a, b))
        return a / b

    def get_weather(city: str) -> str:
        """Get local weather."""
        calls.append(("get_weather", city))
        return f"{city}: sunny"

    requests = [
        {"function": "multiply", "arguments": {"a": 6, "b": 7}},
        {"function": "divide", "arguments": {"a": 84, "b": 2}},
        {"function": "get_weather", "arguments": {"city": "SF"}},
    ]
    branch = tool_branch([{"action_requests": requests}], [multiply, divide, get_weather], "Done")
    assert await branch.ReAct(instruction="Use all tools", tools=True, max_extensions=0) == "Done"
    assert calls == [("multiply", 6, 7), ("divide", 84, 2), ("get_weather", "SF")]
    responses = [m for m in branch.messages if isinstance(m, ActionResponse)]
    assert [(m.function, m.arguments, m.output) for m in responses] == [
        ("multiply", {"a": 6, "b": 7}, 42),
        ("divide", {"a": 84, "b": 2}, 42.0),
        ("get_weather", {"city": "SF"}, "SF: sunny"),
    ]
    api_calls = ScriptedBranch.calls(branch)
    assert len(api_calls) == 2
    assert "42" in str(api_calls[-1].payload["messages"])
    assert "SF: sunny" in str(api_calls[-1].payload["messages"])


if __name__ == "__main__":
    pytest.main([__file__, "-xvs"])
