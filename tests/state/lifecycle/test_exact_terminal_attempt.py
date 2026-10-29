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
@pytest.mark.parametrize("adapter", ["attempt_exact", "attempt_replay"])
@pytest.mark.parametrize("delivery_kind", ["async", "sync-returning-awaitable"])
@pytest.mark.parametrize(
    ("late_outcome", "acknowledged"),
    [
        (DeliveryOutcome.SUCCESS, True),
        (DeliveryOutcome.NOT_APPLICABLE, True),
        (DeliveryOutcome.FAILURE, False),
        (DeliveryOutcome.NO_SESSION, False),
        (DeliveryOutcome.AMBIGUOUS_PROJECT, False),
    ],
)
async def test_cancel_resistant_async_effect_keeps_event_admission_until_finished(
    tmp_path, adapter, delivery_kind, late_outcome, acknowledged
):
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
            return late_outcome

        if delivery_kind == "async":
            return PreparedDelivery(deliver)
        return PreparedDelivery(lambda: deliver())

    async with StateDB(tmp_path / "state.db") as db:
        event_id = await _seed(db)
        await register_consumer(db, "async-late", start="all")
        first = TerminalReconciler("async-late", prepare, batch_size=1, timeout=0.05)
        second = TerminalReconciler("async-late", prepare, batch_size=1, timeout=0.05)
        try:
            result = await getattr(first, adapter)(db, event_id)
            assert entered.is_set() and result.status == "failed"
            assert (await second.attempt_exact(db, event_id)).status == "busy"
            assert calls == ["effect"]
            release.set()

            key = (reconciliation._store_identity(db), "async-late", event_id)

            async def wait_for_admission():
                while True:
                    with reconciliation._workers_lock:
                        active = key in reconciliation._attempts or key in reconciliation._workers
                    if not active:
                        return
                    await asyncio.sleep(0)

            await asyncio.wait_for(wait_for_admission(), 2)
            assert await is_acknowledged(db, event_id, "async-late") is acknowledged
            next_result = await getattr(second, adapter)(db, event_id)
            assert next_result.status == ("not_owed" if acknowledged else "failed")
            assert len(calls) == (1 if acknowledged else 2)
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


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["timeout", "direct-cancel"])
@pytest.mark.parametrize("ack_succeeds", [False, True])
async def test_late_async_success_retains_admission_until_ack_settles(
    tmp_path, monkeypatch, caplog, origin, ack_succeeds
):
    effect_entered, release_effect = asyncio.Event(), asyncio.Event()
    ack_entered, release_ack = asyncio.Event(), asyncio.Event()
    calls = []
    original_ack = reconciliation.ack_delivery
    secret = "private-late-ack-failure"

    async def blocked_ack(db, transition_id, consumer):
        ack_entered.set()
        await release_ack.wait()
        if not ack_succeeds:
            raise OSError(secret)
        await original_ack(db, transition_id, consumer)

    async def prepare(_row, _db):
        async def deliver():
            calls.append("effect")
            effect_entered.set()
            try:
                await release_effect.wait()
            except asyncio.CancelledError:
                await release_effect.wait()
            return DeliveryOutcome.SUCCESS

        return PreparedDelivery(deliver)

    monkeypatch.setattr(reconciliation, "ack_delivery", blocked_ack)
    async with StateDB(tmp_path / "state.db") as db:
        event_id = await _seed(db)
        await register_consumer(db, "async-late-ack", start="all")
        first = TerminalReconciler("async-late-ack", prepare, batch_size=1, timeout=0.05)
        second = TerminalReconciler("async-late-ack", prepare, batch_size=1, timeout=0.05)
        task = asyncio.create_task(first.attempt_exact(db, event_id))
        try:
            await asyncio.wait_for(effect_entered.wait(), 1)
            if origin == "direct-cancel":
                task.cancel("caller cancellation")
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1)
            else:
                assert (await asyncio.wait_for(task, 1)).status == "failed"
            assert (await second.attempt_exact(db, event_id)).status == "busy"
            assert calls == ["effect"]
            release_effect.set()
            with caplog.at_level("INFO", logger=reconciliation.__name__):
                await asyncio.wait_for(ack_entered.wait(), 1)
                assert (await second.attempt_exact(db, event_id)).status == "busy"
                assert not await is_acknowledged(db, event_id, "async-late-ack")
                release_ack.set()
                key = (reconciliation._store_identity(db), "async-late-ack", event_id)

                async def wait_for_admission_release():
                    while True:
                        with reconciliation._workers_lock:
                            active = key in reconciliation._attempts
                        if not active:
                            return
                        await asyncio.sleep(0)

                await asyncio.wait_for(wait_for_admission_release(), 2)
            assert await is_acknowledged(db, event_id, "async-late-ack") is ack_succeeds
            assert secret not in caplog.text
            if not ack_succeeds:
                assert f"Detached ACK failed for {event_id}: OSError" in caplog.text
            result = await second.attempt_exact(db, event_id)
            assert result.status == ("not_owed" if ack_succeeds else "failed")
            assert len(calls) == (1 if ack_succeeds else 2)
        finally:
            release_effect.set()
            release_ack.set()
            await asyncio.gather(task, return_exceptions=True)


