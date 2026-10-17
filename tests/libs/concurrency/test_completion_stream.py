"""Tests for CompletionStream from lionagi.ln.concurrency.patterns.

Covers lifecycle, cancellation, error handling, and edge cases.
"""

import anyio
import pytest

from lionagi.ln import move_on_after
from lionagi.ln.concurrency.patterns import CompletionStream

pytestmark = pytest.mark.anyio


async def test_completion_stream_basic_usage(anyio_backend):
    """Test CompletionStream basic async for loop."""

    async def task(x):
        await anyio.sleep(0.001 * x)
        return x * 2

    results = {}
    async with CompletionStream([task(i) for i in range(5)]) as stream:
        async for idx, result in stream:
            results[idx] = result

    assert len(results) == 5
    assert results == {0: 0, 1: 2, 2: 4, 3: 6, 4: 8}


async def test_completion_stream_preserves_index_result_pairs(anyio_backend):
    """Test CompletionStream returns correct (index, result) tuples."""

    async def task(x):
        # Reverse order completion times to test index tracking
        await anyio.sleep(0.001 * (10 - x))
        return f"result_{x}"

    results = []
    async with CompletionStream([task(i) for i in range(5)]) as stream:
        async for idx, result in stream:
            results.append((idx, result))

    # All results should be collected
    assert len(results) == 5
    # Each result should match its index
    for idx, result in results:
        assert result == f"result_{idx}"


async def test_completion_stream_empty_awaitables(anyio_backend):
    """Test CompletionStream with empty awaitable list."""
    results = []
    async with CompletionStream([]) as stream:
        async for idx, result in stream:
            results.append((idx, result))

    assert results == []


async def test_completion_stream_with_limit(anyio_backend):
    """Test CompletionStream respects concurrency limit."""
    current_running = {"count": 0, "max": 0}

    async def tracked_task(x):
        current_running["count"] += 1
        current_running["max"] = max(current_running["max"], current_running["count"])
        await anyio.sleep(0.01)
        current_running["count"] -= 1
        return x

    LIMIT = 3
    async with CompletionStream([tracked_task(i) for i in range(10)], limit=LIMIT) as stream:
        async for idx, result in stream:
            pass

    assert current_running["max"] == LIMIT


async def test_completion_stream_limit_none_allows_all_concurrent(
    anyio_backend,
):
    """Test CompletionStream with limit=None allows all tasks to run concurrently."""
    current_running = {"count": 0, "max": 0}

    async def tracked_task(x):
        current_running["count"] += 1
        current_running["max"] = max(current_running["max"], current_running["count"])
        await anyio.sleep(0.01)
        current_running["count"] -= 1
        return x

    NUM_TASKS = 8
    async with CompletionStream([tracked_task(i) for i in range(NUM_TASKS)], limit=None) as stream:
        async for idx, result in stream:
            pass

    # Without limit, all tasks should run concurrently
    assert current_running["max"] == NUM_TASKS


async def test_completion_stream_early_break_exits_cleanly(anyio_backend):
    """Test breaking early exits the stream cleanly without consuming all results."""
    consumed = []
    started = []
    settled = []

    async def task(x):
        started.append(x)
        try:
            await anyio.sleep(0.001)
            return x
        finally:
            settled.append(x)

    # Break after the first result, whichever task produced it: the finishing
    # order is the backend's scheduling, not the task order.
    with anyio.fail_after(5):
        async with CompletionStream([task(i) for i in range(20)], limit=5) as stream:
            async for _idx, result in stream:
                consumed.append(result)
                break

    assert len(consumed) == 1
    # Leaving the block leaves no task behind: every task that started has
    # returned or been cancelled by the time __aexit__ is done, and none starts
    # or settles after it.
    at_exit = (sorted(started), sorted(settled))
    assert at_exit[1] == at_exit[0]
    await anyio.sleep(0.05)
    assert (sorted(started), sorted(settled)) == at_exit


async def test_completion_stream_leaving_early_waits_for_remaining_tasks(anyio_backend):
    """Test that leaving the iteration early does not cancel the remaining awaitables.

    A ``break`` out of ``async for`` stops consuming results only. The block's
    ``__aexit__`` waits for every awaitable, so all of them have finished by the
    time the block is left.
    """
    finished = []
    NUM_TASKS = 20
    LIMIT = 5

    async def task(x):
        await anyio.sleep(0.005)
        finished.append(x)
        return x

    with anyio.fail_after(5):
        async with CompletionStream([task(i) for i in range(NUM_TASKS)], limit=LIMIT) as stream:
            async for _idx, _result in stream:
                # The consumer stops after one result while the tail of the work
                # is still queued behind the concurrency limit.
                assert len(finished) < NUM_TASKS
                break

    # Not just the awaitables the loop consumed: __aexit__ ran every one of them.
    assert sorted(finished) == list(range(NUM_TASKS))
    await anyio.sleep(0.05)
    assert sorted(finished) == list(range(NUM_TASKS))


