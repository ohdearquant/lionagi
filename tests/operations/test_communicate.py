# tests/branch_ops/test_communicate.py

import pytest
from pydantic import BaseModel

from lionagi.session.branch import Branch
from lionagi.testing import LionAGIMockFactory


def make_mocked_branch_for_communicate():
    """Branch whose chat_model.invoke yields a JSON string response."""
    return LionAGIMockFactory.create_mocked_branch(
        name="BranchForTests_Communicate",
        user="tester_fixture",
        response='{"data":"mocked_response_string"}',
        model="gpt-4.1-mini",
    )


@pytest.mark.asyncio
async def test_communicate_no_validation():
    """skip_validation=True returns the raw string."""
    branch = make_mocked_branch_for_communicate()

    result = await branch.communicate(instruction="User says hi", skip_validation=True)
    assert result == '{"data":"mocked_response_string"}'

    assert len(branch.messages) == 2


@pytest.mark.asyncio
async def test_communicate_with_model_validation():
    """response_format causes the response to be parsed into the model."""

    class MySimpleModel(BaseModel):
        data: str = "default_data"

    branch = make_mocked_branch_for_communicate()

    parsed = await branch.communicate(
        instruction="Send typed output",
        response_format=MySimpleModel,
    )
    assert parsed.data == "mocked_response_string"
    assert len(branch.messages) == 2


@pytest.mark.asyncio
async def test_communicate_wraps_parse_value_error_with_context(monkeypatch):
    from unittest.mock import AsyncMock, patch

    from pydantic import BaseModel as PydanticBaseModel

    class AnswerModel(PydanticBaseModel):
        answer: str

    branch = make_mocked_branch_for_communicate()

    with patch(
        "lionagi.operations.parse.parse.parse",
        new=AsyncMock(side_effect=ValueError("bad parse")),
    ):
        with pytest.raises(ValueError, match="bad parse"):
            await branch.communicate(
                instruction="some instruction",
                response_format=AnswerModel,
            )


@pytest.mark.asyncio
async def test_communicate_clear_messages_clears_before_turn():
    branch = make_mocked_branch_for_communicate()
    branch.msgs.add_message(
        instruction="pre-existing",
        sender=branch.user or "user",
        recipient=branch.id,
    )
    assert len(branch.messages) >= 1

    await branch.communicate(
        instruction="new instruction",
        clear_messages=True,
        skip_validation=True,
    )

    assert len(branch.messages) == 2


import warnings

from lionagi.operations.communicate.communicate import prepare_communicate_kw


class SomeModel(BaseModel):
    data: str = "default"


def test_prepare_communicate_kw_high_retries_capped():
    """num_parse_retries > 5 raises UserWarning and is capped to 5."""
    branch = make_mocked_branch_for_communicate()
    with pytest.warns(UserWarning, match="num_parse_retries"):
        kw = prepare_communicate_kw(branch, num_parse_retries=10, response_format=SomeModel)
    assert kw["parse_param"] is not None


@pytest.mark.asyncio
async def test_communicate_updates_metadata_when_res2_is_assistant_response():
    """When parse returns (out, AssistantResponse), metadata is updated."""
    from unittest.mock import AsyncMock, patch

    from lionagi.protocols.messages.assistant_response import AssistantResponse

    branch = make_mocked_branch_for_communicate()
    fake_out = SomeModel(data="parsed")
    fake_res2 = AssistantResponse.from_response("parsed")

    with patch(
        "lionagi.operations.parse.parse.parse",
        new=AsyncMock(return_value=(fake_out, fake_res2)),
    ):
        result = await branch.communicate(
            instruction="test",
            response_format=SomeModel,
        )
    assert result == fake_out


@pytest.mark.asyncio
async def test_communicate_with_request_fields_returns_dict():
    """request_fields path uses fuzzy_validate_mapping and returns dict."""
    branch = make_mocked_branch_for_communicate()
    result = await branch.communicate(
        instruction="give me data",
        request_fields={"data": str},
        skip_validation=False,
    )
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_communicate_plain_returns_raw_response():
    """No response_format, no request_fields returns raw response string."""
    branch = make_mocked_branch_for_communicate()
    result = await branch.communicate(instruction="hello")
    assert result == '{"data":"mocked_response_string"}'
