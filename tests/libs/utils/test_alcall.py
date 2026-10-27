"""Tests for alcall and bcall functions."""

from typing import Any
from unittest.mock import AsyncMock, call, patch

import anyio
import pytest
from anyio.lowlevel import checkpoint
from pydantic import BaseModel

from lionagi.ln import AlcallParams, BcallParams, alcall, bcall
from lionagi.ln.concurrency import BaseExceptionGroup


async def async_func(x: int, add: int = 0) -> int:
    await checkpoint()
    return x + add


def sync_func(x: int, add: int = 0) -> int:
    return x + add


async def async_func_with_error(x: int) -> int:
    await checkpoint()
    if x == 3:
        raise ValueError("mock error")
    return x


def sync_func_with_error(x: int) -> int:
    if x == 3:
        raise ValueError("mock error")
    return x


async def async_func_always_error(x: int) -> int:
    raise RuntimeError(f"Error for {x}")


class PydanticTestModel(BaseModel):
    value: int


class TestAlcallBasic:
    @pytest.mark.anyio
    async def test_alcall_basic_async_function(self):
        inputs = [1, 2, 3]
        results = await alcall(inputs, async_func, add=1)
        assert results == [2, 3, 4]

    @pytest.mark.anyio
    async def test_alcall_basic_sync_function(self):
        inputs = [1, 2, 3]
        results = await alcall(inputs, sync_func)
        assert results == [1, 2, 3]

    @pytest.mark.anyio
    async def test_alcall_empty_input(self):
        results = await alcall([], async_func)
        assert results == []

    @pytest.mark.anyio
    @pytest.mark.parametrize("is_coro", [True, False])
    async def test_alcall_passes_a_keyword_named_index_to_func(self, is_coro):
        async def add_index_async(value: int, *, index: int) -> int:
            return value + index

        def add_index_sync(value: int, *, index: int) -> int:
            return value + index

        func = add_index_async if is_coro else add_index_sync
        assert await alcall([1, 2], func, index=40) == [41, 42]

    @pytest.mark.anyio
    @pytest.mark.parametrize("is_coro", [True, False])
    @pytest.mark.parametrize("keyword", ["limiter", "abandon_on_cancel", "cancellable"])
    async def test_alcall_preserves_offload_named_callable_keywords(self, is_coro, keyword):
        business_value = object()
        ordinary_value = object()

        def sync_target(value, *, ordinary, **kwargs):
            return value, ordinary, kwargs[keyword]

        async def async_target(value, *, ordinary, **kwargs):
            return value, ordinary, kwargs[keyword]

        target = async_target if is_coro else sync_target
        results = await alcall([1, 2], target, ordinary=ordinary_value, **{keyword: business_value})
        assert results == [(1, ordinary_value, business_value), (2, ordinary_value, business_value)]

    @pytest.mark.anyio
    @pytest.mark.parametrize("is_coro", [True, False])
    async def test_alcall_preserves_target_error_with_offload_named_keywords(self, is_coro):
        error = ValueError("target failed")

        def sync_target(value, **kwargs):
            raise error

        async def async_target(value, **kwargs):
            raise error

        target = async_target if is_coro else sync_target
        results = await alcall(
            [1],
            target,
            return_exceptions=True,
            limiter=object(),
            abandon_on_cancel=object(),
            cancellable=object(),
        )
        assert len(results) == 1
        assert results[0] is error