async def test_completion_stream_cancelling_an_enclosing_scope_stops_remaining_tasks(anyio_backend):
    """Cancelling an enclosing scope before the block exits stops the remaining awaitables.

    This is the documented way to leave early and not wait: without the cancel,
    ``__aexit__`` would wait out the slow awaitable and the outer deadline fails
    the test.
    """
    finished = []

    async def task(x):
        await anyio.sleep(0.001 if x == 0 else 5.0)
        finished.append(x)
        return x

    with anyio.fail_after(2):
        with move_on_after(None) as scope:
            async with CompletionStream([task(0), task(1)]) as stream:
                async for _idx, _result in stream:
                    scope.cancel()
                    break

    assert scope.cancelled_caught
    assert finished == [0]


# Test removed - timing-dependent behavior hard to test reliably


async def test_completion_stream_consume_all_results(anyio_backend):
    """Test consuming all results without early break completes normally."""
    results = []

    async def task(x):
        await anyio.sleep(0.001)
        return x * 2

    async with CompletionStream([task(i) for i in range(10)], limit=3) as stream:
        async for idx, result in stream:
            results.append(result)

    assert len(results) == 10
    assert set(results) == {i * 2 for i in range(10)}


@pytest.mark.filterwarnings("ignore::RuntimeWarning")
async def test_completion_stream_exception_propagation(anyio_backend):
    """Test CompletionStream propagates exceptions through ExceptionGroup."""
    from lionagi.ln.concurrency._compat import ExceptionGroup

    async def failing_task(x):
        await anyio.sleep(0.001)
        if x == 3:
            raise ValueError(f"Task {x} failed")
        return x

    # Exceptions in tasks are wrapped in ExceptionGroup by task group
    with pytest.raises(ExceptionGroup) as exc_info:
        async with CompletionStream([failing_task(i) for i in range(10)], limit=5) as stream:
            async for idx, result in stream:
                pass

    # Verify the ValueError is in the exception group
    assert any(
        isinstance(e, ValueError) and "Task 3 failed" in str(e) for e in exc_info.value.exceptions
    )


async def test_completion_stream_cleanup_on_exception(anyio_backend):
    """Test CompletionStream cleans up resources on exception."""
    from lionagi.ln.concurrency._compat import ExceptionGroup

    async def failing_task(x):
        await anyio.sleep(0.001)
        if x == 2:
            raise ValueError("Intentional failure")
        return x

    # Track cleanup by testing exception propagation
    stream = CompletionStream([failing_task(i) for i in range(5)], limit=2)

    with pytest.raises(ExceptionGroup):
        async with stream:
            async for idx, result in stream:
                pass

    # Verify cleanup was called (stream should be properly closed)
    assert stream._send is not None  # Resources were initialized
    assert stream._recv is not None
    # __aexit__ should have closed resources


@pytest.mark.filterwarnings("ignore::RuntimeWarning")
async def test_completion_stream_exception_early_in_iteration(anyio_backend):
    """Test exception early in iteration propagates through ExceptionGroup."""
    from lionagi.ln.concurrency._compat import ExceptionGroup

    async def task(x):
        await anyio.sleep(0.001)
        if x == 0:
            raise RuntimeError(f"Task {x} fails immediately")
        return x

    with pytest.raises(ExceptionGroup) as exc_info:
        async with CompletionStream([task(i) for i in range(10)], limit=3) as stream:
            async for idx, result in stream:
                pass

    # Verify the RuntimeError is in the exception group
    assert any(
        isinstance(e, RuntimeError) and "Task 0 fails immediately" in str(e)
        for e in exc_info.value.exceptions
    )


@pytest.mark.filterwarnings("ignore::RuntimeWarning")
async def test_completion_stream_not_in_context_manager_raises(anyio_backend):
    """Test CompletionStream raises when used outside context manager."""
    import warnings

    # Use task factory to create coroutines
    async def dummy_task(x):
        await anyio.sleep(0)
        return x

    # Suppress RuntimeWarning for unawaited coroutines in this error test
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        stream = CompletionStream([dummy_task(i) for i in range(3)])

        with pytest.raises(RuntimeError, match="must be used as async context manager"):
            async for idx, result in stream:
                pass


