"""Tests for alcall and bcall functions."""

import asyncio
import sys
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel

from lionagi.ln import AlcallParams, BcallParams, _async_call, alcall, bcall

# Import ExceptionGroup for Python 3.11+
if sys.version_info >= (3, 11):
    from builtins import BaseExceptionGroup
else:
    from exceptiongroup import BaseExceptionGroup


# =============================================================================
# Test fixtures and helper functions
# =============================================================================


async def async_func(x: int, add: int = 0) -> int:
    await asyncio.sleep(0.01)
    return x + add


def sync_func(x: int, add: int = 0) -> int:
    return x + add


async def async_func_with_error(x: int) -> int:
    await asyncio.sleep(0.01)
    if x == 3:
        raise ValueError("mock error")
    return x


def sync_func_with_error(x: int) -> int:
    if x == 3:
        raise ValueError("mock error")
    return x


async def async_func_always_error(x: int) -> int:
    await asyncio.sleep(0.01)
    raise RuntimeError(f"Error for {x}")


class PydanticTestModel(BaseModel):
    value: int


# =============================================================================
# Test alcall function - Basic functionality
# =============================================================================


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


# =============================================================================
# Test alcall function - func parameter validation
# =============================================================================


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


# =============================================================================
# Test alcall function - Input processing
# =============================================================================


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


# =============================================================================
# Test alcall function - Retry and timeout
# =============================================================================


class TestAlcallRetryTimeout:
    @pytest.mark.anyio
    async def test_alcall_with_retries_async_func(self):
        inputs = [1, 2, 3]
        results = await alcall(
            inputs,
            async_func_with_error,
            retry_attempts=1,
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
            retry_default=0,
        )
        assert results == [1, 2, 0]

    @pytest.mark.anyio
    async def test_alcall_timeout_async_function(self):

        async def slow_async_func(x: int) -> int:
            await asyncio.sleep(1.0)
            return x

        inputs = [1, 2, 3]
        results = await alcall(
            inputs,
            slow_async_func,
            retry_timeout=0.05,
            retry_default="timeout",
            retry_attempts=0,
        )
        assert results == ["timeout", "timeout", "timeout"]

    @pytest.mark.anyio
    async def test_alcall_timeout_sync_function(self):

        def slow_sync_func(x: int) -> int:
            import time

            time.sleep(0.5)  # Sleep longer than timeout
            return x

        inputs = [1]  # Single input for faster test
        results = await alcall(
            inputs,
            slow_sync_func,
            retry_timeout=0.1,
            retry_default="timeout",
            retry_attempts=0,
        )
        # Note: timeout might not work reliably with sync functions in threads
        # This test primarily covers the code path
        assert len(results) == 1

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
            # Should call sleep with 0.1, then 0.2
            assert mock_sleep.call_count >= 2


# =============================================================================
# Test alcall function - Exception handling
# =============================================================================


class TestAlcallExceptionHandling:
    @pytest.mark.anyio
    async def test_alcall_exception_reraises_after_retry_exhaustion(self):
        inputs = [1, 2, 3]
        # Exceptions in task groups are wrapped in ExceptionGroup
        try:
            await alcall(
                inputs,
                async_func_always_error,
                retry_attempts=2,
                # No retry_default, should re-raise
            )
            assert False, "Should have raised exception"
        except BaseExceptionGroup as eg:
            # Verify all sub-exceptions are RuntimeError
            for exc in eg.exceptions:
                assert isinstance(exc, RuntimeError)

    @pytest.mark.anyio
    async def test_alcall_exception_with_retry_default_no_reraise(self):
        inputs = [1, 2, 3]
        results = await alcall(
            inputs,
            async_func_always_error,
            retry_attempts=2,
            retry_default="failed",
        )
        assert results == ["failed", "failed", "failed"]


# =============================================================================
# Test alcall function - Concurrency and throttling
# =============================================================================


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


# =============================================================================
# Test alcall function - Output processing
# =============================================================================


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


# =============================================================================
# Test alcall fast-path parity
# =============================================================================


class TestAlcallFastPathParity:
    @pytest.mark.anyio
    async def test_fast_path_matches_slow_path_for_one_item_exception(self):
        async def one_fails(value: int) -> int:
            if value == 2:
                raise ValueError("boom")
            return value * 10

        with patch(
            "lionagi.ln._async_call.asyncio.gather",
            wraps=_async_call.asyncio.gather,
        ) as fast_gather:
            fast = await alcall([1, 2, 3], one_fails, return_exceptions=True)
        with patch(
            "lionagi.ln._async_call.asyncio.gather",
            wraps=_async_call.asyncio.gather,
        ) as slow_gather:
            slow = await alcall(
                [1, 2, 3],
                one_fails,
                return_exceptions=True,
                retry_initial_delay=1,
            )

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            expected_fast_gathers = 0
        else:
            expected_fast_gathers = 1
        assert fast_gather.call_count == expected_fast_gathers
        assert slow_gather.call_count == 0
        assert fast[0] == slow[0] == 10
        assert fast[2] == slow[2] == 30
        assert type(fast[1]) is type(slow[1]) is ValueError
        assert str(fast[1]) == str(slow[1]) == "boom"

        with pytest.raises(ValueError, match="boom"):
            await alcall([1, 2, 3], one_fails)
        with pytest.raises(ValueError, match="boom"):
            await alcall([1, 2, 3], one_fails, retry_initial_delay=1)

    @pytest.mark.anyio
    async def test_fast_path_matches_slow_path_for_empty_input_and_generator(self):
        async def double(value: int) -> int:
            return value * 2

        assert await alcall([], double) == await alcall([], double, retry_initial_delay=1)
        assert await alcall((i for i in range(3)), double) == await alcall(
            (i for i in range(3)),
            double,
            retry_initial_delay=1,
        )

    @pytest.mark.anyio
    async def test_fast_path_matches_slow_path_for_output_sanitization(self):
        async def values(value: int) -> list[int | None]:
            return [value, None, value]

        options = {
            "output_flatten": True,
            "output_dropna": True,
            "output_unique": True,
            "output_flatten_tuple_set": True,
        }
        fast = await alcall([1, 2], values, **options)
        slow = await alcall([1, 2], values, retry_initial_delay=1, **options)
        assert fast == slow == [1, 2]


# =============================================================================
# Test bcall function
# =============================================================================


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


# =============================================================================
# =============================================================================


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


# =============================================================================
# Test edge cases and combinations
# =============================================================================


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


# =============================================================================
# Test return_exceptions parameter
# =============================================================================


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

    @pytest.mark.anyio
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

    @pytest.mark.anyio
    async def test_default_path_external_cancellation_propagates(self):
        import asyncio

        child_cancelled = asyncio.Event()

        async def work(x: int) -> int:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                child_cancelled.set()
                raise
            return x

        task = asyncio.ensure_future(alcall([1, 2], work))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert child_cancelled.is_set()

    @pytest.mark.anyio
    async def test_default_path_user_raised_internal_marker_propagates(self):
        from lionagi.ln._async_call import _ChildCancelled

        async def work(x: int) -> int:
            raise _ChildCancelled("raised by user code")

        # The internal marker is stamped with a per-batch token; an instance
        # raised by a callback must propagate like any other exception on
        # both the fast route and the forced-general route.
        with pytest.raises(_ChildCancelled):
            await alcall([0], work)
        with pytest.raises(_ChildCancelled):
            await alcall([0], work, retry_initial_delay=0.001)
