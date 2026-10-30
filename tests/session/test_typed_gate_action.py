# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Typed session verdicts protect the actual pre-invoke tool path."""

import pytest

from lionagi.operations.act.act import _act
from lionagi.operations.fields import ActionResponseModel
from lionagi.protocols.action.gate import GateResult
from lionagi.protocols.messages import ActionRequest, ActionResponse
from lionagi.session.branch import Branch
from lionagi.session.control import ToolInvocation
from lionagi.session.session import Session
from lionagi.session.signal import GateDenied


class TruthyDecision:
    def __bool__(self):
        return True


VERDICTS = [
    pytest.param(GateResult(False, "policy", "add", "authorize", "denied"), False, id="deny"),
    pytest.param(
        GateResult(False, "policy", "add", "authorize", "fault", errored=True), False, id="fault"
    ),
    pytest.param(GateResult(True, "policy", "add", "authorize", "allowed"), True, id="allow"),
    pytest.param(
        GateResult(True, "policy", "add", "authorize", "allowed", {"a": 3, "b": 4}),
        False,
        id="equal-rewrite",
    ),
    pytest.param(
        GateResult(True, "policy", "add", "authorize", "allowed", {"a": 99, "b": 4}),
        False,
        id="different-rewrite",
    ),
    pytest.param(
        GateResult("yes", "policy", "add", "authorize", "reason"), False, id="allowed-string"
    ),
    pytest.param(GateResult(1, "policy", "add", "authorize", "reason"), False, id="allowed-int"),
    pytest.param(
        GateResult(TruthyDecision(), "policy", "add", "authorize", "reason"),
        False,
        id="allowed-object",
    ),
    pytest.param(
        GateResult(True, "policy", "add", "authorize", "reason", errored=None),
        False,
        id="errored-none",
    ),
    pytest.param(
        GateResult(True, "policy", "add", "authorize", "reason", errored=0), False, id="errored-int"
    ),
    pytest.param(
        GateResult(True, "policy", "add", "authorize", "reason", errored="fault"),
        False,
        id="errored-string",
    ),
    pytest.param(
        GateResult(True, "policy", "add", "authorize", "reason", errored=True),
        False,
        id="contradiction",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("verdict,allowed", VERDICTS)
async def test_typed_gate_controls_tool_execution_and_denial_record(is_async, verdict, allowed):
    tool_calls, gate_calls = [], []

    def add(a: int, b: int) -> int:
        """Add two numbers and record execution in memory."""
        tool_calls.append((a, b))
        return a + b

    def check(payload):
        # Signals emitted as messages are recorded are not pre-invoke proposals.
        if not isinstance(payload, ToolInvocation):
            return True
        gate_calls.append(payload)
        return verdict

    async def async_check(payload):
        return check(payload)

    branch = Branch()
    branch.register_tools([add])
    session = Session(default_branch=branch)
    session.gate(async_check if is_async else check)
    result = await _act(branch, {"function": "add", "arguments": {"a": 3, "b": 4}})
    await branch.drain_signals()

    assert isinstance(result, ActionResponseModel)
    assert result.function == "add"
    assert result.arguments == {"a": 3, "b": 4}
    assert tool_calls == ([(3, 4)] if allowed else [])
    assert len(gate_calls) == 1
    invocation = gate_calls[0]
    assert invocation == ToolInvocation(
        function="add", arguments={"a": 3, "b": 4}, branch_id=str(branch.id)
    )
    denials = session.observer.by_type(GateDenied)
    if allowed:
        assert result.output == 7
        assert denials == []
        return

    denial = {"error": "denied by governance gate", "function": "add"}
    assert result.output == denial
    assert len(denials) == 1
    assert denials[0].data is None
    requests = [message for message in branch.messages if isinstance(message, ActionRequest)]
    responses = [message for message in branch.messages if isinstance(message, ActionResponse)]
    assert len(requests) == len(responses) == 1
    assert requests[0].function == "add"
    assert requests[0].arguments == {"a": 3, "b": 4}
    assert responses[0].output == denial


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
async def test_typed_rewrite_is_rejected_when_values_compare_equal(is_async):
    tool_calls, gate_calls = [], []

    def observe(value: int) -> int:
        """Record the exact value and type received by the tool."""
        tool_calls.append((value, type(value)))
        return value

    def check(payload):
        if not isinstance(payload, ToolInvocation):
            return True
        gate_calls.append(payload)
        return GateResult(True, "policy", "observe", "authorize", "allow", {"value": False})

    async def async_check(payload):
        return check(payload)

    branch = Branch()
    branch.register_tools([observe])
    session = Session(default_branch=branch)
    session.gate(async_check if is_async else check)
    result = await _act(branch, {"function": "observe", "arguments": {"value": 0}})
    await branch.drain_signals()

    assert len(gate_calls) == 1
    assert gate_calls[0].arguments == {"value": 0}
    assert type(gate_calls[0].arguments["value"]) is int
    assert tool_calls == []
    assert result.output == {"error": "denied by governance gate", "function": "observe"}
    assert [item.data for item in session.observer.by_type(GateDenied)] == [None]
