# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Strict late synchronous results preserve a returned awaitable until settlement."""

from __future__ import annotations

import asyncio
import inspect
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
from lionagi.state.lifecycle.reconciler import (
    AttemptResult,
    DeliveryOutcome,
    PreparedDelivery,
    TerminalReconciler,
)
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


async def _first_attempt(runner, adapter, db, event_id, key, started, tries=3):
    """Run the first attempt, repeating it only while the delivery hook never began.

    One time budget covers the state reads before the hook as well as the hook, so a
    loaded runner can spend all of it before the hook starts; the attempt is then
    correctly reported as not attempted. Repeating it is valid only while ``started``
    is unset and the slot is released. An attempt whose hook ran but is reported as
    not attempted, or one that never begins within ``tries`` attempts, is returned
    unchanged so the caller's assertion still fails.
    """
    result = await getattr(runner, adapter)(db, event_id)
    for _ in range(tries - 1):
        if started.is_set() or result.status != "failed" or result.attempted:
            break
        await _wait_for_attempt_release(key)
        result = await getattr(runner, adapter)(db, event_id)
    return result


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
        if not release_sync.wait(5):
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
        first = TerminalReconciler("late-return", prepare, batch_size=1, timeout=1.0)
        second = TerminalReconciler("late-return", prepare, batch_size=1, timeout=1.0)
        key = (reconciliation._store_identity(db), "late-return", event_id)
        with caplog.at_level(logging.WARNING, logger=reconciliation.__name__):
            try:
                first_result = await _first_attempt(first, adapter, db, event_id, key, sync_started)
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
        if not release_sync.wait(5):
            return DeliveryOutcome.FAILURE
        return effect()

    async def prepare(_row, _db):
        return PreparedDelivery(deliver)

    async with StateDB(tmp_path / "state.db") as db:
        event_id = await _seed(db)
        await register_consumer(db, "cooperative", start="all")
        worker = TerminalReconciler("cooperative", prepare, batch_size=1, timeout=1.0)
        key = (reconciliation._store_identity(db), "cooperative", event_id)
        try:
            result = await _first_attempt(worker, adapter, db, event_id, key, sync_started)
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
        if not release_sync.wait(5):
            return DeliveryOutcome.FAILURE
        coroutine = effect()
        return coroutine if shape == "coroutine" else _Awaitable(coroutine)

    async def prepare(_row, _db):
        return PreparedDelivery(deliver)

    async with StateDB(tmp_path / "state.db") as db:
        event_id = await _seed(db)
        await register_consumer(db, "fast-late", start="all")
        runner = TerminalReconciler("fast-late", prepare, batch_size=1, timeout=1.0)
        key = (reconciliation._store_identity(db), "fast-late", event_id)
        try:
            result = await _first_attempt(runner, adapter, db, event_id, key, sync_started)
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
        if not release_sync.wait(5):
            return DeliveryOutcome.FAILURE
        return effect()

    async def prepare(_row, _db):
        return PreparedDelivery(deliver)

    await db.open()
    try:
        event_id = await _seed(db)
        await register_consumer(db, "closed-store", start="all")
        key = (reconciliation._store_identity(db), "closed-store", event_id)
        runner = TerminalReconciler("closed-store", prepare, batch_size=1, timeout=1.0)
        result = await _first_attempt(runner, adapter, db, event_id, key, started)
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
        if not release_sync.wait(5):
            return DeliveryOutcome.FAILURE
        return effect()

    async def prepare(_row, _db):
        return PreparedDelivery(deliver)

    await db.open()
    try:
        event_id = await _seed(db)
        await register_consumer(db, "private-store", start="all")
        key = (reconciliation._store_identity(db), "private-store", event_id)
        runner = TerminalReconciler("private-store", prepare, batch_size=1, timeout=1.0)
        result = await _first_attempt(runner, adapter, db, event_id, key, started)
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