class TestAlcallFuncValidation:
    @pytest.mark.anyio
    async def test_alcall_func_as_list_with_one_callable(self):
        inputs = [1, 2, 3]
        results = await alcall(inputs, [async_func])
        assert results == [1, 2, 3]

    @pytest.mark.anyio
    async def test_alcall_func_as_tuple_with_one_callable(self):
        inputs = [1, 2, 3]
        results = await alcall(inputs, (sync_func,))
        assert results == [1, 2, 3]

    @pytest.mark.anyio
    async def test_alcall_func_not_callable_not_iterable_raises(self):
        with pytest.raises(ValueError, match="func must be callable"):
            await alcall([1, 2, 3], 123)

    @pytest.mark.anyio
    async def test_alcall_func_iterable_with_multiple_callables_raises(self):
        with pytest.raises(ValueError, match="Only one callable"):
            await alcall([1, 2, 3], [async_func, sync_func])

    @pytest.mark.anyio
    async def test_alcall_func_iterable_with_non_callable_raises(self):
        with pytest.raises(ValueError, match="Only one callable"):
            await alcall([1, 2, 3], ["not_callable"])

    @pytest.mark.anyio
    async def test_alcall_func_empty_iterable_raises(self):
        with pytest.raises(ValueError, match="Only one callable"):
            await alcall([1, 2, 3], [])


class TestAlcallInputProcessing:
    @pytest.mark.anyio
    async def test_alcall_input_flatten(self):
        inputs = [[1, 2], [3, 4]]
        results = await alcall(inputs, async_func, input_flatten=True)
        assert results == [1, 2, 3, 4]

    @pytest.mark.anyio
    async def test_alcall_input_dropna(self):
        inputs = [1, None, 2, None, 3]
        results = await alcall(inputs, async_func, input_dropna=True)
        assert results == [1, 2, 3]

    @pytest.mark.anyio
    async def test_alcall_input_pydantic_model(self):
        model = PydanticTestModel(value=5)
        results = await alcall(model, lambda x: x.value * 2)
        assert results == [10]

    @pytest.mark.anyio
    async def test_alcall_input_tuple(self):
        inputs = (1, 2, 3)
        results = await alcall(inputs, async_func)
        assert results == [1, 2, 3]

    @pytest.mark.anyio
    async def test_alcall_input_generator(self):
        inputs = (x for x in [1, 2, 3])
        results = await alcall(inputs, async_func)
        assert results == [1, 2, 3]

    @pytest.mark.anyio
    async def test_alcall_input_range(self):
        inputs = range(3)
        results = await alcall(inputs, async_func)
        assert results == [0, 1, 2]

    @pytest.mark.anyio
    async def test_alcall_input_non_iterable(self):
        result = await alcall(5, async_func)
        assert result == [5]


class TestAlcallRetryTimeout:
    @pytest.mark.anyio
    async def test_alcall_with_retries_async_func(self):
        inputs = [1, 2, 3]
        results = await alcall(
            inputs,
            async_func_with_error,
            retry_attempts=1,
            retry_initial_delay=0,
            retry_default=0,
        )
        assert results == [1, 2, 0]

    @pytest.mark.anyio
    async def test_alcall_with_retries_sync_func(self):
        inputs = [1, 2, 3]
        results = await alcall(
            inputs,
            sync_func_with_error,
            retry_attempts=1,
            retry_initial_delay=0,
            retry_default=0,
        )
        assert results == [1, 2, 0]

    @pytest.mark.anyio
    async def test_alcall_timeout_async_function(self):

        async def slow_async_func(x: int) -> int:
            await anyio.sleep_forever()
            return x

        inputs = [1, 2, 3]
        results = await alcall(
            inputs,
            slow_async_func,
            retry_timeout=0,
            retry_default="timeout",
            retry_attempts=0,
        )
        assert results == ["timeout", "timeout", "timeout"]

    @pytest.mark.anyio
    async def test_alcall_timeout_sync_function(self, monkeypatch):

        def slow_sync_func(x: int) -> int:  # pragma: no cover - replaced at thread seam
            return x

        async def blocked_run_sync(*args, **kwargs):
            await anyio.sleep_forever()

        monkeypatch.setattr("lionagi.ln._async_call.run_sync", blocked_run_sync)

        inputs = [1]  # Single input for faster test
        results = await alcall(
            inputs,
            slow_sync_func,
            retry_timeout=0,
            retry_default="timeout",
            retry_attempts=0,
        )
        assert results == ["timeout"]

    @pytest.mark.anyio
    @pytest.mark.parametrize("is_coro", [True, False])
    async def test_alcall_timeout_without_a_default_raises_timeout_error(
        self, monkeypatch, is_coro
    ):

        async def slow_async_func(x: int) -> int:
            await anyio.sleep_forever()
            return x

        def slow_sync_func(x: int) -> int:  # pragma: no cover - replaced at thread seam
            return x

        # A worker thread is not cancelled at the timeout, so a sync function that
        # finishes late still returns its result. This stand-in for the thread call can
        # be cancelled, which reaches the timeout branch the sync path shares.
        async def blocked_run_sync(*args, **kwargs):
            await anyio.sleep_forever()

        monkeypatch.setattr("lionagi.ln._async_call.run_sync", blocked_run_sync)

        with pytest.raises(TimeoutError, match="timed out after 0s"):
            await alcall(
                [1],
                slow_async_func if is_coro else slow_sync_func,
                retry_timeout=0,
                retry_attempts=0,
            )

    @pytest.mark.anyio
    async def test_alcall_retry_backoff(self):
        with patch("anyio.sleep", new_callable=AsyncMock) as mock_sleep:
            inputs = [3]  # Only one item that triggers error
            await alcall(
                inputs,
                async_func_with_error,
                retry_attempts=2,
                retry_initial_delay=0.1,
                retry_backoff=2,
                retry_default=0,
            )
            assert mock_sleep.await_args_list == [call(0.1), call(0.2)]


