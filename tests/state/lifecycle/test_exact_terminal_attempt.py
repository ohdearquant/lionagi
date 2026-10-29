# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Offline exact-event kernel and shared replay admission contracts."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import threading
import uuid

import pytest

from lionagi.state.db import StateDB
from lionagi.state.lifecycle import reconciler as reconciliation
from lionagi.state.lifecycle.deliveries import is_acknowledged, register_consumer, retire_consumer
from lionagi.state.lifecycle.reconciler import DeliveryOutcome, PreparedDelivery, TerminalReconciler


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


@pytest.mark.asyncio
async def test_exact_attempt_never_registers_or_drains_another_event(tmp_path):
    calls = []

    async def prepare(row, _db):
        async def deliver():
            calls.append(row["transition_id"])
            return DeliveryOutcome.SUCCESS

        return PreparedDelivery(deliver)

    async with StateDB(tmp_path / "state.db") as db:
        target, other = await _seed(db), await _seed(db)
        reconciler = TerminalReconciler("exact", prepare)
        assert (await reconciler.attempt_exact(db, target)).status == "not_owed"
        assert not await db.fetch_all("SELECT * FROM terminal_consumers")
        assert not calls
        await register_consumer(db, "exact", start="all")
        result = await reconciler.attempt_exact(db, target)
        assert result.status == "acknowledged" and result.attempted
        assert calls == [target]
        assert await is_acknowledged(db, target, "exact")
        assert not await is_acknowledged(db, other, "exact")
        assert (await reconciler.attempt_exact(db, target)).status == "not_owed"
        assert await retire_consumer(db, "exact")
        assert (await reconciler.attempt_exact(db, other)).status == "not_owed"
        assert calls == [target]


@pytest.mark.asyncio
async def test_exact_and_replay_share_admission_before_async_effect(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def prepare(row, _db):
        async def deliver():
            calls.append(row["transition_id"])
            entered.set()
            await release.wait()
            return DeliveryOutcome.SUCCESS

        return PreparedDelivery(deliver)

    async with StateDB(tmp_path / "state.db") as db:
        event_id = await _seed(db)
        await register_consumer(db, "shared", start="all")
        immediate = TerminalReconciler("shared", prepare, batch_size=1)
        replay = TerminalReconciler("shared", prepare, batch_size=1)
        task = asyncio.create_task(immediate.attempt_exact(db, event_id))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            result = await replay.attempt_replay(db, event_id)
            assert result.status == "busy" and calls == [event_id]
            release.set()
            assert (await task).status == "acknowledged"
            assert await is_acknowledged(db, event_id, "shared")
            assert (await replay.attempt_replay(db, event_id)).status == "not_owed"
            assert calls == [event_id]
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_during_ack_keeps_admission_until_transaction_finishes(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    original_ack = reconciliation.ack_delivery

    async def slow_ack(db, transition_id, consumer):
        entered.set()
        await release.wait()
        await original_ack(db, transition_id, consumer)

    async def prepare(row, _db):
        async def deliver():
            calls.append(row["transition_id"])
            return DeliveryOutcome.SUCCESS

        return PreparedDelivery(deliver)

    monkeypatch.setattr(reconciliation, "ack_delivery", slow_ack)
    async with StateDB(tmp_path / "state.db") as db:
        event_id = await _seed(db)
        await register_consumer(db, "ack-owner", start="all")
        first = TerminalReconciler("ack-owner", prepare, batch_size=1)
        second = TerminalReconciler("ack-owner", prepare, batch_size=1)
        task = asyncio.create_task(first.attempt_exact(db, event_id))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert (await second.attempt_exact(db, event_id)).status == "busy"
            assert not await is_acknowledged(db, event_id, "ack-owner")
            release.set()

            async def wait_for_ack_and_admission():
                while not await is_acknowledged(db, event_id, "ack-owner"):
                    await asyncio.sleep(0)
                while (await second.attempt_exact(db, event_id)).status == "busy":
                    await asyncio.sleep(0)

            await asyncio.wait_for(wait_for_ack_and_admission(), 2)
            assert calls == [event_id]
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_ack_response_error_is_resolved_from_ledger(tmp_path, monkeypatch):
    original_ack = reconciliation.ack_delivery

    async def committed_then_error(db, transition_id, consumer):
        await original_ack(db, transition_id, consumer)
        raise RuntimeError("ambiguous response")

    async def prepare(_row, _db):
        return DeliveryOutcome.SUCCESS

    monkeypatch.setattr(reconciliation, "ack_delivery", committed_then_error)
    async with StateDB(tmp_path / "state.db") as db:
        event_id = await _seed(db)
        await register_consumer(db, "ambiguous", start="all")
        result = await TerminalReconciler("ambiguous", prepare).attempt_exact(db, event_id)
        assert result.status == "acknowledged"
        assert await is_acknowledged(db, event_id, "ambiguous")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [DeliveryOutcome.NO_SESSION, DeliveryOutcome.AMBIGUOUS_PROJECT])
async def test_context_diagnostic_does_not_ack_exact_attempt(tmp_path, outcome):
    async def prepare(_row, _db):
        return outcome

    async with StateDB(tmp_path / "state.db") as db:
        event_id = await _seed(db)
        await register_consumer(db, "context", start="all")
        result = await TerminalReconciler("context", prepare).attempt_exact(db, event_id)
        assert result.status == "failed" and result.outcome is outcome
        assert not await is_acknowledged(db, event_id, "context")


@pytest.mark.asyncio
async def test_timed_out_sync_effect_stays_reserved_and_late_success_acks(tmp_path):
    started, release = threading.Event(), threading.Event()
    calls = []

    def deliver():
        calls.append("effect")
        started.set()
        return DeliveryOutcome.SUCCESS if release.wait(3) else DeliveryOutcome.FAILURE

    async def prepare(_row, _db):
        return PreparedDelivery(deliver)

    async with StateDB(tmp_path / "state.db") as db:
        event_id = await _seed(db)
        await register_consumer(db, "late", start="all")
        first = TerminalReconciler("late", prepare, batch_size=1, timeout=0.05)
        second = TerminalReconciler("late", prepare, batch_size=1, timeout=0.05)
        try:
            result = await first.attempt_exact(db, event_id)
            assert started.is_set() and result.status == "failed"
            assert (await second.attempt_exact(db, event_id)).status == "busy"
            assert calls == ["effect"]
            release.set()

            async def wait_for_late_ack():
                while not await is_acknowledged(db, event_id, "late"):
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_late_ack(), 2)
            assert calls == ["effect"]
        finally:
            release.set()


