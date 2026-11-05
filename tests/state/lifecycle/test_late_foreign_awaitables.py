# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Offline strict-delivery tests for Futures owned by another event loop."""

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
from tests.state.lifecycle.test_exact_terminal_attempt import (
    _first_attempt,
    _seed,
    _wait_for_attempt_release,
)


def _forbid(*_args, **_kwargs):
    raise AssertionError("offline test forbids processes, signals and network")


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


async def _wait_thread_event(event: threading.Event) -> None:
    """Wait until another thread sets ``event``; only the per-test timeout bounds it."""
    while not event.is_set():
        await asyncio.sleep(0.005)


class _ForeignOwner:
    """A real asyncio owner loop in one in-process test thread."""

    def __init__(self, kind: str):
        self.kind = kind
        self.ready = threading.Event()
        self.done = threading.Event()
        self.terminated = threading.Event()
        self.cancelled = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.ready.wait()

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.gate = self.loop.create_future()
        if self.kind == "task":

            async def wait_gate():
                return await self.gate

            self.future = self.loop.create_task(wait_gate())
        else:
            self.future = self.gate

        def record(done):
            self.cancelled = done.cancelled()
            self.done.set()

        self.future.add_done_callback(record)
        self.loop.call_soon(self.ready.set)
        try:
            self.loop.run_forever()
        finally:
            self.loop.close()
            self.terminated.set()

    def settle(self, value):
        def finish():
            if value == "cancel":
                self.future.cancel()
            elif value == "exception":
                self.gate.set_exception(RuntimeError("foreign-secret-payload"))
            else:
                self.gate.set_result(value)

        self.loop.call_soon_threadsafe(finish)

    def close(self):
        def stop():
            if self.future.done():
                self.loop.stop()
            else:
                self.future.add_done_callback(lambda _done: self.loop.stop())
                self.future.cancel()

        self.loop.call_soon_threadsafe(stop)
        self.thread.join()


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["attempt_exact", "attempt_replay"])
@pytest.mark.parametrize("arrival", ["early", "late"])
@pytest.mark.parametrize("kind", ["future", "task"])
@pytest.mark.parametrize(
    ("outcome", "acknowledged"),
    [
        (DeliveryOutcome.SUCCESS, True),
        (DeliveryOutcome.NOT_APPLICABLE, True),
        (DeliveryOutcome.FAILURE, False),
        (DeliveryOutcome.NO_SESSION, False),
        (DeliveryOutcome.AMBIGUOUS_PROJECT, False),
        ("cancel", False),
        ("exception", False),
    ],
)
async def test_foreign_future_remains_reserved_through_settlement_and_ack(
    tmp_path, monkeypatch, caplog, adapter, arrival, kind, outcome, acknowledged
):
    owner = _ForeignOwner(kind)
    invoked, release_sync = threading.Event(), threading.Event()
    ack_entered, release_ack = threading.Event(), threading.Event()
    effects, acknowledgements = [], []
    original_ack = reconciliation._acknowledge_late

    async def held_ack(worker):
        acknowledgements.append(worker.key[2])
        ack_entered.set()
        release_ack.wait()
        await original_ack(worker)

    def deliver():
        effects.append("effect")
        invoked.set()
        if arrival == "late":
            release_sync.wait()
        return owner.future

    async def prepare(_row, _db):
        return PreparedDelivery(deliver)

    monkeypatch.setattr(reconciliation, "_acknowledge_late", held_ack)
    monkeypatch.setattr(reconciliation, "HANDLER_BUDGET_SECONDS", 0.05)
    try:
        async with StateDB(tmp_path / "foreign.db") as db:
            event_id = await _seed(db)
            await register_consumer(db, "foreign", start="all")
            key = (reconciliation._store_identity(db), "foreign", event_id)
            first = TerminalReconciler("foreign", prepare, batch_size=1, timeout=1.0)
            second = TerminalReconciler("foreign", prepare, batch_size=1, timeout=1.0)
            attempt = asyncio.create_task(
                _first_attempt(first, adapter, db, event_id, key, invoked)
            )
            try:
                # The attempt ends on the reconciler's own budget; once it
                # reports an attempt, the delivery thread has been started. A budget
                # that expired before the hook began is repeated with a wider one.
                result = await attempt
                assert result.status == "failed" and result.attempted is True
                await _wait_thread_event(invoked)
                if arrival == "late":
                    release_sync.set()
                assert (await getattr(second, adapter)(db, event_id)).status == "busy"
                assert effects == ["effect"] and not owner.done.is_set()
                assert not await is_acknowledged(db, event_id, "foreign")

                owner.settle(outcome)
                await _wait_thread_event(owner.done)
                if acknowledged:
                    # The ACK gate opens only while the event is still reserved;
                    # if the reservation ended first it never will.
                    while not ack_entered.is_set():
                        with reconciliation._workers_lock:
                            reserved = (
                                key in reconciliation._attempts or key in reconciliation._workers
                            )
                        assert reserved, "reservation ended before the late ACK started"
                        await asyncio.sleep(0.005)
                    assert (await getattr(second, adapter)(db, event_id)).status == "busy"
                    assert not await is_acknowledged(db, event_id, "foreign")
                    release_ack.set()
                await _wait_for_attempt_release(key)
                assert await is_acknowledged(db, event_id, "foreign") is acknowledged
                assert acknowledgements == ([event_id] if acknowledged else [])
                assert owner.cancelled is (outcome == "cancel")
                assert "foreign-secret-payload" not in caplog.text
                assert effects == ["effect"]
            finally:
                release_sync.set()
                release_ack.set()
                await asyncio.gather(attempt, return_exceptions=True)
    finally:
        owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["attempt_exact", "attempt_replay"])
