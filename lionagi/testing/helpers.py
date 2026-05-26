# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Reusable async-test helpers, validators, and data builders.

Originally lived at ``tests/utils/helpers.py``; promoted to ``lionagi.testing``
so downstream projects building on lionagi can use the same patterns without
copying.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from typing import Any, TypeVar

from pydantic import Field as _Field

from lionagi.protocols.generic.element import UUID, Element
from lionagi.protocols.generic.event import EventStatus
from lionagi.protocols.graph.node import Node

T = TypeVar("T")


class AsyncTestHelpers:
    """Helpers for the most common async-testing shapes."""

    @staticmethod
    async def assert_eventually(
        condition: Callable[[], bool],
        timeout: float = 5.0,
        interval: float = 0.1,
        error_message: str | None = None,
    ) -> None:
        start_time = time.time()
        while time.time() - start_time < timeout:
            if condition():
                return
            await asyncio.sleep(interval)
        raise AssertionError(error_message or f"Condition not met within {timeout} seconds")

    @staticmethod
    async def collect_async_results(
        async_gen: AsyncGenerator[T, None],
        limit: int = 100,
        timeout: float = 10.0,
    ) -> list[T]:
        results: list[T] = []
        try:
            async with asyncio.timeout(timeout):
                async for item in async_gen:
                    results.append(item)
                    if len(results) >= limit:
                        break
        except asyncio.TimeoutError:
            pass
        return results

    @staticmethod
    async def run_with_timeout(
        coro: Callable[..., Any], timeout: float = 5.0, *args: Any, **kwargs: Any
    ) -> Any:
        async with asyncio.timeout(timeout):
            return await coro(*args, **kwargs)

    @staticmethod
    async def wait_for_all(tasks: list[asyncio.Task], timeout: float = 10.0) -> list[Any]:
        try:
            async with asyncio.timeout(timeout):
                return await asyncio.gather(*tasks)
        except asyncio.TimeoutError:
            for task in tasks:
                if not task.done():
                    task.cancel()
            raise

    @staticmethod
    def assert_async_context_cleanup(func: Callable) -> Callable:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            initial_tasks = len(asyncio.all_tasks())
            try:
                result = await func(*args, **kwargs)
                await asyncio.sleep(0.01)
                final_tasks = len(asyncio.all_tasks())
                if final_tasks > initial_tasks:
                    remaining = [t for t in asyncio.all_tasks() if not t.done()]
                    print(f"Warning: {len(remaining)} tasks not cleaned up: {remaining}")
                return result
            except Exception:
                for task in asyncio.all_tasks():
                    if not task.done():
                        task.cancel()
                raise

        return wrapper


class ValidationHelpers:
    """Structural assertions for lionagi protocol shapes."""

    @staticmethod
    def assert_valid_node(
        node: Any,
        expected_type: type | None = None,
        check_id: bool = True,
        check_timestamp: bool = True,
    ) -> None:
        if expected_type:
            assert isinstance(node, expected_type), f"Expected {expected_type}, got {type(node)}"
        if not isinstance(node, Node):
            if check_id:
                assert hasattr(node, "id"), "Object missing 'id' field"
        else:
            assert isinstance(node, Node), f"Expected Node subclass, got {type(node)}"
        if check_id:
            assert hasattr(node, "id"), "Object missing 'id' field"
            assert node.id is not None, "Object 'id' field is None"
            assert isinstance(node.id, str | UUID), (
                f"Object 'id' should be string or UUID, got {type(node.id)}"
            )
        if check_timestamp and hasattr(node, "timestamp"):
            assert node.timestamp is not None, "Object 'timestamp' field is None"

    @staticmethod
    def assert_api_response_structure(
        response: Any,
        required_fields: list[str] | None = None,
        check_status: bool = True,
    ) -> None:
        if hasattr(response, "execution"):
            execution = response.execution
            if check_status:
                assert hasattr(execution, "status"), "Response missing execution.status"
                assert isinstance(execution.status, EventStatus), (
                    f"Invalid status type: {type(execution.status)}"
                )
            assert hasattr(execution, "response"), "Response missing execution.response"
        if required_fields:
            for field in required_fields:
                assert hasattr(response, field), f"Response missing required field: {field}"

    @staticmethod
    def assert_pydantic_model_valid(
        model_instance: Any, expected_fields: dict[str, Any] | None = None
    ) -> None:
        assert hasattr(model_instance, "model_dump"), "Not a Pydantic model"
        assert hasattr(model_instance, "model_validate"), "Not a Pydantic model"
        dumped = model_instance.model_dump()
        assert isinstance(dumped, dict), "Model dump should return dict"
        if expected_fields:
            for field_name, expected_value in expected_fields.items():
                actual_value = getattr(model_instance, field_name)
                assert actual_value == expected_value, (
                    f"Field {field_name}: expected {expected_value}, got {actual_value}"
                )

    @staticmethod
    def assert_error_handling(
        error_response: Any,
        expected_error_type: str | None = None,
        expected_message_contains: str | None = None,
    ) -> None:
        if hasattr(error_response, "execution"):
            assert error_response.execution.status == EventStatus.FAILED, (
                "Error response should have FAILED status"
            )
        if hasattr(error_response, "error"):
            error_data = error_response.error
            if expected_error_type:
                assert "type" in error_data or "code" in error_data, (
                    "Error response missing type/code"
                )
                error_type = error_data.get("type") or error_data.get("code")
                assert expected_error_type in str(error_type), (
                    f"Expected error type '{expected_error_type}', got '{error_type}'"
                )
            if expected_message_contains:
                message = error_data.get("message", "")
                assert expected_message_contains in message, (
                    f"Expected '{expected_message_contains}' in error message: '{message}'"
                )


