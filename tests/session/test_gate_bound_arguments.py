# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The session gate sees and protects the arguments used by a tool call."""

import logging
from copy import deepcopy

import pytest
from pydantic import BaseModel, field_validator

from lionagi.hooks.bus import HookBus, HookPoint
from lionagi.operations.act._gate_arguments import gate_arguments
from lionagi.operations.act.act import _act
from lionagi.protocols.action.tool import Tool
from lionagi.session.branch import Branch
from lionagi.session.control import ToolInvocation
from lionagi.session.session import Session
from lionagi.session.signal import GateDenied, Signal


class PathArguments(BaseModel):
    path: str
    mode: str = "model-default"


@pytest.mark.asyncio
@pytest.mark.parametrize("model_arguments", [False, True], ids=["dict", "model"])
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("explicit_safe", [False, True], ids=["default-denied", "explicit-allowed"])
async def test_gate_sees_callable_default_before_authorization(
    model_arguments, is_async, explicit_safe
):
    calls, seen = [], []

    def access(path: str, mode: str = "write") -> str:
        """Record an in-memory tool invocation."""
        calls.append((path, mode))
        return mode

    def gate(value):
        if not isinstance(value, ToolInvocation):
            return True
        seen.append(deepcopy(value.arguments))
        # A missing key appears safe to this policy, but the tool's actual
        # default is write and must be visible before the decision.
        return value.arguments.get("mode", "read") == "read"

    async def async_gate(value):
        return gate(value)

    branch = Branch()
    branch.register_tools([access])
    session = Session(default_branch=branch)
    session.gate(async_gate if is_async else gate)
    if explicit_safe:
        arguments = (
            PathArguments(path="/tmp/example", mode="read")
            if model_arguments
            else {"path": "/tmp/example", "mode": "read"}
        )
    elif model_arguments:
        arguments = PathArguments(path="/tmp/example")
    else:
        arguments = {"path": "/tmp/example"}

    result = await _act(branch, {"function": "access", "arguments": arguments})
    await branch.drain_signals()

    expected_mode = "read" if explicit_safe else "write"
    assert seen == [{"path": "/tmp/example", "mode": expected_mode}]
    assert calls == ([("/tmp/example", "read")] if explicit_safe else [])
    if explicit_safe:
        assert result.output == "read"
        assert session.observer.by_type(GateDenied) == []
    else:
        assert result.output == {"error": "denied by governance gate", "function": "access"}
        assert len(session.observer.by_type(GateDenied)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
async def test_bound_kwargs_keep_flat_call_shape(is_async):
    calls, seen = [], []

    def access(path: str, mode: str = "read", **kwargs) -> str:
        """Record explicit extras and the callable default."""
        calls.append((path, mode, kwargs))
        return mode

    def gate(value):
        if isinstance(value, ToolInvocation):
            seen.append(deepcopy(value.arguments))
        return True

    async def async_gate(value):
        return gate(value)

    branch = Branch()
    branch.register_tools([access])
    session = Session(default_branch=branch)
    session.gate(async_gate if is_async else gate)
    result = await _act(branch, {"function": "access", "arguments": {"path": "p", "tag": "v"}})
    await branch.drain_signals()

    assert seen == [{"path": "p", "mode": "read", "tag": "v"}]
    assert calls == [("p", "read", {"tag": "v"})]
    assert result.output == "read"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "strict,explicit_mode,expected_call",
    [
        (False, False, True),
        (False, True, True),
        (True, False, False),
        (True, True, True),
    ],
    ids=["loose-default", "loose-explicit", "strict-missing", "strict-explicit"],
)
async def test_binding_does_not_change_tool_admission(
    strict, explicit_mode, expected_call, monkeypatch
):
    calls, seen, invoked = [], [], []

    def access(path: str, mode: str = "read") -> str:
        """Record the arguments reaching the tool."""
        calls.append((path, mode))
        return mode

    branch = Branch()
    branch.register_tools([Tool(func_callable=access, strict_func_call=strict)])
    manager_type = type(branch._action_manager)
    original_invoke = manager_type.invoke

    async def record_invoke(manager, request):
        invoked.append(deepcopy(request["arguments"]))
        return await original_invoke(manager, request)

    monkeypatch.setattr(manager_type, "invoke", record_invoke)
    session = Session(default_branch=branch)

    def gate(value):
        if isinstance(value, ToolInvocation):
            seen.append(deepcopy(value.arguments))
        return True

    session.gate(gate)
    args = {"path": "/safe"}
    if explicit_mode:
        args["mode"] = "read"
    result = await _act(branch, {"function": "access", "arguments": args}, suppress_errors=True)
    await branch.drain_signals()

    assert seen == [{"path": "/safe", "mode": "read"}]
    # Binding is for the gate view. The manager receives no inserted default.
    assert invoked == [args]
    assert calls == ([("/safe", "read")] if expected_call else [])
    if expected_call:
        assert result.output == "read"
    else:
        assert "error" in result.output


