# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the legacy ``LionAGIMockFactory`` API at its new home.

These mirror what ``tests/fixtures/test_mock_factory.py`` used to do — kept
here so any behavior change in the legacy factory is caught locally rather
than only via docs tests.
"""

from __future__ import annotations

import pytest

from lionagi.testing import (
    AsyncTestHelpers,
    LionAGIMockFactory,
    ValidationHelpers,
    load_test_data,
)


def test_create_mocked_branch():
    branch = LionAGIMockFactory.create_mocked_branch(
        name="SimpleTestBranch", response="Simple test response"
    )
    assert branch is not None
    assert branch.name == "SimpleTestBranch"
    assert branch.chat_model is not None


def test_create_mocked_imodel():
    imodel = LionAGIMockFactory.create_mocked_imodel(
        provider="openai", model="gpt-4o-mini", response="Test response"
    )
    assert imodel is not None
    assert hasattr(imodel, "invoke")


@pytest.mark.asyncio
async def test_async_branch_communication():
    branch = LionAGIMockFactory.create_mocked_branch(response="Async communication test")
    result = await branch.communicate("Test message", skip_validation=True)
    assert result == "Async communication test"


def test_test_data_loading():
    conversations = load_test_data("sample_conversations")
    assert "basic_chat" in conversations
    assert "messages" in conversations["basic_chat"]

    api_responses = load_test_data("api_responses")
    assert "successful_chat_response" in api_responses

    error_scenarios = load_test_data("error_scenarios")
    assert "api_rate_limit_error" in error_scenarios


@pytest.mark.asyncio
async def test_async_helpers_assert_eventually():
    import asyncio

    condition_met = False

    def check_condition():
        return condition_met

    async def set_condition():
        await asyncio.sleep(0.05)
        nonlocal condition_met
        condition_met = True

    task = asyncio.create_task(set_condition())
    await AsyncTestHelpers.assert_eventually(check_condition, timeout=5.0, interval=0.01)
    await task


def test_validation_helpers_node():
    branch = LionAGIMockFactory.create_mocked_branch()
    ValidationHelpers.assert_valid_node(branch)


def test_error_response_mock():
    api_call = LionAGIMockFactory.create_error_response_mock(
        error_message="Test error", error_code="test_code"
    )
    assert api_call.execution.response["error"]["message"] == "Test error"
    assert api_call.execution.response["error"]["code"] == "test_code"
