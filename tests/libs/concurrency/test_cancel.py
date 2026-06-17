import time

import anyio
import pytest

from lionagi.ln.concurrency import (
    CancelScope,
    effective_deadline,
    fail_after,
    fail_at,
    move_on_after,
    move_on_at,
)


@pytest.mark.anyio
async def test_fail_after_zero_deadline_raises_fast(anyio_backend):
    t0 = time.perf_counter()
    with pytest.raises(TimeoutError):
        with fail_after(0):
            await anyio.sleep(0.001)
    assert (time.perf_counter() - t0) < 0.5  # should trip reasonably quickly (CI-friendly)


@pytest.mark.anyio
async def test_move_on_after_zero_deadline_sets_flag(anyio_backend):
    with move_on_after(0) as scope:
        await anyio.sleep(0.001)
    assert scope.cancelled_caught is True


@pytest.mark.anyio
async def test_nested_scopes_inner_shielded_outer_cancel(anyio_backend):
    hit = []
    async with anyio.create_task_group() as tg:

        async def worker():
            with CancelScope(shield=True):  # Explicitly shielded
                hit.append("in")
                await anyio.sleep(0.02)
                hit.append("out")

        tg.start_soon(worker)
        await anyio.sleep(0)
        tg.cancel_scope.cancel()

    assert hit == ["in", "out"]  # shield resisted outer cancel


@pytest.mark.anyio
async def test_nested_scopes_fail_after_inside_move_on_after(anyio_backend):
    # Outer move_on_after should swallow, inner fail_after should raise within block.
    with move_on_after(0.1) as outer:
        with pytest.raises(TimeoutError):
            with fail_after(0.01):
                await anyio.sleep(0.05)
    assert outer.cancelled_caught is False  # inner raised before outer deadline


@pytest.mark.anyio
async def test_cancel_scope_alias_is_anyio_cancel_scope(anyio_backend):
    assert CancelScope is anyio.CancelScope


@pytest.mark.anyio
async def test_fail_at_future_and_past_deadlines(anyio_backend):
    now = anyio.current_time()
    # future: should raise inside
    with pytest.raises(TimeoutError):
        with fail_at(now + 0.01):
            await anyio.sleep(0.05)
    # past: immediate failure
    with pytest.raises(TimeoutError):
        with fail_at(now - 1):
            await anyio.sleep(0)


@pytest.mark.anyio
async def test_move_on_at_future_and_past(anyio_backend):
    now = anyio.current_time()
    with move_on_at(now + 0.005) as scope_future:
        await anyio.sleep(0.02)
    assert scope_future.cancelled_caught is True
    with move_on_at(now - 1) as scope_past:
        await anyio.sleep(0)
    assert scope_past.cancelled_caught is True


@pytest.mark.anyio
async def test_effective_deadline_inside_fail_at(anyio_backend):
    deadline = anyio.current_time() + 0.2  # Increased for CI stability
    with move_on_after(0.4):  # ensure outer has more time
        with fail_at(deadline):
            d = effective_deadline()
            assert d is not None
            remaining = d - anyio.current_time()
            # Allow small negative values for timing jitter on slow CI
            assert -0.1 < remaining <= 0.3


@pytest.mark.anyio
async def test_none_timeout_still_cancellable(anyio_backend):
    cancelled = False
    work_started = anyio.Event()

    async def work():
        nonlocal cancelled
        try:
            with fail_after(None):  # No timeout
                work_started.set()
                await anyio.sleep(1.0)
        except BaseException:
            cancelled = True
            raise

    async with anyio.create_task_group() as tg:
        tg.start_soon(work)
        await work_started.wait()  # deterministic: work is inside fail_after
        tg.cancel_scope.cancel()

    assert cancelled is True


@pytest.mark.anyio
async def test_none_move_on_after_still_cancellable(anyio_backend):
    cancelled = False
    work_started = anyio.Event()

    async def work():
        nonlocal cancelled
        try:
            with move_on_after(None) as scope:  # noqa: F841
                work_started.set()
                await anyio.sleep(1.0)
        except BaseException:
            cancelled = True
            raise
        # Should not reach here due to outer cancellation
        assert False, "Should have been cancelled"

    async with anyio.create_task_group() as tg:
        tg.start_soon(work)
        await work_started.wait()  # deterministic: work is inside move_on_after
        tg.cancel_scope.cancel()

    assert cancelled is True


@pytest.mark.anyio
async def test_fail_at_none_still_cancellable(anyio_backend):
    cancelled = False
    work_started = anyio.Event()

    async def work():
        nonlocal cancelled
        try:
            with fail_at(None):  # No deadline
                work_started.set()
                await anyio.sleep(1.0)
        except BaseException:
            cancelled = True
            raise

    async with anyio.create_task_group() as tg:
        tg.start_soon(work)
        await work_started.wait()  # deterministic: work is inside fail_at
        tg.cancel_scope.cancel()

    assert cancelled is True


@pytest.mark.anyio
async def test_move_on_at_none_still_cancellable(anyio_backend):
    cancelled = False
    work_started = anyio.Event()

    async def work():
        nonlocal cancelled
        try:
            with move_on_at(None):  # No deadline
                work_started.set()
                await anyio.sleep(1.0)
        except BaseException:
            cancelled = True
            raise

    async with anyio.create_task_group() as tg:
        tg.start_soon(work)
        await work_started.wait()  # deterministic: work is inside move_on_at
        tg.cancel_scope.cancel()

    assert cancelled is True


@pytest.mark.anyio
async def test_fail_after_none_completes_successfully(anyio_backend):
    completed = False
    with fail_after(None) as scope:
        await anyio.sleep(0.001)
        completed = True
    assert completed is True
    assert scope is not None


@pytest.mark.anyio
async def test_move_on_after_none_completes_successfully(anyio_backend):
    completed = False
    with move_on_after(None) as scope:
        await anyio.sleep(0.001)
        completed = True
    assert completed is True
    assert scope.cancelled_caught is False


@pytest.mark.anyio
async def test_fail_at_none_completes_successfully(anyio_backend):
    completed = False
    with fail_at(None) as scope:
        await anyio.sleep(0.001)
        completed = True
    assert completed is True
    assert scope is not None


@pytest.mark.anyio
async def test_move_on_at_none_completes_successfully(anyio_backend):
    completed = False
    with move_on_at(None) as scope:
        await anyio.sleep(0.001)
        completed = True
    assert completed is True
    assert scope.cancelled_caught is False
