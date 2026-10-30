# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Strict late ACK ownership for private in-memory state stores."""

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


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["attempt_exact", "attempt_replay"])
@pytest.mark.parametrize("with_drain", [False, True], ids=["no-drain", "drain"])
async def test_private_late_ack_stays_reserved_until_owner_coroutine_settles(
    monkeypatch, caplog, adapter, with_drain
):
    monkeypatch.setattr(reconciliation, "HANDLER_BUDGET_SECONDS", 0.05)
    sync_entered, release_sync = threading.Event(), threading.Event()
    ack_entered, release_ack = asyncio.Event(), asyncio.Event()
    cancel_seen, ack_completed = asyncio.Event(), asyncio.Event()
    ack_calls = []
    real_ack = reconciliation.ack_delivery

    async def delayed_ack(db, transition_id, consumer):
        ack_calls.append(transition_id)
        ack_entered.set()
        try:
            await release_ack.wait()
        except asyncio.CancelledError:
            cancel_seen.set()
            # A cancellation-resistant ACK is still physically active.
            await release_ack.wait()
        await real_ack(db, transition_id, consumer)
        ack_completed.set()

    def deliver():
        sync_entered.set()
        if not release_sync.wait(5):
            raise AssertionError("test did not release synchronous worker")
        return DeliveryOutcome.SUCCESS

    async def prepare(_row, _db):
        return PreparedDelivery(deliver)

    monkeypatch.setattr(reconciliation, "ack_delivery", delayed_ack)
    db = StateDB(":memory:")
    await db.open()
    drain = None
    key = None
    try:
        event_id = await _seed(db)
        await register_consumer(db, "private-settlement", start="all")
        store = reconciliation._store_identity(db)
        key = (store, "private-settlement", event_id)
        first = TerminalReconciler("private-settlement", prepare, batch_size=1, timeout=1.0)
        second = TerminalReconciler("private-settlement", prepare, batch_size=1, timeout=1.0)
        with caplog.at_level("WARNING", logger=reconciliation.__name__):
            result = await getattr(first, adapter)(db, event_id)
            assert sync_entered.is_set() and result.status == "failed" and result.attempted
            release_sync.set()
            await asyncio.wait_for(ack_entered.wait(), 2)

            # Wait beyond the late ACK wait budget. Elapsing the budget must
            # neither cancel the owner-loop coroutine nor make it drainable.
            await asyncio.sleep(0.2)
            assert not cancel_seen.is_set()
            assert not ack_completed.is_set()
            assert ack_calls == [event_id]
            with reconciliation._workers_lock:
                worker = reconciliation._workers.get(key)
                assert worker is not None and not worker.deferred
            assert (await getattr(second, adapter)(db, event_id)).status == "busy"
            assert not await is_acknowledged(db, event_id, "private-settlement")

            if with_drain:
                drain = asyncio.create_task(
                    reconciliation._drain_deferred(db, "private-settlement", store)
                )
                # Active ACK is not a deferred item: drain must not wait on or
                # schedule another ACK while the owner coroutine is in flight.
                await asyncio.wait_for(asyncio.shield(drain), 1)
                assert ack_calls == [event_id] and not ack_completed.is_set()
                assert (await getattr(second, adapter)(db, event_id)).status == "busy"

            release_ack.set()
            await asyncio.wait_for(ack_completed.wait(), 2)
            await _wait_for_attempt_release(key)
            assert not cancel_seen.is_set()
            assert ack_calls == [event_id]
            assert await is_acknowledged(db, event_id, "private-settlement")
            assert (await getattr(second, adapter)(db, event_id)).status == "not_owed"

            await asyncio.wait_for(
                reconciliation._drain_deferred(db, "private-settlement", store), 1
            )
            assert ack_calls == [event_id]
            assert "private-ack-secret" not in caplog.text
    finally:
        release_sync.set()
        release_ack.set()
        if drain is not None and not drain.done():
            try:
                await asyncio.wait_for(asyncio.shield(drain), 2)
            except asyncio.TimeoutError:
                drain.cancel()
                await asyncio.gather(drain, return_exceptions=True)
        if ack_entered.is_set() and not ack_completed.is_set():
            try:
                await asyncio.wait_for(ack_completed.wait(), 2)
            except asyncio.TimeoutError:
                pass
        if key is not None:
            with reconciliation._workers_lock:
                remaining = reconciliation._workers.get(key)
            if remaining is not None:
                reconciliation._release_worker(remaining)
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["attempt_exact", "attempt_replay"])
async def test_private_late_ack_failure_logs_type_not_sensitive_message(
    monkeypatch, caplog, adapter
):
    monkeypatch.setattr(reconciliation, "HANDLER_BUDGET_SECONDS", 0.05)
    sync_entered, release_sync = threading.Event(), threading.Event()
    secret = "private-ack-secret-payload"

    async def failing_ack(_db, _transition_id, _consumer):
        raise RuntimeError(secret)

    def deliver():
        sync_entered.set()
        if not release_sync.wait(5):
            raise AssertionError("test did not release synchronous worker")
        return DeliveryOutcome.SUCCESS

    async def prepare(_row, _db):
        return PreparedDelivery(deliver)

    monkeypatch.setattr(reconciliation, "ack_delivery", failing_ack)
    db = StateDB(":memory:")
    await db.open()
    key = None
    try:
        event_id = await _seed(db)
        await register_consumer(db, "private-failure", start="all")
        key = (reconciliation._store_identity(db), "private-failure", event_id)
        runner = TerminalReconciler("private-failure", prepare, batch_size=1, timeout=1.0)
        with caplog.at_level("WARNING", logger=reconciliation.__name__):
            result = await getattr(runner, adapter)(db, event_id)
            assert sync_entered.is_set() and result.status == "failed" and result.attempted
            release_sync.set()

            async def wait_for_type():
                while "RuntimeError" not in caplog.text:
                    await asyncio.sleep(0.005)

            await asyncio.wait_for(wait_for_type(), 2)
            assert secret not in caplog.text
            assert "Traceback" not in caplog.text
            assert not await is_acknowledged(db, event_id, "private-failure")
    finally:
        release_sync.set()
        if key is not None:
            with reconciliation._workers_lock:
                remaining = reconciliation._workers.get(key)
            if remaining is not None:
                reconciliation._release_worker(remaining)
        await db.close()