@pytest.mark.asyncio
async def test_request_options_are_not_applied_before_authorization(monkeypatch):
    class Input(BaseModel):
        count: int

    calls, seen, invoked = [], [], []

    def access(count: int, mode: str = "read") -> int:
        """Record validated arguments and the tool default."""
        calls.append((count, mode))
        return count

    branch = Branch()
    branch.register_tools([Tool(func_callable=access, request_options=Input)])
    manager_type = type(branch._action_manager)
    original_invoke = manager_type.invoke

    async def record_invoke(manager, request):
        invoked.append(deepcopy(request["arguments"]))
        return await original_invoke(manager, request)

    monkeypatch.setattr(manager_type, "invoke", record_invoke)
    session = Session(default_branch=branch)

    def gate(value):
        if isinstance(value, ToolInvocation):
            seen.append(deepcopy(value.arguments))
        return True

    session.gate(gate)
    args = {"count": "3"}
    result = await _act(branch, {"function": "access", "arguments": args})
    await branch.drain_signals()

    assert seen == [{"count": "3", "mode": "read"}]
    assert invoked == [{"count": "3"}]
    assert args == {"count": "3"}
    assert calls == [(3, "read")]
    assert result.output == 3


@pytest.mark.asyncio
async def test_stateful_request_validator_runs_only_at_existing_manager_steps():
    validation_inputs, gate_inputs, tool_inputs = [], [], []

    class Input(BaseModel):
        count: int

        @field_validator("count")
        @classmethod
        def increment(cls, value):
            validation_inputs.append(value)
            return value + 1

    def access(count: int) -> int:
        """Record the value that reaches the tool."""
        tool_inputs.append(count)
        return count

    branch = Branch()
    branch.register_tools([Tool(func_callable=access, request_options=Input)])
    session = Session(default_branch=branch)

    def gate(value):
        if isinstance(value, ToolInvocation):
            gate_inputs.append(value.arguments["count"])
        return True

    session.gate(gate)
    result = await _act(branch, {"function": "access", "arguments": {"count": 0}})
    await branch.drain_signals()

    assert gate_inputs == [0]
    assert validation_inputs == [0, 1]
    assert tool_inputs == [2]
    assert result.output == 2


@pytest.mark.asyncio
async def test_custom_argument_is_shared_with_gate_and_tool(caplog):
    canary = "uncopyable-fixture-value"

    class Uncopyable:
        def __init__(self):
            self.value = "before"

        def __deepcopy__(self, _memo):
            raise RuntimeError("copy unavailable")

        def __repr__(self):
            return canary

    marker = Uncopyable()
    arguments = {"payload": marker}
    gate_mappings, tool_values = [], []

    def access(payload: object) -> str:
        """Record the object actually passed to the in-memory tool."""
        tool_values.append((payload is marker, payload.value))
        return "done"

    branch = Branch()
    branch.register_tools([access])
    session = Session(default_branch=branch)

    def gate(value):
        if isinstance(value, ToolInvocation):
            gate_mappings.append(value.arguments)
            value.arguments["payload"].value = "after"
        return True

    session.gate(gate)
    # Subsequent event/history persistence may itself reject an uncopyable
    # argument; this test pins the gate and invocation behavior.
    with caplog.at_level(logging.DEBUG):
        try:
            await _act(branch, {"function": "access", "arguments": arguments})
        except Exception:
            pass
        await branch.drain_signals()

    assert len(gate_mappings) == 1
    assert gate_mappings[0] is not arguments
    assert gate_mappings[0]["payload"] is marker
    assert tool_values == [(True, "after")]
    assert all(canary not in record.getMessage() for record in caplog.records)


def test_gate_copy_does_not_invoke_user_copy_hooks():
    class Marker:
        def __deepcopy__(self, _memo):
            raise AssertionError("the gate must not call copy hooks")

    marker = Marker()
    branch = Branch()
    original = {"nested": [{"marker": marker}]}
    invocation, view = gate_arguments(branch._action_manager, "unknown", original)

    assert invocation is not original
    assert view is not invocation
    assert invocation["nested"] is not original["nested"]
    assert view["nested"] is not invocation["nested"]
    assert invocation["nested"][0]["marker"] is marker
    assert view["nested"][0]["marker"] is marker


