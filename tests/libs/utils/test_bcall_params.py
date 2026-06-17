# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Behavior tests for BcallParams.__call__.

BcallParams.__call__ previously failed in two ways:
1. bcall() is an async generator — you cannot await it directly.
2. batch_size was passed both positionally (self.batch_size) and via
   default_kw() → TypeError: multiple values for argument 'batch_size'.

Fix: pop 'batch_size' from kwargs to avoid the duplicate, then collect
all batches from the async generator into a flat list.
"""

import pytest

from lionagi.ln import BcallParams

# ---------------------------------------------------------------------------
# Basic functionality
# ---------------------------------------------------------------------------


class TestBcallParamsCall:
    @pytest.mark.anyio
    async def test_bcall_params_collects_all_batches(self):
        """BcallParams(batch_size=2) on [1..5] should return flattened list."""

        async def double(x: int) -> int:
            return x * 2

        params = BcallParams(batch_size=2)
        result = await params([1, 2, 3, 4, 5], double)
        assert sorted(result) == [2, 4, 6, 8, 10]

    @pytest.mark.anyio
    async def test_bcall_params_batch_size_1(self):
        """batch_size=1 processes each item individually."""

        async def identity(x):
            return x

        params = BcallParams(batch_size=1)
        result = await params([10, 20, 30], identity)
        assert sorted(result) == [10, 20, 30]

    @pytest.mark.anyio
    async def test_bcall_params_batch_size_larger_than_input(self):
        """batch_size larger than input length yields one batch."""

        async def triple(x: int) -> int:
            return x * 3

        params = BcallParams(batch_size=100)
        result = await params([1, 2, 3], triple)
        assert sorted(result) == [3, 6, 9]

    @pytest.mark.anyio
    async def test_bcall_params_empty_input(self):
        """Empty input returns empty list."""

        async def noop(x):
            return x

        params = BcallParams(batch_size=2)
        result = await params([], noop)
        assert result == []

    @pytest.mark.anyio
    async def test_bcall_params_with_extra_kwargs(self):
        """Extra kwargs are forwarded to the wrapped function."""

        async def add(x: int, *, offset: int = 0) -> int:
            return x + offset

        params = BcallParams(batch_size=2)
        result = await params([1, 2, 3], add, offset=10)
        assert sorted(result) == [11, 12, 13]

    @pytest.mark.anyio
    async def test_bcall_params_no_duplicate_batch_size_error(self):
        """batch_size must not be double-passed to bcall (regression guard)."""

        async def identity(x):
            return x

        params = BcallParams(batch_size=3)
        # This must not raise TypeError about multiple values for batch_size
        result = await params([1, 2, 3, 4, 5, 6], identity)
        assert len(result) == 6

    @pytest.mark.anyio
    async def test_bcall_params_sync_func(self):
        """Sync functions are also handled by bcall/alcall underneath."""
        params = BcallParams(batch_size=2)

        def double(x):
            return x * 2

        result = await params([1, 2, 3, 4], double)
        assert sorted(result) == [2, 4, 6, 8]
