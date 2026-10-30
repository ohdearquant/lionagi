# Copyright (c) 2023-2026, LionAGI contributors
# SPDX-License-Identifier: Apache-2.0

"""The first ReAct request carries only model inputs."""

from __future__ import annotations

from importlib import import_module
from inspect import signature

import pytest

from lionagi import Branch
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.imodel import iModel
from lionagi.testing import TestBranch as ScriptedBranch

from ._tool_trace import tool_branch


@pytest.mark.asyncio
@pytest.mark.parametrize("verbose", [False, True])
async def test_react_first_request_has_no_internal_settings(monkeypatch, verbose):
    endpoint = Endpoint(
        EndpointConfig(
            name="generic",
            provider="generic",
            endpoint="chat",
            auth_type="none",
            kwargs={"model": "sample-model"},
        )
    )
    assert endpoint.request_options is None
    branch = Branch(chat_model=iModel(endpoint=endpoint))
    captured = []

    class Captured(BaseException):
        pass

    async def capture(self, **request):
        request.pop("include_token_usage_to_model", None)
        payload, _ = self.endpoint.create_payload(request)
        captured.append(payload)
        raise Captured()

    monkeypatch.setattr(iModel, "invoke", capture)
    with pytest.raises(Captured):
        await branch.ReAct(instruction="hello", max_extensions=0, verbose=verbose)

    assert len(captured) == 1
    assert set(captured[0]) == {"model", "messages"}
    assert captured[0]["messages"]


@pytest.mark.asyncio
@pytest.mark.parametrize("verbose", [False, True])
async def test_react_still_runs_registered_tools_without_action_display(monkeypatch, verbose):
    seen = []
    display_flags = []

    def add_one(value: int) -> int:
        """Add one to a number."""
        seen.append(value)
        return value + 1

    add_one.__annotations__ = {"value": int, "return": int}
    request = {"function": "add_one", "arguments": {"value": 2}}
    branch = tool_branch([{"action_requests": [request]}], [add_one], "done")
    assert branch.chat_model.endpoint.request_options is None
    act_module = import_module("lionagi.operations.act.act")
    original = act_module._act

    async def observe_action(*args, **kwargs):
        bound = signature(original).bind(*args, **kwargs)
        bound.apply_defaults()
        display_flags.append(list(bound.arguments.values())[-1])
        return await original(*args, **kwargs)

    monkeypatch.setattr(act_module, "_act", observe_action)
    result = await branch.ReAct(
        instruction="add one", tools=True, max_extensions=0, verbose=verbose
    )

    assert result == "done"
    assert seen == [2]
    assert display_flags == [False]
    assert set(ScriptedBranch.calls(branch)[0].payload) == {"model", "messages", "stream"}