class TestAlcallExceptionHandling:
    @pytest.mark.anyio
    async def test_alcall_exception_reraises_after_retry_exhaustion(self):
        attempts = []

        async def always_error(x: int) -> int:
            attempts.append(x)
            raise RuntimeError(f"Error for {x}")

        with pytest.raises(RuntimeError, match="Error for 1"):
            await alcall(
                [1],
                always_error,
                retry_attempts=2,
                retry_initial_delay=0,
            )

        assert attempts == [1, 1, 1]

    @pytest.mark.anyio
    async def test_alcall_concurrent_failures_only_propagate_worker_errors(self):
        with pytest.raises(BaseException) as exc_info:
            await alcall(
                [1, 2, 3],
                async_func_always_error,
                retry_attempts=2,
                retry_initial_delay=0,
            )

        def leaves(exc: BaseException) -> list[BaseException]:
            if isinstance(exc, BaseExceptionGroup):
                return [leaf for child in exc.exceptions for leaf in leaves(child)]
            return [exc]

        propagated = leaves(exc_info.value)
        assert propagated
        assert all(isinstance(exc, RuntimeError) for exc in propagated)
        assert {str(exc) for exc in propagated} <= {
            "Error for 1",
            "Error for 2",
            "Error for 3",
        }

    @pytest.mark.anyio
    async def test_alcall_exception_with_retry_default_no_reraise(self):
        inputs = [1, 2, 3]
        results = await alcall(
            inputs,
            async_func_always_error,
            retry_attempts=2,
            retry_initial_delay=0,
            retry_default="failed",
        )
        assert results == ["failed", "failed", "failed"]


class TestAlcallConcurrency:
    @pytest.mark.anyio
    async def test_alcall_max_concurrent(self):
        inputs = [1, 2, 3, 4, 5]
        results = await alcall(inputs, async_func, max_concurrent=2)
        assert results == [1, 2, 3, 4, 5]

    @pytest.mark.anyio
    async def test_alcall_throttle_period(self):
        inputs = [1, 2, 3]
        results = await alcall(inputs, async_func, throttle_period=0.01)
        assert results == [1, 2, 3]

    @pytest.mark.anyio
    async def test_alcall_delay_before_start(self):
        with patch("anyio.sleep", new_callable=AsyncMock) as mock_sleep:
            inputs = [1, 2, 3]
            await alcall(inputs, async_func, delay_before_start=0.5)
            mock_sleep.assert_any_call(0.5)


