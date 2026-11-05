# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A failed call raises `ExecutionError` with the provider's own error as its cause."""

from __future__ import annotations

from types import MethodType

import pytest

from lionagi._errors import ExecutionError
from lionagi._provider_errors import ProviderAuthError
from lionagi.operations.chat.chat import chat
from lionagi.operations.types import ChatParam
from lionagi.protocols.generic import EventStatus
from lionagi.service.imodel import iModel
from lionagi.session.branch import Branch


def _branch_whose_call_fails_with(error) -> Branch:
    model = iModel(provider="claude_code", model="sonnet", api_key="test-key")

    async def invoke(self, api_call=None, **kwargs):
        call = api_call or await self.create_event(**kwargs)
        call.execution.status = EventStatus.FAILED
        call.execution.error = error
        return call

    model.invoke = MethodType(invoke, model)
    return Branch(chat_model=model)


@pytest.mark.asyncio
async def test_a_failed_call_raises_with_the_provider_error_as_its_cause():
    refused = ProviderAuthError("credentials rejected", stderr_tail="401")
    branch = _branch_whose_call_fails_with(refused)

    with pytest.raises(ExecutionError) as info:
        await chat(branch, "go on", ChatParam.from_branch(branch))

    assert info.value.__cause__ is refused
    assert "credentials rejected" in str(info.value)


@pytest.mark.asyncio
async def test_a_failure_recorded_as_text_raises_with_no_cause():
    branch = _branch_whose_call_fails_with("the provider said no")

    with pytest.raises(ExecutionError) as info:
        await chat(branch, "go on", ChatParam.from_branch(branch))

    assert info.value.__cause__ is None
    assert str(info.value) == "API call failed: the provider said no"


@pytest.mark.asyncio
async def test_a_failure_recorded_as_text_keeps_the_exception_the_caller_is_handling():
    branch = _branch_whose_call_fails_with("the provider said no")
    handled = LookupError("the caller's own error")

    with pytest.raises(ExecutionError) as info:
        try:
            raise handled
        except LookupError:
            await chat(branch, "go on", ChatParam.from_branch(branch))

    assert info.value.__cause__ is None
    assert info.value.__context__ is handled
    assert info.value.__suppress_context__ is False
