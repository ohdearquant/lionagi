# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""pytest fixtures bundled with ``lionagi.testing`` (see docs/reference/testing-state-session.md)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from ._branch import TestBranch
from ._endpoint import ScriptedEndpoint
from ._legacy import LionAGIMockFactory
from .helpers import AsyncTestHelpers, TestDataHelpers, ValidationHelpers
from .loaders import TestDataLoader

# ─────────────────────────── helper-class fixtures ────────────────────────


@pytest.fixture
def mock_factory() -> type[LionAGIMockFactory]:
    """Legacy mock factory class — for tests that already use it."""
    return LionAGIMockFactory


@pytest.fixture
def async_helpers() -> type[AsyncTestHelpers]:
    return AsyncTestHelpers


@pytest.fixture
def validation_helpers() -> type[ValidationHelpers]:
    return ValidationHelpers


@pytest.fixture
def test_data_helpers() -> type[TestDataHelpers]:
    return TestDataHelpers


@pytest.fixture
def test_data_loader() -> TestDataLoader:
    return TestDataLoader()


# ─────────────────────────── data fixtures ────────────────────────────────


@pytest.fixture
def sample_conversation_data(test_data_loader: TestDataLoader) -> dict[str, Any]:
    return test_data_loader.get_conversation_data("basic_chat")


@pytest.fixture
def sample_api_responses(test_data_loader: TestDataLoader) -> dict[str, Any]:
    return test_data_loader.load_json("api_responses")


@pytest.fixture
def sample_error_scenarios(test_data_loader: TestDataLoader) -> dict[str, Any]:
    return test_data_loader.load_json("error_scenarios")


# ─────────────────────────── legacy branches ──────────────────────────────


@pytest.fixture
def mocked_branch(mock_factory):
    """Branch with an AsyncMock-backed iModel. Returns a plain text response."""
    return mock_factory.create_mocked_branch()


@pytest.fixture
def make_mocked_branch(mock_factory) -> Callable[..., Any]:
    """Canonical factory fixture: replaces every per-file ``make_mocked_branch_for_*``.

    Returns a callable accepting any kwargs the underlying ``LionAGIMockFactory``
    accepts — ``response`` (str or dict), ``responses`` (sequence), ``system``,
    ``tools``, ``name``, ``user``, ``model``, ``provider``.

    Example::

        async def test_operate(make_mocked_branch):
            branch = make_mocked_branch(
                response='{"foo": "bar"}',
                system="You are a helper.",
            )
            result = await branch.operate(instruction="...", response_format=MyModel)
    """

    def _create_branch(**kwargs: Any):
        return mock_factory.create_mocked_branch(**kwargs)

    return _create_branch


@pytest.fixture
def mocked_error_branch(mock_factory):
    error_response = mock_factory.create_error_response_mock(
        error_message="Test API Error", error_code="test_error"
    )
    return mock_factory.create_mocked_branch(response=error_response)


@pytest.fixture
def test_messages(test_data_helpers):
    return test_data_helpers.create_test_messages()


@pytest.fixture
def test_payload(test_data_helpers):
    return test_data_helpers.create_test_payload()


# ─────────────────────────── scripted branches ────────────────────────────


@pytest.fixture
def scripted_branch_factory() -> Callable[..., Any]:
    """Factory: build a scripted branch from a list of response dicts.

    Usage::

        async def test_foo(scripted_branch_factory):
            branch = scripted_branch_factory([{"type": "text", "content": "hi"}])
            assert await branch.chat("hello") == "hi"
    """

    def _make(responses: list[dict[str, Any]], **kwargs: Any):
        return TestBranch.from_responses(responses, **kwargs)

    return _make


@pytest.fixture
def scripted_branch(scripted_branch_factory):
    """Default scripted branch with a single canned text response."""
    return scripted_branch_factory([{"type": "text", "content": "mocked doc response"}])


@pytest.fixture
def scripted_endpoint_for(scripted_branch_factory) -> Callable[..., ScriptedEndpoint]:
    """Factory: return the ScriptedEndpoint behind a branch built by this fixture.

    Useful when a test cares about call inspection more than the branch itself.
    """

    def _make(responses: list[dict[str, Any]], **kwargs: Any) -> ScriptedEndpoint:
        branch = scripted_branch_factory(responses, **kwargs)
        return TestBranch.scripted(branch)

    return _make


# ─────────────────────────── perf bookkeeping ─────────────────────────────


@pytest.fixture(scope="session")
def performance_benchmark():
    """Session-scoped benchmark recorder used by perf-tagged tests."""
    benchmarks: dict[str, dict[str, float | None]] = {}

    def record_benchmark(test_name: str, duration: float, memory_usage: float | None = None):
        benchmarks[test_name] = {"duration": duration, "memory_usage": memory_usage}

    def get_benchmarks():
        return benchmarks.copy()

    class BenchmarkRecorder:
        def record(self, test_name: str, duration: float, memory_usage: float | None = None):
            record_benchmark(test_name, duration, memory_usage)

        def get_all(self):
            return get_benchmarks()

    return BenchmarkRecorder()


__all__ = ()  # nothing to export — pytest auto-collects fixtures