@pytest.mark.asyncio
async def test_failed_effect_remains_owed(tmp_path):
    async def prepare(_row, _db):
        async def deliver():
            return DeliveryOutcome.FAILURE

        return PreparedDelivery(deliver)

    async with StateDB(tmp_path / "state.db") as db:
        event_id = await _seed(db)
        await register_consumer(db, "failure", start="all")
        result = await TerminalReconciler("failure", prepare).attempt_exact(db, event_id)
        assert result.status == "failed" and result.attempted
        assert not await is_acknowledged(db, event_id, "failure")


@pytest.mark.asyncio
async def test_cancel_resistant_async_effect_keeps_event_admission_until_finished(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def prepare(_row, _db):
        async def deliver():
            calls.append("effect")
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return DeliveryOutcome.SUCCESS

        return PreparedDelivery(deliver)

    async with StateDB(tmp_path / "state.db") as db:
        event_id = await _seed(db)
        await register_consumer(db, "async-late", start="all")
        first = TerminalReconciler("async-late", prepare, batch_size=1, timeout=0.05)
        second = TerminalReconciler("async-late", prepare, batch_size=1, timeout=0.05)
        try:
            result = await first.attempt_exact(db, event_id)
            assert entered.is_set() and result.status == "failed"
            assert (await second.attempt_exact(db, event_id)).status == "busy"
            assert calls == ["effect"]
            release.set()

            async def wait_for_admission():
                while (await second.attempt_exact(db, event_id)).status == "busy":
                    await asyncio.sleep(0)

            await asyncio.wait_for(wait_for_admission(), 2)
            assert len(calls) == 2
        finally:
            release.set()


@pytest.mark.asyncio
async def test_unreadable_ledger_after_ambiguous_ack_reports_uncertain(tmp_path, monkeypatch):
    original_ack = reconciliation.ack_delivery

    async def committed_then_error(db, transition_id, consumer):
        await original_ack(db, transition_id, consumer)
        raise RuntimeError("response lost")

    async def unavailable(*_args):
        raise OSError("ledger unavailable")

    async def prepare(_row, _db):
        return DeliveryOutcome.SUCCESS

    monkeypatch.setattr(reconciliation, "ack_delivery", committed_then_error)
    monkeypatch.setattr(reconciliation, "is_acknowledged", unavailable)
    async with StateDB(tmp_path / "state.db") as db:
        event_id = await _seed(db)
        await register_consumer(db, "unknown-ack", start="all")
        result = await TerminalReconciler("unknown-ack", prepare).attempt_exact(db, event_id)
        assert result.status == "uncertain" and result.attempted
        assert await is_acknowledged(db, event_id, "unknown-ack")


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["attempt_exact", "attempt_replay"])
@pytest.mark.parametrize(
    ("late_outcome", "acknowledged"),
    [
        (DeliveryOutcome.NO_SESSION, False),
        (DeliveryOutcome.AMBIGUOUS_PROJECT, False),
        (DeliveryOutcome.SUCCESS, True),
    ],
)
async def test_late_sync_outcome_obeys_exact_policy(tmp_path, adapter, late_outcome, acknowledged):
    started, release = threading.Event(), threading.Event()

    def deliver():
        started.set()
        return late_outcome if release.wait(3) else DeliveryOutcome.FAILURE

    async def prepare(_row, _db):
        return PreparedDelivery(deliver)

    async with StateDB(tmp_path / "state.db") as db:
        event_id = await _seed(db)
        await register_consumer(db, "late-policy", start="all")
        reconciler = TerminalReconciler("late-policy", prepare, batch_size=1, timeout=0.05)
        try:
            result = await getattr(reconciler, adapter)(db, event_id)
            assert result.status == "failed" and started.is_set()
            key = (reconciliation._store_identity(db), "late-policy", event_id)
            with reconciliation._workers_lock:
                assert key in reconciliation._workers
            release.set()

            async def wait_for_worker_release():
                while True:
                    with reconciliation._workers_lock:
                        active = key in reconciliation._workers
                    if not active:
                        return
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_worker_release(), 2)
            assert await is_acknowledged(db, event_id, "late-policy") is acknowledged
        finally:
            release.set()
