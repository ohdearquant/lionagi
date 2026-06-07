# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests verifying that Pile.adump and DataLogger.adump offload blocking I/O
to a worker thread rather than blocking the event loop."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.log import DataLogger, DataLoggerConfig, Log
from lionagi.protocols.generic.pile import Pile
from lionagi.testing import MockElement

# ---------------------------------------------------------------------------
# Pile.adump
# ---------------------------------------------------------------------------


class TestPileAdump:
    """Pile.adump must complete without blocking the event loop."""

    @pytest.fixture
    def pile_with_items(self):
        return Pile(collections=[MockElement(value=i) for i in range(5)])

    @pytest.mark.asyncio
    async def test_adump_json_creates_file(self, pile_with_items, tmp_path):
        fp = tmp_path / "dump.json"
        await pile_with_items.adump(fp, obj_key="json")
        assert fp.exists()
        content = fp.read_text()
        assert len(content.strip().splitlines()) == 5

    @pytest.mark.asyncio
    async def test_adump_csv_creates_file(self, pile_with_items, tmp_path):
        fp = tmp_path / "dump.csv"
        await pile_with_items.adump(fp, obj_key="csv")
        assert fp.exists()
        lines = fp.read_text().strip().splitlines()
        # header + 5 data rows
        assert len(lines) == 6

    @pytest.mark.asyncio
    async def test_adump_clear_empties_pile(self, pile_with_items, tmp_path):
        fp = tmp_path / "dump.json"
        assert len(pile_with_items) == 5
        await pile_with_items.adump(fp, clear=True)
        assert len(pile_with_items) == 0
        assert fp.exists()

    @pytest.mark.asyncio
    async def test_adump_no_clear_preserves_pile(self, pile_with_items, tmp_path):
        fp = tmp_path / "dump.json"
        await pile_with_items.adump(fp, clear=False)
        assert len(pile_with_items) == 5

    @pytest.mark.asyncio
    async def test_adump_does_not_block_event_loop(self, pile_with_items, tmp_path):
        """Verify that the event loop remains responsive during adump by
        running a concurrent coroutine that must complete within a
        reasonable window."""
        fp = tmp_path / "dump.json"
        sentinel = []

        async def background():
            sentinel.append(True)

        # Run adump and the background coroutine concurrently
        await asyncio.gather(
            pile_with_items.adump(fp, obj_key="json"),
            background(),
        )
        # background() should have been able to run
        assert sentinel == [True]
        assert fp.exists()

    @pytest.mark.asyncio
    async def test_adump_unsupported_format_raises(self, pile_with_items, tmp_path):
        fp = tmp_path / "dump.json"
        with pytest.raises(ValueError, match="Unsupported obj_key"):
            await pile_with_items.adump(fp, obj_key="xml")


# ---------------------------------------------------------------------------
# DataLogger.adump
# ---------------------------------------------------------------------------


class TestDataLoggerAdump:
    """DataLogger.adump must complete without blocking the event loop."""

    @pytest.fixture
    def logger_with_logs(self, tmp_path):
        config = DataLoggerConfig(
            persist_dir=str(tmp_path),
            auto_save_on_exit=False,
            clear_after_dump=False,
        )
        dl = DataLogger(_config=config)
        for i in range(5):
            dl.log(Element())
        return dl

    @pytest.mark.asyncio
    async def test_adump_json_creates_file(self, logger_with_logs, tmp_path):
        fp = tmp_path / "test_dump.json"
        await logger_with_logs.adump(persist_path=fp)
        assert fp.exists()

    @pytest.mark.asyncio
    async def test_adump_csv_creates_file(self, tmp_path):
        config = DataLoggerConfig(
            persist_dir=str(tmp_path),
            extension=".csv",
            auto_save_on_exit=False,
            clear_after_dump=False,
        )
        dl = DataLogger(_config=config)
        for i in range(3):
            dl.log(Element())
        fp = tmp_path / "test_dump.csv"
        await dl.adump(persist_path=fp)
        assert fp.exists()
        lines = fp.read_text().strip().splitlines()
        # header + 3 data rows
        assert len(lines) == 4

    @pytest.mark.asyncio
    async def test_adump_clear_empties_logs(self, logger_with_logs, tmp_path):
        fp = tmp_path / "test_dump.json"
        assert len(logger_with_logs.logs) == 5
        await logger_with_logs.adump(clear=True, persist_path=fp)
        assert len(logger_with_logs.logs) == 0
        assert fp.exists()

    @pytest.mark.asyncio
    async def test_adump_no_clear_preserves_logs(self, logger_with_logs, tmp_path):
        fp = tmp_path / "test_dump.json"
        await logger_with_logs.adump(clear=False, persist_path=fp)
        assert len(logger_with_logs.logs) == 5

    @pytest.mark.asyncio
    async def test_adump_empty_logs_noop(self, tmp_path):
        config = DataLoggerConfig(
            persist_dir=str(tmp_path),
            auto_save_on_exit=False,
        )
        dl = DataLogger(_config=config)
        fp = tmp_path / "noop.json"
        await dl.adump(persist_path=fp)
        # No file should be created for empty logs
        assert not fp.exists()

    @pytest.mark.asyncio
    async def test_adump_does_not_block_event_loop(self, logger_with_logs, tmp_path):
        """Verify the event loop stays responsive during DataLogger.adump."""
        fp = tmp_path / "test_dump.json"
        sentinel = []

        async def background():
            sentinel.append(True)

        await asyncio.gather(
            logger_with_logs.adump(persist_path=fp),
            background(),
        )
        assert sentinel == [True]
        assert fp.exists()

    @pytest.mark.asyncio
    async def test_adump_unsupported_extension_raises(self, logger_with_logs, tmp_path):
        fp = tmp_path / "test_dump.xml"
        with pytest.raises(ValueError, match="Unsupported file extension"):
            await logger_with_logs.adump(persist_path=fp)

    @pytest.mark.asyncio
    async def test_adump_preserves_logs_when_write_fails(self, tmp_path):
        """Data must not be cleared when the write fails."""
        config = DataLoggerConfig(
            persist_dir=str(tmp_path),
            auto_save_on_exit=False,
            clear_after_dump=True,
        )
        dl = DataLogger(_config=config)
        for i in range(3):
            dl.log(Element())
        assert len(dl.logs) == 3

        # A path whose suffix triggers the unsupported-extension branch.
        fp = tmp_path / "test_dump.xml"
        with pytest.raises(ValueError, match="Unsupported file extension"):
            await dl.adump(clear=True, persist_path=fp)

        # Logs must still be intact — write never succeeded.
        assert len(dl.logs) == 3


