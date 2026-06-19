# tests/branch_ops/test_interpret.py

import pytest

from lionagi.testing import LionAGIMockFactory


def make_mocked_branch_for_interpret():
    return LionAGIMockFactory.create_mocked_branch(
        name="BranchForTests_Interpret",
        user="tester_fixture",
        response="mocked_response_string",
        model="gpt-4.1-mini",
    )


@pytest.mark.asyncio
async def test_interpret_basic():
    """branch.interpret() calls communicate with skip_validation and returns raw response."""
    branch = make_mocked_branch_for_interpret()

    refined_prompt = await branch.interpret(
        text="User's raw input", domain="some_domain", style="concise"
    )
    assert refined_prompt == "mocked_response_string"
    assert len(branch.messages) == 0
