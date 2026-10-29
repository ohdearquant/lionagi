# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Invalid model arguments fail before authorization and tool hooks.

A named normalization failure is logged and recorded with sanitized arguments
in both suppression modes; suppression returns an error instead of raising.
Unnamed calls retain their nameless error and never serialize their arguments.
"""

import pytest
from pydantic import BaseModel, model_serializer

from lionagi.hooks.bus import HookBus, HookPoint
from lionagi.operations.act.act import _act
from lionagi.protocols.messages import ActionRequest, ActionResponse
from lionagi.session.branch import Branch
from lionagi.session.control import ToolInvocation
from lionagi.session.session import Session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "function", ["observe", None, "", "   "], ids=["named", "null", "empty", "blank"]
)
@pytest.mark.parametrize("dump_mode", ["raises", "non-dict"])
@pytest.mark.parametrize("suppress_errors", [False, True], ids=["raise", "suppress"])
@pytest.mark.parametrize("as_model", [False, True], ids=["dict-request", "model-request"])
@pytest.mark.parametrize("async_gate", [False, True], ids=["sync-gate", "async-gate"])
async def test_model_argument_failure_is_recorded_before_gate_and_tool_hooks(
    function, dump_mode, suppress_errors, as_model, async_gate, monkeypatch
):
    serializations, tool_calls, gate_calls, hook_calls, logs = [], [], [], [], []

    class Arguments(BaseModel):
        value: int

        @model_serializer
        def serialize(self):
            serializations.append(self.value)
            if dump_mode == "raises":
                raise RuntimeError("synthetic serializer failure")
            return [self.value]

    class Request(BaseModel):
        function: str | None
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
    monkeypatch.setattr(type(branch._log_manager), "log", lambda _self, entry: logs.append(entry))
    arguments = Arguments(value=7)
    request = (
        Request(function=function, arguments=arguments)
        if as_model
        else {"function": function, "arguments": arguments}
    )

    if suppress_errors:
        result = await _act(branch, request, suppress_errors=True)
    else:
        with pytest.raises(Exception) as caught:
            await _act(branch, request, suppress_errors=False)
        result = None
    await branch.drain_signals()

    named = function == "observe"
    assert serializations == ([7] if named else [])
    assert tool_calls == gate_calls == hook_calls == []
    assert len(logs) == 1
    assert isinstance(logs[0], dict)
    assert logs[0].get("error")
    assert logs[0].get("arguments") == {}
    requests = [message for message in branch.messages if isinstance(message, ActionRequest)]
    responses = [message for message in branch.messages if isinstance(message, ActionResponse)]
    assert len(requests) == len(responses) == 1
    assert requests[0].function == ("observe" if named else "")
    assert requests[0].arguments == {}
    assert isinstance(responses[0].output, dict)
    assert responses[0].output.get("error")
    if named:
        assert "normalize" in str(responses[0].output["error"])
        assert "synthetic serializer failure" not in str(responses[0].output)
        assert responses[0].output["arguments"] == {}
    else:
        assert "names no function" in str(responses[0].output["error"])
    if suppress_errors:
        assert result.function == ("observe" if named else "")
        assert result.arguments == {}
        assert isinstance(result.output, dict) and result.output.get("error")
        if not named:
            assert "names no function" in str(result.output["error"])
    else:
        assert str(caught.value)
        if not named:
            assert isinstance(caught.value, ValueError)
            assert "names no function" in str(caught.value)
