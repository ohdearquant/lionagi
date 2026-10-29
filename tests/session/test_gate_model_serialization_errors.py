# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tool-call argument serialization failures are handled before authorization."""

import pytest
from pydantic import BaseModel, ValidationError, model_serializer

from lionagi.hooks.bus import HookBus, HookPoint
from lionagi.operations.act.act import _act
from lionagi.protocols.messages import ActionRequest, ActionResponse
from lionagi.session.branch import Branch
from lionagi.session.control import ToolInvocation
from lionagi.session.session import Session


@pytest.mark.asyncio
@pytest.mark.parametrize("function", [None, "", "   "], ids=["null", "empty", "blank"])
@pytest.mark.parametrize("as_model", [False, True], ids=["dict-request", "model-request"])
async def test_unnamed_call_does_not_serialize_model_arguments(function, as_model):
    serialization_calls = []
    tool_calls = []

    class Arguments(BaseModel):
        value: int

        @model_serializer
        def serialize(self):
            serialization_calls.append(self.value)
            raise RuntimeError("arguments must not be serialized for an unnamed call")

    class Request(BaseModel):
        function: str | None
        arguments: Arguments

    def observe(value: int) -> int:
        """Record invocation without external effects."""
        tool_calls.append(value)
        return value

    branch = Branch()
    branch.register_tools([observe])
    arguments = Arguments(value=7)
    request = (
        Request(function=function, arguments=arguments)
        if as_model
        else {"function": function, "arguments": arguments}
    )

    result = await _act(branch, request, suppress_errors=True)

    assert serialization_calls == []
    assert tool_calls == []
    assert result.function == ""
    assert result.arguments == {}
    assert "names no function" in result.output["error"]
    requests = [message for message in branch.messages if isinstance(message, ActionRequest)]
    responses = [message for message in branch.messages if isinstance(message, ActionResponse)]
    assert len(requests) == len(responses) == 1
    assert requests[0].function == ""
    assert requests[0].arguments == {}
    assert "names no function" in responses[0].output["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("suppress_errors", [False, True], ids=["raise", "suppress"])
@pytest.mark.parametrize("as_model", [False, True], ids=["dict-request", "model-request"])
@pytest.mark.parametrize("async_gate", [False, True], ids=["sync-gate", "async-gate"])
async def test_named_model_serialization_error_preserves_action_error_path(
    suppress_errors, as_model, async_gate
):
    serialization_calls, tool_calls, gate_calls = [], [], []
    hook_calls = []

    class Arguments(BaseModel):
        value: int

        @model_serializer
        def serialize(self):
            serialization_calls.append(self.value)
            raise RuntimeError("synthetic serializer failure")

    class Request(BaseModel):
        function: str
        arguments: Arguments

    def observe(value: int) -> int:
        """Record invocation without external effects."""
        tool_calls.append(value)
        return value

    branch = Branch()
    branch.register_tools([observe])
    session = Session(default_branch=branch)

    def gate(value):
        if isinstance(value, ToolInvocation):
            gate_calls.append(value)
        return True

    async def async_check(value):
        return gate(value)

    session.gate(async_check if async_gate else gate)
    bus = HookBus()
    bus.on(HookPoint.TOOL_PRE, lambda **kwargs: hook_calls.append(("pre", kwargs)))
    bus.on(HookPoint.TOOL_ERROR, lambda **kwargs: hook_calls.append(("error", kwargs)))
    bus.on(HookPoint.TOOL_POST, lambda **kwargs: hook_calls.append(("post", kwargs)))
    branch._hooks = bus
    arguments = Arguments(value=7)
    request = (
        Request(function="observe", arguments=arguments)
        if as_model
        else {"function": "observe", "arguments": arguments}
    )

    with pytest.raises(ValidationError) as caught:
        await _act(branch, request, suppress_errors=suppress_errors)
    await branch.drain_signals()

    assert serialization_calls == ([7, 7] if suppress_errors else [7])
    assert tool_calls == []
    assert gate_calls == []
    assert [kind for kind, _ in hook_calls] == ["pre", "error"]
    assert hook_calls[0][1]["tool_name"] == hook_calls[1][1]["tool_name"] == "observe"
    assert isinstance(hook_calls[1][1]["error"], ValidationError)
    assert "synthetic serializer failure" in str(hook_calls[1][1]["error"])
    requests = [message for message in branch.messages if isinstance(message, ActionRequest)]
    responses = [message for message in branch.messages if isinstance(message, ActionResponse)]
    if suppress_errors:
        assert "ActionResponseModel" in str(caught.value)
        assert len(requests) == len(responses) == 1
        assert requests[0].function == "observe"
        assert responses[0].output["function"] == "observe"
        assert "synthetic serializer failure" in str(responses[0].output["error"])
    else:
        assert "synthetic serializer failure" in str(caught.value)
        assert requests == responses == []
