import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from lionagi.ln import alcall, lcall


async def mock_func(x: int, add: int = 0) -> int:
    await asyncio.sleep(0.1)
    return x + add


async def mock_func_with_error(x: int) -> int:
    await asyncio.sleep(0.1)
    if x == 3:
        raise ValueError("mock error")
    return x


async def mock_handler(e: Exception) -> str:
    return f"handled: {str(e)}"


class TestLCallFunction(unittest.IsolatedAsyncioTestCase):
    async def test_lcall_basic(self):
        inputs = [1, 2, 3]
        results = await alcall(inputs, mock_func, add=1)
        self.assertEqual(results, [2, 3, 4])

    async def test_lcall_with_retries(self):
        inputs = [1, 2, 3]
        results = await alcall(inputs, mock_func_with_error, retry_attempts=1, retry_default=0)
        self.assertEqual(results, [1, 2, 0])

    async def test_lcall_with_timeout(self):
        inputs = [1, 2, 3]
        # With timeout of 0.05s and mock_func sleeping for 0.1s, all should timeout
        results = await alcall(
            inputs,
            mock_func,
            retry_timeout=0.05,
            retry_default="timeout",
            retry_attempts=0,
        )
        self.assertEqual(results, ["timeout", "timeout", "timeout"])

    async def test_lcall_with_max_concurrent(self):
        inputs = [1, 2, 3]
        results = await alcall(inputs, mock_func, max_concurrent=1)
        self.assertEqual(results, [1, 2, 3])

    async def test_lcall_with_throttle_period(self):
        inputs = [1, 2, 3]
        results = await alcall(inputs, mock_func, throttle_period=0.2)
        self.assertEqual(results, [1, 2, 3])

    # test_lcall_with_timing removed - retry_timing parameter no longer exists

    async def test_lcall_with_dropna(self):
        async def func(x: int) -> Any:
            return None if x == 2 else x

        inputs = [1, 2, 3]
        results = await alcall(inputs, func, output_dropna=True)
        self.assertEqual(results, [1, 3])

    async def test_lcall_with_backoff_factor(self):
        inputs = [1, 2, 3]
        with patch("anyio.sleep", new_callable=AsyncMock) as mock_sleep:
            await alcall(
                inputs,
                mock_func_with_error,
                retry_attempts=2,
                retry_initial_delay=0.1,
                retry_backoff=2,
                retry_default=0,
            )
            mock_sleep.assert_any_call(0.1)
            mock_sleep.assert_any_call(0.2)


# =============================================================================
# Synchronous lcall Tests (for improved coverage)
# =============================================================================


