import asyncio
import contextlib
import gc

import anyio
import pytest

from lionagi.ln.concurrency import fail_after, is_cancelled, shield
from lionagi.ln.concurrency.errors import shield_quietly


@pytest.mark.anyio
async def test_shield_propagates_inner_exception(anyio_backend):
    async def bad():
        await anyio.sleep(0)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await shield(bad)


@pytest.mark.anyio
async def test_shield_does_not_block_internal_timeout(anyio_backend):
    async def slow():
        with fail_after(0.01):
            await anyio.sleep(0.1)

    with pytest.raises(TimeoutError):
        await shield(slow)


@pytest.mark.anyio
async def test_is_cancelled_true_for_backend_exception(anyio_backend):
    caught = {}
    victim_started = anyio.Event()

    async def victim():
        try:
            victim_started.set()
            await anyio.sleep(0.1)
        except BaseException as e:
            caught["e"] = e
            raise

    async with anyio.create_task_group() as tg:
        tg.start_soon(victim)
        await victim_started.wait()  # deterministic: victim is sleeping
        tg.cancel_scope.cancel()

    assert "e" in caught and is_cancelled(caught["e"])


@pytest.mark.anyio
async def test_shield_protects_from_external_cancellation(anyio_backend):
    completed = False
    shield_worked = False

    async def protected_work():
        nonlocal completed
        await anyio.sleep(0.005)  # Reduced
        completed = True
        return "done"

    outer_started = anyio.Event()

    async def outer_work():
        nonlocal shield_worked
        try:
            # Shield protects the inner work
            outer_started.set()
            result = await shield(protected_work)
            shield_worked = result == "done"
        except BaseException:
            # Should not get here if shield works
            pass

    async with anyio.create_task_group() as tg:
        tg.start_soon(outer_work)
        await outer_started.wait()  # deterministic: outer_work is running
        tg.cancel_scope.cancel()  # Cancel the group

    # Give shielded work time to complete
    await anyio.sleep(0.01)  # Reduced

    assert completed is True
    assert shield_worked is True


@pytest.mark.anyio
async def test_shield_still_allows_internal_cancellation(anyio_backend):
    async def work_with_timeout():
        with fail_after(0.01):
            await anyio.sleep(1.0)
        return "should_not_reach"

    with pytest.raises(TimeoutError):
        await shield(work_with_timeout)


# shield_quietly keeps asyncio.shield's behaviour for the caller and never hands the
# inner's failure to the loop's exception handler.

_SHIELDS = pytest.mark.parametrize(
    "shield_fn", [asyncio.shield, shield_quietly], ids=["asyncio.shield", "shield_quietly"]
)


@contextlib.contextmanager
def _recording_loop_handler():
    """Replace the running loop's exception handler with one that records its calls."""
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    calls = []
    loop.set_exception_handler(lambda _loop, context: calls.append(context))
    try:
        yield calls
    finally:
        loop.set_exception_handler(previous)


async def _settle():
    """Let the loop run the callbacks that a finished task has queued."""
    for _ in range(10):
        await asyncio.sleep(0)


async def _await_shielded(shield_fn, arg):
    return await shield_fn(arg)


@_SHIELDS
async def test_shield_quietly_result_passes_through(shield_fn):
    async def work():
        await asyncio.sleep(0)
        return "done"

    assert await shield_fn(work()) == "done"
    assert await shield_fn(asyncio.ensure_future(work())) == "done"


@_SHIELDS
async def test_shield_quietly_exception_passes_through(shield_fn):
    async def boom():
        await asyncio.sleep(0)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await shield_fn(boom())
    with pytest.raises(RuntimeError, match="boom"):
        await shield_fn(asyncio.ensure_future(boom()))


@_SHIELDS
async def test_shield_quietly_returns_a_finished_future_unchanged(shield_fn):
    done = asyncio.get_running_loop().create_future()
    done.set_result("ready")

    assert shield_fn(done) is done


@_SHIELDS
async def test_shield_quietly_cancelling_the_caller_leaves_a_task_running(shield_fn):
    started = asyncio.Event()
    release = asyncio.Event()

    async def work():
        started.set()
        await release.wait()
        return "done"

    inner = asyncio.ensure_future(work())
    caller = asyncio.ensure_future(_await_shielded(shield_fn, inner))
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    assert not inner.done()
    release.set()
    assert await inner == "done"


@_SHIELDS
async def test_shield_quietly_cancelling_the_caller_leaves_a_coroutine_running(shield_fn):
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def work():
        started.set()
        await release.wait()
        finished.set()

    caller = asyncio.ensure_future(_await_shielded(shield_fn, work()))
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    assert not finished.is_set()
    release.set()
    await asyncio.wait_for(finished.wait(), timeout=5)


@_SHIELDS
async def test_shield_quietly_cancelling_the_inner_cancels_the_caller(shield_fn):
    started = asyncio.Event()

    async def work():
        started.set()
        await asyncio.Event().wait()

    inner = asyncio.ensure_future(work())
    caller = asyncio.ensure_future(_await_shielded(shield_fn, inner))
    await started.wait()
    inner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller


async def test_shield_quietly_failure_reaching_the_caller_is_not_reported():
    async def boom():
        await asyncio.sleep(0)
        raise RuntimeError("boom")

    with _recording_loop_handler() as calls:
        with pytest.raises(RuntimeError, match="boom"):
            await shield_quietly(boom())
        await _settle()
        gc.collect()

    assert calls == []


@pytest.mark.parametrize("as_task", [False, True], ids=["coroutine", "task"])
async def test_shield_quietly_failure_after_the_caller_was_cancelled_is_not_reported(as_task):
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def fail_later():
        started.set()
        try:
            await release.wait()
            raise RuntimeError("lookup output that must stay out of the log")
        finally:
            finished.set()

    with _recording_loop_handler() as calls:
        arg = asyncio.ensure_future(fail_later()) if as_task else fail_later()
        caller = asyncio.ensure_future(_await_shielded(shield_quietly, arg))
        del arg
        await started.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        del caller

        release.set()
        await asyncio.wait_for(finished.wait(), timeout=5)
        await _settle()
        gc.collect()

    assert calls == []


async def test_shield_quietly_failure_stays_retrievable_after_the_caller_was_cancelled():
    started = asyncio.Event()
    release = asyncio.Event()

    async def fail_later():
        started.set()
        await release.wait()
        raise RuntimeError("late failure")

    inner = asyncio.ensure_future(fail_later())
    caller = asyncio.ensure_future(_await_shielded(shield_quietly, inner))
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    release.set()
    await asyncio.wait({inner})
    with pytest.raises(RuntimeError, match="late failure"):
        await inner
