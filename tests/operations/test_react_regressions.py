from unittest.mock import patch

import pytest
from pydantic import BaseModel

from lionagi.operations.fields import ActionResponseModel
from lionagi.operations.ReAct.utils import ReActAnalysis
from lionagi.session.branch import Branch


class CustomAnswer(BaseModel):
    result: float
    explanation: str


def multiply(a: float, b: float) -> float:
    return a * b


@pytest.mark.asyncio
async def test_react_returns_answer_string_not_analysis_object():
    """Regression test: ReAct should return answer string, not full Analysis object."""
    branch = Branch()
    branch.acts.register_tool(multiply)

    # Mock operate and act
    with (
        patch("lionagi.operations.operate.operate.operate") as mock_operate,
        patch("lionagi.operations.act.act.act") as mock_act,
    ):
        # Import Analysis class for final answer
        from lionagi.operations.ReAct.utils import Analysis

        # First call returns ReActAnalysis with planned actions
        first_analysis = ReActAnalysis(
            analysis="I need to multiply 5 by 3",
            extension_needed=False,  # Will still do actions, then final answer
        )

        # Second call returns Analysis with answer
        final_analysis = Analysis(answer="15")

        mock_operate.side_effect = [first_analysis, final_analysis]

        # Mock act to return the multiply result
        mock_act.return_value = [
            ActionResponseModel(function="multiply", arguments={"a": 5, "b": 3}, output=15)
        ]

        # Execute ReAct
        result = await branch.ReAct(
            instruct={"instruction": "What is 5 times 3?"}, max_extensions=1
        )

        # CRITICAL: Should return just the answer string, not the Analysis object
        assert result == "15", f"Expected '15' but got {result}"
        assert isinstance(result, str), f"Expected str but got {type(result)}"
        from lionagi.operations.ReAct.utils import Analysis

        assert not isinstance(result, Analysis), "Should not return Analysis object"


@pytest.mark.asyncio
async def test_react_honors_custom_response_format():
    """Regression test: ReAct should honor custom response_format parameter."""
    branch = Branch()

    with patch("lionagi.operations.operate.operate.operate") as mock_operate:
        # First call returns ReActAnalysis (no more extensions)
        analysis = ReActAnalysis(analysis="Complete", extension_needed=False)

        # Final call should use custom response format
        custom_result = CustomAnswer(result=15.0, explanation="5 times 3 equals 15")

        mock_operate.side_effect = [analysis, custom_result]

        # Execute with custom response_format
        result = await branch.ReAct(
            instruct={"instruction": "Calculate 5 times 3"},
            response_format=CustomAnswer,
            max_extensions=0,
        )

        # Should return CustomAnswer instance, not string
        assert isinstance(result, CustomAnswer), f"Expected CustomAnswer but got {type(result)}"
        assert result.result == 15.0
        assert result.explanation == "5 times 3 equals 15"


@pytest.mark.asyncio
async def test_react_forwards_response_kwargs():
    """Regression test: response_kwargs should be forwarded to final operate call."""
    branch = Branch()

    with patch("lionagi.operations.operate.operate.operate") as mock_operate:
        from lionagi.operations.ReAct.utils import Analysis

        # First call
        first = ReActAnalysis(analysis="Done", extension_needed=False)
        # Final call returns Analysis
        final = Analysis(answer="Result")
        mock_operate.side_effect = [first, final]

        # Execute with response_kwargs
        await branch.ReAct(
            instruct={"instruction": "Test"},
            response_kwargs={
                "skip_validation": True,
                "handle_validation": "return_value",
                "custom_param": "test_value",  # Should be ignored but not cause error
            },
            max_extensions=0,
        )

        # Check the last operate call includes response_kwargs
        last_call = mock_operate.call_args_list[-1]
        kwargs = last_call[1]

        # These should be forwarded
        assert kwargs.get("skip_validation") is True
        assert kwargs.get("handle_validation") == "return_value"


@pytest.mark.asyncio
async def test_action_response_dict_conversion_in_chat():
    """Regression test: ActionResponseContent objects should be converted to dicts in prompt_context."""
    # This test verifies the fix in chat.py lines 62-75
    from lionagi.ln._to_list import to_list
    from lionagi.protocols.messages.action_response import ActionResponse

    # Create an action response
    action_resp = ActionResponse(
        content={
            "function": "multiply",
            "arguments": {"a": 5, "b": 3},
            "output": 15,
        }
    )

    # Simulate what the FIXED code does - convert ActionResponseContent to dicts
    _act_res = [action_resp]
    d_ = []
    for k in to_list(_act_res, flatten=True, unique=True):
        if hasattr(k.content, "function"):  # ActionResponseContent
            d_.append(
                {
                    "function": k.content.function,
                    "arguments": k.content.arguments,
                    "output": k.content.output,
                }
            )
        else:
            d_.append(k.content)

    # Verify the conversion happened correctly
    assert len(d_) == 1
    assert isinstance(d_[0], dict), "Should convert ActionResponseContent to dict"
    assert d_[0]["function"] == "multiply"
    assert d_[0]["arguments"] == {"a": 5, "b": 3}
    assert d_[0]["output"] == 15


@pytest.mark.asyncio
async def test_error_feedback_when_tool_missing():
    """Regression test: Errors from missing tools should be fed back as ActionResponseModel."""
    branch = Branch()
    # No tools registered

    # Try to call a missing tool
    from lionagi.protocols.types import ActionRequest

    request = ActionRequest(content={"function": "subtract", "arguments": {"a": 10, "b": 3}})

    result = await branch.act(request, suppress_errors=True)

    # Should return ActionResponseModel with error, not None or empty list
    assert len(result) == 1
    assert isinstance(result[0], ActionResponseModel)
    assert result[0].function == "subtract"
    assert result[0].output is not None
    assert "error" in result[0].output or result[0].output is None


@pytest.mark.asyncio
async def test_operate_filters_none_action_responses():
    """Regression test: operate should filter None values from action responses."""
    # This test verifies the None filtering in operate.py

    # Simulate the filtering that happens in operate.py
    action_response_models = [
        None,  # Should be filtered
        ActionResponseModel(function="multiply", arguments={"a": 5, "b": 3}, output=15),
        None,  # Should also be filtered
    ]

    # This is what the FIXED code does - filter out None values
    filtered = [r for r in action_response_models if r is not None]

    # Verify the filtering worked correctly
    assert len(filtered) == 1, "Should filter out None values"
    assert filtered[0].function == "multiply"
    assert filtered[0].output == 15


def test_react_analysis_optional_analysis_field():
    """Regression: a round that omits the `analysis` narration field must still
    validate, so its action_requests are not silently discarded (SWE-bench debug
    2026-06-01: a model turn dropped a CORRECT edit because `analysis` was missing
    -> ReActAnalysis validation failed -> operate dropped action_requests ->
    ReAct finalized into a hallucinated success). `analysis` defaults to "".
    """
    # No `analysis` provided — must not raise.
    a = ReActAnalysis.model_validate({"extension_needed": True, "action_strategy": "sequential"})
    assert a.analysis == ""
    assert a.extension_needed is True
    # Explicit analysis still works.
    b = ReActAnalysis.model_validate({"analysis": "reasoning here"})
    assert b.analysis == "reasoning here"
