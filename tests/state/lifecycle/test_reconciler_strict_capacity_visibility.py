# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Capacity telemetry includes strict attempts and shared worker keys."""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from lionagi.state.db import StateDB
from lionagi.state.lifecycle import reconciler as reconciliation
from lionagi.state.lifecycle.deliveries import register_consumer
from lionagi.state.lifecycle.reconciler import DeliveryOutcome, PreparedDelivery, TerminalReconciler
from tests.state.lifecycle.test_reconciler_saturation_visibility import _seed, _warnings


async def _wait_released(key):
    async def wait():
        while True:
            with reconciliation._workers_lock:
                if key not in reconciliation._workers and key not in reconciliation._attempts:
                    return
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout=2)


@pytest.mark.asyncio
async def test_strict_async_reservation_is_reported_and_warning_resets(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger=reconciliation.__name__)
    clock = [100.0]
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def hook():
        entered.set()
        try:
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return DeliveryOutcome.FAILURE
        finally:
            finished.set()

    async def prepare(_row, _db):
        return PreparedDelivery(hook)

    async with StateDB(tmp_path / "state.db") as db:
        (event_id,) = await _seed(db, 1)
        await register_consumer(db, "strict-metrics", start="all")
        strict = TerminalReconciler(
            "strict-metrics", prepare, batch_size=1, timeout=0.03, clock=lambda: clock[0]
        )
        reporter = TerminalReconciler(
            "strict-metrics", prepare, batch_size=1, timeout=0.03, clock=lambda: clock[0]
        )
        key = (reconciliation._store_identity(db), "strict-metrics", event_id)
        try:
            result = await strict.attempt_exact(db, event_id)
            assert entered.is_set() and result.status == "failed"
            with reconciliation._workers_lock:
                assert key in reconciliation._attempts and key not in reconciliation._workers
            clock[0] = 103.0
            snapshot = await reporter.run_pass(db)
            assert snapshot["saturated"] == 1
            assert snapshot["outstanding"] == snapshot["past_timeout"] == 1
            assert snapshot["oldest_age_seconds"] == 3.0
            assert len(_warnings(caplog, "strict-metrics")) == 1
            await reporter.run_pass(db)
            assert len(_warnings(caplog, "strict-metrics")) == 1
            release.set()
            await asyncio.wait_for(finished.wait(), 2)
            await _wait_released(key)
            with reconciliation._workers_lock:
                assert key[:2] not in reconciliation._saturation_warnings
        finally:
            release.set()
            if entered.is_set():
                await asyncio.wait_for(finished.wait(), 2)
                await _wait_released(key)


@pytest.mark.asyncio
async def test_strict_worker_and_admission_share_one_reported_slot(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger=reconciliation.__name__)
    entered, release = threading.Event(), threading.Event()

    def hook():
        entered.set()
        if not release.wait(2):
            raise AssertionError("test did not release worker")
        return DeliveryOutcome.FAILURE

    async def prepare(_row, _db):
        return PreparedDelivery(hook)

    async with StateDB(tmp_path / "state.db") as db:
        (event_id,) = await _seed(db, 1)
        await register_consumer(db, "strict-dedup", start="all")
        strict = TerminalReconciler("strict-dedup", prepare, batch_size=1, timeout=0.3)
        reporter = TerminalReconciler("strict-dedup", prepare, batch_size=1, timeout=0.3)
        key = (reconciliation._store_identity(db), "strict-dedup", event_id)
        attempt = asyncio.create_task(strict.attempt_exact(db, event_id))
        try:

            async def wait_entered():
                while not entered.is_set():
                    await asyncio.sleep(0.005)

            await asyncio.wait_for(wait_entered(), 2)
            with reconciliation._workers_lock:
                assert key in reconciliation._attempts and key in reconciliation._workers
            snapshot = await reporter.run_pass(db)
            assert snapshot["saturated"] == 1 and snapshot["outstanding"] == 1
            assert len(_warnings(caplog, "strict-dedup")) == 1
            assert (await attempt).status == "failed"
            with reconciliation._workers_lock:
                assert key in reconciliation._workers
                assert key[:2] in reconciliation._saturation_warnings
            release.set()
            await _wait_released(key)
            with reconciliation._workers_lock:
                assert key[:2] not in reconciliation._saturation_warnings
        finally:
            release.set()
            await asyncio.gather(attempt, return_exceptions=True)
            await _wait_released(key)
