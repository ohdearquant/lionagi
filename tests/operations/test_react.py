from unittest.mock import AsyncMock, patch

import pytest

# We'll import or define the ReActAnalysis class to create a real instance:
from lionagi.operations.ReAct.utils import ReActAnalysis
from lionagi.testing import LionAGIMockFactory


def make_mocked_branch_for_react():
    return LionAGIMockFactory.create_mocked_branch(
        name="BranchForTests_ReAct",
        user="tester_fixture",
        response="mocked_response_string",
        model="gpt-4.1-mini",
    )


@pytest.mark.asyncio
async def test_react_basic_flow():
    """
    ReAct(...) => calls branch.operate for analysis, then for final answer.
    We'll patch branch.operate to yield a real ReActAnalysis -> Analysis object.
    """
    from lionagi.operations.ReAct.utils import Analysis

    branch = make_mocked_branch_for_react()

    # 1) Create a mock ReActAnalysis object with extension_needed=False so we skip expansions:
    class FakeAnalysis(ReActAnalysis):
        extension_needed: bool = False

    # 2) Create call counter to return different values for different calls
    call_count = 0

    async def mock_operate(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call - return ReActAnalysis
            return FakeAnalysis(
                analysis="intermediate_reasoning",
                extension_needed=False,
            )
        else:
            # Second call - return final Analysis
            return Analysis(answer="final_answer_mock")

    # 3) Patch operate
    with patch(
        "lionagi.operations.operate.operate.operate",
        new=AsyncMock(side_effect=mock_operate),
    ):
        res = await branch.ReAct(
            instruct={"instruction": "Solve a puzzle with ReAct strategy"},
            interpret=False,
            extension_allowed=False,
        )

    # 4) Confirm we got the final answer as a string
    assert res == "final_answer_mock"