@pytest.mark.parametrize("kind", ["future", "task"])
async def test_already_settled_foreign_success_acks_once(tmp_path, monkeypatch, adapter, kind):
    owner = _ForeignOwner(kind)
    ack_entered, release_ack = threading.Event(), threading.Event()
    acknowledgements = []
    original_ack = reconciliation._acknowledge_late

    async def held_ack(worker):
        acknowledgements.append(worker.key[2])
        ack_entered.set()
        if not release_ack.wait(5):
            raise AssertionError("test did not release fake ACK gate")
        await original_ack(worker)

    async def prepare(_row, _db):
        return PreparedDelivery(lambda: owner.future)

    monkeypatch.setattr(reconciliation, "_acknowledge_late", held_ack)
    try:
        owner.settle(DeliveryOutcome.SUCCESS)
        await _wait_thread_event(owner.done)
        async with StateDB(tmp_path / "done.db") as db:
            event_id = await _seed(db)
            await register_consumer(db, "done", start="all")
            key = (reconciliation._store_identity(db), "done", event_id)
            first = TerminalReconciler("done", prepare, batch_size=1, timeout=1.0)
            second = TerminalReconciler("done", prepare, batch_size=1, timeout=1.0)
            attempt = asyncio.create_task(
                _first_attempt(first, adapter, db, event_id, key, ack_entered)
            )
            try:
                # A budget that expired before delivery began is repeated with a wider one.
                result = await asyncio.wait_for(attempt, 10)
                assert result.status == "failed" and result.attempted is True
                await _wait_thread_event(ack_entered)
                assert (await getattr(second, adapter)(db, event_id)).status == "busy"
                assert not await is_acknowledged(db, event_id, "done")
                release_ack.set()
                await _wait_for_attempt_release(key)
                assert await is_acknowledged(db, event_id, "done")
                assert acknowledgements == [event_id]
            finally:
                release_ack.set()
                await asyncio.gather(attempt, return_exceptions=True)
    finally:
        owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["attempt_exact", "attempt_replay"])
@pytest.mark.parametrize("kind", ["future", "task"])
async def test_caller_cancellation_never_cancels_foreign_future(tmp_path, adapter, kind):
    owner = _ForeignOwner(kind)
    invoked = threading.Event()

    def deliver():
        invoked.set()
        return owner.future

    async def prepare(_row, _db):
        return PreparedDelivery(deliver)

    try:
        async with StateDB(tmp_path / "cancel.db") as db:
            event_id = await _seed(db)
            await register_consumer(db, "cancel", start="all")
            key = (reconciliation._store_identity(db), "cancel", event_id)
            first = TerminalReconciler("cancel", prepare, batch_size=1, timeout=1)
            second = TerminalReconciler("cancel", prepare, batch_size=1, timeout=1.0)
            attempt = asyncio.create_task(getattr(first, adapter)(db, event_id))
            try:
                await _wait_thread_event(invoked)

                # Wait until the sync worker has finished and handed its result off.
                async def detached():
                    while True:
                        with reconciliation._workers_lock:
                            worker = reconciliation._workers.get(key)
                            if worker is not None and worker.detached:
                                return
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(detached(), 5)
                attempt.cancel("parent cancelled")
                with pytest.raises(asyncio.CancelledError):
                    await attempt
                assert not owner.done.is_set()
                assert (await getattr(second, adapter)(db, event_id)).status == "busy"
                owner.settle(DeliveryOutcome.SUCCESS)
                await _wait_thread_event(owner.done)
                await _wait_for_attempt_release(key)
                assert owner.cancelled is False
                assert await is_acknowledged(db, event_id, "cancel")
            finally:
                await asyncio.gather(attempt, return_exceptions=True)
    finally:
        owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["attempt_exact", "attempt_replay"])