@pytest.mark.asyncio
@pytest.mark.parametrize("with_session", [False, True], ids=["standalone", "no-session-gate"])
async def test_no_gate_preserves_argument_identity(with_session, monkeypatch):
    seen = []
    payload = ["before"]
    arguments = {"payload": payload}

    def access(payload: list) -> str:
        """Mutate the same caller-owned list."""
        payload.append("after")
        return "done"

    branch = Branch()
    branch.register_tools([access])
    session = Session(default_branch=branch) if with_session else None
    manager_type = type(branch._action_manager)
    original_invoke = manager_type.invoke

    async def record_invoke(manager, request):
        seen.append((request["arguments"] is arguments, request["arguments"]["payload"] is payload))
        return await original_invoke(manager, request)

    monkeypatch.setattr(manager_type, "invoke", record_invoke)
    result = await _act(branch, {"function": "access", "arguments": arguments})
    await branch.drain_signals()

    assert seen == [(True, True)]
    assert payload == ["before", "after"]
    assert result.output == "done"
    assert session is None or session.observer._gate is None


@pytest.mark.asyncio
async def test_request_options_failure_uses_raw_gate_view_and_keeps_rejection():
    class Input(BaseModel):
        count: int

    calls, seen = [], []

    def access(count: int) -> int:
        """Record a successfully validated value."""
        calls.append(count)
        return count

    branch = Branch()
    branch.register_tools([Tool(func_callable=access, request_options=Input)])
    session = Session(default_branch=branch)

    def gate(value):
        if isinstance(value, ToolInvocation):
            seen.append(deepcopy(value.arguments))
        return True

    session.gate(gate)
    result = await _act(
        branch,
        {"function": "access", "arguments": {"count": "not-an-int"}},
        suppress_errors=True,
    )
    await branch.drain_signals()

    assert seen == [{"count": "not-an-int"}]
    assert calls == []
    assert "error" in result.output


@pytest.mark.asyncio
async def test_uninspectable_callable_uses_raw_gate_view_and_still_invokes(monkeypatch):
    calls, seen = [], []

    def access(path: str) -> str:
        """Record a path without external effects."""
        calls.append(path)
        return path

    branch = Branch()
    branch.register_tools([access])
    # Registration has already constructed the schema. The actual callable
    # continues to work even if introspection is not available at invocation.
    monkeypatch.setattr(access, "__signature__", object(), raising=False)
    session = Session(default_branch=branch)

    def gate(value):
        if isinstance(value, ToolInvocation):
            seen.append(deepcopy(value.arguments))
        return True

    session.gate(gate)
    result = await _act(branch, {"function": "access", "arguments": {"path": "/safe"}})
    await branch.drain_signals()

    assert seen == [{"path": "/safe"}]
    assert calls == ["/safe"]
    assert result.output == "/safe"


@pytest.mark.asyncio
async def test_signature_bind_failure_uses_raw_gate_view_and_keeps_rejection():
    calls, seen = [], []

    def access(path: str, required: int) -> str:
        """Record complete calls only."""
        calls.append((path, required))
        return path

    branch = Branch()
    branch.register_tools([access])
    session = Session(default_branch=branch)

    def gate(value):
        if isinstance(value, ToolInvocation):
            seen.append(deepcopy(value.arguments))
        return True

    session.gate(gate)
    result = await _act(
        branch, {"function": "access", "arguments": {"path": "/safe"}}, suppress_errors=True
    )
    await branch.drain_signals()

    assert seen == [{"path": "/safe"}]
    assert calls == []
    assert "error" in result.output


@pytest.mark.asyncio
async def test_unknown_tool_uses_isolated_raw_gate_view_and_still_fails():
    seen = []
    branch = Branch()
    session = Session(default_branch=branch)
    original = {"nested": {"path": "/safe"}}

    def gate(value):
        if isinstance(value, ToolInvocation):
            seen.append(deepcopy(value.arguments))
            value.arguments["nested"]["path"] = "/mutated"
        return True

    session.gate(gate)
    result = await _act(
        branch, {"function": "unknown_tool", "arguments": original}, suppress_errors=True
    )
    await branch.drain_signals()

    assert seen == [{"nested": {"path": "/safe"}}]
    assert original == {"nested": {"path": "/safe"}}
    assert "not registered" in result.output["error"]


@pytest.mark.asyncio
async def test_binding_does_not_replace_the_tool_default_object():
    sentinel = object()
    calls, seen = [], []

    def access(path: str, marker=sentinel) -> bool:
        """Observe identity of the default used by the Python callable."""
        calls.append(marker is sentinel)
        return marker is sentinel

    branch = Branch()
    branch.register_tools([access])
    session = Session(default_branch=branch)

    def gate(value):
        if isinstance(value, ToolInvocation):
            seen.append(value.arguments)
        return True

    session.gate(gate)
    result = await _act(branch, {"function": "access", "arguments": {"path": "/safe"}})
    await branch.drain_signals()

    assert len(seen) == 1 and "marker" in seen[0]
    assert calls == [True]
    assert result.output is True