async def _wait_for_attempt_release(key):
    async def wait():
        while True:
            with reconciliation._workers_lock:
                if key not in reconciliation._attempts and key not in reconciliation._workers:
                    return
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), 2)


def _late_async_prepare(release, calls):
    async def prepare(_row, _db):
        async def deliver():
            calls.append("effect")
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return DeliveryOutcome.SUCCESS

        return PreparedDelivery(deliver)

    return prepare


@pytest.mark.asyncio
async def test_late_async_success_acks_after_file_handle_closed(tmp_path):
    path = tmp_path / "late.db"
    release, calls = asyncio.Event(), []
    first = StateDB(path)
    await first.open()
    try:
        event_id = await _seed(first)
        await register_consumer(first, "late-file", start="all")
        key = (reconciliation._store_identity(first), "late-file", event_id)
        runner = TerminalReconciler("late-file", _late_async_prepare(release, calls), timeout=0.05)
        assert (await runner.attempt_exact(first, event_id)).status == "failed"
        assert calls == ["effect"]
        await first.close()
        release.set()
        await _wait_for_attempt_release(key)
        async with StateDB(path) as observer:
            assert await is_acknowledged(observer, event_id, "late-file")
            assert calls == ["effect"]
    finally:
        release.set()
        await first.close()


@pytest.mark.asyncio
async def test_late_async_ack_rejects_file_replaced_between_check_and_connect(
    tmp_path, monkeypatch, caplog
):
    import lionagi.state.engine as engine_module

    path, replacement, original = (
        tmp_path / name for name in ("state.db", "replacement.db", "old.db")
    )
    release, calls = asyncio.Event(), []
    first = StateDB(path)
    await first.open()
    try:
        event_id = await _seed(first)
        await register_consumer(first, "late-race", start="all")
        key = (reconciliation._store_identity(first), "late-race", event_id)
        async with StateDB(replacement) as other:
            other_id = await _seed(other)
            await other.execute(
                "UPDATE status_transitions SET id = ? WHERE id = ?", (event_id, other_id)
            )
            await register_consumer(other, "late-race", start="all")
        runner = TerminalReconciler("late-race", _late_async_prepare(release, calls), timeout=0.05)
        assert (await runner.attempt_exact(first, event_id)).status == "failed"
        await first.close()
        real_make_engine = engine_module.make_engine
        replaced = []

        def replace_before_connect(url, *, before_settings=None, **kwargs):
            assert before_settings is not None
            os.replace(path, original)
            os.replace(replacement, path)
            replaced.append(True)
            return real_make_engine(url, before_settings=before_settings, **kwargs)

        monkeypatch.setattr(engine_module, "make_engine", replace_before_connect)
        with caplog.at_level("WARNING", logger=reconciliation.__name__):
            release.set()
            await _wait_for_attempt_release(key)
        assert replaced == [True]
        assert f"Detached ACK failed for {event_id}: RuntimeError" in caplog.text
        assert str(path) not in caplog.text
        async with StateDB(path) as observer:
            assert not await is_acknowledged(observer, event_id, "late-race")
        async with StateDB(original) as observer:
            assert not await is_acknowledged(observer, event_id, "late-race")
    finally:
        release.set()
        await first.close()