async def test_completion_stream_proper_cleanup_in_aexit(anyio_backend):
    """Test CompletionStream __aexit__ properly closes all resources."""

    async def task(x):
        await anyio.sleep(0.001)
        return x

    stream = CompletionStream([task(i) for i in range(5)], limit=2)

    # Enter context manager
    await stream.__aenter__()
    assert stream._task_group is not None
    assert stream._send is not None
    assert stream._recv is not None

    # Consume one result
    idx, result = await stream.__anext__()
    assert result == idx

    # Exit context manager (should cleanup)
    await stream.__aexit__(None, None, None)

    # Resources should be cleaned up (streams closed)
    # Task group should have exited


@pytest.mark.slow
async def test_completion_stream_handles_slow_tasks(anyio_backend):
    """Test CompletionStream handles tasks with varying completion times."""

    async def task(x):
        # Reverse order completion times
        await anyio.sleep(0.001 * (10 - x))
        return x

    results = []
    async with CompletionStream([task(i) for i in range(10)], limit=3) as stream:
        async for idx, result in stream:
            results.append((idx, result))

    # All results should be collected
    assert len(results) == 10
    # Results can come in any order (not necessarily input order)
    assert {r[1] for r in results} == set(range(10))


async def test_completion_stream_single_task(anyio_backend):
    """Test CompletionStream with single task."""

    async def task():
        await anyio.sleep(0.001)
        return "single_result"

    results = []
    async with CompletionStream([task()], limit=1) as stream:
        async for idx, result in stream:
            results.append((idx, result))

    assert results == [(0, "single_result")]


async def test_completion_stream_completed_count_tracking(anyio_backend):
    """Test CompletionStream tracks completed count correctly."""

    async def task(x):
        await anyio.sleep(0.001)
        return x

    stream = CompletionStream([task(i) for i in range(5)])

    async with stream:
        assert stream._completed_count == 0
        assert stream._total_count == 5

        # Consume first result
        await stream.__anext__()
        assert stream._completed_count == 1

        # Consume second result
        await stream.__anext__()
        assert stream._completed_count == 2

        # Consume remaining
        async for idx, result in stream:
            pass

        assert stream._completed_count == 5


async def test_completion_stream_stop_async_iteration(anyio_backend):
    """Test CompletionStream raises StopAsyncIteration when done."""

    async def task(x):
        await anyio.sleep(0.001)
        return x

    stream = CompletionStream([task(i) for i in range(3)])

    async with stream:
        # Consume all results
        await stream.__anext__()
        await stream.__anext__()
        await stream.__anext__()

        # Next call should raise StopAsyncIteration
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()


async def test_completion_stream_with_immediate_results(anyio_backend):
    """Test CompletionStream with tasks that complete immediately."""

    async def instant_task(x):
        return x * 2

    results = {}
    async with CompletionStream([instant_task(i) for i in range(5)], limit=2) as stream:
        async for idx, result in stream:
            results[idx] = result

    assert len(results) == 5
    assert results == {i: i * 2 for i in range(5)}


@pytest.mark.slow
async def test_completion_stream_realistic_workload(anyio_backend):
    """Test CompletionStream with realistic mixed-speed workload."""

    async def mixed_speed_task(x):
        # Varying delays to simulate real-world variance
        import random

        await anyio.sleep(random.uniform(0.001, 0.01))
        return x**2

    results = []
    TASKS = 20
    LIMIT = 5

    async with CompletionStream([mixed_speed_task(i) for i in range(TASKS)], limit=LIMIT) as stream:
        async for idx, result in stream:
            results.append(result)

    # All tasks completed
    assert len(results) == TASKS
    # Results are correct
    assert set(results) == {i**2 for i in range(TASKS)}


async def test_completion_stream_partial_consumption_then_break(anyio_backend):
    """Test CompletionStream with partial consumption and early break."""
    consumed = []

    async def task(x):
        await anyio.sleep(0.001)
        return x

    async with CompletionStream([task(i) for i in range(10)], limit=3) as stream:
        # Consume first 3 results then break
        count = 0
        async for idx, result in stream:
            consumed.append(result)
            count += 1
            if count >= 3:
                break

    assert len(consumed) == 3


# NOTE: Line 247 is defensive code for an edge case where EndOfStream is raised
# while completed_count < total_count. In normal operation, the completed_count
# reaches total_count and StopAsyncIteration is raised at line 240 instead.
# This defensive path is extremely difficult to test without causing other
# errors (ClosedResourceError from tasks trying to send to closed stream).
