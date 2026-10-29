# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Process-local worker lifetime across timeout, cancellation, and restart."""

from __future__ import annotations

import asyncio
import threading
import uuid

import pytest

from lionagi.state.db import StateDB
from lionagi.state.lifecycle.deliveries import is_acknowledged, retire_consumer
from lionagi.state.lifecycle.reconciler import DeliveryOutcome, PreparedDelivery, TerminalReconciler


async def _seed(db: StateDB) -> str:
    sid, pid = str(uuid.uuid4()), str(uuid.uuid4())
    await db.create_progression(pid)
    await db.create_session({"id": sid, "progression_id": pid, "status": "running"})
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


async def _wait_until(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition not reached before timeout")
        await asyncio.sleep(0.01)


async def _wait_for_ack(db: StateDB, transition_id: str, consumer: str):
    deadline = asyncio.get_running_loop().time() + 5.0
    while not await is_acknowledged(db, transition_id, consumer):
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("late acknowledgement was not written")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_timeout_reservation_is_shared_and_late_success_acks_retired(tmp_path):
    started, release = threading.Event(), threading.Event()
    calls = []

    def work():
        calls.append("started")
        started.set()
        if not release.wait(5):
            return DeliveryOutcome.FAILURE
        return DeliveryOutcome.SUCCESS

    async def prepare(_row, _db):
        return PreparedDelivery(work)

    async with StateDB(tmp_path / "state.db") as db:
        event_id = await _seed(db)
        one = TerminalReconciler("runtime-test", prepare, batch_size=1, timeout=0.05)
        two = TerminalReconciler("runtime-test", prepare, batch_size=1, timeout=0.05)
        try:
            first = await one.run_pass(db)
            assert started.is_set() and first["failed"] == 1
            second = await two.run_pass(db)
            assert second["saturated"] == 1 and calls == ["started"]
            assert await retire_consumer(db, "runtime-test")
            release.set()
            await _wait_for_ack(db, event_id, "runtime-test")
            assert calls == ["started"]
        finally:
            release.set()


@pytest.mark.asyncio
async def test_busy_smaller_worker_preserves_cursor_and_stops_larger_pass(tmp_path):
    started, release = threading.Event(), threading.Event()
    prepared_ids = []
    called_ids = []

    async def prepare_large(row, _db):
        transition_id = row["transition_id"]
        prepared_ids.append(transition_id)

        def work():
            called_ids.append(transition_id)
            return DeliveryOutcome.SUCCESS

        return PreparedDelivery(work)

    def stalled_work():
        started.set()
        return DeliveryOutcome.SUCCESS if release.wait(10) else DeliveryOutcome.FAILURE

    async def prepare_small(_row, _db):
        return PreparedDelivery(stalled_work)

    async with StateDB(tmp_path / "state.db") as db:
        earlier = await _seed(db)
        await db.execute("UPDATE status_transitions SET created_at = ? WHERE id = ?", (1, earlier))
        large = TerminalReconciler("shared-capacity", prepare_large, batch_size=3, timeout=1)
        small = TerminalReconciler("shared-capacity", prepare_small, batch_size=1, timeout=30)
        assert (await large.run_pass(db))["acknowledged"] == 1
        assert large._cursor == (1.0, earlier)

        stalled, due, later = [await _seed(db) for _ in range(3)]
        for timestamp, transition_id in enumerate((stalled, due, later), start=2):
            await db.execute(
                "UPDATE status_transitions SET created_at = ? WHERE id = ?",
                (timestamp, transition_id),
            )
        task = asyncio.create_task(small.run_pass(db))
        try:
            await _wait_until(started.is_set)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            prepared_ids.clear()
            called_ids.clear()

            result = await large.run_pass(db)
            assert result["saturated"] == 1
            assert result["attempted"] == 0
            assert result["failed"] == 0
            assert prepared_ids == [due]
            assert called_ids == []
            assert large._cursor == (1.0, earlier)

            release.set()
            await _wait_for_ack(db, stalled, "shared-capacity")
            await _wait_until(lambda: large._pending()[1] == 0)
            prepared_ids.clear()
            result = await large.run_pass(db)
            assert result["acknowledged"] == 2
            assert prepared_ids == [due, later]
            assert called_ids == [due, later]
        finally:
            release.set()
            if not task.done():
                task.cancel()


@pytest.mark.asyncio
async def test_cancellation_keeps_running_worker_reserved(tmp_path):
    started, release = threading.Event(), threading.Event()
    calls = []

    def work():
        calls.append("started")
        started.set()
        return DeliveryOutcome.SUCCESS if release.wait(5) else DeliveryOutcome.FAILURE

    async def prepare(_row, _db):
        return PreparedDelivery(work)

    async with StateDB(tmp_path / "state.db") as db:
        event_id = await _seed(db)
        first = TerminalReconciler("cancel-test", prepare, batch_size=1, timeout=3)
        second = TerminalReconciler("cancel-test", prepare, batch_size=1, timeout=0.05)
        task = asyncio.create_task(first.run_pass(db))
        try:
            await _wait_until(started.is_set)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            result = await second.run_pass(db)
            assert result["saturated"] == 1 and calls == ["started"]
            release.set()
            await _wait_for_ack(db, event_id, "cancel-test")
            assert calls == ["started"]
        finally:
            release.set()


def test_worker_survives_event_loop_shutdown_and_late_acks(tmp_path):
    started, release = threading.Event(), threading.Event()
    calls = []
    db_path = tmp_path / "state.db"

    def work():
        calls.append("started")
        started.set()
        return DeliveryOutcome.SUCCESS if release.wait(5) else DeliveryOutcome.FAILURE

    async def prepare(_row, _db):
        return PreparedDelivery(work)

    async def start_and_stop():
        async with StateDB(db_path) as db:
            event_id = await _seed(db)
            reconciler = TerminalReconciler("restart-test", prepare, batch_size=1, timeout=3)
            task = asyncio.create_task(reconciler.run_pass(db))
            await _wait_until(started.is_set)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return event_id

    async def restart(event_id):
        async with StateDB(db_path) as db:
            second = TerminalReconciler("restart-test", prepare, batch_size=1, timeout=0.05)
            result = await second.run_pass(db)
            assert result["saturated"] == 1 and calls == ["started"]
            assert await retire_consumer(db, "restart-test")
            release.set()
            await _wait_for_ack(db, event_id, "restart-test")

    try:
        transition_id = asyncio.run(start_and_stop())
        asyncio.run(restart(transition_id))
        assert calls == ["started"]
    finally:
        release.set()


@pytest.mark.asyncio
async def test_distinct_not_applicable_causes_are_acked_and_counted(tmp_path):
    async with StateDB(tmp_path / "state.db") as db:
        no_session = await _seed(db)
        ambiguous = await _seed(db)

        async def prepare(row, _db):
            return (
                DeliveryOutcome.NO_SESSION
                if row["transition_id"] == no_session
                else DeliveryOutcome.AMBIGUOUS_PROJECT
            )

        reconciler = TerminalReconciler("causes", prepare, batch_size=2)
        result = await reconciler.run_pass(db)
        assert result["attempted"] == result["acknowledged"] == 2
        assert result["not_applicable"] == 2
        assert result["no_session"] == result["ambiguous_project"] == 1
        assert await is_acknowledged(db, no_session, "causes")
        assert await is_acknowledged(db, ambiguous, "causes")


@pytest.mark.asyncio
async def test_retire_after_admission_allows_that_ack_but_no_next_attempt(tmp_path, monkeypatch):
    from lionagi.state.lifecycle import reconciler as reconciler_module

    async with StateDB(tmp_path / "state.db") as db:
        first = await _seed(db)
        second = await _seed(db)
        calls = []

        async def prepare(row, _db):
            async def work():
                calls.append(row["transition_id"])
                return DeliveryOutcome.SUCCESS

            return PreparedDelivery(work)

        original = reconciler_module._consumer_is_active
        retired_after_check = False

        async def check_then_retire(current_db, consumer):
            nonlocal retired_after_check
            active = await original(current_db, consumer)
            if active and not retired_after_check:
                assert await retire_consumer(current_db, consumer)
                retired_after_check = True
            return active

        monkeypatch.setattr(reconciler_module, "_consumer_is_active", check_then_retire)
        reconciler = TerminalReconciler("retirement-seam", prepare, batch_size=2)
        result = await reconciler.run_pass(db)
        assert result["attempted"] == result["acknowledged"] == 1
        assert len(calls) == 1
        assert await is_acknowledged(db, calls[0], "retirement-seam")
        assert calls[0] in {first, second}
        assert not await is_acknowledged(
            db, ({first, second} - set(calls)).pop(), "retirement-seam"
        )
        assert (await reconciler.run_pass(db))["attempted"] == 0
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_cooling_row_skipped_until_due_then_attempted_on_next_traversal(tmp_path):
    async with StateDB(tmp_path / "state.db") as db:
        cooling = await _seed(db)
        first_due = [await _seed(db) for _ in range(3)]
        clock = [0.0]
        calls = []

        async def prepare(row, _db):
            async def work():
                event_id = row["transition_id"]
                calls.append(event_id)
                return DeliveryOutcome.FAILURE if event_id == cooling else DeliveryOutcome.SUCCESS

            return PreparedDelivery(work)

        reconciler = TerminalReconciler("fairness", prepare, batch_size=4, clock=lambda: clock[0])
        first = await reconciler.run_pass(db)
        assert first["failed"] == 1 and first["acknowledged"] == 3
        assert set(calls) == {cooling, *first_due}
        # Model a repeatedly failing row at the one-hour cap without waiting in real time.
        reconciler._backoff[cooling].attempts = 13
        reconciler._backoff[cooling].retry_at = 3600.0
        later_due = [await _seed(db) for _ in range(3)]
        for _ in range(4):
            await reconciler.run_pass(db)
            if all(item in calls for item in later_due):
                break
        assert all(item in calls for item in later_due)
        assert calls.count(cooling) == 1
        clock[0] = 3600.0
        for _ in range(4):
            await reconciler.run_pass(db)
            if calls.count(cooling) == 2:
                break
        assert calls.count(cooling) == 2


@pytest.mark.asyncio
async def test_pass_counts_once_and_preserves_retired_counts(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from lionagi.state.lifecycle import deliveries

    async def prepare(_row, _db):
        raise AssertionError("unclassified rows are not owed")

    async with StateDB(tmp_path / "state.db") as db:
        await db.execute(
            "INSERT INTO status_transitions "
            "(id, entity_type, entity_id, previous_status, status, reason_code, "
            "source, created_at, delivery_eligible) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "late-null",
                "session",
                "s",
                None,
                "failed",
                "run.failed.exception",
                "system",
                0,
                None,
            ),
        )
        count = AsyncMock(wraps=deliveries._counts)
        monkeypatch.setattr(deliveries, "_counts", count)
        reconciler = TerminalReconciler("count-once", prepare)

        active = await reconciler.run_pass(db)
        assert count.await_count == 1
        assert active["unclassified"] == 0
        assert active["post_migration_null"] == 1
        assert active["attempted"] == 0

        assert await retire_consumer(db, "count-once")
        retired = await reconciler.run_pass(db)
        assert count.await_count == 2
        assert retired["unclassified"] == 0
        assert retired["post_migration_null"] == 1
        assert retired["attempted"] == 0


@pytest.mark.parametrize("surface", ["direct-async", "settings-python"])
@pytest.mark.parametrize("cancellation", ["raise", "self-cancel"])
@pytest.mark.asyncio
async def test_python_hook_self_cancellation_is_failure_and_retries(
    tmp_path, monkeypatch, surface, cancellation
):
    from lionagi.state.lifecycle import settings_delivery
    from lionagi.state.lifecycle.notify_settings import ResolvedNotifyHandler
    from tests.state.lifecycle.test_settings_delivery import _event

    calls = []

    async def hook(_envelope):
        calls.append("called")
        if len(calls) == 1:
            if cancellation == "self-cancel":
                asyncio.current_task().cancel()
                await asyncio.sleep(0)
            raise asyncio.CancelledError
        return DeliveryOutcome.SUCCESS

    monkeypatch.setattr(settings_delivery, "build_handler", lambda _resolved: hook)

    async def prepare(_row, _db):
        if surface == "direct-async":

            async def invoke():
                return await hook(_event())

            return PreparedDelivery(invoke)
        return settings_delivery.prepare_resolved_delivery(
            ResolvedNotifyHandler(python_ref="test:hook"), _event()
        )

    clock = [0.0]
    async with StateDB(tmp_path / "state.db") as db:
        event_id = await _seed(db)
        reconciler = TerminalReconciler(
            "self-cancel", prepare, batch_size=1, clock=lambda: clock[0]
        )
        result = await reconciler.run_pass(db)
        assert result["attempted"] == 1 and result["failed"] == 1
        assert result["acknowledged"] == 0 and not await is_acknowledged(
            db, event_id, "self-cancel"
        )
        assert len(calls) == 1
        clock[0] = 2.0
        for _ in range(3):
            result = await reconciler.run_pass(db)
            if result["acknowledged"]:
                break
        assert result["acknowledged"] == 1 and len(calls) == 2
        assert await is_acknowledged(db, event_id, "self-cancel")


@pytest.mark.parametrize("surface", ["direct-async", "settings-python"])
@pytest.mark.asyncio
async def test_async_hook_preserves_enclosing_cancellation(tmp_path, monkeypatch, surface):
    from lionagi.state.lifecycle import settings_delivery
    from lionagi.state.lifecycle.notify_settings import ResolvedNotifyHandler
    from tests.state.lifecycle.test_settings_delivery import _event

    started, stopped = asyncio.Event(), asyncio.Event()

    async def hook(_envelope):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        return DeliveryOutcome.SUCCESS

    monkeypatch.setattr(settings_delivery, "build_handler", lambda _resolved: hook)

    async def prepare(_row, _db):
        if surface == "direct-async":

            async def invoke():
                return await hook(_event())

            return PreparedDelivery(invoke)
        return settings_delivery.prepare_resolved_delivery(
            ResolvedNotifyHandler(python_ref="test:hook"), _event()
        )

    async with StateDB(tmp_path / "state.db") as db:
        event_id = await _seed(db)
        reconciler = TerminalReconciler("outer-cancel", prepare, batch_size=1, timeout=3)
        task = asyncio.create_task(reconciler.run_pass(db))
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(stopped.wait(), timeout=2)
        assert not await is_acknowledged(db, event_id, "outer-cancel")


@pytest.mark.asyncio
async def test_same_file_with_driver_query_shares_timed_out_worker(tmp_path):
    started, release = threading.Event(), threading.Event()
    calls = []
    path = tmp_path / "state.db"

    def work():
        calls.append("started")
        started.set()
        return DeliveryOutcome.SUCCESS if release.wait(5) else DeliveryOutcome.FAILURE

    async def prepare(_row, _db):
        return PreparedDelivery(work)

    async with (
        StateDB(path) as first_db,
        StateDB(url=f"sqlite+aiosqlite:///{path}?timeout=5") as second_db,
    ):
        event_id = await _seed(first_db)
        one = TerminalReconciler("query-alias", prepare, batch_size=1, timeout=0.05)
        two = TerminalReconciler("query-alias", prepare, batch_size=1, timeout=0.05)
        try:
            first = await one.run_pass(first_db)
            assert started.is_set() and first["failed"] == 1
            assert not await is_acknowledged(second_db, event_id, "query-alias")
            second = await two.run_pass(second_db)
            assert second["saturated"] == 1 and calls == ["started"]
            release.set()
            await _wait_for_ack(second_db, event_id, "query-alias")
            assert calls == ["started"]
        finally:
            release.set()


def test_store_identity_parses_sqlite_uri_and_keeps_private_memory_distinct(tmp_path):
    from lionagi.state.lifecycle.reconciler import _store_identity

    path = tmp_path / "a space.db"
    path.touch()
    plain = StateDB(path)
    driver_query = StateDB(url=f"sqlite+aiosqlite:///{path}?timeout=5")
    uri = StateDB(url=f"sqlite+aiosqlite:///{path.as_uri()}?mode=rw&uri=true")
    assert _store_identity(plain) == _store_identity(driver_query) == _store_identity(uri)
    memory_one, memory_two = StateDB(":memory:"), StateDB(":memory:")
    assert _store_identity(memory_one) != _store_identity(memory_two)
    first = StateDB(url="sqlite+aiosqlite:///file:isolated?mode=memory&uri=true")
    second = StateDB(url="sqlite+aiosqlite:///file:isolated?mode=memory&uri=true")
    assert _store_identity(first) != _store_identity(second)
    shared = StateDB(url="sqlite+aiosqlite:///file:shared?mode=memory&cache=shared&uri=true")
    shared_again = StateDB(url="sqlite+aiosqlite:///file:shared?cache=shared&mode=memory&uri=true")
    assert _store_identity(shared) == _store_identity(shared_again)


@pytest.mark.asyncio
async def test_late_ack_refuses_replacement_database_at_same_path(tmp_path):
    import os

    from lionagi.state.lifecycle.deliveries import register_consumer
    from lionagi.state.lifecycle.reconciler import _store_identity, _workers, _workers_lock

    path, replacement = tmp_path / "state.db", tmp_path / "replacement.db"
    started, release = threading.Event(), threading.Event()

    def work():
        started.set()
        return DeliveryOutcome.SUCCESS if release.wait(5) else DeliveryOutcome.FAILURE

    async def prepare(_row, _db):
        return PreparedDelivery(work)

    db = StateDB(path)
    await db.open()
    try:
        event_id = await _seed(db)
        original_identity = _store_identity(db)
        reconciler = TerminalReconciler("replacement-test", prepare, batch_size=1, timeout=0.05)
        assert (await reconciler.run_pass(db))["failed"] == 1
        assert started.is_set()
        with _workers_lock:
            assert (original_identity, "replacement-test", event_id) in _workers

        async with StateDB(replacement) as other:
            other_id = await _seed(other)
            await other.execute(
                "UPDATE status_transitions SET id = ? WHERE id = ?", (event_id, other_id)
            )
            await register_consumer(other, "replacement-test", start="all")
        await db.close()
        os.replace(path, tmp_path / "original.db")
        os.replace(replacement, path)
        assert _store_identity(StateDB(path)) != original_identity
        release.set()

        def no_worker():
            with _workers_lock:
                return (original_identity, "replacement-test", event_id) not in _workers

        await _wait_until(no_worker)
        async with StateDB(path) as current:
            assert not await is_acknowledged(current, event_id, "replacement-test")
    finally:
        release.set()
        await db.close()


@pytest.mark.asyncio
async def test_late_shared_memory_success_survives_closed_origin_handle():
    name = "shared-" + uuid.uuid4().hex
    url = f"sqlite+aiosqlite:///file:{name}?mode=memory&cache=shared&uri=true"
    first_db, second_db = StateDB(url=url), StateDB(url=url)
    started, release = threading.Event(), threading.Event()
    calls = []

    def work():
        calls.append("started")
        started.set()
        return DeliveryOutcome.SUCCESS if release.wait(5) else DeliveryOutcome.FAILURE

    async def prepare(_row, _db):
        return PreparedDelivery(work)

    await first_db.open()
    await second_db.open()
    try:
        event_id = await _seed(first_db)
        one = TerminalReconciler("shared-memory-test", prepare, batch_size=1, timeout=0.05)
        two = TerminalReconciler("shared-memory-test", prepare, batch_size=1, timeout=0.05)
        assert (await one.run_pass(first_db))["failed"] == 1 and started.is_set()
        await first_db.close()
        release.set()
        from lionagi.state.lifecycle.reconciler import _store_identity, _workers, _workers_lock

        identity = _store_identity(second_db)

        def no_worker():
            with _workers_lock:
                return (identity, "shared-memory-test", event_id) not in _workers

        await _wait_until(no_worker)
        assert await is_acknowledged(second_db, event_id, "shared-memory-test")
        result = await two.run_pass(second_db)
        assert result["attempted"] == 0
        assert await is_acknowledged(second_db, event_id, "shared-memory-test")
        assert calls == ["started"]
    finally:
        release.set()
        await first_db.close()
        await second_db.close()


def test_shared_memory_keeper_survives_all_handles_and_original_loop_closing():
    from lionagi.state.lifecycle.deliveries import register_consumer
    from lionagi.state.lifecycle.reconciler import _store_identity, _workers, _workers_lock

    url = f"sqlite+aiosqlite:///file:keeper-{uuid.uuid4().hex}?mode=memory&cache=shared&uri=true"
    started, release = threading.Event(), threading.Event()
    calls = []

    def work():
        calls.append("started")
        started.set()
        return DeliveryOutcome.SUCCESS if release.wait(10) else DeliveryOutcome.FAILURE

    async def prepare(_row, _db):
        return PreparedDelivery(work)

    async def start():
        async with StateDB(url=url) as db:
            event_id = await _seed(db)
            reconciler = TerminalReconciler("keeper-test", prepare, batch_size=1, timeout=0.05)
            assert (await reconciler.run_pass(db))["failed"] == 1 and started.is_set()
            return event_id

    async def reconnect(event_id):
        async with StateDB(url=url) as db:
            # Reopening the URI while the worker is blocked must reach the
            # original generation, despite all its StateDB handles having closed.
            row = await db.fetch_one("SELECT id FROM status_transitions WHERE id = ?", (event_id,))
            assert row is not None
            assert not await is_acknowledged(db, event_id, "keeper-test")
            release.set()
            identity = _store_identity(db)

            def no_worker():
                with _workers_lock:
                    return (identity, "keeper-test", event_id) not in _workers

            await _wait_until(no_worker)
            assert await is_acknowledged(db, event_id, "keeper-test")
            assert calls == ["started"]

    async def fresh_generation(event_id):
        async with StateDB(url=url) as db:
            assert (
                await db.fetch_one("SELECT id FROM status_transitions WHERE id = ?", (event_id,))
                is None
            )
            new_id = await _seed(db)
            await db.execute(
                "UPDATE status_transitions SET id = ? WHERE id = ?", (event_id, new_id)
            )
            await register_consumer(db, "keeper-test", start="all")
            assert not await is_acknowledged(db, event_id, "keeper-test")

    try:
        event_id = asyncio.run(start())
        asyncio.run(reconnect(event_id))
        asyncio.run(fresh_generation(event_id))
    finally:
        release.set()


@pytest.mark.asyncio
async def test_private_memory_reopen_cannot_ack_new_generation():
    from lionagi.state.lifecycle.deliveries import register_consumer
    from lionagi.state.lifecycle.reconciler import _store_identity, _workers, _workers_lock

    db = StateDB(":memory:")
    started, release = threading.Event(), threading.Event()

    def work():
        started.set()
        return DeliveryOutcome.SUCCESS if release.wait(5) else DeliveryOutcome.FAILURE

    async def prepare(_row, _db):
        return PreparedDelivery(work)

    await db.open()
    try:
        event_id = await _seed(db)
        old_identity = _store_identity(db)
        reconciler = TerminalReconciler(
            "private-generation-test", prepare, batch_size=1, timeout=0.05
        )
        assert (await reconciler.run_pass(db))["failed"] == 1 and started.is_set()
        await db.close()
        await db.open()
        assert _store_identity(db) != old_identity
        new_id = await _seed(db)
        await db.execute("UPDATE status_transitions SET id = ? WHERE id = ?", (event_id, new_id))
        await register_consumer(db, "private-generation-test", start="all")
        release.set()

        def no_worker():
            with _workers_lock:
                return (old_identity, "private-generation-test", event_id) not in _workers

        await _wait_until(no_worker)
        assert not await is_acknowledged(db, event_id, "private-generation-test")
    finally:
        release.set()
        await db.close()


@pytest.mark.parametrize("storage", ["file", "shared-memory"])
@pytest.mark.parametrize("missing", ["consumer-table", "eligibility-column"])
@pytest.mark.asyncio
async def test_late_ack_does_not_initialize_incomplete_schema(tmp_path, caplog, storage, missing):
    from lionagi.state.lifecycle.reconciler import (
        _release_worker,
        _store_identity,
        _workers,
        _workers_lock,
    )

    path = tmp_path / "state.db"
    url = f"sqlite+aiosqlite:///file:missing-{uuid.uuid4().hex}?mode=memory&cache=shared&uri=true"
    first = StateDB(path) if storage == "file" else StateDB(url=url)
    observer = StateDB(path) if storage == "file" else StateDB(url=url)
    started, release = threading.Event(), threading.Event()
    worker = None

    def work():
        started.set()
        return DeliveryOutcome.SUCCESS if release.wait(8) else DeliveryOutcome.FAILURE

    async def prepare(_row, _db):
        return PreparedDelivery(work)

    await first.open()
    await observer.open()
    try:
        event_id = await _seed(first)
        key = (_store_identity(first), "missing-schema-test", event_id)
        reconciler = TerminalReconciler(key[1], prepare, batch_size=1, timeout=0.05)
        assert (await reconciler.run_pass(first))["failed"] == 1 and started.is_set()
        with _workers_lock:
            worker = _workers[key]
        if missing == "consumer-table":
            await observer.execute("DROP TABLE terminal_consumers")
        else:
            await observer.execute("DROP INDEX idx_status_transitions_unclassified")
            await observer.execute("DROP INDEX idx_status_transitions_eligible_order")
            await observer.execute("ALTER TABLE status_transitions DROP COLUMN delivery_eligible")
        await first.close()
        release.set()

        def finished():
            with _workers_lock:
                return key not in _workers or _workers[key].deferred

        await _wait_until(finished)
        assert "ConsumerSchemaError" in caplog.text
        tables = await observer.fetch_all(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'terminal_consumers'"
        )
        columns = await observer.fetch_all("PRAGMA table_info(status_transitions)")
        assert bool(tables) is (missing != "consumer-table")
        assert ("delivery_eligible" in {row["name"] for row in columns}) is (
            missing != "eligibility-column"
        )
        rows = await observer.fetch_all("SELECT transition_id FROM terminal_deliveries")
        assert rows == []
    finally:
        release.set()
        if worker is not None:
            _release_worker(worker)
        await first.close()
        await observer.close()


@pytest.mark.asyncio
async def test_shared_memory_late_ack_is_idempotent_and_accepts_retirement():
    from lionagi.state.lifecycle.deliveries import ack_delivery
    from lionagi.state.lifecycle.reconciler import _store_identity, _workers, _workers_lock

    url = f"sqlite+aiosqlite:///file:retired-{uuid.uuid4().hex}?mode=memory&cache=shared&uri=true"
    started, release = threading.Event(), threading.Event()

    def work():
        started.set()
        return DeliveryOutcome.SUCCESS if release.wait(5) else DeliveryOutcome.FAILURE

    async def prepare(_row, _db):
        return PreparedDelivery(work)

    async with StateDB(url=url) as db:
        event_id = await _seed(db)
        key = (_store_identity(db), "retired-shared-test", event_id)
        reconciler = TerminalReconciler(key[1], prepare, batch_size=1, timeout=0.05)
        try:
            assert (await reconciler.run_pass(db))["failed"] == 1 and started.is_set()
            await ack_delivery(db, event_id, key[1])
            before = await db.fetch_one(
                "SELECT acked_at FROM terminal_deliveries WHERE transition_id = ? AND consumer = ?",
                (event_id, key[1]),
            )
            assert await retire_consumer(db, key[1])
            release.set()

            def no_worker():
                with _workers_lock:
                    return key not in _workers

            await _wait_until(no_worker)
            rows = await db.fetch_all(
                "SELECT acked_at FROM terminal_deliveries WHERE transition_id = ? AND consumer = ?",
                (event_id, key[1]),
            )
            assert rows == [before]
        finally:
            release.set()


def test_private_memory_late_ack_waits_for_original_loop_next_pass(monkeypatch):
    from lionagi.state.lifecycle import reconciler as reconciliation

    db = StateDB(":memory:")
    started, release = threading.Event(), threading.Event()
    calls = []
    loop = asyncio.new_event_loop()
    real_ack = reconciliation.ack_delivery

    async def guarded_ack(*args, **kwargs):
        # A different loop must not touch the original private connection.
        assert asyncio.get_running_loop() is loop
        return await real_ack(*args, **kwargs)

    monkeypatch.setattr(reconciliation, "ack_delivery", guarded_ack)

    def work():
        calls.append("started")
        started.set()
        return DeliveryOutcome.SUCCESS if release.wait(8) else DeliveryOutcome.FAILURE

    async def prepare(_row, _db):
        return PreparedDelivery(work)

    async def start():
        await db.open()
        event_id = await _seed(db)
        key = (reconciliation._store_identity(db), "private-loop-test", event_id)
        instance = TerminalReconciler(key[1], prepare, batch_size=1, timeout=0.05)
        assert (await instance.run_pass(db))["failed"] == 1 and started.is_set()
        return key, instance

    async def wait_and_try_wrong_loop(key):
        def deferred():
            with reconciliation._workers_lock:
                worker = reconciliation._workers.get(key)
                return worker is not None and worker.deferred

        await _wait_until(deferred)
        # No database operation is permitted through the wrong loop.
        await reconciliation._drain_deferred(db, key[1], key[0])
        assert deferred()

    async def finish(key, instance):
        assert not await is_acknowledged(db, key[2], key[1])
        result = await instance.run_pass(db)
        assert result["attempted"] == 0
        assert await is_acknowledged(db, key[2], key[1])
        assert calls == ["started"]
        await db.close()

    try:
        key, instance = loop.run_until_complete(start())
        release.set()
        asyncio.run(wait_and_try_wrong_loop(key))
        loop.run_until_complete(finish(key, instance))
    finally:
        release.set()
        if db._engine is not None:
            loop.run_until_complete(db.close())
        loop.close()


@pytest.mark.parametrize("missing", ["consumer-table", "eligibility-column"])
@pytest.mark.asyncio
async def test_postgres_late_ack_does_not_initialize_incomplete_schema(
    pg_url, monkeypatch, caplog, missing
):
    from sqlalchemy import event, text

    import lionagi.state.db as db_module
    import lionagi.state.engine as engine_module
    from lionagi.state.lifecycle.reconciler import _store_identity, _workers, _workers_lock

    schema = "late_ack_" + uuid.uuid4().hex
    original_engine = engine_module.make_engine
    admin = original_engine(pg_url)
    state = None
    created = False
    started, release = threading.Event(), threading.Event()

    def work():
        started.set()
        return DeliveryOutcome.SUCCESS if release.wait(8) else DeliveryOutcome.FAILURE

    async def prepare(_row, _db):
        return PreparedDelivery(work)

    def scoped_engine(url, **kwargs):
        engine = original_engine(url, **kwargs)

        @event.listens_for(engine.sync_engine, "connect")
        def set_schema(connection, _record):
            cursor = connection.cursor()
            try:
                cursor.execute(f'SET search_path TO "{schema}"')  # noqa: S608
            finally:
                cursor.close()
            connection.commit()

        return engine

    try:
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))  # noqa: S608
        created = True
        monkeypatch.setattr(db_module, "make_engine", scoped_engine)
        monkeypatch.setattr(engine_module, "make_engine", scoped_engine)
        state = StateDB(url=pg_url)
        await state.open()
        event_id = await _seed(state)
        key = (_store_identity(state), "pg-missing-schema-test", event_id)
        reconciler = TerminalReconciler(key[1], prepare, batch_size=1, timeout=0.05)
        assert (await reconciler.run_pass(state))["failed"] == 1 and started.is_set()
        if missing == "consumer-table":
            await state.execute("DROP TABLE terminal_consumers")
        else:
            await state.execute("DROP INDEX idx_status_transitions_unclassified")
            await state.execute("DROP INDEX idx_status_transitions_eligible_order")
            await state.execute("ALTER TABLE status_transitions DROP COLUMN delivery_eligible")
        release.set()

        def no_worker():
            with _workers_lock:
                return key not in _workers

        await _wait_until(no_worker)
        assert "ConsumerSchemaError" in caplog.text
        rows = await state.fetch_all("SELECT transition_id FROM terminal_deliveries")
        assert rows == []
        async with state._read() as conn:
            from sqlalchemy import inspect

            shape = await conn.run_sync(
                lambda c: {
                    name: {column["name"] for column in inspect(c).get_columns(name)}
                    for name in inspect(c).get_table_names()
                }
            )
        assert ("terminal_consumers" in shape) is (missing != "consumer-table")
        assert ("delivery_eligible" in shape["status_transitions"]) is (
            missing != "eligibility-column"
        )
    finally:
        release.set()
        if state is not None:
            await state.close()
        if created:
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))  # noqa: S608
        await admin.dispose()


