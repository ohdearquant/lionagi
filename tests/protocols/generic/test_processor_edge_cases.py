"""Edge case tests for lionagi's Processor class."""

from __future__ import annotations

import asyncio

import pytest

from lionagi.protocols.generic.event import Event, EventStatus
from lionagi.protocols.generic.processor import Processor


class _OkEvent(Event):
    async def _invoke(self):
        return "ok"


class _FailEvent(Event):
    async def _invoke(self):
        raise ValueError("intentional failure")


class _SlowEvent(Event):
    async def _invoke(self):
        await asyncio.sleep(0.05)
        return "slow-ok"


class _Proc(Processor):
    event_type = _OkEvent


def _proc(**kw) -> _Proc:
    defaults = dict(queue_capacity=10, capacity_refresh_time=0.01, concurrency_limit=2)
    defaults.update(kw)
    return _Proc(**defaults)


class TestProcessorInit:
    def test_basic_init(self):
        p = _proc()
        assert p.queue_capacity == 10
        assert p.capacity_refresh_time == 0.01
        assert p.available_capacity == 10
        assert p.execution_mode is False
        assert not p.is_stopped()

    def test_with_max_queue_size(self):
        p = _proc(max_queue_size=5)
        assert p.max_queue_size == 5

    def test_zero_concurrency_limit_no_semaphore(self):
        p = _proc(concurrency_limit=0)
        assert p._concurrency_sem is None

    def test_invalid_capacity_raises(self):
        with pytest.raises(ValueError, match="capacity"):
            _proc(queue_capacity=0)

    def test_invalid_refresh_time_raises(self):
        with pytest.raises(ValueError, match="refresh"):
            _proc(capacity_refresh_time=0)


class TestProcessorQueueFull:
    def test_queue_full_unlimited(self):
        p = _proc(max_queue_size=0)
        assert p.queue_full is False

    def test_queue_full_when_at_capacity(self):
        p = _proc(max_queue_size=1)
        assert not p.queue_full
        p.try_enqueue(_OkEvent())
        assert p.queue_full

    def test_try_enqueue_returns_true_when_space(self):
        p = _proc(max_queue_size=5)
        assert p.try_enqueue(_OkEvent()) is True

    def test_try_enqueue_returns_false_when_full(self):
        p = _proc(max_queue_size=1)
        p.try_enqueue(_OkEvent())
        assert p.try_enqueue(_OkEvent()) is False


class TestProcessorStartStop:
    async def test_stop_sets_stopped(self):
        p = _proc()
        assert not p.is_stopped()
        await p.stop()
        assert p.is_stopped()

    async def test_start_clears_stopped(self):
        p = _proc()
        await p.stop()
        await p.start()
        assert not p.is_stopped()

    async def test_start_when_not_stopped_is_noop(self):
        p = _proc()
        await p.start()
        assert not p.is_stopped()


class TestProcessorEnqueueDequeue:
    async def test_enqueue_adds_to_queue(self):
        p = _proc()
        event = _OkEvent()
        await asyncio.wait_for(p.enqueue(event), timeout=0.5)
        assert p.queue.qsize() == 1

    async def test_dequeue_retrieves_event(self):
        p = _proc()
        event = _OkEvent()
        await asyncio.wait_for(p.enqueue(event), timeout=0.5)
        result = await asyncio.wait_for(p.dequeue(), timeout=0.5)
        assert result is event


