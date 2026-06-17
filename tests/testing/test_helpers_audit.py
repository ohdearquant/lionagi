# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio

import pytest

# ---------------------------------------------------------------------------
# AsyncTestHelpers Python 3.10 compatibility (no asyncio.timeout)
# ---------------------------------------------------------------------------


class TestAsyncTestHelpersPy310:
    async def test_collect_async_results_returns_items(self):
        from lionagi.testing.helpers import AsyncTestHelpers

        async def _gen():
            for i in range(3):
                yield i

        results = await AsyncTestHelpers.collect_async_results(_gen(), timeout=5.0)
        assert results == [0, 1, 2]

    async def test_collect_async_results_respects_limit(self):
        from lionagi.testing.helpers import AsyncTestHelpers

        async def _gen():
            for i in range(100):
                yield i

        results = await AsyncTestHelpers.collect_async_results(_gen(), limit=5, timeout=5.0)
        assert len(results) == 5

    async def test_collect_async_results_times_out_without_error(self):
        from lionagi.testing.helpers import AsyncTestHelpers

        collected = []

        async def _slow_gen():
            for i in range(10):
                collected.append(i)
                yield i
                await asyncio.sleep(0.5)  # each item takes 0.5 s

        # timeout is 0.2 s — we expect at most one item before timeout
        results = await AsyncTestHelpers.collect_async_results(_slow_gen(), timeout=0.2)
        assert len(results) <= 2, f"Too many items collected before timeout: {results}"

    async def test_run_with_timeout_returns_result(self):
        from lionagi.testing.helpers import AsyncTestHelpers

        async def _fn():
            return 42

        result = await AsyncTestHelpers.run_with_timeout(_fn, timeout=5.0)
        assert result == 42

    async def test_run_with_timeout_raises_on_expiry(self):
        from lionagi.testing.helpers import AsyncTestHelpers

        async def _slow():
            await asyncio.sleep(10)

        with pytest.raises(TimeoutError):
            await AsyncTestHelpers.run_with_timeout(_slow, timeout=0.05)

    async def test_wait_for_all_returns_results(self):
        from lionagi.testing.helpers import AsyncTestHelpers

        async def _one(n: int) -> int:
            await asyncio.sleep(0)
            return n

        tasks = [asyncio.create_task(_one(i)) for i in range(3)]
        results = await AsyncTestHelpers.wait_for_all(tasks, timeout=5.0)
        assert sorted(results) == [0, 1, 2]

    async def test_wait_for_all_cancels_tasks_on_timeout(self):
        from lionagi.testing.helpers import AsyncTestHelpers

        async def _forever() -> int:
            await asyncio.sleep(9999)
            return 0

        tasks = [asyncio.create_task(_forever())]
        with pytest.raises(TimeoutError):
            await AsyncTestHelpers.wait_for_all(tasks, timeout=0.05)

        # Task must be cancelled
        await asyncio.sleep(0)
        assert tasks[0].cancelled() or tasks[0].done()


# ---------------------------------------------------------------------------
# TestDataLoader path traversal boundary
# ---------------------------------------------------------------------------


class TestDataLoaderPathBoundary:
    """TestDataLoader.load_json must not escape data_dir via path traversal."""

    def test_traversal_with_dotdot_is_rejected(self, tmp_path):
        from lionagi.testing.loaders import TestDataLoader

        # Create a data dir inside tmp_path and a file outside it
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        outside = tmp_path / "secret.json"
        outside.write_text('{"secret": true}')

        loader = TestDataLoader(data_dir=data_dir)

        with pytest.raises((ValueError, PermissionError)):
            loader.load_json("../secret")

    def test_absolute_path_is_rejected(self, tmp_path):
        from lionagi.testing.loaders import TestDataLoader

        data_dir = tmp_path / "data"
        data_dir.mkdir()

        loader = TestDataLoader(data_dir=data_dir)

        with pytest.raises(ValueError, match="plain name"):
            loader.load_json("/etc/passwd")

    def test_forward_slash_in_name_is_rejected(self, tmp_path):
        from lionagi.testing.loaders import TestDataLoader

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        loader = TestDataLoader(data_dir=data_dir)

        with pytest.raises(ValueError, match="plain name"):
            loader.load_json("subdir/secret")

    def test_valid_filename_is_loaded(self, tmp_path):
        from lionagi.testing.loaders import TestDataLoader

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        fixture = data_dir / "fixture.json"
        fixture.write_text('{"key": "value"}')

        loader = TestDataLoader(data_dir=data_dir)
        result = loader.load_json("fixture")
        assert result == {"key": "value"}

    def test_missing_file_raises_file_not_found(self, tmp_path):
        from lionagi.testing.loaders import TestDataLoader

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        loader = TestDataLoader(data_dir=data_dir)

        with pytest.raises(FileNotFoundError):
            loader.load_json("nonexistent")