class TestAlcallOutputProcessing:
    @pytest.mark.anyio
    async def test_alcall_output_flatten(self):

        async def func_returning_list(x: int) -> list:
            return [x, x * 2]

        inputs = [1, 2, 3]
        results = await alcall(inputs, func_returning_list, output_flatten=True)
        assert results == [1, 2, 2, 4, 3, 6]

    @pytest.mark.anyio
    async def test_alcall_output_dropna(self):

        async def func_with_none(x: int) -> Any:
            return None if x == 2 else x

        inputs = [1, 2, 3]
        results = await alcall(inputs, func_with_none, output_dropna=True)
        assert results == [1, 3]

    @pytest.mark.anyio
    async def test_alcall_output_unique(self):

        async def func_with_duplicates(x: int) -> list:
            return [x, x]

        inputs = [1, 2, 3]
        results = await alcall(
            inputs,
            func_with_duplicates,
            output_flatten=True,
            output_unique=True,
        )
        assert sorted(results) == [1, 2, 3]


class TestBcall:
    @pytest.mark.anyio
    async def test_bcall_basic(self):
        inputs = [1, 2, 3, 4, 5]
        batches = []
        async for batch in bcall(inputs, async_func, batch_size=2):
            batches.append(batch)
        assert batches == [[1, 2], [3, 4], [5]]

    @pytest.mark.anyio
    async def test_bcall_with_retries(self):
        inputs = [1, 2, 3, 4, 5]
        batches = []
        async for batch in bcall(
            inputs,
            async_func_with_error,
            batch_size=2,
            retry_attempts=1,
            retry_initial_delay=0,
            retry_default=0,
        ):
            batches.append(batch)
        assert batches == [[1, 2], [0, 4], [5]]

    @pytest.mark.anyio
    async def test_bcall_with_kwargs(self):
        inputs = [1, 2, 3, 4, 5]
        batches = []
        async for batch in bcall(inputs, async_func, batch_size=2, add=10):
            batches.append(batch)
        assert batches == [[11, 12], [13, 14], [15]]

    @pytest.mark.anyio
    async def test_bcall_with_all_options(self):
        inputs = [1, 2, 3, 4, 5]
        batches = []
        async for batch in bcall(
            inputs,
            async_func,
            batch_size=2,
            input_flatten=False,
            output_flatten=False,
            max_concurrent=2,
            throttle_period=0.01,
        ):
            batches.append(batch)
        assert batches == [[1, 2], [3, 4], [5]]


class TestParams:
    # AlcallParams/BcallParams.__call__ are thin alcall/bcall wrappers; dataclass inheritance
    # makes them hard to unit-test directly — coverage comes from the alcall tests above.

    @pytest.mark.anyio
    async def test_alcall_params_concept(self):
        # Verify the class exists and has correct structure
        assert hasattr(AlcallParams, "__call__")
        assert hasattr(AlcallParams, "_func")
        # Lines 297-298 covered conceptually through alcall tests

    @pytest.mark.anyio
    async def test_bcall_params_concept(self):
        # Verify the class exists and has correct structure
        assert hasattr(BcallParams, "__call__")
        assert hasattr(BcallParams, "_func")
        assert hasattr(BcallParams, "__annotations__")
        assert "batch_size" in BcallParams.__annotations__
        # Lines 310-311 covered conceptually through bcall tests


class TestEdgeCases:
    @pytest.mark.anyio
    async def test_alcall_combined_input_output_processing(self):

        async def func_returning_list(x: int) -> list:
            return [x, x * 2]

        inputs = [[1, 2], [3, 4]]
        results = await alcall(
            inputs,
            func_returning_list,
            input_flatten=True,
            output_flatten=True,
        )
        assert results == [1, 2, 2, 4, 3, 6, 4, 8]

    @pytest.mark.anyio
    async def test_alcall_with_both_flatten_and_unique(self):

        async def func_with_duplicates(x: int) -> list:
            return [x, x, x + 1]

        inputs = [1, 2, 3]
        results = await alcall(
            inputs,
            func_with_duplicates,
            output_flatten=True,
            output_unique=True,
        )
        assert sorted(results) == [1, 2, 3, 4]

    @pytest.mark.anyio
    async def test_alcall_max_concurrent_with_throttle(self):
        inputs = [1, 2, 3, 4, 5]
        results = await alcall(
            inputs,
            async_func,
            max_concurrent=2,
            throttle_period=0.01,
        )
        assert results == [1, 2, 3, 4, 5]


