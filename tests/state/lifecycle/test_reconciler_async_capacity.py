# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Offline regressions for retained capacity after an async hook timeout."""

import asyncio
import uuid

import pytest

from lionagi.state.db import StateDB
from lionagi.state.lifecycle.deliveries import is_acknowledged
from lionagi.state.lifecycle.reconciler import DeliveryOutcome, PreparedDelivery, TerminalReconciler


async def _seed(db):
    sid, progression = str(uuid.uuid4()), str(uuid.uuid4())
    await db.create_progression(progression)
    await db.create_session({"id": sid, "progression_id": progression, "status": "running"})
    assert await db.update_status(
        "session",
        sid,
        new_status="failed",
        reason_code="session.stale.no_heartbeat",
        source="system",
        actor="test",
    )
    row = await db.fetch_one(
        "SELECT id FROM status_transitions WHERE entity_type = ? AND entity_id = ? AND status = ?",
        ("session", sid, "failed"),
    )
    return row["id"]


async def _cleanup(release, active):
    release.set()
    if active:
        await asyncio.wait_for(asyncio.gather(*active, return_exceptions=True), timeout=2)


@pytest.mark.asyncio
async def test_native_async_timeout_keeps_capacity_reserved(tmp_path):
    release = asyncio.Event()
    active, calls = [], []

    async def hook():
        active.append(asyncio.current_task())
        calls.append("started")
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()  # Deliberately resist cancellation until the test releases us.
        return DeliveryOutcome.SUCCESS

    async def prepare(_row, _db):
        return PreparedDelivery(hook)

    async with StateDB(tmp_path / "state.db") as db:
        transition = await _seed(db)
        first = TerminalReconciler("native-async-capacity", prepare, batch_size=1, timeout=0.02)
        second = TerminalReconciler("native-async-capacity", prepare, batch_size=1, timeout=0.02)
        try:
            result = await first.run_pass(db)
            assert result["attempted"] == 1 and result["failed"] == 1
            assert len(calls) == 1 and not active[0].done()
            # This capacity must be shared across instances, not just the first pass.
            assert not second._backoff and second._cursor is None
            blocked = await second.run_pass(db)
            assert (
                blocked["saturated"] == 1 and blocked["attempted"] == 0 and calls == ["started"]
            ), (
                f"second pass={blocked!r}, hook calls={calls!r}, active={sum(not task.done() for task in active)}"
            )
            assert not await is_acknowledged(db, transition, "native-async-capacity")
        finally:
            await _cleanup(release, active)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["sync-factory", "settings-python"])
async def test_sync_factory_returned_awaitable_cannot_overlap_after_timeout(
    tmp_path, monkeypatch, surface
):
    release = asyncio.Event()
    active, calls = [], []

    async def hook():
        active.append(asyncio.current_task())
        calls.append("started")
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return DeliveryOutcome.SUCCESS

    # This is a synchronous factory returning an awaitable, distinct from a
    # coroutine function. The current path reserves a worker then releases it.
    def hook_factory():
        return hook()

    from lionagi.state.lifecycle import settings_delivery
    from lionagi.state.lifecycle.callbacks import EntityRef, RunTerminalEnvelope
    from lionagi.state.lifecycle.notify_settings import ResolvedNotifyHandler

    monkeypatch.setattr(settings_delivery, "build_handler", lambda _resolved: lambda _event: hook())
    envelope = RunTerminalEnvelope(
        event_id="transition",
        entity=EntityRef(kind="session", id="session"),
        previous_status="running",
        terminal_status="failed",
        reason_code="session.failed",
        occurred_at=1.0,
    )

    async def prepare(_row, _db):
        if surface == "settings-python":
            return settings_delivery.prepare_resolved_delivery(
                ResolvedNotifyHandler(python_ref="test:hook"), envelope
            )
        return PreparedDelivery(hook_factory)

    async with StateDB(tmp_path / "state.db") as db:
        transition = await _seed(db)
        first = TerminalReconciler("awaitable-capacity", prepare, batch_size=1, timeout=0.02)
        second = TerminalReconciler("awaitable-capacity", prepare, batch_size=1, timeout=0.02)
        try:
            result = await first.run_pass(db)
            assert result["attempted"] == 1 and result["failed"] == 1
            assert len(calls) == 1 and not active[0].done()
            assert not second._backoff and second._cursor is None
            blocked = await second.run_pass(db)
            assert (
                blocked["saturated"] == 1 and blocked["attempted"] == 0 and calls == ["started"]
            ), (
                f"second pass={blocked!r}, hook calls={calls!r}, active={sum(not task.done() for task in active)}"
            )
            assert not await is_acknowledged(db, transition, "awaitable-capacity")
        finally:
            await _cleanup(release, active)