@pytest.mark.asyncio
async def test_late_async_private_memory_close_does_not_ack_new_generation(caplog):
    release, calls = asyncio.Event(), []
    db = StateDB(":memory:")
    await db.open()
    try:
        event_id = await _seed(db)
        await register_consumer(db, "late-private", start="all")
        key = (reconciliation._store_identity(db), "late-private", event_id)
        runner = TerminalReconciler(
            "late-private", _late_async_prepare(release, calls), timeout=0.05
        )
        assert (await runner.attempt_exact(db, event_id)).status == "failed"
        await db.close()
        await db.open()
        new_id = await _seed(db)
        await db.execute("UPDATE status_transitions SET id = ? WHERE id = ?", (event_id, new_id))
        await register_consumer(db, "late-private", start="all")
        with caplog.at_level("WARNING", logger=reconciliation.__name__):
            release.set()
            await _wait_for_attempt_release(key)
        assert f"Detached ACK failed for {event_id}: _MemoryStoreChangedError" in caplog.text
        assert not await is_acknowledged(db, event_id, "late-private")
        assert calls == ["effect"]
    finally:
        release.set()
        await db.close()


@pytest.mark.asyncio
async def test_late_async_shared_memory_keeper_survives_closed_handle():
    name = "late-async-" + uuid.uuid4().hex
    url = f"sqlite+aiosqlite:///file:{name}?mode=memory&cache=shared&uri=true"
    release, calls = asyncio.Event(), []
    first = StateDB(url=url)
    await first.open()
    try:
        event_id = await _seed(first)
        await register_consumer(first, "late-shared", start="all")
        key = (reconciliation._store_identity(first), "late-shared", event_id)
        runner = TerminalReconciler(
            "late-shared", _late_async_prepare(release, calls), timeout=0.05
        )
        assert (await runner.attempt_exact(first, event_id)).status == "failed"
        await first.close()
        # Reconnect while the reservation's keeper pins the original generation.
        async with StateDB(url=url) as observer:
            assert not await is_acknowledged(observer, event_id, "late-shared")
            release.set()
            await _wait_for_attempt_release(key)
            assert await is_acknowledged(observer, event_id, "late-shared")
            assert calls == ["effect"]
    finally:
        release.set()
        await first.close()


