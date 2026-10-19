# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Non-coroutine handler results retain their value and scheduler accounting."""

from __future__ import annotations

import asyncio

import pytest

from lionagi.ln.concurrency import ExceptionGroup
from lionagi.studio.scheduler.signals import (
    SchedulerHandlerCancelled,
    SchedulerSignalBus,
    ScheduleRunSucceeded,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "value"),
    (
        pytest.param("future", False, id="future-false"),
        pytest.param("future", True, id="future-true"),
        pytest.param("task", False, id="task-false"),
        pytest.param("task", True, id="task-true"),
        pytest.param("custom", False, id="custom-false"),
    ),
)
async def test_bus_accounts_for_the_resolved_non_coroutine_handler_value(kind, value):
    bus = SchedulerSignalBus()
    signal = ScheduleRunSucceeded(run_id="awaitable-result", schedule_id="s")
    seen = []
    handles = []

    async def result():
        return value

    class Returned:
        def __await__(self):
            return result().__await__()

    def handler(received):
        seen.append(received)
        if kind == "future":
            pending = asyncio.get_running_loop().create_future()
            pending.set_result(value)
            handles.append(pending)
            return pending
        if kind == "task":
            task = asyncio.create_task(result())
            handles.append(task)
            return task
        return Returned()

    bus.observe(ScheduleRunSucceeded, handler=handler)
    try:
        results = await bus.emit(signal)

        assert len(seen) == 1
        assert seen[0] is signal
        assert len(results) == 1
        assert results[0] is value
        assert bus.pop_run_counters(signal.run_id) == {
            "emitted": {"ScheduleRunSucceeded": 1},
            "received": 1,
            "acted_on": int(value),
        }
    finally:
        # Drain test-created loop handles even when a mutation forgets to await
        # the returned result. These tasks perform no I/O or external work.
        if handles:
            await asyncio.gather(*handles, return_exceptions=True)


@pytest.mark.asyncio
async def test_bus_groups_the_original_error_from_a_returned_future():
    bus = SchedulerSignalBus()
    signal = ScheduleRunSucceeded(run_id="future-error", schedule_id="s")
    future = asyncio.get_running_loop().create_future()
    failure = LookupError("test-local future failure")
    future.set_exception(failure)
    bus.observe(ScheduleRunSucceeded, handler=lambda _signal: future)

    try:
        with pytest.raises(ExceptionGroup) as caught:
            await bus.emit(signal)

        assert caught.value.exceptions == (failure,)
        assert bus.pop_run_counters(signal.run_id) == {
            "emitted": {"ScheduleRunSucceeded": 1},
            "received": 1,
            "acted_on": 0,
        }
    finally:
        # A broken dispatch path must not leave an unobserved Future exception.
        if not future.cancelled():
            future.exception()


@pytest.mark.asyncio
async def test_bus_distinguishes_a_returned_cancelled_future_from_emitter_cancellation():
    bus = SchedulerSignalBus()
    signal = ScheduleRunSucceeded(run_id="future-cancel", schedule_id="s")
    future = asyncio.get_running_loop().create_future()
    future.cancel()
    bus.observe(ScheduleRunSucceeded, handler=lambda _signal: future)

    with pytest.raises(SchedulerHandlerCancelled) as caught:
        await bus.emit(signal)

    assert type(caught.value) is SchedulerHandlerCancelled
    assert isinstance(caught.value.__cause__, asyncio.CancelledError)
    assert bus.pop_run_counters(signal.run_id) == {
        "emitted": {"ScheduleRunSucceeded": 1},
        "received": 1,
        "acted_on": 0,
    }
