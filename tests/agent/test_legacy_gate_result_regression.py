# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Security-pre verdicts remain authoritative through real tool invocation."""

import logging

import pytest

from lionagi.agent.factory import _attach_hooks
from lionagi.agent.spec import AgentSpec
from lionagi.protocols.action.function_calling import FunctionCalling
from lionagi.protocols.action.gate import GateDeniedError, GateResult
from lionagi.protocols.action.tool import Tool
from lionagi.protocols.generic.event import EventStatus


class _SensitiveUnknown:
    def __repr__(self):
        return "secret-value-must-not-be-logged"


def _hook(return_value, asynchronous):
    if asynchronous:

        async def security_hook(tool_name, action, args):
            return return_value
    else:

        def security_hook(tool_name, action, args):
            return return_value

    return security_hook


async def _invoke(hook, *, user_hook=None):
    calls = []

    async def execute(action: str, value: str) -> str:
        calls.append((action, value))
        return value

    spec = AgentSpec.coding(secure=False)
    spec.security_pre("execute", hook)
    if user_hook is not None:
        spec.pre("execute", user_hook)
    tool = _attach_hooks(Tool(func_callable=execute), spec, "execute")
    event = FunctionCalling(func_tool=tool, arguments={"action": "read", "value": "original"})
    await event.invoke()
    return event, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("errored", [False, True], ids=["policy-denial", "control-fault"])
async def test_gate_result_denial_stops_real_tool_and_preserves_verdict(
    asynchronous, errored, caplog
):
    caplog.set_level(logging.ERROR, logger="lionagi.agent.gate")
    verdict = GateResult(
        False, "policy_alpha", "execute", "read", "explicit refusal", errored=errored
    )
    event, calls = await _invoke(_hook(verdict, asynchronous))
    assert calls == []
    assert event.status is EventStatus.FAILED
    assert isinstance(event.execution.error, GateDeniedError)
    assert event.execution.error.result == verdict
    assert event.execution.error.result.control == "policy_alpha"
    assert event.execution.error.result.errored is errored
    if errored:
        assert "policy_alpha" in caplog.text and "without reaching a verdict" in caplog.text
    else:
        assert "without reaching a verdict" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
async def test_allowed_gate_result_rewrite_reaches_the_tool(asynchronous):
    verdict = GateResult(
        True,
        "policy_alpha",
        "execute",
        "read",
        "allow",
        mutated_args={"action": "write", "value": "rewritten"},
    )
    event, calls = await _invoke(_hook(verdict, asynchronous))
    assert event.status is EventStatus.COMPLETED
    assert calls == [("write", "rewritten")]
    assert event.response == "rewritten"


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("value", [False, True, 0, "untrusted", [], object(), _SensitiveUnknown()])
async def test_unknown_legacy_result_fails_closed_without_logging_value(
    asynchronous, value, caplog
):
    caplog.set_level(logging.ERROR, logger="lionagi.agent.gate")
    event, calls = await _invoke(_hook(value, asynchronous))
    assert calls == []
    assert event.status is EventStatus.FAILED
    error = event.execution.error
    assert isinstance(error, GateDeniedError)
    assert error.result.errored is True
    assert error.result.control == "security_hook"
    assert type(value).__name__ in error.result.reason
    assert "secret-value-must-not-be-logged" not in error.result.reason
    assert "secret-value-must-not-be-logged" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("rewrite", [False, True], ids=["none", "dict-rewrite"])
async def test_legacy_none_and_dict_results_keep_existing_behavior(asynchronous, rewrite):
    value = {"action": "write", "value": "rewritten"} if rewrite else None
    event, calls = await _invoke(_hook(value, asynchronous))
    assert event.status is EventStatus.COMPLETED
    assert calls == [("write", "rewritten") if rewrite else ("read", "original")]


@pytest.mark.asyncio
async def test_gate_result_denial_in_second_security_pass_prevents_tool_call():
    seen = []
    denial = GateResult(False, "second_pass", "execute", "write", "write denied")

    async def security_hook(tool_name, action, args):
        seen.append((action, args["value"]))
        if action == "write":
            return denial
        return GateResult(True, "first_pass", tool_name, action, "allow")

    async def rewrite(tool_name, action, args):
        return {"action": "write", "value": "modified"}

    event, calls = await _invoke(security_hook, user_hook=rewrite)
    assert seen == [("read", "original"), ("write", "modified")]
    assert calls == []
    assert event.status is EventStatus.FAILED
    assert isinstance(event.execution.error, GateDeniedError)
    assert event.execution.error.result == denial
