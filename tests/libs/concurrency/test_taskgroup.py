import time
from typing import TYPE_CHECKING

import anyio
import pytest

from lionagi.ln.concurrency import create_task_group
from lionagi.ln.concurrency._compat import ExceptionGroup

if TYPE_CHECKING or hasattr(BaseException, "__group__"):
    # Python 3.11+ has ExceptionGroup
    try:
        from exceptiongroup import ExceptionGroup
    except ImportError:
        ExceptionGroup = BaseException


@pytest.mark.slow
@pytest.mark.anyio
async def test_error_in_one_task_cancels_peers_promptly(anyio_backend):
    started = anyio.Event()
    seen_cancel = anyio.Event()

    async def bad():
        started.set()
        await anyio.sleep(0.005)
        raise ValueError("fail")

    async def peer():
        try:
            await started.wait()
            await anyio.sleep(10)
        except BaseException:
            seen_cancel.set()
            raise

    t0 = time.perf_counter()
    # TaskGroup raises ExceptionGroup in Python 3.11+
    with pytest.raises((ValueError, ExceptionGroup)) as exc_info:
        async with create_task_group() as tg:
            tg.start_soon(bad, name="bad")
            tg.start_soon(peer, name="peer")

    # Check that ValueError was raised (either directly or in ExceptionGroup)
    if isinstance(exc_info.value, ExceptionGroup):
        assert any(isinstance(e, ValueError) for e in exc_info.value.exceptions)
    dt = time.perf_counter() - t0
    assert seen_cancel.is_set()
    assert dt < 0.5  # cancelled quickly


@pytest.mark.anyio
async def test_taskgroup_name_passthrough(anyio_backend):
    executed = []

    async def noop():
        executed.append(True)

    async with create_task_group() as tg:
        tg.start_soon(noop, name="x")

    assert executed == [True]


@pytest.mark.anyio
async def test_taskgroup_happy_path_completion(anyio_backend):
    """Test that TaskGroup waits for all tasks to complete successfully."""
    results = []

    async def worker(n):
        await anyio.sleep(0.01 * n)  # Varying completion times
        results.append(n)
        return n

    async with create_task_group() as tg:
        for i in range(5):
            tg.start_soon(worker, i)

    # After context exit, all tasks should be complete
    assert set(results) == {0, 1, 2, 3, 4}


@pytest.mark.anyio
async def test_taskgroup_start_with_initialization(anyio_backend):
    """Test TaskGroup.start() for tasks that signal initialization."""
    initialized = []
    completed = []

    async def worker_with_startup(n, *, task_status):
        # Initialization phase
        initialized.append(n)
        task_status.started(f"worker_{n}")

        # Main work
        await anyio.sleep(0.01)
        completed.append(n)

    async with create_task_group() as tg:
        # start() waits for task_status.started() to be called
        result1 = await tg.start(worker_with_startup, 1)
        assert result1 == "worker_1"
        assert 1 in initialized  # Already initialized
        assert 1 not in completed  # Still running

        result2 = await tg.start(worker_with_startup, 2)
        assert result2 == "worker_2"
        assert 2 in initialized

    # After context exit, all tasks complete
    assert set(completed) == {1, 2}  # Order not guaranteed


@pytest.mark.anyio
async def test_taskgroup_mixed_success_and_cancellation(anyio_backend):
    """Test TaskGroup with some tasks completing before cancellation."""
    completed = []
    cancelled = []

    async def fast_worker(n):
        await anyio.sleep(0.001)
        completed.append(n)

    async def slow_worker(n):
        try:
            await anyio.sleep(0.5)  # Reduced from 1.0
            completed.append(n)
        except BaseException:  # Catch all cancellations
            cancelled.append(n)
            raise

    async def canceller():
        await anyio.sleep(0.01)  # Reduced from 0.02
        tg.cancel_scope.cancel()

    async with create_task_group() as tg:
        # Fast tasks complete before cancellation
        tg.start_soon(fast_worker, 1)
        tg.start_soon(fast_worker, 2)

        # Slow tasks get cancelled
        tg.start_soon(slow_worker, 3)
        tg.start_soon(slow_worker, 4)

        # Trigger cancellation
        tg.start_soon(canceller)

    assert set(completed) == {1, 2}  # Fast tasks completed
    assert set(cancelled) == {3, 4}  # Slow tasks cancelled


@pytest.mark.anyio
async def test_taskgroup_cancel_scope_propagation(anyio_backend):
    """Test that TaskGroup properly propagates its cancel scope."""
    outer_cancelled = False
    inner_cancelled = False
    # Deterministic signal: outer_task sets this once it has entered the
    # inner task group and is ready to be cancelled.  Avoids a timing race
    # where tg.cancel_scope.cancel() fired before outer_task was scheduled.
    outer_ready = anyio.Event()

    async def outer_task():
        nonlocal outer_cancelled
        try:
            async with create_task_group() as inner_tg:
                inner_tg.start_soon(inner_task)
                outer_ready.set()  # signal: inner task is running, ready to cancel
                await anyio.sleep_forever()
        except BaseException:
            outer_cancelled = True
            raise

    async def inner_task():
        nonlocal inner_cancelled
        try:
            await anyio.sleep_forever()
        except BaseException:
            inner_cancelled = True
            raise

    async with create_task_group() as tg:
        tg.start_soon(outer_task)
        await outer_ready.wait()  # wait until outer_task is running, not a fixed sleep
        tg.cancel_scope.cancel()

    assert outer_cancelled
    assert inner_cancelled  # Cancellation propagated to nested task
