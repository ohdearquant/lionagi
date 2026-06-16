# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for Event lifecycle wrapper (invoke/stream template method pattern)."""

from __future__ import annotations

import asyncio

import pytest

from lionagi.ln.types import Unset
from lionagi.protocols.generic.event import Event, EventStatus

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class SuccessEvent(Event):
    """Event subclass that succeeds via _invoke()."""

    async def _invoke(self):
        return "ok"


class FailingEvent(Event):
    """Event subclass that raises via _invoke()."""

    async def _invoke(self) -> None:
        raise ValueError("boom")


class SlowEvent(Event):
    """Event subclass with a measurable delay."""

    async def _invoke(self):
        await asyncio.sleep(0.05)
        return "done"


class DirectOverrideEvent(Event):
    """Subclass that overrides invoke() directly (backwards compat)."""

    async def invoke(self) -> None:
        self.execution.status = EventStatus.COMPLETED
        self.execution.response = "direct"


class StreamSuccessEvent(Event):
    """Event subclass that streams successfully via _stream()."""

    async def _stream(self):
        for chunk in ["a", "b", "c"]:
            yield chunk


class StreamFailEvent(Event):
    """Event subclass whose _stream() raises mid-iteration."""

    async def _stream(self):
        yield "first"
        raise RuntimeError("stream failed")


class StreamDirectOverrideEvent(Event):
    """Subclass that overrides stream() directly (backwards compat)."""

    async def stream(self):
        self.execution.status = EventStatus.COMPLETED
        self.execution.response = "direct-stream"
        yield "direct-chunk"


class StreamCancelledEvent(Event):
    """Event subclass whose _stream() blocks long enough to be cancelled."""

    async def _stream(self):
        yield "before"
        await asyncio.sleep(10)  # will be cancelled before completion
        yield "after"  # never reached


# ---------------------------------------------------------------------------
# invoke() lifecycle tests
# ---------------------------------------------------------------------------


class TestInvokeLifecycle:
    @pytest.mark.asyncio
    async def test_invoke_calls_inner_invoke(self):
        """_invoke() is called by invoke()."""
        event = SuccessEvent()
        await event.invoke()
        assert event.execution.response == "ok"

    @pytest.mark.asyncio
    async def test_status_pending_to_completed(self):
        """Status transitions PENDING -> PROCESSING -> COMPLETED on success."""
        event = SuccessEvent()
        assert event.execution.status == EventStatus.PENDING
        await event.invoke()
        assert event.execution.status == EventStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_status_pending_to_failed(self):
        """Status transitions PENDING -> PROCESSING -> FAILED on error."""
        event = FailingEvent()
        assert event.execution.status == EventStatus.PENDING
        await event.invoke()  # total: a business failure is captured, not raised
        assert event.execution.status == EventStatus.FAILED

    @pytest.mark.asyncio
    async def test_error_captured_on_failure(self):
        """Error is captured in execution.error via add_error()."""
        event = FailingEvent()
        await event.invoke()
        assert event.execution.error is not None
        assert isinstance(event.execution.error, ValueError)
        assert "boom" in str(event.execution.error)

    @pytest.mark.asyncio
    async def test_error_not_reraised(self):
        """A business failure is captured as state, NOT re-raised (total invoke)."""
        event = FailingEvent()
        await event.invoke()  # must not raise
        assert event.execution.status == EventStatus.FAILED
        assert isinstance(event.execution.error, ValueError)

    @pytest.mark.asyncio
    async def test_idempotency_completed(self):
        """Calling invoke() on a COMPLETED event is a no-op."""
        event = SuccessEvent()
        await event.invoke()
        assert event.execution.status == EventStatus.COMPLETED
        first_duration = event.execution.duration

        # Invoke again -- should be a no-op
        await event.invoke()
        assert event.execution.status == EventStatus.COMPLETED
        assert event.execution.duration == first_duration
        assert event.execution.response == "ok"

    @pytest.mark.asyncio
    async def test_idempotency_failed(self):
        """Calling invoke() on a FAILED event is a no-op."""
        event = FailingEvent()
        await event.invoke()
        assert event.execution.status == EventStatus.FAILED
        first_duration = event.execution.duration

        # Invoke again -- should be a no-op (no exception)
        await event.invoke()
        assert event.execution.status == EventStatus.FAILED
        assert event.execution.duration == first_duration

    @pytest.mark.asyncio
    async def test_duration_recorded(self):
        """Duration is recorded in execution.duration."""
        event = SlowEvent()
        await event.invoke()
        assert event.execution.duration is not None
        # Slept 50ms, so duration should be at least 0.04s (allow for timing jitter)
        assert event.execution.duration >= 0.04

    @pytest.mark.asyncio
    async def test_duration_recorded_on_failure(self):
        """Duration is recorded even when _invoke() fails."""
        event = FailingEvent()
        await event.invoke()
        assert event.execution.status == EventStatus.FAILED
        assert event.execution.duration is not None
        assert event.execution.duration >= 0

    @pytest.mark.asyncio
    async def test_backwards_compat_direct_override(self):
        """Subclass overriding invoke() directly bypasses lifecycle wrapper."""
        event = DirectOverrideEvent()
        await event.invoke()
        assert event.execution.status == EventStatus.COMPLETED
        assert event.execution.response == "direct"
        # No duration set because the direct override does not use the wrapper
        assert event.execution.duration is Unset

    @pytest.mark.asyncio
    async def test_base_event_invoke_captures(self):
        """Calling invoke() on bare Event captures NotImplementedError as FAILED."""
        event = Event()
        await event.invoke()
        assert event.execution.status == EventStatus.FAILED
        assert isinstance(event.execution.error, NotImplementedError)

    @pytest.mark.asyncio
    async def test_response_preserved_on_success(self):
        """Response set in _invoke() is preserved after lifecycle completes."""
        event = SuccessEvent()
        await event.invoke()
        assert event.response == "ok"
        assert event.execution.response == "ok"


