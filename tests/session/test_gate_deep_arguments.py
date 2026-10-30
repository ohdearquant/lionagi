# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Deep arguments cannot bypass a session gate when its snapshot fails."""

import importlib
import logging
import sys

import pytest

from lionagi.hooks.bus import HookBus, HookPoint
from lionagi.operations.act._gate_arguments import gate_arguments
from lionagi.operations.act.act import _act
from lionagi.protocols.messages import ActionRequest, ActionResponse
from lionagi.session.branch import Branch
from lionagi.session.control import ToolInvocation
from lionagi.session.session import Session


def _nested_value(depth: int, leaf: str):
    value = leaf
    for _ in range(depth):
        value = [value]
    return value


@pytest.mark.asyncio
async def test_deep_gate_snapshot_failure_refuses_without_disclosing_arguments(monkeypatch, caplog):
    secret = "deep-argument-fixture-secret"
    calls, decisions, refusals = [], [], []

    def tool(payload: list):
        """Record if an over-deep argument reaches a tool."""
        calls.append(payload)
        return "ran"

    def gate(value):
        if isinstance(value, ToolInvocation):
            decisions.append(value)
        return True

    branch = Branch()
    branch.register_tools([tool])
    session = Session(default_branch=branch)
    session.gate(gate)
    act_module = importlib.import_module("lionagi.operations.act.act")
    monkeypatch.setattr(act_module, "record_guard_refusal", lambda **kw: refusals.append(kw))

    arguments = {"payload": _nested_value(sys.getrecursionlimit() + 100, secret)}
    # Prove that this environment's snapshot actually reaches the refusal path.
    with pytest.raises(RecursionError):
        gate_arguments(branch._action_manager, "tool", arguments)

    with caplog.at_level(logging.DEBUG):
        result = await _act(branch, {"function": "tool", "arguments": arguments})
        await branch.drain_signals()

    assert result.output == {"error": "denied by governance gate", "function": "tool"}
    assert result.arguments == {}
    assert calls == []
    assert decisions == []
    assert len(refusals) == 1 and refusals[0]["call_id"]
    requests = [message for message in branch.messages if isinstance(message, ActionRequest)]
    responses = [message for message in branch.messages if isinstance(message, ActionResponse)]
    assert len(requests) == len(responses) == 1
    assert requests[0].arguments == {}
    assert responses[0].output == result.output
    assert secret not in caplog.text
    assert all(secret not in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_deep_argument_without_gate_still_reaches_manager_unchanged(monkeypatch):
    class ReachedManager(BaseException):
        pass

    class SummarySafeArguments(dict):
        def __str__(self):
            return "<deep arguments>"

    branch = Branch()
    # Avoid the existing argument-summary repr limit while checking gate-free identity.
    arguments = SummarySafeArguments(payload=_nested_value(sys.getrecursionlimit() + 100, "leaf"))
    seen = []

    async def invoke(_manager, request):
        seen.append(request["arguments"] is arguments)
        raise ReachedManager

    monkeypatch.setattr(type(branch._action_manager), "invoke", invoke)
    with pytest.raises(ReachedManager):
        await _act(branch, {"function": "tool", "arguments": arguments})

    assert seen == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("verbose_action", [False, True])
@pytest.mark.parametrize("suppress_errors", [False, True])
async def test_plain_deep_arguments_reach_manager_without_gate(
    monkeypatch, verbose_action, suppress_errors
):
    class ReachedManager(BaseException):
        pass

    arguments = {"payload": _nested_value(50_000, "private-leaf")}
    with pytest.raises(RecursionError):
        str(arguments)

    branch = Branch()
    seen = []
    pre_calls = []
    bus = HookBus()

    async def on_pre(**kwargs):
        pre_calls.append(kwargs)

    bus.on(HookPoint.TOOL_PRE, on_pre)
    branch._hooks = bus

    async def invoke(_manager, request):
        seen.append(request["arguments"] is arguments)
        raise ReachedManager

    monkeypatch.setattr(type(branch._action_manager), "invoke", invoke)
    with pytest.raises(ReachedManager):
        await _act(
            branch,
            {"function": "tool", "arguments": arguments},
            verbose_action=verbose_action,
            suppress_errors=suppress_errors,
        )
    assert seen == [True]
    assert len(pre_calls) == 1
    assert pre_calls[0]["args_summary"] == "<arguments too deeply nested>"
