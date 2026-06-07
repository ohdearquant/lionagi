# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Test stream handlers functionality and integration."""

import pytest

from lionagi.protocols.types import EventStatus
from lionagi.service.hooks.hook_registry import HookRegistry
from tests.service.hooks.conftest import MyCancelled


@pytest.fixture(autouse=True)
def patch_cancel(monkeypatch):
    """Auto-patch cancellation class for all tests in this module."""
    from lionagi.service.hooks import hook_registry

    monkeypatch.setattr(hook_registry, "get_cancelled_exc_class", lambda: MyCancelled)


class TestStreamHandlerBasics:
    """Test basic stream handler functionality."""

    @pytest.mark.anyio
    async def test_stream_handler_called_with_correct_args(self):
        """Test that stream handlers are called with the correct arguments."""
        captured = {}

        async def handler(ev, chunk_type, chunk, **kw):
            captured["ev"] = ev
            captured["chunk_type"] = chunk_type
            captured["chunk"] = chunk
            captured["kw"] = kw
            return "handled"

        registry = HookRegistry(stream_handlers={"text": handler})

        res, se, st = await registry.handle_streaming_chunk(
            "text", "chunk data", exit=False, custom_param="test"
        )

        assert captured["ev"] is None  # event is None for streaming
        assert captured["chunk_type"] == "text"
        assert captured["chunk"] == "chunk data"
        assert captured["kw"]["exit"] is False
        assert captured["kw"]["custom_param"] == "test"
        assert res == "handled"
        assert se is False
        assert st is None

    @pytest.mark.anyio
    async def test_stream_handler_via_call_method(self):
        """Test stream handlers work through the main call() method."""

        async def handler(ev, chunk_type, chunk, **kw):
            return f"processed {chunk}"

        registry = HookRegistry(stream_handlers={"data": handler})

        # Call via main call() method
        result = await registry.call(
            None,  # event_like not used for streaming
            chunk_type="data",
            chunk="test_chunk",
            exit=True,
        )

        res, se, st = result  # No meta for streaming
        assert res == "processed test_chunk"
        assert se is False
        assert st is None

    @pytest.mark.anyio
    async def test_multiple_stream_handlers(self):
        """Test that multiple stream handlers can be registered."""
        handlers_called = []

        async def text_handler(ev, chunk_type, chunk, **kw):
            handlers_called.append("text")
            return f"text: {chunk}"

        async def data_handler(ev, chunk_type, chunk, **kw):
            handlers_called.append("data")
            return f"data: {chunk}"

        registry = HookRegistry(
            stream_handlers={
                "text": text_handler,
                "data": data_handler,
            }
        )

        # Call text handler
        res, _, _ = await registry.handle_streaming_chunk("text", "hello", exit=False)
        assert res == "text: hello"

        # Call data handler
        res, _, _ = await registry.handle_streaming_chunk("data", "binary", exit=False)
        assert res == "data: binary"

        assert handlers_called == ["text", "data"]

    @pytest.mark.anyio
    async def test_stream_handler_with_type_keys(self):
        """Test that stream handlers work with type keys, not just strings."""

        async def int_handler(ev, chunk_type, chunk, **kw):
            return f"int handler: {chunk}"

        async def str_handler(ev, chunk_type, chunk, **kw):
            return f"str handler: {chunk}"

        registry = HookRegistry(
            stream_handlers={
                int: int_handler,
                str: str_handler,
            }
        )

        # Test with type keys
        res, _, _ = await registry.handle_streaming_chunk(int, 42, exit=False)
        assert res == "int handler: 42"

        res, _, _ = await registry.handle_streaming_chunk(str, "hello", exit=False)
        assert res == "str handler: hello"


class TestStreamHandlerErrors:
    """Test error handling in stream handlers."""

    @pytest.mark.anyio
    async def test_missing_stream_handler_returns_error(self):
        """Missing stream handler raises RuntimeError (not ValidationError)."""
        registry = HookRegistry()

        res, se, st = await registry.handle_streaming_chunk("missing", "data", exit=False)

        assert isinstance(res, RuntimeError)
        assert "No stream handler registered for missing" in str(res)
        assert se is False
        assert st == EventStatus.ABORTED

    @pytest.mark.anyio
    async def test_stream_handler_cancellation(self):
        """Test cancellation in stream handlers."""

        async def cancelling_handler(ev, chunk_type, chunk, **kw):
            raise MyCancelled("stream cancelled")

        registry = HookRegistry(stream_handlers={"test": cancelling_handler})

        res, se, st = await registry.handle_streaming_chunk("test", "data", exit=False)

        assert se is True
        assert st == EventStatus.CANCELLED
        assert isinstance(res, tuple) and len(res) == 2

    @pytest.mark.anyio
    async def test_stream_handler_other_exception(self):
        """Test non-cancellation exceptions in stream handlers."""

        async def failing_handler(ev, chunk_type, chunk, **kw):
            raise RuntimeError("stream failed")

        registry = HookRegistry(stream_handlers={"test": failing_handler})

        # Test with exit=False
        res, se, st = await registry.handle_streaming_chunk("test", "data", exit=False)
        assert se is False
        assert st == EventStatus.ABORTED
        assert isinstance(res, RuntimeError)

        # Test with exit=True
        res, se, st = await registry.handle_streaming_chunk("test", "data", exit=True)
        assert se is True
        assert st == EventStatus.ABORTED
        assert isinstance(res, RuntimeError)


class TestStreamHandlerIntegration:
    """Test stream handler integration with sync/async wrapping."""

    @pytest.mark.anyio
    async def test_sync_stream_handler_wrapped(self):
        """Test that sync stream handlers are properly wrapped."""

        def sync_handler(ev, chunk_type, chunk, **kw):
            return f"sync: {chunk_type}:{chunk}"

        registry = HookRegistry(stream_handlers={"sync": sync_handler})

        res, se, st = await registry.handle_streaming_chunk("sync", "test_data", exit=False)

        assert res == "sync: sync:test_data"
        assert se is False
        assert st is None

    @pytest.mark.anyio
    async def test_mixed_sync_async_stream_handlers(self):
        """Test mixing sync and async stream handlers."""

        def sync_handler(ev, chunk_type, chunk, **kw):
            return f"sync: {chunk}"

        async def async_handler(ev, chunk_type, chunk, **kw):
            return f"async: {chunk}"

        registry = HookRegistry(
            stream_handlers={
                "sync": sync_handler,
                "async": async_handler,
            }
        )

        # Test sync handler
        res, _, _ = await registry.handle_streaming_chunk("sync", "data1", exit=False)
        assert res == "sync: data1"

        # Test async handler
        res, _, _ = await registry.handle_streaming_chunk("async", "data2", exit=False)
        assert res == "async: data2"

    @pytest.mark.anyio
    async def test_stream_handler_call_via_internal_method(self):
        """Test calling stream handlers via _call_stream_handler."""
        captured = {}

        async def handler(ev, chunk_type, chunk, **kw):
            captured.update(kw)
            return "internal_call"

        registry = HookRegistry(stream_handlers={"test": handler})

        result = await registry._call_stream_handler(
            "test",
            "chunk_data",
            None,
            custom_param="value",  # ev
        )

        assert result == "internal_call"
        assert captured["custom_param"] == "value"
