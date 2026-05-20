# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.operations.act.act — _act, act, prepare_act_kw, strategies."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lionagi.operations.act.act import (
    _act,
    _get_default_call_params,
    _sequential_act,
    act,
    prepare_act_kw,
)
from lionagi.operations.fields import ActionResponseModel
from lionagi.operations.types import ActionParam
from lionagi.protocols.messages import ActionRequest
from lionagi.session.branch import Branch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_branch_with_tool(tool_fn=None):
    """Branch with one registered tool."""
    branch = Branch()
    if tool_fn is None:

        def add(a: int, b: int) -> int:
            """Add two numbers."""
            return a + b

        tool_fn = add
    branch.register_tools([tool_fn])
    return branch


# ---------------------------------------------------------------------------
# _act() — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_act_dict_request_success():
    """_act() with a plain dict {function, arguments} returns ActionResponseModel."""
    branch = _make_branch_with_tool()
    req = {"function": "add", "arguments": {"a": 3, "b": 4}}
    result = await _act(branch, req)
    assert isinstance(result, ActionResponseModel)
    assert result.function == "add"
    assert result.output == 7


@pytest.mark.asyncio
async def test_act_action_request_instance():
    """_act() with ActionRequest instance converts it to dict (line 31)."""
    branch = _make_branch_with_tool()
    ar = ActionRequest(
        content={"function": "add", "arguments": {"a": 1, "b": 1}},
        sender=branch.id,
        recipient=branch.id,
    )
    result = await _act(branch, ar)
    assert isinstance(result, ActionResponseModel)
    assert result.output == 2


@pytest.mark.asyncio
async def test_act_basemodel_with_function_and_arguments():
    """_act() accepts a BaseModel with 'function' and 'arguments' fields (lines 35-41)."""
    from pydantic import BaseModel

    class FuncCall(BaseModel):
        function: str
        arguments: dict

    branch = _make_branch_with_tool()
    req = FuncCall(function="add", arguments={"a": 5, "b": 5})
    result = await _act(branch, req)
    assert result.output == 10


# ---------------------------------------------------------------------------
# _act() — error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_act_invalid_request_missing_keys_raises():
    """_act() raises ValueError when dict lacks 'function' and 'arguments' (line 45)."""
    branch = Branch()
    with pytest.raises(ValueError, match="action_request must be"):
        await _act(branch, {"x": "bad"})


@pytest.mark.asyncio
async def test_act_suppress_errors_returns_error_response():
    """_act() with suppress_errors=True returns ActionResponseModel when action_manager raises."""
    branch = Branch()
    req = {"function": "bad_tool", "arguments": {"x": 1}}
    with patch.object(
        branch._action_manager,
        "invoke",
        new=AsyncMock(side_effect=RuntimeError("tool exploded")),
    ):
        result = await _act(branch, req, suppress_errors=True)
    assert isinstance(result, ActionResponseModel)
    assert "error" in result.output
    assert "tool exploded" in result.output["error"]


@pytest.mark.asyncio
async def test_act_suppress_errors_adds_messages_to_history():
    """_act() with suppress_errors=True records ActionRequest+ActionResponse in branch.messages."""
    branch = Branch()
    req = {"function": "bad_tool", "arguments": {"x": 1}}
    with patch.object(
        branch._action_manager,
        "invoke",
        new=AsyncMock(side_effect=RuntimeError("tool exploded")),
    ):
        result = await _act(branch, req, suppress_errors=True)

    assert isinstance(result, ActionResponseModel)

    # Both the request and the error response must be in chat history.
    from lionagi.protocols.messages import ActionRequest, ActionResponse

    action_requests = [m for m in branch.messages if isinstance(m, ActionRequest)]
    action_responses = [m for m in branch.messages if isinstance(m, ActionResponse)]
    assert len(action_requests) == 1, "Failed ActionRequest must be in branch.messages"
    assert len(action_responses) == 1, "Error ActionResponse must be in branch.messages"

    # The response output must include the error fields.
    response_output = action_responses[0].output
    assert "error" in response_output
    assert "tool exploded" in str(response_output["error"])


@pytest.mark.asyncio
async def test_act_suppress_errors_error_output_contains_function_and_args():
    """ActionResponse output includes function name and arguments for model context."""
    branch = Branch()
    req = {"function": "missing_fn", "arguments": {"key": "val"}}
    with patch.object(
        branch._action_manager,
        "invoke",
        new=AsyncMock(side_effect=ValueError("Function missing_fn is not registered.")),
    ):
        await _act(branch, req, suppress_errors=True)

    from lionagi.protocols.messages import ActionResponse

    responses = [m for m in branch.messages if isinstance(m, ActionResponse)]
    assert len(responses) == 1
    out = responses[0].output
    assert out.get("function") == "missing_fn"
    assert out.get("arguments") == {"key": "val"}