async def test_same_caller_loop_future_keeps_existing_behavior(tmp_path, adapter):
    future = asyncio.get_running_loop().create_future()

    async def prepare(_row, _db):
        return PreparedDelivery(lambda: future)

    async with StateDB(tmp_path / "same-loop.db") as db:
        event_id = await _seed(db)
        await register_consumer(db, "same-loop", start="all")
        reconciler = TerminalReconciler("same-loop", prepare, batch_size=1, timeout=0.5)
        attempt = asyncio.create_task(getattr(reconciler, adapter)(db, event_id))
        try:
            await asyncio.sleep(0.01)
            future.set_result(DeliveryOutcome.SUCCESS)
            result = await asyncio.wait_for(attempt, 2)
            assert result.status == "acknowledged" and result.attempted is True
            assert await is_acknowledged(db, event_id, "same-loop")
        finally:
            if not future.done():
                future.cancel()
            await asyncio.gather(attempt, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["attempt_exact", "attempt_replay"])
@pytest.mark.parametrize("loop_state", ["stopped", "closed"])
async def test_unobservable_foreign_loop_quarantines_and_reports_stuck(
    tmp_path, monkeypatch, caplog, adapter, loop_state
):
    # No thread is started for this never-running owner. A Future on it cannot
    # be observed from the caller's loop; the stopped-loop callback queue is
    # discarded by close before the explicit test-only worker cleanup.
    foreign_loop = asyncio.new_event_loop()
    future = foreign_loop.create_future()
    if loop_state == "closed":
        foreign_loop.close()
    invoked = threading.Event()

    def deliver():
        invoked.set()
        return future

    async def prepare(_row, _db):
        return PreparedDelivery(deliver)

    monkeypatch.setattr(reconciliation, "HANDLER_BUDGET_SECONDS", 0.05)
    try:
        async with StateDB(tmp_path / "unobservable-secret.db") as db:
            event_id = await _seed(db)
            await register_consumer(db, "unobservable", start="all")
            key = (reconciliation._store_identity(db), "unobservable", event_id)
            first = TerminalReconciler("unobservable", prepare, batch_size=1, timeout=1.0)
            second = TerminalReconciler("unobservable", prepare, batch_size=1, timeout=1.0)
            try:
                with caplog.at_level("WARNING", logger=reconciliation.__name__):
                    # A budget that expired before the hook began is repeated with a wider one.
                    result = await _first_attempt(first, adapter, db, event_id, key, invoked)
                    assert invoked.is_set() and result.status == "failed" and result.attempted
                    assert (await getattr(second, adapter)(db, event_id)).status == "busy"
                    assert not await is_acknowledged(db, event_id, "unobservable")

                    async def wait_for_warning():
                        while "stuck" not in caplog.text.lower():
                            await asyncio.sleep(0.005)

                    await asyncio.wait_for(wait_for_warning(), 2)
                    assert "unobservable-secret.db" not in caplog.text
                    assert not future.done()
                    with reconciliation._workers_lock:
                        worker = reconciliation._workers.get(key)
                    assert worker is not None
                    assert (await getattr(second, adapter)(db, event_id)).status == "busy"
                    assert not await is_acknowledged(db, event_id, "unobservable")
            finally:
                # Cancel a callback that may have been queued on a stopped loop
                # before clearing the in-process reservation for the next test.
                if not foreign_loop.is_closed():
                    foreign_loop.close()
                with reconciliation._workers_lock:
                    worker = reconciliation._workers.get(key)
                if worker is not None:
                    reconciliation._release_worker(worker)
    finally:
        if not foreign_loop.is_closed():
            foreign_loop.close()


