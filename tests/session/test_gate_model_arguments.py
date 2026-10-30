# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The session gate sees the model arguments that the tool will receive."""

import pytest
from pydantic import BaseModel

from lionagi.operations.act.act import _act
from lionagi.session.branch import Branch
from lionagi.session.control import ToolInvocation
from lionagi.session.session import Session
from lionagi.session.signal import GateDenied


class PathArguments(BaseModel):
    path: str
    mode: str = "write"


class ModelRequest(BaseModel):
    function: str
    arguments: PathArguments


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("model_request", [False, True], ids=["dict-request", "model-request"])
@pytest.mark.parametrize("permitted", [False, True], ids=["denied", "allowed"])
async def test_gate_authorizes_actual_model_arguments(
    is_async, model_request, permitted, monkeypatch
):
    calls = []
    seen = []
    manager_arguments = []
    path = "/safe/report" if permitted else "/restricted/secret"

    def read_path(path: str, mode: str = "read") -> str:
        """Record a requested path."""
        calls.append((path, mode))
        return path

    def gate(payload):
        if not isinstance(payload, ToolInvocation):
            return True
        seen.append(payload)
        return payload.arguments.get("path", "").startswith("/safe/")

    async def async_gate(payload):
        return gate(payload)

    branch = Branch()
    branch.register_tools([read_path])
    manager_type = type(branch._action_manager)
    original_invoke = manager_type.invoke

    async def record_invoke(manager, request):
        manager_arguments.append(request["arguments"])
        return await original_invoke(manager, request)

    monkeypatch.setattr(manager_type, "invoke", record_invoke)
    session = Session(default_branch=branch)
    session.gate(async_gate if is_async else gate)
    args = PathArguments(path=path)
    request = (
        ModelRequest(function="read_path", arguments=args)
        if model_request
        else {"function": "read_path", "arguments": args}
    )

    result = await _act(branch, request)
    await branch.drain_signals()

    assert len(seen) == 1
    assert seen[0].arguments == {"path": path, "mode": "read"}
    assert manager_arguments == ([{"path": path}] if permitted else [])
    if permitted:
        assert manager_arguments[0] is not seen[0].arguments
    assert calls == ([(path, "read")] if permitted else [])
    assert result.output == (
        path if permitted else {"error": "denied by governance gate", "function": "read_path"}
    )
    denials = session.observer.by_type(GateDenied)
    assert len(denials) == (0 if permitted else 1)
    if denials:
        assert denials[0].data is None


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("model_request", [False, True], ids=["dict-request", "model-request"])
async def test_nested_model_dump_cannot_bypass_restricted_path_gate(is_async, model_request):
    class OuterArguments(BaseModel):
        def model_dump(self, *args, **kwargs):
            return PathArguments(path="/restricted/secret")

    class OuterRequest(BaseModel):
        function: str
        arguments: OuterArguments

    tool_calls, gate_calls = [], []

    def read_path(path: str, mode: str = "read") -> str:
        """Record the exact arguments reaching the tool."""
        tool_calls.append((path, mode))
        return path

    def gate(payload):
        if not isinstance(payload, ToolInvocation):
            return True
        gate_calls.append(payload)
        # A policy that rejects this particular restricted path can appear to
        # allow an empty view even though the tool later receives the path.
        return payload.arguments.get("path") != "/restricted/secret"

    async def async_gate(payload):
        return gate(payload)

    branch = Branch()
    branch.register_tools([read_path])
    session = Session(default_branch=branch)
    session.gate(async_gate if is_async else gate)
    arguments = OuterArguments()
    request = (
        OuterRequest(function="read_path", arguments=arguments)
        if model_request
        else {"function": "read_path", "arguments": arguments}
    )

    try:
        with pytest.raises(ValueError, match="could not normalize action arguments"):
            await _act(branch, request)
    finally:
        await branch.drain_signals()

    assert tool_calls == []
    assert gate_calls == []
    assert session.observer.by_type(GateDenied) == []
