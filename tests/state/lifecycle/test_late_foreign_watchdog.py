# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Offline watchdog controls for reserved foreign-loop delivery results."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import subprocess
import threading

import pytest

from lionagi.state.db import StateDB
from lionagi.state.lifecycle import reconciler as reconciliation
from lionagi.state.lifecycle.deliveries import is_acknowledged, register_consumer
from lionagi.state.lifecycle.reconciler import DeliveryOutcome, PreparedDelivery, TerminalReconciler
from tests.state.lifecycle.test_exact_terminal_attempt import _seed, _wait_for_attempt_release
from tests.state.lifecycle.test_late_foreign_awaitables import _ForeignOwner, _wait_thread_event


def _forbid(*_args, **_kwargs):
    raise AssertionError("offline test forbids processes, signals, and network")


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):
    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, _forbid)
    for name in ("create_subprocess_exec", "create_subprocess_shell"):
        monkeypatch.setattr(asyncio, name, _forbid)
    for name in ("kill", "killpg", "system"):
        monkeypatch.setattr(os, name, _forbid, raising=False)
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, _forbid)
    monkeypatch.setattr(socket, "create_connection", _forbid)


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["attempt_exact", "attempt_replay"])
async def test_foreign_watchdog_start_failure_reports_stuck_without_releasing(
    tmp_path, monkeypatch, caplog, adapter
):
    """A failed diagnostic timer must not leave an unreported quarantine."""
    secret = "private-timer-start-message"
    started = threading.Event()

    class FailingTimer:
        def __init__(self, _interval, _callback):
            self.daemon = False

        def start(self):
            started.set()
            raise RuntimeError(secret)

        def cancel(self):
            pass

    monkeypatch.setattr(reconciliation.threading, "Timer", FailingTimer)
    monkeypatch.setattr(reconciliation, "HANDLER_BUDGET_SECONDS", 0.05)
    foreign_loop = asyncio.new_event_loop()
    future = foreign_loop.create_future()  # the loop is deliberately never running

    async def prepare(_row, _db):
        return PreparedDelivery(lambda: future)

    try:
        async with StateDB(tmp_path / "timer-failure.db") as db:
            event_id = await _seed(db)
            await register_consumer(db, "timer-failure", start="all")
            key = (reconciliation._store_identity(db), "timer-failure", event_id)
            first = TerminalReconciler("timer-failure", prepare, batch_size=1, timeout=0.05)
            second = TerminalReconciler("timer-failure", prepare, batch_size=1, timeout=0.05)
            with caplog.at_level(logging.WARNING, logger=reconciliation.__name__):
                try:
                    result = await getattr(first, adapter)(db, event_id)
                    assert result.status == "failed" and result.attempted
                    await _wait_thread_event(started)

                    async def stuck_warning():
                        while not any(
                            event_id in record.getMessage()
                            and "stuck" in record.getMessage().lower()
                            for record in caplog.records
                        ):
                            await asyncio.sleep(0.005)

                    await asyncio.wait_for(stuck_warning(), 2)
                    assert (await getattr(second, adapter)(db, event_id)).status == "busy"
                    assert not await is_acknowledged(db, event_id, "timer-failure")
                    assert secret not in caplog.text
                    assert not any(record.exc_info for record in caplog.records)
                finally:
                    # Owner-loop observation is impossible by construction. This
                    # test-only cleanup makes the intentional quarantine bounded.
                    with reconciliation._workers_lock:
                        worker = reconciliation._workers.get(key)
                    if worker is not None:
                        reconciliation._release_worker(worker)
                    await _wait_for_attempt_release(key)
    finally:
        future.cancel()
        foreign_loop.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["attempt_exact", "attempt_replay"])
async def test_foreign_watchdog_ignores_deferred_ack_drained_before_timer_fires(
    monkeypatch, caplog, adapter
):
    """A retained result is no longer stuck once deferred ACK is drained."""
    timers = []
    settle_finished = threading.Event()
    original_thread = threading.Thread

    class CapturedTimer:
        def __init__(self, _interval, callback):
            self.callback = callback
            self.daemon = False
            self.cancelled = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

        def fire_even_if_cancelled(self):
            # Model a timer callback already dispatched concurrently with cancel.
            self.callback()

    def instrument_thread(*args, **kwargs):
        target = kwargs.get("target")
        if target is not None and getattr(target, "__name__", "") == "settle":

            def run_and_record():
                try:
                    target()
                finally:
                    settle_finished.set()

            kwargs["target"] = run_and_record
        return original_thread(*args, **kwargs)

    owner = _ForeignOwner("future")
    monkeypatch.setattr(reconciliation.threading, "Timer", CapturedTimer)
    monkeypatch.setattr(reconciliation.threading, "Thread", instrument_thread)

    async def fail_ack(_target):
        raise reconciliation._LateAckDeferredError("private-deferred-message")

    async def prepare(_row, _db):
        return PreparedDelivery(lambda: owner.future)

    monkeypatch.setattr(reconciliation, "_acknowledge_late", fail_ack)
    try:
        async with StateDB(":memory:") as db:
            event_id = await _seed(db)
            await register_consumer(db, "deferred-watchdog", start="all")
            store = reconciliation._store_identity(db)
            key = (store, "deferred-watchdog", event_id)
            runner = TerminalReconciler("deferred-watchdog", prepare, batch_size=1, timeout=0.05)
            with caplog.at_level(logging.WARNING, logger=reconciliation.__name__):
                try:
                    result = await getattr(runner, adapter)(db, event_id)
                    assert result.status == "failed" and result.attempted
                    owner.settle(DeliveryOutcome.SUCCESS)
                    await _wait_thread_event(owner.done)
                    # Wait for the late-result observer to have actually exited,
                    # so drain occurs after the former lost-notification window.
                    await _wait_thread_event(settle_finished)
                    with reconciliation._workers_lock:
                        worker = reconciliation._workers.get(key)
                        assert worker is not None and worker.deferred
                    assert len(timers) == 1
                    await reconciliation._drain_deferred(db, "deferred-watchdog", store)
                    await _wait_for_attempt_release(key)
                    assert await is_acknowledged(db, event_id, "deferred-watchdog")
                    timers[0].fire_even_if_cancelled()
                    assert not any(
                        event_id in record.getMessage() and "stuck" in record.getMessage().lower()
                        for record in caplog.records
                    )
                    assert "private-deferred-message" not in caplog.text
                    assert not any(record.exc_info for record in caplog.records)
                finally:
                    with reconciliation._workers_lock:
                        worker = reconciliation._workers.get(key)
                    if worker is not None:
                        reconciliation._release_worker(worker)
                    await _wait_for_attempt_release(key)
    finally:
        owner.close()