@pytest.mark.asyncio
async def test_cancelled_late_shared_ack_observes_failed_keeper_future(monkeypatch, caplog):
    import gc

    name = "late-async-failure-" + uuid.uuid4().hex
    url = f"sqlite+aiosqlite:///file:{name}?mode=memory&cache=shared&uri=true"
    release_effect = asyncio.Event()
    keeper_started, release_keeper = asyncio.Event(), threading.Event()
    calls, ack_tasks, loop_errors = [], [], []
    secret = "PRIVATE_KEEPER_ERROR_SENTINEL"
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    original_retain = reconciliation._AttemptAdmission.retain

    def capture_ack(admission, task, *, label="hook", safe=True, observe=True):
        if label == "ACK":
            ack_tasks.append(task)
        return original_retain(admission, task, label=label, safe=safe, observe=observe)

    def failing_keeper(_admission):
        loop.call_soon_threadsafe(keeper_started.set)
        if not release_keeper.wait(3):
            raise RuntimeError("fake keeper was not released")
        raise OSError(secret)

    monkeypatch.setattr(reconciliation._AttemptAdmission, "retain", capture_ack)
    monkeypatch.setattr(reconciliation, "_keeper_ack", failing_keeper)
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    db = StateDB(url=url)
    await db.open()
    try:
        event_id = await _seed(db)
        await register_consumer(db, "late-keeper-failure", start="all")
        key = (reconciliation._store_identity(db), "late-keeper-failure", event_id)
        prepare = _late_async_prepare(release_effect, calls)
        first = TerminalReconciler("late-keeper-failure", prepare, batch_size=1, timeout=0.05)
        second = TerminalReconciler("late-keeper-failure", prepare, batch_size=1, timeout=0.05)
        assert (await first.attempt_exact(db, event_id)).status == "failed"
        release_effect.set()
        with caplog.at_level("INFO", logger=reconciliation.__name__):
            await asyncio.wait_for(keeper_started.wait(), 1)
            assert len(ack_tasks) == 1
            ack_tasks[0].cancel()
            await asyncio.gather(ack_tasks[0], return_exceptions=True)
            assert ack_tasks[0].cancelled()
            assert (await second.attempt_exact(db, event_id)).status == "busy"
            release_keeper.set()
            await _wait_for_attempt_release(key)

            async def wait_for_keeper_log():
                while f"Detached keeper failed for {event_id}: OSError" not in caplog.text:
                    await asyncio.sleep(0)

            await asyncio.wait_for(wait_for_keeper_log(), 1)
            gc.collect()
            await asyncio.sleep(0)
        assert not await is_acknowledged(db, event_id, "late-keeper-failure")
        assert calls == ["effect"]
        assert not loop_errors
        assert f"Detached keeper failed for {event_id}: OSError" in caplog.text
        assert secret not in caplog.text
    finally:
        release_effect.set()
        release_keeper.set()
        await db.close()
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["before-success", "before-connect"])
@pytest.mark.parametrize("remove", ["unlink", "move"])
@pytest.mark.parametrize("location", ["ordinary", "encoded-uri"])
async def test_late_async_ack_never_recreates_missing_sqlite_file(
    tmp_path, monkeypatch, caplog, timing, remove, location
):
    import lionagi.state.engine as engine_module

    path = tmp_path / ("state.db" if location == "ordinary" else "state #?% space.db")
    moved = tmp_path / "original.db"
    url = f"sqlite+aiosqlite:///{path.as_uri()}?mode=rwc&uri=true&timeout=5"
    db = StateDB(path) if location == "ordinary" else StateDB(url=url)
    release, calls = asyncio.Event(), []
    removed = []

    def remove_path():
        if remove == "unlink":
            path.unlink()
        else:
            path.rename(moved)
        removed.append(True)

    await db.open()
    try:
        event_id = await _seed(db)
        await register_consumer(db, "late-missing", start="all")
        key = (reconciliation._store_identity(db), "late-missing", event_id)
        runner = TerminalReconciler(
            "late-missing", _late_async_prepare(release, calls), timeout=0.05
        )
        assert (await runner.attempt_exact(db, event_id)).status == "failed"
        await db.close()
        if timing == "before-success":
            remove_path()
        else:
            real_make_engine = engine_module.make_engine

            def remove_after_identity_check(url, *, before_settings=None, **kwargs):
                assert before_settings is not None
                remove_path()
                return real_make_engine(url, before_settings=before_settings, **kwargs)

            monkeypatch.setattr(engine_module, "make_engine", remove_after_identity_check)
        with caplog.at_level("WARNING", logger=reconciliation.__name__):
            release.set()
            await _wait_for_attempt_release(key)
        assert removed == [True]
        assert not path.exists()
        assert calls == ["effect"]
        assert f"Detached ACK failed for {event_id}:" in caplog.text
        assert str(path) not in caplog.text
        if remove == "move":
            async with StateDB(moved) as observer:
                assert not await is_acknowledged(observer, event_id, "late-missing")
        # If unlinked there is no surviving ledger to inspect: do not call its
        # missing ACK a durable debt in a database that no longer exists.
    finally:
        release.set()
        await db.close()