class TestDataHelpers:
    """Data builders for hand-rolled payloads in tests."""

    @staticmethod
    def create_test_messages(
        count: int = 3,
        message_type: str = "user",
        base_content: str = "Test message",
    ) -> list[dict[str, Any]]:
        return [
            {
                "role": message_type,
                "content": f"{base_content} {i + 1}",
                "timestamp": time.time() + i,
            }
            for i in range(count)
        ]

    @staticmethod
    def create_test_payload(
        model: str = "gpt-4o-mini",
        messages: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if messages is None:
            messages = TestDataHelpers.create_test_messages()
        return {"model": model, "messages": messages, **kwargs}


# ─────────────────────────── canonical lightweight mocks ─────────────────


class IModelKwargCaptor:
    """Captor: replaces ``iModel`` to record constructor kwargs without instantiating.

    The 3+ places that ad-hoc'd ``class FakeIModel`` with ``captures.append(kwargs)``
    can use this directly. Usage::

        import lionagi.cli._providers as pmod
        from lionagi.testing import IModelKwargCaptor

        captor = IModelKwargCaptor.fresh()  # resets the captures list
        monkeypatch.setattr(pmod, "iModel", captor)
        build_imodel_from_spec("codex/gpt-5.5", fast=True)
        assert captor.captures[0]["fast_mode"] is True

    The class itself is the captor — ``monkeypatch.setattr(module, "iModel",
    IModelKwargCaptor)`` works because Python calls the class on each construction,
    which appends to the class-level ``captures``. Use ``.fresh()`` to get a
    pristine subclass with its own ``captures`` list so multiple tests don't
    interfere.
    """

    captures: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        type(self).captures.append(kwargs)

    @classmethod
    def fresh(cls) -> type[IModelKwargCaptor]:
        """Return a subclass with its own ``captures`` list."""

        class _Local(cls):
            captures: list[dict[str, Any]] = []

        return _Local


# Module-level ``MockElement`` — defined once so it's picklable. The
# duplicated ``class MockElement(Element): value: Any`` snippets in
# tests/protocols/generic/* should import this instead.
class MockElement(Element):
    """Minimal ``Element`` subclass for Pile/Progression tests."""

    value: Any = _Field(None)


def make_mock_element_class() -> type[MockElement]:
    """Back-compat shim — returns the module-level ``MockElement``."""
    return MockElement


class MockClaudeCode:
    """Mock Claude Code model — a callable returning a dict response.

    Mirrors the duplicated ``MockClaudeCode`` from the flow-pattern tests.
    Returns different shapes based on the last user message:

    - "generate tasks ..." → ``{"content": ..., "instruct_model": [...]}``
    - "research ..."       → ``{"content": ..., "findings": [...]}``
    - otherwise            → ``{"content": "Processed: ..."}``

    Subclass to customize the response logic if a test needs different shapes.
    """

    def __init__(self, name: str = "mock") -> None:
        self.name = name
        self.call_count = 0

    async def __call__(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        self.call_count += 1
        last_msg = messages[-1]["content"] if messages else ""
        text = str(last_msg).lower()
        if "generate tasks" in text:
            return {
                "content": "I'll generate 3 research tasks",
                "instruct_model": [
                    {"instruction": "Research A", "context": "ctx_a"},
                    {"instruction": "Research B", "context": "ctx_b"},
                    {"instruction": "Research C", "context": "ctx_c"},
                ],
            }
        if "research" in text:
            return {
                "content": f"Research complete for: {last_msg}",
                "findings": ["finding1", "finding2"],
            }
        return {"content": f"Processed: {last_msg}"}


__all__ = (
    "AsyncTestHelpers",
    "IModelKwargCaptor",
    "MockClaudeCode",
    "MockElement",
    "TestDataHelpers",
    "ValidationHelpers",
    "make_mock_element_class",
)
