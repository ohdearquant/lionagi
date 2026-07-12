# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Pre-invoke governance gate (ADR-0047 Follow-up 1).

``session.gate(check)`` is consulted BEFORE a tool runs, not just after an event
is recorded. A falsy/raised verdict blocks the tool; the denial is surfaced to
the model as a tool result (never raised) and recorded onto the observer Flow as
a ``GateDenied`` audit signal. With no gate set, behaviour is unchanged.
"""

import pytest

from lionagi.operations.act.act import _act
from lionagi.operations.fields import ActionResponseModel
from lionagi.session.branch import Branch
from lionagi.session.control import ToolInvocation
from lionagi.session.observer import SessionObserver
from lionagi.session.session import Session
from lionagi.session.signal import GateDenied


def _adder():
    """A tool that records every execution, so a test can prove it did NOT run."""
    calls: list = []

    def add(a: int, b: int) -> int:
        """Add two numbers."""
        calls.append((a, b))
        return a + b

    return add, calls


# -- observer.authorize (unit) -----------------------------------------------


@pytest.mark.asyncio
async def test_authorize_allows_when_no_gate():
    obs = SessionObserver()
    assert await obs.authorize(ToolInvocation(function="rm")) is True
    assert obs.by_type(GateDenied) == []


@pytest.mark.asyncio
async def test_authorize_denies_and_records_audit_signal():
    obs = SessionObserver()
    obs.gate(lambda inv: inv.function != "rm")  # deny "rm"
    assert await obs.authorize(ToolInvocation(function="rm")) is False
    assert await obs.authorize(ToolInvocation(function="ls")) is True
    denials = obs.by_type(GateDenied)
    assert len(denials) == 1  # only the denied action is recorded


@pytest.mark.asyncio
async def test_authorize_treats_raising_gate_as_denial():
    obs = SessionObserver()

    def boom(inv):
        raise RuntimeError("policy engine down")

    obs.gate(boom)
    assert await obs.authorize(ToolInvocation(function="x")) is False


@pytest.mark.asyncio
async def test_authorize_supports_async_gate():
    obs = SessionObserver()

    async def agate(inv):
        return inv.function == "ok"

    obs.gate(agate)
    assert await obs.authorize(ToolInvocation(function="ok")) is True
    assert await obs.authorize(ToolInvocation(function="no")) is False


@pytest.mark.asyncio
async def test_branch_authorize_standalone_allows():
    # A branch with no session/observer must never block (ungoverned default).
    assert await Branch().authorize(ToolInvocation(function="rm")) is True


# -- end-to-end tool gating through _act -------------------------------------


@pytest.mark.asyncio
async def test_gate_blocks_denied_tool_before_execution():
    add, calls = _adder()
    branch = Branch()
    branch.register_tools([add])
    session = Session(default_branch=branch)
    session.gate(lambda inv: inv.function != "add")  # deny the only tool

    result = await _act(branch, {"function": "add", "arguments": {"a": 3, "b": 4}})

    assert isinstance(result, ActionResponseModel)
    assert result.output == {"error": "denied by governance gate", "function": "add"}
    assert calls == []  # the tool body NEVER ran — blocked pre-invoke
    assert len(session.observer.by_type(GateDenied)) == 1


@pytest.mark.asyncio
async def test_gate_denial_is_surfaced_into_chat_history():
    # A ReAct loop must SEE the denial to adapt — it lands as a message, not a raise.
    add, _ = _adder()
    branch = Branch()
    branch.register_tools([add])
    session = Session(default_branch=branch)
    session.gate(lambda inv: False)  # deny everything

    before = len(branch.messages)
    await _act(branch, {"function": "add", "arguments": {"a": 1, "b": 1}})
    assert len(branch.messages) > before  # request + denial response recorded


@pytest.mark.asyncio
async def test_gate_allows_permitted_tool_runs_normally():
    add, calls = _adder()
    branch = Branch()
    branch.register_tools([add])
    session = Session(default_branch=branch)
    session.gate(lambda inv: True)  # allow all

    result = await _act(branch, {"function": "add", "arguments": {"a": 2, "b": 5}})
    assert result.output == 7
    assert calls == [(2, 5)]  # tool actually executed
    assert session.observer.by_type(GateDenied) == []


@pytest.mark.asyncio
async def test_gate_can_scope_policy_by_arguments():
    # The gate sees arguments, so it can allow/deny on payload, not just name.
    add, calls = _adder()
    branch = Branch()
    branch.register_tools([add])
    session = Session(default_branch=branch)
    session.gate(lambda inv: inv.arguments.get("a", 0) >= 0)  # deny negative a

    ok = await _act(branch, {"function": "add", "arguments": {"a": 1, "b": 2}})
    assert ok.output == 3
    blocked = await _act(branch, {"function": "add", "arguments": {"a": -1, "b": 2}})
    assert blocked.output["error"] == "denied by governance gate"
    assert calls == [(1, 2)]  # only the permitted call ran