@pytest.mark.asyncio
async def test_late_async_ack_preserves_encoded_sqlite_uri_and_options(tmp_path):
    path = tmp_path / "state #?% space.db"
    url = f"sqlite+aiosqlite:///{path.as_uri()}?mode=rwc&uri=true&timeout=5"
    release, calls = asyncio.Event(), []
    db = StateDB(url=url)
    await db.open()
    try:
        event_id = await _seed(db)
        await register_consumer(db, "late-encoded", start="all")
        key = (reconciliation._store_identity(db), "late-encoded", event_id)
        runner = TerminalReconciler(
            "late-encoded", _late_async_prepare(release, calls), timeout=0.05
        )
        assert (await runner.attempt_exact(db, event_id)).status == "failed"
        await db.close()
        release.set()
        await _wait_for_attempt_release(key)
        async with StateDB(url=url) as observer:
            assert await is_acknowledged(observer, event_id, "late-encoded")
        assert calls == ["effect"]
    finally:
        release.set()
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["attempt_exact", "attempt_replay"])
@pytest.mark.parametrize("readonly", [True, False], ids=["readonly", "writable"])
async def test_readonly_attempt_refused_before_admission_or_effect(
    tmp_path, monkeypatch, adapter, readonly
):
    import lionagi.state.engine as engine_module

    path = tmp_path / "readonly.db"
    async with StateDB(path) as db:
        event_id = await _seed(db)
        await register_consumer(db, "readonly-control", start="all")

    preparations, effects, acks, new_engines = [], [], [], []
    original_ack, original_engine = reconciliation.ack_delivery, engine_module.make_engine

    async def prepare(_row, _db):
        preparations.append(True)

        async def deliver():
            effects.append(True)
            return DeliveryOutcome.SUCCESS

        return PreparedDelivery(deliver)

    async def observe_ack(*args, **kwargs):
        acks.append(True)
        return await original_ack(*args, **kwargs)

    def observe_engine(*args, **kwargs):
        new_engines.append(True)
        return original_engine(*args, **kwargs)

    async with StateDB(path, readonly=readonly) as handle:
        assert not await is_acknowledged(handle, event_id, "readonly-control")
        key = (reconciliation._store_identity(handle), "readonly-control", event_id)
        with monkeypatch.context() as patch:
            patch.setattr(reconciliation, "ack_delivery", observe_ack)
            patch.setattr(engine_module, "make_engine", observe_engine)
            result = await getattr(TerminalReconciler("readonly-control", prepare), adapter)(
                handle, event_id
            )
        assert new_engines == []
        with reconciliation._workers_lock:
            assert key not in reconciliation._attempts and key not in reconciliation._workers
        if readonly:
            assert result.status == "failed" and result.attempted is False
            assert preparations == effects == acks == []
        else:
            assert result.status == "acknowledged" and result.attempted is True
            assert preparations == effects == acks == [True]
        assert await is_acknowledged(handle, event_id, "readonly-control") is (not readonly)
    async with StateDB(path) as observer:
        assert await is_acknowledged(observer, event_id, "readonly-control") is (not readonly)


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["attempt_exact", "attempt_replay"])
@pytest.mark.parametrize("mode", ["mode-ro", "immutable", "mode-rw"])
async def test_sqlite_readonly_uri_refused_before_effect_with_writable_control(
    tmp_path, monkeypatch, adapter, mode
):
    from lionagi.state.engine import make_readonly_engine

    path = tmp_path / "uri-readonly.db"
    async with StateDB(path) as initial:
        event_id = await _seed(initial)
        await register_consumer(initial, "uri-control", start="all")
    query = {"mode-ro": "mode=ro", "immutable": "immutable=1", "mode-rw": "mode=rw"}[mode]
    handle = StateDB(url=f"sqlite+aiosqlite:///{path.as_uri()}?{query}&uri=true")
    if mode == "mode-rw":
        await handle.open()
    else:
        # A readable real ledger, with a handle whose raw URL carries the
        # restriction. Opening it through StateDB.open would first run native
        # schema setup, which is not part of attempt admission.
        handle._engine = make_readonly_engine(StateDB(path).url)
    preparations, effects, acks = [], [], []
    original_ack = reconciliation.ack_delivery

    async def prepare(_row, _db):
        preparations.append(True)

        async def deliver():
            effects.append(True)
            return DeliveryOutcome.SUCCESS

        return PreparedDelivery(deliver)

    async def observe_ack(*args, **kwargs):
        acks.append(True)
        return await original_ack(*args, **kwargs)

    try:
        assert not await is_acknowledged(handle, event_id, "uri-control")
        key = (reconciliation._store_identity(handle), "uri-control", event_id)
        monkeypatch.setattr(reconciliation, "ack_delivery", observe_ack)
        result = await getattr(TerminalReconciler("uri-control", prepare), adapter)(
            handle, event_id
        )
        with reconciliation._workers_lock:
            assert key not in reconciliation._attempts and key not in reconciliation._workers
        if mode == "mode-rw":
            assert result.status == "acknowledged" and result.attempted is True
            assert preparations == effects == acks == [True]
        else:
            assert result.status == "failed" and result.attempted is False
            assert preparations == effects == acks == []
        assert await is_acknowledged(handle, event_id, "uri-control") is (mode == "mode-rw")
    finally:
        await handle.close()
    async with StateDB(path) as observer:
        assert await is_acknowledged(observer, event_id, "uri-control") is (mode == "mode-rw")