class TestReturnExceptions:
    @pytest.mark.anyio
    async def test_return_exceptions_collects_errors(self):

        async def maybe_fail(x: int) -> int:
            if x == 2:
                raise ValueError("fail on 2")
            return x * 10

        results = await alcall([1, 2, 3], maybe_fail, return_exceptions=True)
        assert results[0] == 10
        assert isinstance(results[1], ValueError)
        assert results[2] == 30

    @pytest.mark.anyio
    async def test_return_exceptions_preserves_order(self):

        async def flaky(x: int) -> int:
            if x % 2 == 0:
                raise RuntimeError(f"err-{x}")
            return x

        results = await alcall([1, 2, 3, 4, 5], flaky, return_exceptions=True)
        assert results[0] == 1
        assert isinstance(results[1], RuntimeError)
        assert results[2] == 3
        assert isinstance(results[3], RuntimeError)
        assert results[4] == 5

    @pytest.mark.anyio
    async def test_return_exceptions_false_raises(self):

        async def fail(x: int) -> int:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await alcall([1], fail, return_exceptions=False)

    @pytest.mark.anyio
    async def test_return_exceptions_all_succeed(self):
        results = await alcall([1, 2, 3], async_func, return_exceptions=True)
        assert results == [1, 2, 3]

    @pytest.mark.anyio
    async def test_return_exceptions_preserves_child_cancel_payload(self):
        import asyncio

        async def work(x: int) -> int:
            async def nested():
                raise asyncio.CancelledError("nested-child-cancel")

            await nested()
            return x

        results = await alcall([0], work, return_exceptions=True)
        assert type(results[0]) is asyncio.CancelledError
        assert results[0].args == ("nested-child-cancel",)

    # asyncio only: drives the cancellation with asyncio tasks and events.
    @pytest.mark.asyncio
    async def test_default_path_failure_cancels_running_siblings(self):
        import asyncio

        started = asyncio.Event()
        peer_cancelled = asyncio.Event()

        async def work(x: int) -> int:
            if x == 0:
                await started.wait()
                raise ValueError("boom")
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                peer_cancelled.set()
                raise
            return x

        with pytest.raises(ValueError, match="boom"):
            await alcall([0, 1], work)
        assert peer_cancelled.is_set()

    # asyncio only: drives the cancellation with asyncio tasks and events.
    @pytest.mark.asyncio
    async def test_default_path_child_self_cancel_matches_task_group_shape(self):
        import asyncio

        started = asyncio.Event()
        peer_cancelled = asyncio.Event()

        async def work(x: int) -> int:
            if x == 0:
                await started.wait()
                raise asyncio.CancelledError("child cancelled")
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                peer_cancelled.set()
                raise
            return x

        results = await alcall([0, 1], work)
        assert peer_cancelled.is_set()
        assert all(isinstance(r, asyncio.CancelledError) for r in results)

    # asyncio only: drives the cancellation with asyncio tasks and events.
    @pytest.mark.asyncio
    async def test_default_path_external_cancellation_propagates(self):
        import asyncio

        child_cancelled = asyncio.Event()
        all_children_started = asyncio.Event()
        started = 0

        async def work(x: int) -> int:
            nonlocal started
            started += 1
            if started == 2:
                all_children_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                child_cancelled.set()
                raise
            return x

        task = asyncio.ensure_future(alcall([1, 2], work))
        await all_children_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert child_cancelled.is_set()


def test_alcall_input_unique_without_flatten_raises():
    """alcall input_unique without flatten raises the documented validation error."""

    async def _run():
        return await alcall([1, 1], lambda x: x, input_unique=True)

    with pytest.raises(ValueError, match="unique=True requires flatten=True"):
        anyio.run(_run)