@pytest.mark.asyncio
async def test_late_ack_does_not_recreate_missing_sqlite_file(tmp_path):
    from lionagi.state.lifecycle.reconciler import _store_identity, _workers, _workers_lock

    path = tmp_path / "state.db"
    started, release = threading.Event(), threading.Event()

    def work():
        started.set()
        return DeliveryOutcome.SUCCESS if release.wait(5) else DeliveryOutcome.FAILURE

    async def prepare(_row, _db):
        return PreparedDelivery(work)

    db = StateDB(path)
    await db.open()
    try:
        event_id = await _seed(db)
        key = (_store_identity(db), "missing-file-test", event_id)
        reconciler = TerminalReconciler(key[1], prepare, batch_size=1, timeout=0.05)
        assert (await reconciler.run_pass(db))["failed"] == 1 and started.is_set()
        await db.close()
        path.rename(tmp_path / "old.db")
        assert not path.exists()
        release.set()

        def no_worker():
            with _workers_lock:
                return key not in _workers

        await _wait_until(no_worker)
        assert not path.exists()
    finally:
        release.set()
        await db.close()


@pytest.mark.asyncio
async def test_shared_memory_first_late_ack_after_consumer_retirement():
    from lionagi.state.lifecycle.reconciler import _store_identity, _workers, _workers_lock

    url = f"sqlite+aiosqlite:///file:retire-first-{uuid.uuid4().hex}?mode=memory&cache=shared&uri=true"
    started, release = threading.Event(), threading.Event()
    calls = []

    def work():
        calls.append("started")
        started.set()
        return DeliveryOutcome.SUCCESS if release.wait(5) else DeliveryOutcome.FAILURE

    async def prepare(_row, _db):
        return PreparedDelivery(work)

    async with StateDB(url=url) as db:
        event_id = await _seed(db)
        key = (_store_identity(db), "retire-first-shared-test", event_id)
        reconciler = TerminalReconciler(key[1], prepare, batch_size=1, timeout=0.05)
        try:
            assert (await reconciler.run_pass(db))["failed"] == 1 and started.is_set()
            assert not await is_acknowledged(db, event_id, key[1])
            assert await retire_consumer(db, key[1])
            assert not await is_acknowledged(db, event_id, key[1])
            release.set()

            def no_worker():
                with _workers_lock:
                    return key not in _workers

            await _wait_until(no_worker)
            assert await is_acknowledged(db, event_id, key[1])
            rows = await db.fetch_all(
                "SELECT transition_id FROM terminal_deliveries WHERE consumer = ?", (key[1],)
            )
            assert rows == [{"transition_id": event_id}]
            assert (await reconciler.run_pass(db))["attempted"] == 0
            assert calls == ["started"]
        finally:
            release.set()