class TestProcessorProcess:
    async def test_process_empty_queue(self):
        p = _proc()
        await asyncio.wait_for(p.process(), timeout=1.0)
        assert p.available_capacity == 10

    async def test_process_completes_event(self):
        p = _proc()
        event = _OkEvent()
        await p.enqueue(event)
        await asyncio.wait_for(p.process(), timeout=2.0)
        assert event.status == EventStatus.COMPLETED
        assert event.response == "ok"

    async def test_process_handles_failing_event(self):
        p = _proc()
        event = _FailEvent()
        await p.enqueue(event)
        await asyncio.wait_for(p.process(), timeout=2.0)
        assert event.status == EventStatus.FAILED

    async def test_process_resets_capacity(self):
        p = _proc(queue_capacity=5)
        await p.enqueue(_OkEvent())
        await asyncio.wait_for(p.process(), timeout=2.0)
        assert p.available_capacity == 5

    async def test_process_multiple_events(self):
        p = _proc(queue_capacity=10)
        events = [_OkEvent() for _ in range(3)]
        for e in events:
            await p.enqueue(e)
        await asyncio.wait_for(p.process(), timeout=3.0)
        for e in events:
            assert e.status == EventStatus.COMPLETED

    async def test_process_respects_capacity(self):
        p = _proc(queue_capacity=2, concurrency_limit=2)
        events = [_OkEvent() for _ in range(4)]
        for e in events:
            await p.enqueue(e)
        await asyncio.wait_for(p.process(), timeout=2.0)
        completed = sum(1 for e in events if e.status == EventStatus.COMPLETED)
        assert completed <= 4


class TestProcessorRequestPermission:
    async def test_default_permits_all(self):
        p = _proc()
        assert await p.request_permission() is True

    async def test_default_permits_with_kwargs(self):
        p = _proc()
        assert await p.request_permission(foo="bar") is True


class TestProcessorAvailableCapacity:
    def test_getter(self):
        p = _proc(queue_capacity=7)
        assert p.available_capacity == 7

    def test_setter(self):
        p = _proc(queue_capacity=7)
        p.available_capacity = 3
        assert p.available_capacity == 3


class TestProcessorExecutionMode:
    def test_default_false(self):
        p = _proc()
        assert p.execution_mode is False

    def test_setter(self):
        p = _proc()
        p.execution_mode = True
        assert p.execution_mode is True

    async def test_execute_sets_and_clears_execution_mode(self):
        p = _proc(capacity_refresh_time=0.01)

        async def stopper():
            await asyncio.sleep(0.05)
            await p.stop()

        stopper_task = asyncio.create_task(stopper())
        exec_task = asyncio.create_task(p.execute())
        await asyncio.wait_for(asyncio.gather(exec_task, stopper_task), timeout=2.0)
        assert p.execution_mode is False


class TestProcessorCreate:
    async def test_create_classmethod(self):
        p = await asyncio.wait_for(
            _Proc.create(
                queue_capacity=5,
                capacity_refresh_time=0.1,
                concurrency_limit=1,
            ),
            timeout=1.0,
        )
        assert isinstance(p, _Proc)
        assert p.queue_capacity == 5


class _RejectProc(Processor):
    """Denies every event with a terminal (reject) decision."""

    event_type = _OkEvent

    async def request_permission(self, **kwargs):
        return False


class _DeferProc(Processor):
    """Denies every event as a DEFERRAL (rate-limit style backpressure)."""

    event_type = _OkEvent

    async def request_permission(self, **kwargs):
        return False

    async def handle_denied(self, event) -> bool:
        return False  # defer, do not terminalize


class _DeferUntilProc(Processor):
    """Denies the first ``deny_first`` checks then allows; models a replenishing rate limit."""

    event_type = _OkEvent

    def __init__(self, *a, deny_first: int = 1, **kw):
        super().__init__(*a, **kw)
        self._remaining_denials = deny_first

    async def request_permission(self, **kwargs):
        if self._remaining_denials > 0:
            self._remaining_denials -= 1
            return False
        return True

    async def handle_denied(self, event) -> bool:
        return False  # defer