@pytest.mark.asyncio
async def test_act_suppress_errors_with_action_request_instance():
    """When action_request is already an ActionRequest, it is added to messages directly."""
    branch = Branch()
    ar = ActionRequest(
        content={"function": "nonexistent", "arguments": {}},
        sender=branch.id,
        recipient=branch.id,
    )
    with patch.object(
        branch._action_manager,
        "invoke",
        new=AsyncMock(side_effect=RuntimeError("no such tool")),
    ):
        await _act(branch, ar, suppress_errors=True)

    from lionagi.protocols.messages import ActionRequest as AR
    from lionagi.protocols.messages import ActionResponse

    requests = [m for m in branch.messages if isinstance(m, AR)]
    responses = [m for m in branch.messages if isinstance(m, ActionResponse)]
    assert len(requests) == 1
    assert len(responses) == 1


@pytest.mark.asyncio
async def test_act_suppress_errors_false_reraises():
    """_act() with suppress_errors=False re-raises when action_manager raises (line 82)."""
    branch = Branch()
    req = {"function": "exploding_tool", "arguments": {"x": 1}}
    with patch.object(
        branch._action_manager,
        "invoke",
        new=AsyncMock(side_effect=ValueError("boom")),
    ):
        with pytest.raises(ValueError, match="boom"):
            await _act(branch, req, suppress_errors=False)


@pytest.mark.asyncio
async def test_act_verbose_logging(caplog):
    """verbose_action=True emits debug log lines (lines 52-54, 57-60)."""
    import logging

    branch = _make_branch_with_tool()
    req = {"function": "add", "arguments": {"a": 2, "b": 3}}
    with caplog.at_level(logging.DEBUG, logger="lionagi.operations.act.act"):
        result = await _act(branch, req, verbose_action=True)
    assert isinstance(result, ActionResponseModel)
    assert result.output == 5


# ---------------------------------------------------------------------------
# act() — strategy dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_act_concurrent_strategy():
    """act() with strategy='concurrent' returns list of ActionResponseModel."""
    branch = _make_branch_with_tool()
    requests = [
        {"function": "add", "arguments": {"a": 1, "b": 1}},
        {"function": "add", "arguments": {"a": 2, "b": 2}},
    ]
    action_param = ActionParam(
        action_call_params=_get_default_call_params(),
        tools=None,
        strategy="concurrent",
    )
    results = await act(branch, requests, action_param)
    assert len(results) == 2
    assert all(isinstance(r, ActionResponseModel) for r in results)
    outputs = {r.output for r in results}
    assert outputs == {2, 4}


@pytest.mark.asyncio
async def test_act_sequential_strategy():
    """act() with strategy='sequential' runs actions one-by-one (lines 147-155, 185-192)."""
    branch = _make_branch_with_tool()
    order: list[int] = []

    def tracked_add(a: int, b: int) -> int:
        """Add with side effect."""
        order.append(a)
        return a + b

    branch.register_tools([tracked_add], update=True)

    requests = [
        {"function": "tracked_add", "arguments": {"a": 10, "b": 0}},
        {"function": "tracked_add", "arguments": {"a": 20, "b": 0}},
    ]
    action_param = ActionParam(
        action_call_params=_get_default_call_params(),
        tools=None,
        strategy="sequential",
    )
    results = await act(branch, requests, action_param)
    assert len(results) == 2
    assert order == [10, 20], "Sequential: must run in order"


@pytest.mark.asyncio
async def test_act_invalid_strategy_raises():
    """act() with unsupported strategy raises ValueError (line 155)."""
    branch = Branch()
    action_param = ActionParam(
        action_call_params=_get_default_call_params(),
        tools=None,
        strategy="bogus",  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="Invalid strategy"):
        await act(branch, {"function": "x", "arguments": {}}, action_param)


# ---------------------------------------------------------------------------
# prepare_act_kw
# ---------------------------------------------------------------------------


def test_prepare_act_kw_returns_correct_structure():
    """prepare_act_kw returns dict with action_request and action_param (lines 118-125)."""
    branch = Branch()
    req = {"function": "add", "arguments": {"a": 1, "b": 2}}
    kw = prepare_act_kw(branch, req, strategy="sequential", suppress_errors=False)
    assert "action_request" in kw
    assert "action_param" in kw
    assert kw["action_param"].strategy == "sequential"
    assert kw["action_param"].suppress_errors is False


def test_prepare_act_kw_defaults():
    """prepare_act_kw with defaults has strategy=concurrent and suppress_errors=True."""
    branch = Branch()
    req = {"function": "fn", "arguments": {}}
    kw = prepare_act_kw(branch, req)
    assert kw["action_param"].strategy == "concurrent"
    assert kw["action_param"].suppress_errors is True


# ---------------------------------------------------------------------------
# _sequential_act() directly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sequential_act_single_request():
    """_sequential_act() wraps a non-list request in a list."""
    branch = _make_branch_with_tool()
    result = await _sequential_act(
        branch, {"function": "add", "arguments": {"a": 7, "b": 3}}
    )
    assert len(result) == 1
    assert result[0].output == 10