def _stall_first_state_read(monkeypatch):
    """Make the first owed-event read outlive the attempt budget.

    This models a loaded runner on which the state reads that precede the delivery
    hook consume the whole budget. It needs neither a sleep nor machine load.
    """
    real = reconciliation.get_owed_transition
    reads = []

    async def read(db, consumer, transition_id):
        reads.append(transition_id)
        if len(reads) == 1:
            await asyncio.Event().wait()
        return await real(db, consumer, transition_id)

    monkeypatch.setattr(reconciliation, "get_owed_transition", read)
    return reads


_STALLED_READ_SCENARIOS = [
    pytest.param(
        test_late_returned_awaitable_keeps_capacity_through_settlement_and_ack,
        {"shape": "custom", "outcome": DeliveryOutcome.SUCCESS, "acknowledged": True},
        id="settlement-and-ack",
    ),
    pytest.param(
        test_late_returned_coroutine_cooperative_cancel_leaves_delivery_owed,
        {},
        id="cooperative-cancel",
    ),
    pytest.param(
        test_late_returned_awaitable_success_before_inner_budget_acks,
        {"shape": "custom"},
        id="success-before-inner-budget",
    ),
    pytest.param(
        test_late_returned_coroutine_acks_original_store_after_handle_close,
        {"store": "file"},
        id="closed-file-store",
    ),
    pytest.param(
        test_late_returned_coroutine_does_not_ack_reopened_private_memory,
        {},
        id="reopened-private-memory",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("scenario", "extra"), _STALLED_READ_SCENARIOS)
async def test_scenarios_survive_a_stalled_first_state_read(
    tmp_path, monkeypatch, caplog, scenario, extra
):
    """The attempt budget also covers the reads before the hook; a stall there is retried."""
    reads = _stall_first_state_read(monkeypatch)
    available = {"tmp_path": tmp_path, "monkeypatch": monkeypatch, "caplog": caplog}
    wanted = inspect.signature(scenario).parameters
    kwargs = {name: value for name, value in available.items() if name in wanted}
    await scenario(adapter="attempt_replay", **kwargs, **extra)
    assert len(reads) >= 2


class _ScriptedRunner:
    """Answers each attempt with the next scripted result and counts the attempts."""

    consumer = "scripted"

    def __init__(self, *results):
        self.results = list(results)
        self.attempts = 0

    async def attempt_replay(self, _db, _transition_id):
        self.attempts += 1
        return self.results.pop(0)


_NOT_STARTED = AttemptResult("failed", attempted=False)
_STARTED = AttemptResult("failed", attempted=True)
_SCRIPTED_KEY = ("store", "scripted", "event")


@pytest.mark.asyncio
async def test_first_attempt_repeats_only_an_attempt_that_never_began_the_effect():
    started = threading.Event()
    runner = _ScriptedRunner(_NOT_STARTED, _STARTED)
    result = await _first_attempt(runner, "attempt_replay", None, "event", _SCRIPTED_KEY, started)
    assert result == _STARTED and runner.attempts == 2


@pytest.mark.asyncio
async def test_first_attempt_returns_a_misreported_attempt_instead_of_repeating_it():
    started = threading.Event()
    started.set()
    runner = _ScriptedRunner(_NOT_STARTED, _STARTED)
    result = await _first_attempt(runner, "attempt_replay", None, "event", _SCRIPTED_KEY, started)
    assert result == _NOT_STARTED and runner.attempts == 1


@pytest.mark.asyncio
async def test_first_attempt_gives_up_after_three_attempts_without_an_effect():
    started = threading.Event()
    runner = _ScriptedRunner(_NOT_STARTED, _NOT_STARTED, _NOT_STARTED, _STARTED)
    result = await _first_attempt(runner, "attempt_replay", None, "event", _SCRIPTED_KEY, started)
    assert result == _NOT_STARTED and runner.attempts == 3
