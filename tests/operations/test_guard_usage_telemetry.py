# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Usage counts typed action-policy boundaries without changing their outcomes."""

from unittest.mock import AsyncMock

import pytest

from lionagi.agent.gate import GateDeniedError, GateResult
from lionagi.cli._api_usage import APIUsageCollector
from lionagi.hooks.bus import HookBus, HookPoint
from lionagi.operations.act.act import _act
from lionagi.protocols.action.function_calling import RevalidationDeniedError
from lionagi.protocols.action.tool_hooks import ToolHookDeniedError, ToolPreDecision
from lionagi.service._api_telemetry import collect_api_turn
from lionagi.session import Session
from lionagi.state.compat import StateCompatibilityRefused
from tests.operations.test_act import _make_branch_with_tool

REQUEST = {"function": "add", "arguments": {"a": 2, "b": 3}}


def _collector():
    return APIUsageCollector(run_id="run-a", branch_id="branch-a")


@pytest.mark.asyncio
async def test_actual_session_false_gate_counts_each_action_once_without_hooks():
    branch = _make_branch_with_tool()
    session = Session()
    session.include_branches(branch)
    session.gate(lambda action: False)
    assert branch._hooks is None
    collector = _collector()
    with collect_api_turn(collector, 1):
        results = [await _act(branch, REQUEST) for _ in range(2)]
    assert [result.output["error"] for result in results] == [
        "denied by governance gate",
        "denied by governance gate",
    ]
    usage = collector.close()
    assert usage["guard_refusal_count"] == 2
    assert usage["guard_refusal_coverage"] == "act_policy_denial_v1"
    assert usage["requests"] == []


@pytest.mark.asyncio
async def test_real_tool_hook_denial_counts_captured_failure_once():
    branch = _make_branch_with_tool()
    branch._action_manager.add_tool_pre_hook(
        lambda name, arguments: ToolPreDecision(decision="deny", reason="policy")
    )
    collector = _collector()
    with collect_api_turn(collector, 1):
        result = await _act(branch, REQUEST, suppress_errors=True)
    assert "policy" in result.output["error"]
    assert collector.close()["guard_refusal_count"] == 1


@pytest.mark.parametrize("suppress", [False, True])
@pytest.mark.parametrize("kind", ["gate", "revalidation"])
@pytest.mark.asyncio
async def test_direct_typed_denials_preserve_raise_or_response(monkeypatch, kind, suppress):
    branch = _make_branch_with_tool()
    error = (
        GateDeniedError(GateResult(False, "policy", "add", "invoke", "refused"))
        if kind == "gate"
        else RevalidationDeniedError("refused")
    )
    monkeypatch.setattr(branch._action_manager, "invoke", AsyncMock(side_effect=error))
    collector = _collector()
    with collect_api_turn(collector, 1):
        if suppress:
            result = await _act(branch, REQUEST, suppress_errors=True)
            assert "refused" in result.output["error"]
        else:
            with pytest.raises(type(error)) as caught:
                await _act(branch, REQUEST)
            assert caught.value is error
    assert collector.close()["guard_refusal_count"] == 1


@pytest.mark.asyncio
async def test_nested_compatibility_refusal_wins_over_typed_denial(monkeypatch):
    from exceptiongroup import ExceptionGroup

    branch = _make_branch_with_tool()
    refusal = StateCompatibilityRefused("write", "future", "read only")
    error = ExceptionGroup("both", [ToolHookDeniedError("policy", "deny"), refusal])
    monkeypatch.setattr(branch._action_manager, "invoke", AsyncMock(side_effect=error))
    collector = _collector()
    with collect_api_turn(collector, 1):
        with pytest.raises(StateCompatibilityRefused) as caught:
            await _act(branch, REQUEST, suppress_errors=True)
    assert caught.value is refusal
    assert collector.close()["guard_refusal_count"] == 0


@pytest.mark.asyncio
async def test_tool_pre_bus_denial_stays_excluded_and_propagates_unchanged():
    branch = _make_branch_with_tool()
    bus = HookBus()
    error = ToolHookDeniedError("audit", "deny")

    async def deny(**kwargs):
        raise error

    bus.on(HookPoint.TOOL_PRE, deny)
    branch.attach_hook_bus(bus)
    collector = _collector()
    with collect_api_turn(collector, 1):
        with pytest.raises(ToolHookDeniedError) as caught:
            await _act(branch, REQUEST, suppress_errors=True)
    assert caught.value is error
    assert collector.close()["guard_refusal_count"] == 0


@pytest.mark.asyncio
async def test_business_permission_errors_and_successful_denial_text_do_not_count():
    def business_error(a: int, b: int):
        raise PermissionError("denied by business data")

    def denied_text(a: int, b: int):
        return {"error": "denied by governance gate"}

    collector = _collector()
    with collect_api_turn(collector, 1):
        failed = await _act(
            _make_branch_with_tool(business_error),
            {"function": "business_error", "arguments": {"a": 1, "b": 2}},
            suppress_errors=True,
        )
        successful = await _act(
            _make_branch_with_tool(denied_text),
            {"function": "denied_text", "arguments": {"a": 1, "b": 2}},
        )
    assert failed.output is None
    assert successful.output == {"error": "denied by governance gate"}
    assert collector.close()["guard_refusal_count"] == 0


@pytest.mark.asyncio
async def test_guard_sink_failure_preserves_false_gate_response(monkeypatch):
    branch = _make_branch_with_tool()
    session = Session()
    session.include_branches(branch)
    session.gate(lambda action: False)
    collector = _collector()

    def broken(**kwargs):
        raise RuntimeError("observation unavailable")

    monkeypatch.setattr(collector, "record_guard_refusal", broken)
    with collect_api_turn(collector, 1):
        result = await _act(branch, REQUEST)
    assert result.output["error"] == "denied by governance gate"
