# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A cancelled pass must retain capacity for a hook still executing."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from lionagi.state.db import StateDB
from lionagi.state.lifecycle.deliveries import is_acknowledged
from lionagi.state.lifecycle.reconciler import DeliveryOutcome, PreparedDelivery, TerminalReconciler


async def _terminal(db: StateDB) -> str:
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


async def _eventually(predicate, *, timeout=2.0):
    async def wait():
        while not await predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["direct-async", "settings-python"])
async def test_cancelled_pass_keeps_resistant_hook_reserved_until_late_completion(
    tmp_path, monkeypatch, surface
):
    from lionagi.state.lifecycle import settings_delivery
    from lionagi.state.lifecycle.notify_settings import ResolvedNotifyHandler
    from tests.state.lifecycle.test_settings_delivery import _event

    started, cancellation_seen, release, finished = (asyncio.Event() for _ in range(4))
    calls = []
    hook_tasks = []

    async def hook(_envelope):
        calls.append("called")
        if len(calls) > 1:
            return None
        hook_tasks.append(asyncio.current_task())
        started.set()
        try:
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await release.wait()
            return None
        finally:
            finished.set()

    monkeypatch.setattr(settings_delivery, "build_handler", lambda _resolved: hook)

    async def prepare(_row, _db):
        if surface == "direct-async":

            async def invoke():
                await hook(_event())
                return DeliveryOutcome.SUCCESS

            return PreparedDelivery(invoke)
        return settings_delivery.prepare_resolved_delivery(
            ResolvedNotifyHandler(python_ref="test:hook"), _event()
        )

    async with StateDB(tmp_path / "state.db") as db:
        first_id, next_id = await _terminal(db), await _terminal(db)
        for timestamp, transition_id in ((1, first_id), (2, next_id)):
            await db.execute(
                "UPDATE status_transitions SET created_at = ? WHERE id = ?",
                (timestamp, transition_id),
            )
        first = TerminalReconciler("cancel-capacity", prepare, batch_size=1, timeout=5)
        second = TerminalReconciler("cancel-capacity", prepare, batch_size=1, timeout=5)
        pass_task = asyncio.create_task(first.run_pass(db))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            pass_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pass_task, timeout=2)
            await asyncio.wait_for(cancellation_seen.wait(), timeout=2)
            assert not finished.is_set()
            assert not await is_acknowledged(db, first_id, "cancel-capacity")

            # A separate reconciler for the same consumer sees the very same
            # reservation, and cannot start even the next transition yet.
            blocked = await asyncio.wait_for(second.run_pass(db), timeout=2)
            assert blocked["saturated"] == 1
            assert blocked["attempted"] == 0
            assert calls == ["called"]
            assert not await is_acknowledged(db, next_id, "cancel-capacity")

            release.set()
            await asyncio.wait_for(finished.wait(), timeout=2)

            async def late_ack():
                return await is_acknowledged(db, first_id, "cancel-capacity")

            async def released():
                return second._pending()[1] == 0

            await _eventually(late_ack)
            await _eventually(released)
            completed = await asyncio.wait_for(second.run_pass(db), timeout=2)
            assert completed["attempted"] == completed["acknowledged"] == 1
            assert await is_acknowledged(db, next_id, "cancel-capacity")
            assert calls == ["called", "called"]
        finally:
            release.set()
            if not pass_task.done():
                pass_task.cancel()
            await asyncio.gather(pass_task, return_exceptions=True)
            if hook_tasks:
                await asyncio.wait_for(asyncio.gather(*hook_tasks, return_exceptions=True), 2)
