"""Tests for lionagi.service.broadcaster module."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from lionagi.protocols.generic.event import Event
from lionagi.service.broadcaster import Broadcaster


class SampleEvent(Event):
    event_type: str = "test_event"


class TestBroadcaster:
    @pytest.fixture(autouse=True)
    def reset_broadcaster(self):
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
    """Verify broadcast only awaits coroutines: refactor to maybe_await/isawaitable would also await Tasks/Futures, breaking fire-and-return semantics."""

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
        """Async subscriber's coroutine must still be awaited (regression safety net)."""
        results = []

        async def async_callback(event):
            results.append("done")

        self.TaskBroadcaster.subscribe(async_callback)
        event = SampleEvent()
        await self.TaskBroadcaster.broadcast(event)

        assert results == ["done"], "async subscriber coroutine was not awaited"


##################################################
#  maybe_await widening: non-coroutine awaitables #
##################################################


class _FlagAwaitable:
    """Bare `__await__`-only awaitable (not a coroutine, not a Task/Future)."""

    def __init__(self, flag: list) -> None:
        self._flag = flag

    def __await__(self):
        async def _mark():
            self._flag.append(True)

        return _mark().__await__()


class TestBroadcasterMaybeAwaitWidening:
    """Verify broadcast awaits ANY awaitable via maybe_await, not just coroutines (opposite contract of the class above; see PR description)."""

    @pytest.fixture(autouse=True)
    def fresh_broadcaster(self):
        class _WideningBroadcaster(Broadcaster):
            _event_type = SampleEvent

        self.WideningBroadcaster = _WideningBroadcaster
        yield
        _WideningBroadcaster._subscribers.clear()
        _WideningBroadcaster._instance = None

    @pytest.mark.asyncio
    async def test_sync_subscriber_returning_bare_awaitable_is_awaited(self):
        flag = []

        def sync_callback_returns_bare_awaitable(event):
            return _FlagAwaitable(flag)

        self.WideningBroadcaster.subscribe(sync_callback_returns_bare_awaitable)
        await self.WideningBroadcaster.broadcast(SampleEvent())

        assert flag == [True], (
            "broadcast() did not await a bare __await__-only awaitable returned "
            "by a sync subscriber"
        )

    @pytest.mark.asyncio
    async def test_sync_subscriber_returning_future_is_awaited(self):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        loop.call_soon(future.set_result, None)
        observed = []

        def sync_callback_returns_future(event):
            observed.append("called")
            return future

        self.WideningBroadcaster.subscribe(sync_callback_returns_future)
        await self.WideningBroadcaster.broadcast(SampleEvent())

        assert observed == ["called"]
        assert future.done(), "broadcast() did not await the Future returned by a sync subscriber"

    @pytest.mark.asyncio
    async def test_async_subscriber_coroutine_still_awaited(self):
        results = []

        async def async_callback(event):
            results.append("done")

        self.WideningBroadcaster.subscribe(async_callback)
        await self.WideningBroadcaster.broadcast(SampleEvent())

        assert results == ["done"]