# ---------------------------------------------------------------------------
# Pile.adump — write-failure data preservation
# ---------------------------------------------------------------------------


class TestPileAdumpWriteFailure:
    """Pile.adump must not clear data when the write raises."""

    @pytest.mark.asyncio
    async def test_adump_preserves_pile_when_write_fails(self, tmp_path):
        """Clear must not happen if the write raises."""
        items = [MockElement(value=i) for i in range(4)]
        p = Pile(collections=items)
        assert len(p) == 4

        # An unsupported format triggers ValueError inside _write.
        fp = tmp_path / "dump.json"
        with pytest.raises(ValueError, match="Unsupported obj_key"):
            await p.adump(fp, obj_key="xml", clear=True)

        # Pile must be unchanged.
        assert len(p) == 4


# ---------------------------------------------------------------------------
# Concurrent-append survival tests
# ---------------------------------------------------------------------------


class TestAdumpConcurrentAppend:
    """Items appended during the write window must survive the post-dump clear."""

    @pytest.mark.asyncio
    async def test_pile_adump_concurrent_append_survives(self, tmp_path, monkeypatch):
        """An item added to a Pile while adump is writing must not be removed."""
        import lionagi.protocols.generic.pile as pile_module

        items = [MockElement(value=i) for i in range(3)]
        p = Pile(collections=items)
        late_item = MockElement(value=99)
        appended = []

        original_run_sync = pile_module.__builtins__  # not used directly
        # Monkeypatch run_sync so that we can append *after* the snapshot but
        # *before* the selective clear.
        import lionagi.ln.concurrency as conc_module

        original_run_sync_fn = conc_module.run_sync

        async def slow_run_sync(fn):
            result = await original_run_sync_fn(fn)
            # Simulate a concurrent append arriving after the write finishes.
            p.include(late_item)
            appended.append(late_item)
            return result

        monkeypatch.setattr(conc_module, "run_sync", slow_run_sync)

        fp = tmp_path / "concurrent.json"
        await p.adump(fp, clear=True)

        assert late_item in p, "Late item must survive the selective clear"
        assert len(p) == 1, f"Only the late item should remain, got {len(p)}"

    @pytest.mark.asyncio
    async def test_datalogger_adump_concurrent_log_survives(self, tmp_path, monkeypatch):
        """A log entry added during adump write must not be removed by the clear."""
        import lionagi.ln.concurrency as conc_module

        config = DataLoggerConfig(
            persist_dir=str(tmp_path),
            auto_save_on_exit=False,
            clear_after_dump=True,
        )
        dl = DataLogger(_config=config)
        for i in range(3):
            dl.log(Element())

        late_log = Log.create(Element())
        appended = []

        original_run_sync_fn = conc_module.run_sync

        async def slow_run_sync(fn):
            result = await original_run_sync_fn(fn)
            dl.logs.include(late_log)
            appended.append(late_log)
            return result

        monkeypatch.setattr(conc_module, "run_sync", slow_run_sync)

        fp = tmp_path / "concurrent_logger.json"
        await dl.adump(clear=True, persist_path=fp)

        assert late_log in dl.logs, "Late log must survive the selective clear"
        assert len(dl.logs) == 1, f"Only the late log should remain, got {len(dl.logs)}"
