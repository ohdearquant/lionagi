# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Strict late synchronous results preserve a returned awaitable until settlement."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import subprocess
import threading
import uuid

import pytest

from lionagi.state.db import StateDB
from lionagi.state.lifecycle import reconciler as reconciliation
from lionagi.state.lifecycle.deliveries import is_acknowledged, register_consumer
from lionagi.state.lifecycle.reconciler import DeliveryOutcome, PreparedDelivery, TerminalReconciler
from tests.state.lifecycle.test_exact_terminal_attempt import _seed, _wait_for_attempt_release


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


async def _until(predicate, timeout=2):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout)


class _FatalChild(BaseException):
    pass


class _Awaitable:
    """A custom awaitable runnable on the detached worker's loop."""

    def __init__(self, coroutine):
        self.coroutine = coroutine

    def __await__(self):
        return self.coroutine.__await__()


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["attempt_exact", "attempt_replay"])
@pytest.mark.parametrize("shape", ["coroutine", "custom"])
@pytest.mark.parametrize(
    ("outcome", "acknowledged"),
    [
        (DeliveryOutcome.SUCCESS, True),
        (DeliveryOutcome.NOT_APPLICABLE, True),
        (DeliveryOutcome.FAILURE, False),
        (DeliveryOutcome.NO_SESSION, False),
        (DeliveryOutcome.AMBIGUOUS_PROJECT, False),
        ("unexpected", False),
        ("exception", False),
        ("base-exception", False),
    ],
)
async def test_late_returned_awaitable_keeps_capacity_through_settlement_and_ack(
    tmp_path, monkeypatch, caplog, adapter, shape, outcome, acknowledged
):
    """Cancellation-resistant work owns its slot until the actual outcome and ACK."""
    monkeypatch.setattr(reconciliation, "HANDLER_BUDGET_SECONDS", 0.05)
    secret = "private-child-error-3981"
    sync_started = threading.Event()
    release_sync = threading.Event()
    async_started = threading.Event()
    cancel_seen = threading.Event()
    release_effect = threading.Event()
    effect_finished = threading.Event()
    ack_started = threading.Event()
    release_ack = threading.Event()
    calls = []
    cancellations = []
    ack_calls = []
    original_ack = reconciliation._acknowledge_late

    async def effect():
        async_started.set()
        try:
            while not release_effect.is_set():
                try:
                    await asyncio.sleep(0.005)
                except asyncio.CancelledError:
                    cancellations.append(True)
                    cancel_seen.set()
            if outcome == "exception":
                raise RuntimeError(secret)
            if outcome == "base-exception":
                raise _FatalChild(secret)
            return outcome
        finally:
            effect_finished.set()

    def deliver():
        calls.append(True)
        sync_started.set()
        if not release_sync.wait(3):
            return DeliveryOutcome.FAILURE
        coroutine = effect()
        return coroutine if shape == "coroutine" else _Awaitable(coroutine)

    async def prepare(_row, _db):
        return PreparedDelivery(deliver)

    async def blocked_ack(target):
        ack_calls.append(True)
        ack_started.set()
        await _until(release_ack.is_set)
        await original_ack(target)

    monkeypatch.setattr(reconciliation, "_acknowledge_late", blocked_ack)
    async with StateDB(tmp_path / "state.db") as db:
        event_id, other_id = await _seed(db), await _seed(db)
        await register_consumer(db, "late-return", start="all")
        first = TerminalReconciler("late-return", prepare, batch_size=1, timeout=0.05)
        second = TerminalReconciler("late-return", prepare, batch_size=1, timeout=0.05)
        key = (reconciliation._store_identity(db), "late-return", event_id)
        with caplog.at_level(logging.WARNING, logger=reconciliation.__name__):
            try:
                first_result = await getattr(first, adapter)(db, event_id)
                assert first_result.status == "failed" and first_result.attempted
                assert sync_started.is_set()
                release_sync.set()
                await _until(async_started.is_set)
                await _until(cancel_seen.is_set)
                await _until(
                    lambda: any(
                        event_id in record.getMessage() and "stuck" in record.getMessage().lower()
                        for record in caplog.records
                    )
                )
                assert (await second.attempt_exact(db, event_id)).status == "busy"
                assert (await second.attempt_replay(db, other_id)).status == "busy"
                assert not await is_acknowledged(db, event_id, "late-return")
                assert calls == [True]
                assert cancellations == [True]
                release_effect.set()
                await _until(effect_finished.is_set)
                if acknowledged:
                    await _until(ack_started.is_set)
                    assert (await second.attempt_exact(db, event_id)).status == "busy"
                    assert not await is_acknowledged(db, event_id, "late-return")
                    release_ack.set()
                await _wait_for_attempt_release(key)
                assert await is_acknowledged(db, event_id, "late-return") is acknowledged
                assert ack_calls == ([True] if acknowledged else [])
                assert cancellations == [True]
                stuck = [
                    record
                    for record in caplog.records
                    if event_id in record.getMessage() and "stuck" in record.getMessage().lower()
                ]
                assert len(stuck) == 1
                assert secret not in caplog.text
                assert not any(record.exc_info for record in caplog.records)
            finally:
                release_sync.set()
                release_effect.set()
                release_ack.set()
                await _wait_for_attempt_release(key)


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["attempt_exact", "attempt_replay"])
async def test_late_returned_coroutine_cooperative_cancel_leaves_delivery_owed(
    tmp_path, monkeypatch, adapter
):
    monkeypatch.setattr(reconciliation, "HANDLER_BUDGET_SECONDS", 0.05)
    sync_started, release_sync, cancelled = (threading.Event() for _ in range(3))
    never = threading.Event()

    async def effect():
        try:
            while not never.is_set():
                await asyncio.sleep(0.005)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return DeliveryOutcome.SUCCESS

    def deliver():
        sync_started.set()
        if not release_sync.wait(3):
            return DeliveryOutcome.FAILURE
        return effect()

    async def prepare(_row, _db):
        return PreparedDelivery(deliver)

    async with StateDB(tmp_path / "state.db") as db:
        event_id = await _seed(db)
        await register_consumer(db, "cooperative", start="all")
        worker = TerminalReconciler("cooperative", prepare, batch_size=1, timeout=0.05)
        key = (reconciliation._store_identity(db), "cooperative", event_id)
        try:
            result = await getattr(worker, adapter)(db, event_id)
            assert result.status == "failed" and result.attempted and sync_started.is_set()
            release_sync.set()
            await _until(cancelled.is_set)
            await _wait_for_attempt_release(key)
            assert not await is_acknowledged(db, event_id, "cooperative")
        finally:
            release_sync.set()
            never.set()
            await _wait_for_attempt_release(key)


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["attempt_exact", "attempt_replay"])
@pytest.mark.parametrize("shape", ["coroutine", "custom"])
async def test_late_returned_awaitable_success_before_inner_budget_acks(
    tmp_path, monkeypatch, adapter, shape
):
    monkeypatch.setattr(reconciliation, "HANDLER_BUDGET_SECONDS", 0.5)
    sync_started, release_sync = threading.Event(), threading.Event()
    calls = []

    async def effect():
        calls.append("effect")
        return DeliveryOutcome.SUCCESS

    def deliver():
        sync_started.set()
        if not release_sync.wait(3):
            return DeliveryOutcome.FAILURE
        coroutine = effect()
        return coroutine if shape == "coroutine" else _Awaitable(coroutine)

    async def prepare(_row, _db):
        return PreparedDelivery(deliver)

    async with StateDB(tmp_path / "state.db") as db:
        event_id = await _seed(db)
        await register_consumer(db, "fast-late", start="all")
        runner = TerminalReconciler("fast-late", prepare, batch_size=1, timeout=0.05)
        key = (reconciliation._store_identity(db), "fast-late", event_id)
        try:
            result = await getattr(runner, adapter)(db, event_id)
            assert result.status == "failed" and result.attempted and sync_started.is_set()
            release_sync.set()
            await _wait_for_attempt_release(key)
            assert calls == ["effect"]
            assert await is_acknowledged(db, event_id, "fast-late")
        finally:
            release_sync.set()
            await _wait_for_attempt_release(key)


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["attempt_exact", "attempt_replay"])
@pytest.mark.parametrize("store", ["file", "shared-memory"])
async def test_late_returned_coroutine_acks_original_store_after_handle_close(
    tmp_path, monkeypatch, adapter, store
):
    monkeypatch.setattr(reconciliation, "HANDLER_BUDGET_SECONDS", 0.05)
    if store == "file":
        location = {"path": tmp_path / "closed.db"}
    else:
        name = "late-return-" + uuid.uuid4().hex
        location = {"url": f"sqlite+aiosqlite:///file:{name}?mode=memory&cache=shared&uri=true"}
    db = StateDB(**location)
    started, release_sync, cancelled, release_effect = (threading.Event() for _ in range(4))

    async def effect():
        while not release_effect.is_set():
            try:
                await asyncio.sleep(0.005)
            except asyncio.CancelledError:
                cancelled.set()
        return DeliveryOutcome.SUCCESS

    def deliver():
        started.set()
        if not release_sync.wait(3):
            return DeliveryOutcome.FAILURE
        return effect()

    async def prepare(_row, _db):
        return PreparedDelivery(deliver)

    await db.open()
    try:
        event_id = await _seed(db)
        await register_consumer(db, "closed-store", start="all")
        key = (reconciliation._store_identity(db), "closed-store", event_id)
        runner = TerminalReconciler("closed-store", prepare, batch_size=1, timeout=0.05)
        result = await getattr(runner, adapter)(db, event_id)
        assert result.status == "failed" and result.attempted and started.is_set()
        release_sync.set()
        await _until(cancelled.is_set)
        await db.close()
        # The shared-memory worker keeper must pin this generation while no
        # StateDB handle is open; a file observer reopens the original path.
        async with StateDB(**location) as observer:
            assert not await is_acknowledged(observer, event_id, "closed-store")
            release_effect.set()
            await _wait_for_attempt_release(key)
            assert await is_acknowledged(observer, event_id, "closed-store")
    finally:
        release_sync.set()
        release_effect.set()
        if "key" in locals():
            await _wait_for_attempt_release(key)
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["attempt_exact", "attempt_replay"])
async def test_late_returned_coroutine_does_not_ack_reopened_private_memory(monkeypatch, adapter):
    monkeypatch.setattr(reconciliation, "HANDLER_BUDGET_SECONDS", 0.05)
    db = StateDB(":memory:")
    started, release_sync, cancelled, release_effect = (threading.Event() for _ in range(4))

    async def effect():
        while not release_effect.is_set():
            try:
                await asyncio.sleep(0.005)
            except asyncio.CancelledError:
                cancelled.set()
        return DeliveryOutcome.SUCCESS

    def deliver():
        started.set()
        if not release_sync.wait(3):
            return DeliveryOutcome.FAILURE
        return effect()

    async def prepare(_row, _db):
        return PreparedDelivery(deliver)

    await db.open()
    try:
        event_id = await _seed(db)
        await register_consumer(db, "private-store", start="all")
        key = (reconciliation._store_identity(db), "private-store", event_id)
        runner = TerminalReconciler("private-store", prepare, batch_size=1, timeout=0.05)
        result = await getattr(runner, adapter)(db, event_id)
        assert result.status == "failed" and result.attempted and started.is_set()
        release_sync.set()
        await _until(cancelled.is_set)
        await db.close()
        await db.open()
        new_id = await _seed(db)
        await db.execute("UPDATE status_transitions SET id = ? WHERE id = ?", (event_id, new_id))
        await register_consumer(db, "private-store", start="all")
        release_effect.set()
        await _wait_for_attempt_release(key)
        assert not await is_acknowledged(db, event_id, "private-store")
    finally:
        release_sync.set()
        release_effect.set()
        if "key" in locals():
            await _wait_for_attempt_release(key)
        await db.close()