class TestProcessorDenial:
    async def test_terminal_denial_marks_skipped_and_drains(self):
        """A rejected (terminal) event reaches SKIPPED and leaves the queue."""
        p = _RejectProc(queue_capacity=10, capacity_refresh_time=0.01, concurrency_limit=2)
        event = _OkEvent()
        await p.enqueue(event)
        await asyncio.wait_for(p.process(), timeout=1.0)
        assert event.status == EventStatus.SKIPPED
        assert p.queue.empty()

    async def test_deferred_denial_requeues_not_drops(self):
        """Regression: process() used to dequeue a deferred event, silently losing rate-limited work."""
        p = _DeferProc(queue_capacity=10, capacity_refresh_time=0.01, concurrency_limit=2)
        event = _OkEvent()
        await p.enqueue(event)
        await asyncio.wait_for(p.process(), timeout=1.0)
        # Re-enqueued, not dropped: still queued and still PENDING.
        assert not p.queue.empty()
        assert event.status == EventStatus.PENDING

    async def test_deferred_batch_does_not_busy_spin(self):
        """process() must return promptly after a full lap of deferrals, not spin infinitely."""
        p = _DeferProc(queue_capacity=100, capacity_refresh_time=0.01, concurrency_limit=2)
        events = [_OkEvent() for _ in range(3)]
        for e in events:
            await p.enqueue(e)
        await asyncio.wait_for(p.process(), timeout=1.0)
        # All three preserved in the queue, none dispatched.
        assert p.queue.qsize() == 3
        assert all(e.status == EventStatus.PENDING for e in events)

    async def test_deferred_then_granted_dispatches(self):
        """A deferred event must retry and dispatch on a later cycle, not be dropped."""
        p = _DeferUntilProc(
            queue_capacity=10,
            capacity_refresh_time=0.01,
            concurrency_limit=2,
            deny_first=1,
        )
        event = _OkEvent()
        await p.enqueue(event)

        # First cycle: denied -> deferred (re-enqueued, still PENDING).
        await asyncio.wait_for(p.process(), timeout=1.0)
        assert not p.queue.empty()
        assert event.status == EventStatus.PENDING

        # Second cycle: permission now granted -> dispatched and completed.
        await asyncio.wait_for(p.process(), timeout=1.0)
        assert p.queue.empty()
        assert event.status == EventStatus.COMPLETED


class TestProcessorJoin:
    """join() drains the queue by looping process() until empty."""

    async def test_join_empty_queue_returns_immediately(self):
        p = _proc()
        await asyncio.wait_for(p.join(), timeout=1.0)
        assert p.queue.empty()

    async def test_join_drains_all_events_to_completion(self):
        p = _proc(queue_capacity=10)
        events = [_OkEvent() for _ in range(5)]
        for e in events:
            await p.enqueue(e)
        await asyncio.wait_for(p.join(), timeout=3.0)
        assert p.queue.empty()
        assert all(e.status == EventStatus.COMPLETED for e in events)

    async def test_join_drains_when_queue_exceeds_capacity(self):
        # More events than one batch's capacity: join must loop until empty.
        p = _proc(queue_capacity=2, concurrency_limit=2)
        events = [_OkEvent() for _ in range(5)]
        for e in events:
            await p.enqueue(e)
        await asyncio.wait_for(p.join(), timeout=3.0)
        assert p.queue.empty()
        assert all(e.status == EventStatus.COMPLETED for e in events)

    async def test_join_terminal_denial_drains_skipped(self):
        p = _RejectProc(queue_capacity=10, capacity_refresh_time=0.01, concurrency_limit=2)
        events = [_OkEvent() for _ in range(3)]
        for e in events:
            await p.enqueue(e)
        await asyncio.wait_for(p.join(), timeout=1.0)
        assert p.queue.empty()
        assert all(e.status == EventStatus.SKIPPED for e in events)

    async def test_join_completes_deferred_then_granted(self):
        # Event is deferred on the first lap, then granted: join() loops past
        # the no-progress cycle (sleeping capacity_refresh_time) and completes it.
        p = _DeferUntilProc(
            queue_capacity=10,
            capacity_refresh_time=0.01,
            concurrency_limit=2,
            deny_first=1,
        )
        event = _OkEvent()
        await p.enqueue(event)
        await asyncio.wait_for(p.join(), timeout=2.0)
        assert p.queue.empty()
        assert event.status == EventStatus.COMPLETED