# ---------------------------------------------------------------------------
# stream() lifecycle tests
# ---------------------------------------------------------------------------


class TestStreamLifecycle:
    @pytest.mark.asyncio
    async def test_stream_yields_chunks(self):
        """_stream() chunks are yielded by stream()."""
        event = StreamSuccessEvent()
        chunks = []
        async for chunk in event.stream():
            chunks.append(chunk)
        assert chunks == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_stream_status_completed(self):
        """Status transitions to COMPLETED after successful streaming."""
        event = StreamSuccessEvent()
        async for _ in event.stream():
            pass
        assert event.execution.status == EventStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_stream_status_failed_on_error(self):
        """Status transitions to FAILED when _stream() raises."""
        event = StreamFailEvent()
        chunks = []
        async for chunk in event.stream():  # total: failure captured, not raised
            chunks.append(chunk)
        assert event.execution.status == EventStatus.FAILED
        # First chunk was yielded before the error
        assert chunks == ["first"]

    @pytest.mark.asyncio
    async def test_stream_error_captured(self):
        """Error is captured in execution.error during streaming."""
        event = StreamFailEvent()
        async for _ in event.stream():
            pass
        assert event.execution.error is not None
        assert isinstance(event.execution.error, RuntimeError)

    @pytest.mark.asyncio
    async def test_stream_duration_recorded(self):
        """Duration is recorded after streaming completes."""
        event = StreamSuccessEvent()
        async for _ in event.stream():
            pass
        assert event.execution.duration is not None
        assert event.execution.duration >= 0

    @pytest.mark.asyncio
    async def test_stream_duration_recorded_on_failure(self):
        """Duration is recorded even when streaming fails."""
        event = StreamFailEvent()
        async for _ in event.stream():
            pass
        assert event.execution.status == EventStatus.FAILED
        assert event.execution.duration is not None
        assert event.execution.duration >= 0

    @pytest.mark.asyncio
    async def test_stream_idempotency_completed(self):
        """Calling stream() on a COMPLETED event yields nothing."""
        event = StreamSuccessEvent()
        async for _ in event.stream():
            pass
        assert event.execution.status == EventStatus.COMPLETED

        # Stream again -- should yield nothing
        chunks = []
        async for chunk in event.stream():
            chunks.append(chunk)
        assert chunks == []

    @pytest.mark.asyncio
    async def test_stream_idempotency_failed(self):
        """Calling stream() on a FAILED event yields nothing."""
        event = StreamFailEvent()
        async for _ in event.stream():
            pass
        assert event.execution.status == EventStatus.FAILED

        # Stream again -- should yield nothing (no exception)
        chunks = []
        async for chunk in event.stream():
            chunks.append(chunk)
        assert chunks == []

    @pytest.mark.asyncio
    async def test_stream_backwards_compat_direct_override(self):
        """Subclass overriding stream() directly bypasses lifecycle wrapper."""
        event = StreamDirectOverrideEvent()
        chunks = []
        async for chunk in event.stream():
            chunks.append(chunk)
        assert chunks == ["direct-chunk"]
        assert event.execution.status == EventStatus.COMPLETED
        assert event.execution.response == "direct-stream"

    @pytest.mark.asyncio
    async def test_base_event_stream_captures(self):
        """Calling stream() on bare Event captures NotImplementedError as FAILED."""
        event = Event()
        async for _ in event.stream():
            pass
        assert event.execution.status == EventStatus.FAILED
        assert isinstance(event.execution.error, NotImplementedError)

    @pytest.mark.asyncio
    async def test_stream_cancelled_status(self):
        """Status transitions to CANCELLED when _stream() is cancelled."""

        async def consume(event):
            chunks = []
            async for chunk in event.stream():
                chunks.append(chunk)
            return chunks

        event = StreamCancelledEvent()
        task = asyncio.create_task(consume(event))
        # Give the generator time to yield "before" and enter the sleep.
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert event.execution.status == EventStatus.CANCELLED
        assert event.execution.error is not None
        assert isinstance(event.execution.error, asyncio.CancelledError)
        assert event.execution.duration is not None

    @pytest.mark.asyncio
    async def test_stream_idempotency_cancelled(self):
        """Calling stream() on a CANCELLED event yields nothing."""

        async def consume(event):
            async for _ in event.stream():
                pass

        event = StreamCancelledEvent()
        task = asyncio.create_task(consume(event))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert event.execution.status == EventStatus.CANCELLED

        # Stream again -- terminal status means no-op, no exception
        chunks = []
        async for chunk in event.stream():
            chunks.append(chunk)
        assert chunks == []


# File: tests/protocols/generic/test_event_lifecycle.py
