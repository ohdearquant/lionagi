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
    mode: str = "read"


class ModelRequest(BaseModel):
    function: str
    arguments: PathArguments


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("model_request", [False, True], ids=["dict-request", "model-request"])
@pytest.mark.parametrize("permitted", [False, True], ids=["denied", "allowed"])
async def test_gate_authorizes_actual_model_arguments(is_async, model_request, permitted):
    calls = []
    seen = []
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
    assert seen[0].arguments == {"path": path}
    assert calls == ([(path, "read")] if permitted else [])
    assert result.output == (
        path if permitted else {"error": "denied by governance gate", "function": "read_path"}
    )
    denials = session.observer.by_type(GateDenied)
    assert len(denials) == (0 if permitted else 1)
    if denials:
        assert denials[0].data is seen[0]
