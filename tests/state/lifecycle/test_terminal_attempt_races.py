# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Offline races for real reconciliation and exact-event admission."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import uuid

import pytest

from lionagi.state.db import StateDB
from lionagi.state.lifecycle import reconciler as kernel
from lionagi.state.lifecycle.callbacks import TerminalCallbackRegistry
from lionagi.state.lifecycle.deliveries import (
    ack_delivery,
    change_consumer,
    is_acknowledged,
    register_consumer,
)
from lionagi.state.lifecycle.reconciler import DeliveryOutcome, PreparedDelivery, TerminalReconciler


def _forbid(*_args, **_kwargs):
    raise AssertionError("offline test forbids processes, signals and network")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
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
    sid, progression = uuid.uuid4().hex, uuid.uuid4().hex
    await db.create_progression(progression)
    await db.create_session({"id": sid, "progression_id": progression, "status": "running"})
    db._lifecycle_service()._terminal_callbacks = TerminalCallbackRegistry()
    assert await db.update_status(
        "session", sid, new_status="failed", reason_code="session.stale.no_heartbeat"
    )
    row = await db.fetch_one(
        "SELECT id FROM status_transitions WHERE entity_type = ? AND entity_id = ? AND status = ?",
        ("session", sid, "failed"),
    )
    return row["id"]


async def _wait_admission_released(db, consumer, event_id):
    key = (kernel._store_identity(db), consumer, event_id)

    async def wait():
        while True:
            with kernel._workers_lock:
                active = key in kernel._attempts or key in kernel._workers
            if not active:
                return
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("commits", [False, True], ids=["failure-before-commit", "late-commit"])
@pytest.mark.parametrize("origin", ["timeout", "direct-cancel"])
async def test_ack_cancellation_is_uncertain_or_propagated_and_reserved(
    tmp_path, monkeypatch, caplog, commits, origin
):
    entered, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []
    original_ack = kernel.ack_delivery
    secret = "private-ack-exception-sentinel"
    caplog.set_level("INFO", logger=kernel.__name__)

    async def blocked_ack(db, transition_id, consumer):
        entered.set()
        try:
            await release.wait()
            if commits:
                await original_ack(db, transition_id, consumer)
            else:
                raise OSError(secret)
        finally:
            settled.set()

    async def prepare(row, _db):
        async def effect():
            calls.append(row["transition_id"])
            return DeliveryOutcome.SUCCESS

        return PreparedDelivery(effect)

    monkeypatch.setattr(kernel, "ack_delivery", blocked_ack)
    async with StateDB(tmp_path / "state.db") as db:
        event_id = await _seed(db)
        await register_consumer(db, "ack-timeout", start="all")
        first = TerminalReconciler("ack-timeout", prepare, batch_size=1, timeout=0.10)
        second = TerminalReconciler("ack-timeout", prepare, batch_size=1, timeout=0.10)
        task = asyncio.create_task(first.attempt_exact(db, event_id))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            if origin == "timeout":
                result = await asyncio.wait_for(task, 1)
                assert result.status == "uncertain"
                assert result.outcome is DeliveryOutcome.SUCCESS and result.attempted is True
            else:
                task.cancel("caller cancelled during ACK")
                with pytest.raises(asyncio.CancelledError) as caught:
                    await asyncio.wait_for(task, 1)
                if sys.version_info >= (3, 11):
                    assert caught.value.args == ("caller cancelled during ACK",)
            assert (await second.attempt_exact(db, event_id)).status == "busy"
            assert not await is_acknowledged(db, event_id, "ack-timeout")
            assert calls == [event_id]
            release.set()
            await asyncio.wait_for(settled.wait(), 1)
            await _wait_admission_released(db, "ack-timeout", event_id)
            assert await is_acknowledged(db, event_id, "ack-timeout") is commits
            assert secret not in caplog.text
            assert (
                f"Detached ACK completed for {event_id}"
                if commits
                else f"Detached ACK failed for {event_id}: OSError"
            ) in caplog.text
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await _wait_admission_released(db, "ack-timeout", event_id)


@pytest.mark.asyncio
async def test_ack_on_other_handle_during_prepare_suppresses_effect(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def prepare(row, _db):
        entered.set()
        await release.wait()

        async def effect():
            calls.append(row["transition_id"])
            return DeliveryOutcome.SUCCESS

        return PreparedDelivery(effect)

    path = tmp_path / "state.db"
    async with StateDB(path) as first, StateDB(path) as second:
        event_id = await _seed(first)
        await register_consumer(first, "recheck", start="all")
        reconciler = TerminalReconciler("recheck", prepare)
        task = asyncio.create_task(reconciler.attempt_exact(first, event_id))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await ack_delivery(second, event_id, "recheck")
            release.set()
            result = await asyncio.wait_for(task, 1)
            assert result.status == "not_owed" and not result.attempted
            assert calls == []
            assert await is_acknowledged(first, event_id, "recheck")
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["cancel", "timeout"])
async def test_cancel_resistant_preparation_keeps_admission_until_settled(tmp_path, origin):
    entered, cancellation_seen, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    effects = []

    async def prepare(_row, _db):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await release.wait()

        async def effect():
            effects.append("effect")
            return DeliveryOutcome.SUCCESS

        return PreparedDelivery(effect)

    async with StateDB(tmp_path / "state.db") as db:
        event_id = await _seed(db)
        await register_consumer(db, "preparation", start="all")
        first = TerminalReconciler("preparation", prepare, batch_size=1, timeout=0.10)
        second = TerminalReconciler("preparation", prepare, batch_size=1, timeout=0.10)
        task = asyncio.create_task(first.attempt_exact(db, event_id))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            if origin == "cancel":
                task.cancel("caller cancelled")
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1)
            else:
                result = await asyncio.wait_for(task, 1)
                assert result.status == "failed" and not result.attempted
            await asyncio.wait_for(cancellation_seen.wait(), 1)
            assert (await second.attempt_exact(db, event_id)).status == "busy"
            assert effects == [] and not await is_acknowledged(db, event_id, "preparation")
            release.set()
            await _wait_admission_released(db, "preparation", event_id)
            assert effects == []
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await _wait_admission_released(db, "preparation", event_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("start", [None, "unexpected"], ids=["missing", "unexpected"])
async def test_retired_reactivation_requires_valid_explicit_start_without_writes(tmp_path, start):
    async with StateDB(tmp_path / "state.db") as db:
        event_id = await _seed(db)
        await register_consumer(db, "retired", start="all")
        await change_consumer(db, "retired", action="retire")
        before = await db.fetch_one(
            "SELECT name, registered_at, retired_at FROM terminal_consumers WHERE name = ?",
            ("retired",),
        )
        with pytest.raises(ValueError):
            await change_consumer(db, "retired", action="reactivate", start=start)
        after = await db.fetch_one(
            "SELECT name, registered_at, retired_at FROM terminal_consumers WHERE name = ?",
            ("retired",),
        )
        assert dict(after) == dict(before)
        assert not await is_acknowledged(db, event_id, "retired")