class _PausableForeignOwner:
    """Stop after observing callback registration, then restart the same loop."""

    def __init__(self):
        self.ready = threading.Event()
        self.paused = threading.Event()
        self.resume = threading.Event()
        self.resumed = threading.Event()
        self.done = threading.Event()
        self.shutdown = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        if not self.ready.wait(2):
            raise AssertionError("foreign owner did not start")

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.future = self.loop.create_future()
        self.future.add_done_callback(lambda _future: self.done.set())
        self.loop.call_soon(self.ready.set)
        try:
            self.loop.run_forever()
            if not self.shutdown:
                self.paused.set()
                if not self.resume.wait(3):
                    raise AssertionError("test did not resume the foreign loop")
                self.loop.call_soon(self.resumed.set)
                self.loop.run_forever()
        finally:
            self.loop.close()

    def pause_when_observer_is_registered(self):
        def check():
            # The first callback belongs to this test. The next one is the
            # delivery observer installed on this Future's owner loop.
            if len(self.future._callbacks or ()) >= 2:
                self.loop.stop()
            else:
                self.loop.call_later(0.005, check)

        self.loop.call_soon_threadsafe(check)

    def close(self):
        # Also unblock a pause racing this cleanup before paused is published.
        self.resume.set()
        if self.paused.is_set() and not self.resumed.is_set():
            if not self.resumed.wait(2):
                raise AssertionError("foreign loop did not resume for cleanup")

        def stop():
            self.shutdown = True
            if self.future.done():
                self.loop.stop()
            else:
                self.future.add_done_callback(lambda _future: self.loop.stop())
                self.future.cancel()

        self.loop.call_soon_threadsafe(stop)
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise AssertionError("foreign test loop did not stop")


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["attempt_exact", "attempt_replay"])
@pytest.mark.parametrize("arrival", ["early", "late"])
async def test_foreign_loop_stops_after_observer_registration_and_restarts(
    tmp_path, monkeypatch, caplog, adapter, arrival
):
    owner = _PausableForeignOwner()
    invoked, release_sync = threading.Event(), threading.Event()

    def deliver():
        invoked.set()
        if arrival == "late" and not release_sync.wait(5):
            raise AssertionError("test did not release sync worker")
        return owner.future

    async def prepare(_row, _db):
        return PreparedDelivery(deliver)

    monkeypatch.setattr(reconciliation, "HANDLER_BUDGET_SECONDS", 0.05)
    try:
        async with StateDB(tmp_path / "pause-secret.db") as db:
            event_id = await _seed(db)
            await register_consumer(db, "paused", start="all")
            key = (reconciliation._store_identity(db), "paused", event_id)
            first = TerminalReconciler("paused", prepare, batch_size=1, timeout=1.0)
            second = TerminalReconciler("paused", prepare, batch_size=1, timeout=1.0)
            attempt = asyncio.create_task(
                _first_attempt(first, adapter, db, event_id, key, invoked)
            )
            try:
                with caplog.at_level("WARNING", logger=reconciliation.__name__):
                    await _wait_thread_event(invoked)
                    result = await asyncio.wait_for(attempt, 10)
                    assert result.status == "failed" and result.attempted
                    release_sync.set()
                    owner.pause_when_observer_is_registered()
                    await _wait_thread_event(owner.paused)
                    assert not owner.done.is_set()
                    assert (await getattr(second, adapter)(db, event_id)).status == "busy"
                    assert not await is_acknowledged(db, event_id, "paused")

                    async def wait_for_warning():
                        while "stuck" not in caplog.text.lower():
                            await asyncio.sleep(0.005)

                    await asyncio.wait_for(wait_for_warning(), 2)
                    assert "pause-secret.db" not in caplog.text
                    assert (await getattr(second, adapter)(db, event_id)).status == "busy"

                    owner.resume.set()
                    await _wait_thread_event(owner.resumed)
                    owner.loop.call_soon_threadsafe(
                        owner.future.set_result, DeliveryOutcome.SUCCESS
                    )
                    await _wait_thread_event(owner.done)
                    await _wait_for_attempt_release(key)
                    assert await is_acknowledged(db, event_id, "paused")
                    assert (await getattr(second, adapter)(db, event_id)).status == "not_owed"
            finally:
                release_sync.set()
                await asyncio.gather(attempt, return_exceptions=True)
    finally:
        owner.close()
