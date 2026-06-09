"""Tests for lionagi.service.broadcaster module."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from lionagi.protocols.generic.event import Event
from lionagi.service.broadcaster import Broadcaster


class SampleEvent(Event):
    """Sample event class for broadcaster tests."""

    event_type: str = "test_event"


class TestBroadcaster:
    @pytest.fixture(autouse=True)
    def reset_broadcaster(self):
        """Reset broadcaster state before each test."""
        # Clear subscribers before each test
        Broadcaster._subscribers.clear()
        Broadcaster._instance = None
        yield
        # Clean up after test
        Broadcaster._subscribers.clear()
        Broadcaster._instance = None

    def test_broadcaster_singleton(self):

        # Create a subclass for testing
        class TestBroadcaster(Broadcaster):
            _event_type = SampleEvent

        broadcaster1 = TestBroadcaster()
        broadcaster2 = TestBroadcaster()

        assert broadcaster1 is broadcaster2
        assert TestBroadcaster._instance is broadcaster1

    def test_subscribe_adds_callback(self):

        class TestBroadcaster(Broadcaster):
            _event_type = SampleEvent

        callback = MagicMock()

        TestBroadcaster.subscribe(callback)

        assert TestBroadcaster.get_subscriber_count() == 1

    def test_subscribe_prevents_duplicates(self):

        class TestBroadcaster(Broadcaster):
            _event_type = SampleEvent

        callback = MagicMock()

        TestBroadcaster.subscribe(callback)
        TestBroadcaster.subscribe(callback)

        assert TestBroadcaster.get_subscriber_count() == 1

    def test_unsubscribe_removes_callback(self):

        class TestBroadcaster(Broadcaster):
            _event_type = SampleEvent

        callback = MagicMock()

        TestBroadcaster.subscribe(callback)
        assert TestBroadcaster.get_subscriber_count() == 1

        TestBroadcaster.unsubscribe(callback)
        assert TestBroadcaster.get_subscriber_count() == 0

    def test_unsubscribe_nonexistent_callback_no_error(self):

        class TestBroadcaster(Broadcaster):
            _event_type = SampleEvent

        callback = MagicMock()

        # Should not raise error
        TestBroadcaster.unsubscribe(callback)
        assert TestBroadcaster.get_subscriber_count() == 0

    @pytest.mark.asyncio
    async def test_broadcast_calls_sync_callback(self):

        class TestBroadcaster(Broadcaster):
            _event_type = SampleEvent

        callback = MagicMock()
        event = SampleEvent()

        TestBroadcaster.subscribe(callback)
        await TestBroadcaster.broadcast(event)

        callback.assert_called_once_with(event)

    @pytest.mark.asyncio
    async def test_broadcast_calls_async_callback(self):

        class TestBroadcaster(Broadcaster):
            _event_type = SampleEvent

        callback = AsyncMock()
        event = SampleEvent()

        TestBroadcaster.subscribe(callback)
        await TestBroadcaster.broadcast(event)

        callback.assert_awaited_once_with(event)

    @pytest.mark.asyncio
    async def test_broadcast_calls_multiple_subscribers(self):

        class TestBroadcaster(Broadcaster):
            _event_type = SampleEvent

        callback1 = MagicMock()
        callback2 = MagicMock()
        callback3 = AsyncMock()
        event = SampleEvent()

        TestBroadcaster.subscribe(callback1)
        TestBroadcaster.subscribe(callback2)
        TestBroadcaster.subscribe(callback3)

        await TestBroadcaster.broadcast(event)

        callback1.assert_called_once_with(event)
        callback2.assert_called_once_with(event)
        callback3.assert_awaited_once_with(event)

    @pytest.mark.asyncio
    async def test_broadcast_validates_event_type(self):

        class SpecificBroadcaster(Broadcaster):
            _event_type = SampleEvent

        class OtherEvent(Event):
            event_type: str = "other"

        callback = MagicMock()
        wrong_event = OtherEvent()

        SpecificBroadcaster.subscribe(callback)

        with pytest.raises(ValueError, match="Event must be of type SampleEvent"):
            await SpecificBroadcaster.broadcast(wrong_event)

        # Callback should not have been called
        callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_broadcast_handles_callback_exception(self):

        class TestBroadcaster(Broadcaster):
            _event_type = SampleEvent

        failing_callback = MagicMock(side_effect=RuntimeError("Callback error"))
        successful_callback = MagicMock()
        event = SampleEvent()

        TestBroadcaster.subscribe(failing_callback)
        TestBroadcaster.subscribe(successful_callback)

        # Should not raise, but log the error
        await TestBroadcaster.broadcast(event)

        # Both callbacks should be attempted
        failing_callback.assert_called_once_with(event)
        successful_callback.assert_called_once_with(event)

    @pytest.mark.asyncio
    async def test_broadcast_handles_async_callback_exception(self):

        class TestBroadcaster(Broadcaster):
            _event_type = SampleEvent

        failing_callback = AsyncMock(side_effect=RuntimeError("Async callback error"))
        successful_callback = AsyncMock()
        event = SampleEvent()

        TestBroadcaster.subscribe(failing_callback)
        TestBroadcaster.subscribe(successful_callback)

        # Should not raise, but log the error
        await TestBroadcaster.broadcast(event)

        # Both callbacks should be attempted
        assert failing_callback.await_count == 1
        successful_callback.assert_awaited_once_with(event)

    @pytest.mark.asyncio
    async def test_broadcast_with_no_subscribers(self):

        class TestBroadcaster(Broadcaster):
            _event_type = SampleEvent

        event = SampleEvent()

        # Should not raise error
        await TestBroadcaster.broadcast(event)
        assert TestBroadcaster.get_subscriber_count() == 0

    def test_get_subscriber_count_accuracy(self):

        class TestBroadcaster(Broadcaster):
            _event_type = SampleEvent

        assert TestBroadcaster.get_subscriber_count() == 0

        callback1 = MagicMock()
        callback2 = MagicMock()
        callback3 = MagicMock()

        TestBroadcaster.subscribe(callback1)
        assert TestBroadcaster.get_subscriber_count() == 1

        TestBroadcaster.subscribe(callback2)
        TestBroadcaster.subscribe(callback3)
        assert TestBroadcaster.get_subscriber_count() == 3

        TestBroadcaster.unsubscribe(callback2)
        assert TestBroadcaster.get_subscriber_count() == 2

    def test_multiple_broadcaster_subclasses_independent(self):

        class BroadcasterA(Broadcaster):
            _event_type = SampleEvent
            _subscribers = []
            _instance = None

        class TestEvent2(Event):
            event_type: str = "test2"

        class BroadcasterB(Broadcaster):
            _event_type = TestEvent2
            _subscribers = []
            _instance = None

        callback_a = MagicMock()
        callback_b = MagicMock()

        BroadcasterA.subscribe(callback_a)
        BroadcasterB.subscribe(callback_b)

        assert BroadcasterA.get_subscriber_count() == 1
        assert BroadcasterB.get_subscriber_count() == 1

    @pytest.mark.asyncio
    async def test_broadcast_mixed_sync_async_callbacks(self):

        class TestBroadcaster(Broadcaster):
            _event_type = SampleEvent

        sync_callback1 = MagicMock()
        async_callback = AsyncMock()
        sync_callback2 = MagicMock()
        event = SampleEvent()

        TestBroadcaster.subscribe(sync_callback1)
        TestBroadcaster.subscribe(async_callback)
        TestBroadcaster.subscribe(sync_callback2)

        await TestBroadcaster.broadcast(event)

        sync_callback1.assert_called_once_with(event)
        async_callback.assert_awaited_once_with(event)
        sync_callback2.assert_called_once_with(event)


##################################################
#  Regression: asyncio.iscoroutine-only check    #
##################################################


class TestBroadcasterCoroutineOnlyRegression:
    """Verify that broadcast only awaits coroutines, not Tasks or other awaitables.

    origin/main used `if asyncio.iscoroutine(result): await result`.
    The refactor replaced this with `await maybe_await(callback(event))`, which
    uses inspect.isawaitable and would also await Tasks/Futures.  These tests
    confirm the narrow coroutine-only semantics are restored.
    """

    @pytest.fixture(autouse=True)
    def fresh_broadcaster(self):
        class _TaskBroadcaster(Broadcaster):
            _event_type = SampleEvent

        self.TaskBroadcaster = _TaskBroadcaster
        yield
        _TaskBroadcaster._subscribers.clear()
        _TaskBroadcaster._instance = None

    @pytest.mark.asyncio
    async def test_sync_subscriber_returning_task_is_not_awaited(self):
        """Sync subscriber that schedules and returns an asyncio.Task must NOT block broadcast.

        Old behavior: asyncio.iscoroutine(task) is False → broadcast does not await it.
        New (broken) behavior: inspect.isawaitable(task) is True → broadcast awaits it.
        """
        task_completed = []

        async def _background():
            await asyncio.sleep(0.05)
            task_completed.append(True)

        def sync_callback_schedules_task(event):
            # Fire-and-forget: schedule work and return the Task.
            return asyncio.ensure_future(_background())

        self.TaskBroadcaster.subscribe(sync_callback_schedules_task)
        event = SampleEvent()

        # broadcast() must return before _background() finishes (non-blocking).
        await self.TaskBroadcaster.broadcast(event)

        # The task has NOT completed yet because broadcast did not await it.
        assert task_completed == [], (
            "broadcast() awaited an asyncio.Task returned by a sync subscriber — "
            "this changes fire-and-return to fire-and-wait, breaking origin/main behavior."
        )

        # Give the background task a chance to run to avoid ResourceWarning.
        await asyncio.sleep(0.1)
        assert task_completed == [True]

    @pytest.mark.asyncio
    async def test_async_subscriber_coroutine_is_still_awaited(self):
        results = []

        async def async_callback(event):
            results.append("done")

        self.TaskBroadcaster.subscribe(async_callback)
        event = SampleEvent()
        await self.TaskBroadcaster.broadcast(event)

        assert results == ["done"], "async subscriber coroutine was not awaited"


# ---------------------------------------------------------------------------
# Edge cases: subscribe/unsubscribe during broadcast, many subscribers,
# weakref GC cleanup, slow subscriber does not block broadcast
# ---------------------------------------------------------------------------


class TestBroadcasterEdgeCases:
    @pytest.fixture(autouse=True)
    def fresh_broadcaster(self):
        class _EdgeBroadcaster(Broadcaster):
            _event_type = SampleEvent

        self.EdgeBroadcaster = _EdgeBroadcaster
        yield
        _EdgeBroadcaster._subscribers.clear()
        _EdgeBroadcaster._instance = None

    @pytest.mark.asyncio
    async def test_subscribe_during_broadcast_does_not_call_new_subscriber(self):
        called = []

        def late_subscriber(event):
            called.append("late")

        def first_subscriber(event):
            called.append("first")
            self.EdgeBroadcaster.subscribe(late_subscriber)

        self.EdgeBroadcaster.subscribe(first_subscriber)
        event = SampleEvent()
        await self.EdgeBroadcaster.broadcast(event)
        assert called == ["first"]

    @pytest.mark.asyncio
    async def test_unsubscribe_during_broadcast_does_not_crash(self):
        called = []

        def self_removing_subscriber(event):
            called.append("self_removing")
            self.EdgeBroadcaster.unsubscribe(self_removing_subscriber)

        def other_subscriber(event):
            called.append("other")

        self.EdgeBroadcaster.subscribe(self_removing_subscriber)
        self.EdgeBroadcaster.subscribe(other_subscriber)
        event = SampleEvent()
        await self.EdgeBroadcaster.broadcast(event)
        assert "self_removing" in called
        assert "other" in called

    @pytest.mark.asyncio
    async def test_hundreds_of_subscribers_all_called(self):
        results = []
        n = 200
        callbacks = [MagicMock(side_effect=lambda e, i=i: results.append(i)) for i in range(n)]
        for cb in callbacks:
            self.EdgeBroadcaster.subscribe(cb)
        assert self.EdgeBroadcaster.get_subscriber_count() == n
        event = SampleEvent()
        await self.EdgeBroadcaster.broadcast(event)
        assert len(results) == n

    def test_weakref_bound_method_gc_cleanup(self):
        class Handler:
            def handle(self, event):
                pass

        h = Handler()
        self.EdgeBroadcaster.subscribe(h.handle)
        assert self.EdgeBroadcaster.get_subscriber_count() == 1
        del h
        import gc

        gc.collect()
        count = self.EdgeBroadcaster.get_subscriber_count()
        assert count == 0

    @pytest.mark.asyncio
    async def test_slow_sync_subscriber_runs_synchronously_blocking(self):
        import time

        start_times = []
        end_times = []

        def slow_subscriber(event):
            start_times.append(time.monotonic())
            time.sleep(0.05)
            end_times.append(time.monotonic())

        fast_results = []

        def fast_subscriber(event):
            fast_results.append(time.monotonic())

        self.EdgeBroadcaster.subscribe(slow_subscriber)
        self.EdgeBroadcaster.subscribe(fast_subscriber)
        event = SampleEvent()
        await self.EdgeBroadcaster.broadcast(event)
        assert len(start_times) == 1
        assert len(fast_results) == 1
        assert fast_results[0] >= end_times[0]

    @pytest.mark.asyncio
    async def test_never_unsubscribed_subscribers_accumulate(self):
        n = 50
        for i in range(n):
            cb = MagicMock()
            self.EdgeBroadcaster.subscribe(cb)
        assert self.EdgeBroadcaster.get_subscriber_count() == n
