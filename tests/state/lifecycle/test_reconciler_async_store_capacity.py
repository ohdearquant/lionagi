# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Offline proposed coverage for asynchronous terminal-delivery reservations.

Uses private SQLite stores and local synchronization only. These tests do not
claim delivery or late acknowledgement after the owning event loop has closed.
"""

from __future__ import annotations

import asyncio
import threading
import uuid

import pytest

from lionagi.state.db import StateDB
from lionagi.state.lifecycle import reconciler as reconciliation
from lionagi.state.lifecycle.deliveries import is_acknowledged
from lionagi.state.lifecycle.reconciler import DeliveryOutcome, PreparedDelivery, TerminalReconciler


async def _seed(db):
    session_id, progression_id = str(uuid.uuid4()), str(uuid.uuid4())
    await db.create_progression(progression_id)
    await db.create_session(
        {"id": session_id, "progression_id": progression_id, "status": "running"}
    )
    assert await db.update_status(
        "session",
        session_id,
        new_status="failed",
        reason_code="session.stale.no_heartbeat",
        source="system",
        actor="test",
    )
    row = await db.fetch_one(
        "SELECT id FROM status_transitions WHERE entity_type = ? AND entity_id = ? AND status = ?",
        ("session", session_id, "failed"),
    )
    return row["id"]


async def _eventually(predicate, timeout=2):
    async def poll():
        while not await predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout)


@pytest.mark.asyncio
async def test_async_reservation_is_shared_across_two_sqlite_url_aliases(tmp_path):
    path = tmp_path / "state.db"
    release = asyncio.Event()
    tasks, calls = [], []

    async def hook():
        tasks.append(asyncio.current_task())
        calls.append("started")
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return DeliveryOutcome.SUCCESS

    async def prepare(_row, _db):
        return PreparedDelivery(hook)

    async with (
        StateDB(path) as first_db,
        StateDB(url=f"sqlite+aiosqlite:///{path}?timeout=5") as second_db,
    ):
        transition = await _seed(first_db)
        first = TerminalReconciler("alias-async", prepare, batch_size=1, timeout=0.05)
        second = TerminalReconciler("alias-async", prepare, batch_size=1, timeout=0.05)
        try:
            initial = await first.run_pass(first_db)
            assert initial["attempted"] == initial["failed"] == 1
            assert calls == ["started"] and not tasks[0].done()
            assert not await is_acknowledged(second_db, transition, "alias-async")
            blocked = await second.run_pass(second_db)
            assert blocked["saturated"] == 1 and blocked["attempted"] == 0
            assert calls == ["started"]
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)

            async def acknowledged():
                return await is_acknowledged(second_db, transition, "alias-async")

            await _eventually(acknowledged)
            assert calls == ["started"]
        finally:
            release.set()
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_detached_sync_factory_keeps_slot_for_its_late_awaitable(tmp_path, monkeypatch):
    # The factory finishes only after the worker timeout. Threading events are
    # safe whether the returned coroutine runs on this loop or a late loop.
    factory_release, async_release = threading.Event(), threading.Event()
    factory_started, async_started, async_finished = (threading.Event() for _ in range(3))
    calls = []
    monkeypatch.setattr(reconciliation, "HANDLER_BUDGET_SECONDS", 0.05)

    async def late_hook():
        calls.append("async")
        async_started.set()
        try:
            while not async_release.is_set():
                try:
                    await asyncio.sleep(0.005)
                except asyncio.CancelledError:
                    # Keep the side effect active through any repeated cancel.
                    pass
            return DeliveryOutcome.SUCCESS
        finally:
            async_finished.set()

    def factory():
        calls.append("factory")
        factory_started.set()
        if not factory_release.wait(2):
            return DeliveryOutcome.FAILURE
        return late_hook()

    async def prepare(_row, _db):
        return PreparedDelivery(factory)

    async with StateDB(tmp_path / "state.db") as db:
        transition = await _seed(db)
        first = TerminalReconciler("late-factory", prepare, batch_size=1, timeout=0.05)
        second = TerminalReconciler("late-factory", prepare, batch_size=1, timeout=0.05)
        try:
            initial = await first.run_pass(db)
            assert factory_started.is_set() and initial["failed"] == 1
            factory_release.set()

            async def started():
                return async_started.is_set()

            await _eventually(started)
            # Outlast the detached path's await budget while the child continues.
            await asyncio.sleep(0.15)
            assert not async_finished.is_set()
            blocked = await second.run_pass(db)
            assert blocked["saturated"] == 1 and blocked["attempted"] == 0
            assert calls == ["factory", "async"]
            assert not await is_acknowledged(db, transition, "late-factory")
            async_release.set()

            async def acknowledged():
                return await is_acknowledged(db, transition, "late-factory")

            await _eventually(acknowledged)
            assert calls == ["factory", "async"]
        finally:
            factory_release.set()
            async_release.set()

            async def settled():
                return first._pending()[1] == 0

            await _eventually(settled)


@pytest.mark.asyncio
async def test_late_success_ack_error_remains_unacknowledged_and_recoverable(tmp_path, monkeypatch):
    release = asyncio.Event()
    ack_failed = threading.Event()
    tasks, calls = [], []
    real_ack = reconciliation.ack_delivery

    async def fail_ack(*_args, **_kwargs):
        ack_failed.set()
        raise RuntimeError("injected acknowledgement write failure")

    async def hook():
        tasks.append(asyncio.current_task())
        calls.append("called")
        if len(calls) == 1:
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
        return DeliveryOutcome.SUCCESS

    async def prepare(_row, _db):
        return PreparedDelivery(hook)

    async with StateDB(tmp_path / "state.db") as db:
        transition = await _seed(db)
        first = TerminalReconciler("late-ack-failure", prepare, batch_size=1, timeout=0.05)
        try:
            result = await first.run_pass(db)
            assert result["failed"] == 1 and calls == ["called"]
            monkeypatch.setattr(reconciliation, "ack_delivery", fail_ack)
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)

            async def failed():
                return ack_failed.is_set()

            await _eventually(failed)
            assert not await is_acknowledged(db, transition, "late-ack-failure")
            monkeypatch.setattr(reconciliation, "ack_delivery", real_ack)
            retry = TerminalReconciler("late-ack-failure", prepare, batch_size=1, timeout=0.1)

            async def recovered():
                if await is_acknowledged(db, transition, "late-ack-failure"):
                    return True
                await retry.run_pass(db)
                return await is_acknowledged(db, transition, "late-ack-failure")

            await _eventually(recovered)
            row = await db.fetch_one(
                "SELECT COUNT(*) AS n FROM terminal_deliveries "
                "WHERE transition_id = ? AND consumer = ?",
                (transition, "late-ack-failure"),
            )
            assert row["n"] == 1
        finally:
            release.set()
            monkeypatch.setattr(reconciliation, "ack_delivery", real_ack)
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