@pytest.mark.asyncio
async def test_gate_cannot_mutate_nested_arguments_used_by_tool():
    calls, seen = [], []

    def access(payload: dict) -> str:
        """Record the argument actually passed to the tool."""
        calls.append(deepcopy(payload))
        return payload["path"]

    branch = Branch()
    branch.register_tools([access])
    session = Session(default_branch=branch)

    def gate(value):
        if isinstance(value, ToolInvocation):
            seen.append(deepcopy(value.arguments))
            value.arguments["payload"]["path"] = "/restricted"
        return True

    session.gate(gate)
    original = {"payload": {"path": "/safe"}}
    result = await _act(branch, {"function": "access", "arguments": original})
    await branch.drain_signals()

    assert seen == [{"payload": {"path": "/safe"}}]
    assert original == {"payload": {"path": "/safe"}}
    assert calls == [{"path": "/safe"}]
    assert result.output == "/safe"


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
async def test_gate_mutating_caller_arguments_cannot_change_invocation(is_async):
    calls, seen, mutations = [], [], []
    original = {"payload": {"path": "/safe"}}

    def access(payload: dict) -> str:
        """Record the nested argument passed to the in-memory tool."""
        calls.append(deepcopy(payload))
        return payload["path"]

    branch = Branch()
    branch.register_tools([access])
    session = Session(default_branch=branch)

    def gate(value):
        if isinstance(value, ToolInvocation):
            seen.append(deepcopy(value.arguments))
            original["payload"]["path"] = "/restricted"
            mutations.append(True)
        return True

    async def async_gate(value):
        return gate(value)

    session.gate(async_gate if is_async else gate)
    result = await _act(branch, {"function": "access", "arguments": original})
    await branch.drain_signals()

    assert seen == [{"payload": {"path": "/safe"}}]
    assert mutations == [True]
    assert original == {"payload": {"path": "/restricted"}}
    assert calls == [{"path": "/safe"}]
    assert result.output == "/safe"


@pytest.mark.asyncio
async def test_tool_pre_mutation_of_caller_arguments_cannot_change_invocation():
    calls, seen, mutations = [], [], []
    original = {"payload": {"path": "/safe"}}

    def access(payload: dict) -> str:
        """Record the argument passed to this in-memory tool."""
        calls.append(deepcopy(payload))
        return payload["path"]

    branch = Branch()
    branch.register_tools([access])
    session = Session(default_branch=branch)

    def gate(value):
        if isinstance(value, ToolInvocation):
            seen.append(deepcopy(value.arguments))
        return True

    session.gate(gate)
    bus = HookBus()

    def mutate_caller(**_kwargs):
        original["payload"]["path"] = "/restricted"
        mutations.append(True)

    bus.on(HookPoint.TOOL_PRE, mutate_caller)
    branch._hooks = bus
    result = await _act(branch, {"function": "access", "arguments": original})
    await branch.drain_signals()

    assert seen == [{"payload": {"path": "/safe"}}]
    assert mutations == [True]
    assert original == {"payload": {"path": "/restricted"}}
    assert calls == [{"path": "/safe"}]
    assert result.output == "/safe"


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
async def test_subscriber_cannot_mutate_arguments_after_gate_allows(is_async):
    calls, actions, authorized, mutations = [], [], [], []

    def access(payload: dict, mode: str = "read") -> str:
        """Record a nested argument and the callable default."""
        calls.append((deepcopy(payload), mode))
        return payload["path"]

    def gate(value):
        if isinstance(value, ToolInvocation):
            actions.append(value)
            authorized.append(deepcopy(value.arguments))
        return True

    async def async_gate(value):
        return gate(value)

    branch = Branch()
    branch.register_tools([access])
    session = Session(default_branch=branch)
    session.gate(async_gate if is_async else gate)

    def mutate(event, _context):
        if event.data == "mutate":
            actions[0].arguments["payload"]["path"] = "/restricted"
            mutations.append(True)

    session.observe(Signal, handler=mutate)
    bus = HookBus()

    async def after_allow(**_kwargs):
        # TOOL_PRE runs after authorize returns; explicitly emit a signal here
        # so the subscriber attempts its mutation before the tool is invoked.
        await session.emit(Signal(data="mutate"))

    bus.on(HookPoint.TOOL_PRE, after_allow)
    branch._hooks = bus
    result = await _act(branch, {"function": "access", "arguments": {"payload": {"path": "/safe"}}})
    await branch.drain_signals()

    assert authorized == [{"payload": {"path": "/safe"}, "mode": "read"}]
    assert mutations == [True]
    assert actions[0].arguments["payload"]["path"] == "/restricted"
    assert calls == [({"path": "/safe"}, "read")]
    assert result.output == "/safe"
