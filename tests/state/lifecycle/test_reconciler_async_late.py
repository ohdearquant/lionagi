# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Offline probes of late terminal-delivery outcomes against a real SQLite store.

These are proposed behavioral tests. They do not claim cross-process exactly-once
delivery or completion after the event loop has shut down.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from lionagi.state.db import StateDB
from lionagi.state.lifecycle.deliveries import is_acknowledged, retire_consumer
from lionagi.state.lifecycle.reconciler import DeliveryOutcome, PreparedDelivery, TerminalReconciler


async def _seed(db: StateDB) -> str:
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


async def _wait_for_ack(db: StateDB, transition: str, consumer: str) -> bool:
    async def wait() -> None:
        while not await is_acknowledged(db, transition, consumer):
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(wait(), timeout=2)
    except asyncio.TimeoutError:
        return False
    return True


async def _wait_for_release(reconciler: TerminalReconciler) -> bool:
    async def wait() -> None:
        while reconciler._pending()[1]:
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(wait(), timeout=2)
    except asyncio.TimeoutError:
        return False
    return True


async def _cleanup(release: asyncio.Event, tasks: list[asyncio.Task]) -> None:
    release.set()
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2)


def _make_hook(release, started, tasks, calls, outcome):
    async def hook():
        tasks.append(asyncio.current_task())
        calls.append("started")
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # The side effect remains active even after the delivery timeout.
            await release.wait()
        return outcome

    return hook


def _prepare(hook, surface):
    async def prepare(_row, _db):
        if surface == "native-async":
            return PreparedDelivery(hook)
        return PreparedDelivery(lambda: hook())

    return prepare


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["native-async", "sync-returned-awaitable"])
@pytest.mark.parametrize("retired", [False, True], ids=["active", "retired"])
async def test_late_success_is_acknowledged_once_even_after_retirement(tmp_path, surface, retired):
    release, started = asyncio.Event(), asyncio.Event()
    tasks, calls = [], []
    consumer = f"late-success-{surface}-{retired}"
    hook = _make_hook(release, started, tasks, calls, DeliveryOutcome.SUCCESS)
    async with StateDB(tmp_path / "state.db") as db:
        transition = await _seed(db)
        reconciler = TerminalReconciler(
            consumer, _prepare(hook, surface), batch_size=1, timeout=0.05
        )
        try:
            timed_out = await reconciler.run_pass(db)
            assert started.is_set()
            assert timed_out["attempted"] == 1 and timed_out["failed"] == 1
            assert not await is_acknowledged(db, transition, consumer)
            if retired:
                assert await retire_consumer(db, consumer)
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2)
            assert await _wait_for_ack(db, transition, consumer)
            row = await db.fetch_one(
                "SELECT COUNT(*) AS n FROM terminal_deliveries WHERE transition_id = ? AND consumer = ?",
                (transition, consumer),
            )
            assert row["n"] == 1
            again = await reconciler.run_pass(db)
            assert again["attempted"] == 0
            assert calls == ["started"]
        finally:
            await _cleanup(release, tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["native-async", "sync-returned-awaitable"])
@pytest.mark.parametrize("outcome", [DeliveryOutcome.FAILURE, False], ids=["failure", "false"])
async def test_late_failure_stays_unacknowledged_with_one_backoff_and_no_overlap(
    tmp_path, surface, outcome
):
    release, started = asyncio.Event(), asyncio.Event()
    tasks, calls = [], []
    consumer = f"late-failure-{surface}"
    hook = _make_hook(release, started, tasks, calls, outcome)
    async with StateDB(tmp_path / "state.db") as db:
        transition = await _seed(db)
        prepare = _prepare(hook, surface)
        first = TerminalReconciler(
            consumer, prepare, batch_size=1, timeout=0.05, clock=lambda: 100.0
        )
        second = TerminalReconciler(
            consumer, prepare, batch_size=1, timeout=0.05, clock=lambda: 100.0
        )
        try:
            timed_out = await first.run_pass(db)
            assert started.is_set()
            assert timed_out["attempted"] == 1 and timed_out["failed"] == 1
            assert first._backoff[transition].attempts == 1
            blocked = await second.run_pass(db)
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2)
            assert await _wait_for_release(first)
            assert blocked["saturated"] == 1 and blocked["attempted"] == 0
            assert calls == ["started"]
            assert not await is_acknowledged(db, transition, consumer)
            assert first._backoff[transition].attempts == 1
            assert (await first.run_pass(db))["attempted"] == 0
        finally:
            await _cleanup(release, tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("completion", ["before-timeout", "after-timeout"])
async def test_completion_and_timeout_handoff_acknowledges_once(tmp_path, completion):
    release, started = asyncio.Event(), asyncio.Event()
    tasks, calls = [], []
    consumer = f"handoff-{completion}"
    hook = _make_hook(release, started, tasks, calls, DeliveryOutcome.SUCCESS)
    async with StateDB(tmp_path / "state.db") as db:
        transition = await _seed(db)
        reconciler = TerminalReconciler(
            consumer, _prepare(hook, "native-async"), batch_size=1, timeout=0.1
        )
        pass_task = asyncio.create_task(reconciler.run_pass(db))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            if completion == "before-timeout":
                release.set()
            first = await asyncio.wait_for(pass_task, timeout=2)
            if completion == "after-timeout":
                assert first["failed"] == 1
                release.set()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2)
            assert await _wait_for_ack(db, transition, consumer)
            row = await db.fetch_one(
                "SELECT COUNT(*) AS n FROM terminal_deliveries WHERE transition_id = ? AND consumer = ?",
                (transition, consumer),
            )
            assert row["n"] == 1
            assert (await reconciler.run_pass(db))["attempted"] == 0
            assert calls == ["started"]
        finally:
            release.set()
            await asyncio.gather(pass_task, return_exceptions=True)
            await _cleanup(release, tasks)
