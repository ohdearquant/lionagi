# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Offline coverage for completed and pending foreign-loop futures."""

from __future__ import annotations

import asyncio
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


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):
    def forbid(*_args, **_kwargs):
        raise AssertionError("offline test forbids processes, signals and network")

    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, forbid)
    for name in ("create_subprocess_exec", "create_subprocess_shell"):
        monkeypatch.setattr(asyncio, name, forbid)
    for name in ("kill", "killpg", "system"):
        monkeypatch.setattr(os, name, forbid, raising=False)
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, forbid)
    monkeypatch.setattr(socket, "create_connection", forbid)


async def _exercise_settled_future(tmp_path, monkeypatch, adapter, value, *, close_owner):
    owner = _ForeignOwner("future")
    owner_closed = False
    settled, finish_called = [], threading.Event()
    original_finish = reconciliation._finish_late

    def record_finish(worker, outcome=reconciliation._NO_LATE_RESULT):
        if outcome is reconciliation._NO_LATE_RESULT:
            return original_finish(worker)
        settled.append(outcome)
        try:
            return original_finish(worker, outcome)
        finally:
            finish_called.set()

    async def prepare(_row, _db):
        return PreparedDelivery(lambda: owner.future)

    monkeypatch.setattr(reconciliation, "_finish_late", record_finish)
    try:
        # The value is set on its owning loop, and its completion callback is
        # observed before the owning loop is either stopped or left running.
        owner.settle(value)
        await _wait_thread_event(owner.done)
        assert owner.future.done()
        if close_owner:
            owner.close()
            owner_closed = True
            assert owner.loop.is_closed()
        else:
            assert owner.loop.is_running()

        async with StateDB(tmp_path / "settled.db") as db:
            event_id = await _seed(db)
            await register_consumer(db, "settled", start="all")
            key = (reconciliation._store_identity(db), "settled", event_id)
            runner = TerminalReconciler("settled", prepare, batch_size=1, timeout=1.0)
            try:
                result = await asyncio.wait_for(getattr(runner, adapter)(db, event_id), 4)
                assert result.attempted is True
                await _wait_thread_event(finish_called)
                await _wait_for_attempt_release(key)
                expected = value if isinstance(value, DeliveryOutcome) else DeliveryOutcome.FAILURE
                assert settled == [expected]
                assert await is_acknowledged(db, event_id, "settled") is (
                    expected is DeliveryOutcome.SUCCESS
                )
                with reconciliation._workers_lock:
                    assert key not in reconciliation._workers
            finally:
                # The pre-fix implementation retains a closed-loop worker; do
                # not leak that reservation into another test on a red run.
                with reconciliation._workers_lock:
                    worker = reconciliation._workers.get(key)
                if worker is not None:
                    reconciliation._release_worker(worker)
    finally:
        if not owner_closed:
            owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["attempt_exact", "attempt_replay"])
async def test_completed_closed_foreign_success_acknowledges_and_releases(
    tmp_path, monkeypatch, adapter
):
    await _exercise_settled_future(
        tmp_path, monkeypatch, adapter, DeliveryOutcome.SUCCESS, close_owner=True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["attempt_exact", "attempt_replay"])
async def test_completed_closed_foreign_non_outcome_fails_and_releases(
    tmp_path, monkeypatch, adapter
):
    await _exercise_settled_future(
        tmp_path, monkeypatch, adapter, "not-an-outcome", close_owner=True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["attempt_exact", "attempt_replay"])
async def test_completed_live_foreign_success_remains_observable(tmp_path, monkeypatch, adapter):
    await _exercise_settled_future(
        tmp_path, monkeypatch, adapter, DeliveryOutcome.SUCCESS, close_owner=False
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["attempt_exact", "attempt_replay"])
async def test_pending_stopped_foreign_future_warns_without_watchdog_and_stays_reserved(
    tmp_path, monkeypatch, caplog, adapter
):
    class ManualTimer:
        def __init__(self, _interval, _callback):
            self.daemon = False

        def start(self):
            pass

        def cancel(self):
            pass

    class ThreadingProxy:
        Timer = ManualTimer

        def __getattr__(self, name):
            return getattr(threading, name)

    # The watchdog cannot fire in this test, so the warning must come from
    # detecting the stopped owner, not from waiting for a later timer.
    monkeypatch.setattr(reconciliation, "threading", ThreadingProxy())
    foreign_loop = asyncio.new_event_loop()
    future = foreign_loop.create_future()
    invoked = threading.Event()

    def deliver():
        invoked.set()
        return future

    async def prepare(_row, _db):
        return PreparedDelivery(deliver)

    try:
        async with StateDB(tmp_path / "pending.db") as db:
            event_id = await _seed(db)
            await register_consumer(db, "pending", start="all")
            key = (reconciliation._store_identity(db), "pending", event_id)
            first = TerminalReconciler("pending", prepare, batch_size=1, timeout=1.0)
            second = TerminalReconciler("pending", prepare, batch_size=1, timeout=1.0)
            try:
                with caplog.at_level("WARNING", logger=reconciliation.__name__):
                    result = await asyncio.wait_for(getattr(first, adapter)(db, event_id), 4)
                    assert invoked.is_set() and result.attempted is True

                    async def wait_for_stopped_warning():
                        while not any(
                            "stopped loop" in record.getMessage()
                            for record in caplog.records
                            if record.name == reconciliation.__name__
                        ):
                            await asyncio.sleep(0.005)

                    await asyncio.wait_for(wait_for_stopped_warning(), 2)
                    assert not future.done()
                    with reconciliation._workers_lock:
                        assert key in reconciliation._workers
                    assert (await getattr(second, adapter)(db, event_id)).status == "busy"
                    assert not await is_acknowledged(db, event_id, "pending")
            finally:
                foreign_loop.close()
                with reconciliation._workers_lock:
                    worker = reconciliation._workers.get(key)
                if worker is not None:
                    reconciliation._release_worker(worker)
    finally:
        if not foreign_loop.is_closed():
            foreign_loop.close()