class TestLCallSyncFunction:
    @pytest.mark.unit
    def test_lcall_basic_usage(self):
        result = lcall([1, 2, 3], lambda x: x * 2)
        assert result == [2, 4, 6]

    @pytest.mark.unit
    def test_lcall_func_not_callable_raises(self):
        with pytest.raises(ValueError, match="exactly one callable"):
            lcall([1, 2, 3], "not_callable")

    @pytest.mark.unit
    def test_lcall_func_iterable_with_one_callable(self):
        result = lcall([1, 2, 3], [lambda x: x * 2])
        assert result == [2, 4, 6]

    @pytest.mark.unit
    def test_lcall_func_iterable_with_multiple_callables_raises(self):
        with pytest.raises(ValueError, match="exactly one callable"):
            lcall([1, 2, 3], [lambda x: x * 2, lambda x: x * 3])

    @pytest.mark.unit
    def test_lcall_func_iterable_with_non_callable_raises(self):
        with pytest.raises(ValueError, match="exactly one callable"):
            lcall([1, 2, 3], ["not_callable"])

    @pytest.mark.unit
    def test_lcall_func_non_iterable_non_callable_raises(self):
        with pytest.raises(ValueError, match="func must be callable"):
            lcall([1, 2, 3], 123)

    @pytest.mark.unit
    def test_lcall_output_unique_without_flatten_raises(self):
        with pytest.raises(ValueError, match="unique_output requires"):
            lcall([1, 2, 3], lambda x: x, output_unique=True)

    @pytest.mark.unit
    def test_lcall_output_unique_with_flatten(self):
        result = lcall(
            [[1, 2], [2, 3], [3, 4]],
            lambda x: x,
            output_flatten=True,
            output_unique=True,
        )
        assert set(result) == {1, 2, 3, 4}
        assert len(result) == 4  # No duplicates

    @pytest.mark.unit
    def test_lcall_output_unique_with_dropna(self):
        result = lcall(
            [1, 2, 3],
            lambda x: [x, x] if x != 2 else None,
            output_flatten=True,
            output_dropna=True,
            output_unique=True,
        )
        # After dropna: [[1, 1], [3, 3]], after flatten: [1, 1, 3, 3], after unique: [1, 3]
        assert sorted(result) == [1, 3]
        assert len(result) == 2

    @pytest.mark.unit
    def test_lcall_interrupted_error_returns_partial(self):

        def func(x):
            if x == 2:
                raise InterruptedError()
            return x * 2

        result = lcall([1, 2, 3], func)
        assert result == [2]  # Only first result before interruption

    @pytest.mark.unit
    def test_lcall_exception_propagates(self):

        def func(x):
            if x == 2:
                raise ValueError("boom")
            return x

        with pytest.raises(ValueError, match="boom"):
            lcall([1, 2, 3], func)

    @pytest.mark.unit
    def test_lcall_input_flatten(self):
        result = lcall(
            [[1, 2], [3, 4]],
            lambda x: x * 2,
            input_flatten=True,
        )
        assert result == [2, 4, 6, 8]

    @pytest.mark.unit
    def test_lcall_input_dropna(self):
        result = lcall(
            [1, None, 2, None, 3],
            lambda x: x * 2,
            input_dropna=True,
        )
        assert result == [2, 4, 6]

    @pytest.mark.unit
    def test_lcall_non_list_input_conversion(self):
        # Tuple input
        result = lcall((1, 2, 3), lambda x: x * 2)
        assert result == [2, 4, 6]

        # Generator input
        result = lcall((x for x in range(3)), lambda x: x * 2)
        assert result == [0, 2, 4]

        # Range input
        result = lcall(range(3), lambda x: x * 2)
        assert result == [0, 2, 4]

    @pytest.mark.unit
    def test_lcall_non_iterable_input_single_element(self):
        result = lcall(5, lambda x: x * 2)
        assert result == [10]

    @pytest.mark.unit
    def test_lcall_with_args_kwargs(self):

        def func(x, add, multiply=1):
            return (x + add) * multiply

        result = lcall([1, 2, 3], func, 10, multiply=2)
        assert result == [22, 24, 26]

    @pytest.mark.unit
    def test_lcall_output_flatten(self):

        def func(x):
            return [x, x * 2]

        result = lcall([1, 2, 3], func, output_flatten=True)
        assert result == [1, 2, 2, 4, 3, 6]

    @pytest.mark.unit
    def test_lcall_output_dropna(self):

        def func(x):
            return None if x == 2 else x

        result = lcall([1, 2, 3], func, output_dropna=True)
        assert result == [1, 3]

    @pytest.mark.property
    @given(
        values=st.lists(st.integers(), min_size=0, max_size=20),
        input_flatten=st.booleans(),
        output_flatten=st.booleans(),
    )
    @settings(max_examples=30)
    def test_lcall_flatten_options_property(self, values, input_flatten, output_flatten):
        result = lcall(
            values,
            lambda x: x * 2,
            input_flatten=input_flatten,
            output_flatten=output_flatten,
        )
        assert isinstance(result, list)
        if not input_flatten and not output_flatten:
            assert len(result) == len(values)

    @pytest.mark.property
    @given(values=st.lists(st.integers(min_value=0, max_value=100), max_size=15))
    @settings(max_examples=20)
    def test_lcall_preserves_order_property(self, values):

        def double(x):
            return x * 2

        result = lcall(values, double)
        expected = [double(v) for v in values]
        assert result == expected

    @pytest.mark.unit
    def test_lcall_empty_input(self):
        result = lcall([], lambda x: x * 2)
        assert result == []

    @pytest.mark.unit
    def test_lcall_input_unique_with_flatten(self):
        result = lcall(
            [[1, 2], [2, 3], [3, 4]],
            lambda x: x * 2,
            input_flatten=True,
            input_unique=True,
        )
        # Unique input: [1, 2, 3, 4] -> doubled: [2, 4, 6, 8]
        assert sorted(result) == [2, 4, 6, 8]
        assert len(result) == 4


if __name__ == "__main__":
    unittest.main()
