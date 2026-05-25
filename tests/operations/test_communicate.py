# tests/branch_ops/test_communicate.py

import pytest
from pydantic import BaseModel

from lionagi.session.branch import Branch
from lionagi.testing import LionAGIMockFactory


def make_mocked_branch_for_communicate():
    """Returns a Branch whose chat_model.invoke yields a JSON string response."""
    return LionAGIMockFactory.create_mocked_branch(
        name="BranchForTests_Communicate",
        user="tester_fixture",
        response='{"data":"mocked_response_string"}',
        model="gpt-4.1-mini",
    )


@pytest.mark.asyncio
async def test_communicate_no_validation():
    """
    If skip_validation=True, branch.communicate(...) should directly return the raw string.
    """
    branch = make_mocked_branch_for_communicate()

    result = await branch.communicate(instruction="User says hi", skip_validation=True)
    assert result == '{"data":"mocked_response_string"}'

    # If your updated code doesn't store messages, or does so differently, adjust accordingly:
    assert len(branch.messages) == 2


@pytest.mark.asyncio
async def test_communicate_with_model_validation():
    """
    If we provide a request_model, the final response is parsed into that model.
    """

    class MySimpleModel(BaseModel):
        data: str = "default_data"

    branch = make_mocked_branch_for_communicate()

    parsed = await branch.communicate(
        instruction="Send typed output",
        request_model=MySimpleModel,
    )
    # We'll assume your code sets parsed.data = "mocked_response_string"
    assert parsed.data == "mocked_response_string"
    assert len(branch.messages) == 2


@pytest.mark.asyncio
async def test_communicate_wraps_parse_value_error_with_context(monkeypatch):
    from unittest.mock import AsyncMock, patch

    from pydantic import BaseModel as PydanticBaseModel

    class AnswerModel(PydanticBaseModel):
        answer: str

    branch = make_mocked_branch_for_communicate()

    # parse is imported locally inside communicate(); patch at source module
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

    # After clear + 1 turn: exactly instruction + assistant_response = 2
    assert len(branch.messages) == 2


# ---------------------------------------------------------------------------
# Coverage gap tests for communicate.py lines 48, 60, 71-76, 158-160, 169-178
# ---------------------------------------------------------------------------

import warnings

from lionagi.operations.communicate.communicate import prepare_communicate_kw


class SomeModel(BaseModel):
    data: str = "default"


def test_prepare_communicate_kw_operative_model_deprecation():
    """Line 48: operative_model param → DeprecationWarning."""
    branch = make_mocked_branch_for_communicate()
    with pytest.warns(DeprecationWarning, match="operative_model"):
        kw = prepare_communicate_kw(branch, operative_model=SomeModel)
    assert kw["chat_param"].response_format is SomeModel


def test_prepare_communicate_kw_dual_response_format_raises():
    """Line 60: response_format + request_model together → ValueError."""
    branch = make_mocked_branch_for_communicate()
    with pytest.raises(ValueError, match="Cannot specify both"):
        prepare_communicate_kw(branch, response_format=SomeModel, request_model=SomeModel)


def test_prepare_communicate_kw_operative_model_and_response_format_raises():
    """Line 60: operative_model + response_format → ValueError."""
    branch = make_mocked_branch_for_communicate()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        with pytest.raises(ValueError, match="Cannot specify both"):
            prepare_communicate_kw(branch, operative_model=SomeModel, response_format=SomeModel)


def test_prepare_communicate_kw_high_retries_capped():
    """Lines 71-76: num_parse_retries > 5 → UserWarning and capped to 5."""
    branch = make_mocked_branch_for_communicate()
    with pytest.warns(UserWarning, match="num_parse_retries"):
        kw = prepare_communicate_kw(branch, num_parse_retries=10, response_format=SomeModel)
    # parse_param should be set (response_format provided)
    assert kw["parse_param"] is not None


@pytest.mark.asyncio
async def test_communicate_updates_metadata_when_res2_is_assistant_response():
    """Lines 158-160: parse returns (out, AssistantResponse) → metadata updated."""
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
    """Lines 169-178: request_fields path → fuzzy_validate_mapping → dict."""
    branch = make_mocked_branch_for_communicate()
    # Mocked response is '{"data":"mocked_response_string"}'
    result = await branch.communicate(
        instruction="give me data",
        request_fields={"data": str},
        skip_validation=False,
    )
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_communicate_plain_returns_raw_response():
    """Line 178: no response_format, no request_fields → return res.response."""
    branch = make_mocked_branch_for_communicate()
    result = await branch.communicate(instruction="hello")
    assert result == '{"data":"mocked_response_string"}'
