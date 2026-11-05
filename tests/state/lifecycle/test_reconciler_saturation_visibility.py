# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Shared saturation reporting follows the actual delivery reservations."""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid

import pytest

from lionagi.state.db import StateDB
from lionagi.state.lifecycle import reconciler as reconciliation
from lionagi.state.lifecycle.deliveries import is_acknowledged, retire_consumer
from lionagi.state.lifecycle.reconciler import DeliveryOutcome, PreparedDelivery, TerminalReconciler


async def _seed(db, count):
    ids = []
    for index in range(count):
        session, progression = str(uuid.uuid4()), str(uuid.uuid4())
        await db.create_progression(progression)
        await db.create_session({"id": session, "progression_id": progression, "status": "running"})
        assert await db.update_status(
            "session", session, new_status="failed", reason_code="session.stale.no_heartbeat"
        )
        row = await db.fetch_one(
            "SELECT id FROM status_transitions WHERE entity_type = ? AND entity_id = ? AND status = ?",
            ("session", session, "failed"),
        )
        await db.execute(
            "UPDATE status_transitions SET created_at = ? WHERE id = ?", (index + 1, row["id"])
        )
        ids.append(row["id"])
    return ids


def _warnings(caplog, consumer):
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == reconciliation.__name__
        and "Terminal delivery saturated" in record.getMessage()
        and f"consumer={consumer} " in record.getMessage()
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [1, 3])
async def test_shared_saturation_warning_is_throttled_and_resets_on_recovery(
    tmp_path, monkeypatch, caplog, batch
):
    caplog.set_level(logging.WARNING, logger=reconciliation.__name__)
    path = tmp_path / "private-store-name.db"
    clock_a, clock_b = [100.0], [10000.0]
    release, released = asyncio.Event(), asyncio.Event()
    tasks, release_count = [], []
    loop = asyncio.get_running_loop()
    lock = threading.Lock()
    real_release = reconciliation._release_worker

    def track_release(worker):
        real_release(worker)
        if worker.key[1] == "visibility":
            with lock:
                release_count.append(worker.key)
                enough = len(release_count) >= batch
            if enough:
                loop.call_soon_threadsafe(released.set)

    monkeypatch.setattr(reconciliation, "_release_worker", track_release)

    async def hook():
        tasks.append(asyncio.current_task())
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return DeliveryOutcome.FAILURE

    async def prepare(_row, _db):
        return PreparedDelivery(hook)

    async with StateDB(path) as db, StateDB(url=f"sqlite+aiosqlite:///{path}?timeout=5") as alias:
        await _seed(db, batch)
        first = TerminalReconciler(
            "visibility", prepare, batch_size=batch, timeout=0.02, clock=lambda: clock_a[0]
        )
        second = TerminalReconciler(
            "visibility", prepare, batch_size=batch, timeout=0.02, clock=lambda: clock_b[0]
        )
        try:
            initial = await first.run_pass(db)
            assert initial["saturated"] == 1 and initial["outstanding"] == batch
            assert initial["past_timeout"] == batch and initial["oldest_age_seconds"] == 0
            assert len(_warnings(caplog, "visibility")) == 1
            warning = _warnings(caplog, "visibility")[0]
            assert str(path) not in warning and "store=" in warning
            assert f"outstanding={batch} " in warning and f"past_timeout={batch} " in warning
            assert "oldest_age_seconds=0.000" in warning

            clock_a[0] = 101
            clock_b[0] = 50000
            shared = await second.run_pass(alias)
            assert shared["saturated"] == 1 and shared["outstanding"] == batch
            assert shared["past_timeout"] == batch and shared["oldest_age_seconds"] == 1
            assert len(_warnings(caplog, "visibility")) == 1
            clock_a[0] = 699.9
            await second.run_pass(alias)
            assert len(_warnings(caplog, "visibility")) == 1
            clock_a[0] = 700
            await second.run_pass(alias)
            assert len(_warnings(caplog, "visibility")) == 2

            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.wait_for(released.wait(), timeout=3)
            assert first._pending()[1] == 0
            with reconciliation._workers_lock:
                assert (
                    first._store_identity,
                    "visibility",
                ) not in reconciliation._saturation_warnings
            release.clear()
            released.clear()
            release_count.clear()
            tasks.clear()
            clock_a[0] = 701
            again = await first.run_pass(db)
            if not again["attempted"]:
                again = await first.run_pass(db)
            assert again["saturated"] == 1 and again["outstanding"] == batch
            assert len(_warnings(caplog, "visibility")) == 3
        finally:
            release.set()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
                await asyncio.wait_for(released.wait(), timeout=3)


@pytest.mark.asyncio
async def test_cancellation_honoring_hook_releases_slot_and_allows_next_attempt(
    tmp_path, monkeypatch
):
    release_seen = asyncio.Event()
    loop = asyncio.get_running_loop()
    real_release = reconciliation._release_worker
    calls = []
    now = [100.0]

    def notify_release(worker):
        real_release(worker)
        if worker.key[1] == "honors-cancel":
            loop.call_soon_threadsafe(release_seen.set)

    monkeypatch.setattr(reconciliation, "_release_worker", notify_release)

    async def prepare(row, _db):
        async def hook():
            calls.append(row["transition_id"])
            if len(calls) == 1:
                await asyncio.Event().wait()
            return DeliveryOutcome.SUCCESS

        return PreparedDelivery(hook)

    async with StateDB(tmp_path / "honors.db") as db:
        (first_id,) = await _seed(db, 1)
        reconciler = TerminalReconciler(
            "honors-cancel", prepare, batch_size=1, timeout=0.02, clock=lambda: now[0]
        )
        initial = await reconciler.run_pass(db)
        assert initial["failed"] == 1 and calls == [first_id]
        await asyncio.wait_for(release_seen.wait(), timeout=2)
        assert reconciler._pending()[1] == 0
        assert not await is_acknowledged(db, first_id, "honors-cancel")
        now[0] = 101
        later = await reconciler.run_pass(db)
        if not later["attempted"]:
            later = await reconciler.run_pass(db)
        assert later["acknowledged"] == 1 and calls == [first_id, first_id]
        assert await is_acknowledged(db, first_id, "honors-cancel")
        assert later["outstanding"] == later["past_timeout"] == 0


@pytest.mark.asyncio
async def test_lower_active_limit_reports_busy_without_duplicate_warning(
    tmp_path, monkeypatch, caplog
):
    caplog.set_level(logging.WARNING, logger=reconciliation.__name__)
    release, finished = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    real_release, real_reserve = reconciliation._release_worker, reconciliation._reserve_worker
    busy = []

    def reserve(*args, **kwargs):
        worker = real_reserve(*args, **kwargs)
        if args[2] == "lower-limit" and worker is None:
            busy.append(args[3])
        return worker

    def complete(worker):
        real_release(worker)
        if worker.key[1] == "lower-limit":
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr(reconciliation, "_reserve_worker", reserve)
    monkeypatch.setattr(reconciliation, "_release_worker", complete)

    async def hook():
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return DeliveryOutcome.FAILURE

    async def prepare(_row, _db):
        return PreparedDelivery(hook)

    async with StateDB(tmp_path / "busy.db") as db:
        _first_id, next_id = await _seed(db, 2)
        small = TerminalReconciler("lower-limit", prepare, batch_size=1, timeout=0.02)
        large = TerminalReconciler("lower-limit", prepare, batch_size=3, timeout=0.02)
        try:
            assert (await small.run_pass(db))["saturated"] == 1
            result = await large.run_pass(db)
            assert result["saturated"] == 1 and result["attempted"] == 0
            assert result["outstanding"] == result["past_timeout"] == 1
            assert busy == [next_id]
            assert len(_warnings(caplog, "lower-limit")) == 1
        finally:
            release.set()
            await asyncio.wait_for(finished.wait(), timeout=3)


@pytest.mark.asyncio
async def test_retirement_clears_warning_but_reports_outstanding(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=reconciliation.__name__)
    release, finished = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    real_release = reconciliation._release_worker

    def complete(worker):
        real_release(worker)
        if worker.key[1] == "retired-visibility":
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr(reconciliation, "_release_worker", complete)

    async def hook():
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return DeliveryOutcome.FAILURE

    async def prepare(_row, _db):
        return PreparedDelivery(hook)

    async with StateDB(tmp_path / "retired.db") as db:
        await _seed(db, 1)
        reconciler = TerminalReconciler("retired-visibility", prepare, batch_size=1, timeout=0.02)
        try:
            assert (await reconciler.run_pass(db))["saturated"] == 1
            assert await retire_consumer(db, "retired-visibility")
            result = await reconciler.run_pass(db)
            assert result["outstanding"] == result["past_timeout"] == 1
            assert result["saturated"] == result["attempted"] == 0
            with reconciliation._workers_lock:
                assert (
                    reconciler._store_identity,
                    "retired-visibility",
                ) not in reconciliation._saturation_warnings
            assert len(_warnings(caplog, "retired-visibility")) == 1
        finally:
            release.set()
            await asyncio.wait_for(finished.wait(), timeout=3)


@pytest.mark.asyncio
async def test_caller_cancellation_preserves_returned_awaitable_phase_deadline(
    tmp_path, monkeypatch
):
    now = [100.0]
    started, cancelled, release, finished = (asyncio.Event() for _ in range(4))
    loop = asyncio.get_running_loop()
    real_release = reconciliation._release_worker

    def complete(worker):
        real_release(worker)
        if worker.key[1] == "phase-deadline":
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr(reconciliation, "_release_worker", complete)

    async def hook():
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return DeliveryOutcome.FAILURE

    def factory():
        # The sync phase used part of its own budget. The awaitable starts a
        # distinct phase and therefore gets a new deadline.
        now[0] = 200.0
        return hook()

    async def prepare(_row, _db):
        return PreparedDelivery(factory)

    async with StateDB(tmp_path / "phase.db") as db:
        await _seed(db, 1)
        first = TerminalReconciler(
            "phase-deadline", prepare, batch_size=1, timeout=1, clock=lambda: now[0]
        )
        second = TerminalReconciler(
            "phase-deadline", prepare, batch_size=1, timeout=1, clock=lambda: now[0]
        )
        task = asyncio.create_task(first.run_pass(db))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(cancelled.wait(), timeout=2)
            before = await second.run_pass(db)
            assert before["outstanding"] == 1 and before["past_timeout"] == 0
            assert before["oldest_age_seconds"] == 100
            now[0] = 201
            after = await second.run_pass(db)
            assert after["outstanding"] == after["past_timeout"] == 1
            assert after["oldest_age_seconds"] == 101
        finally:
            release.set()
            await asyncio.wait_for(finished.wait(), timeout=3)
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["native", "sync-returned"])
async def test_injected_clock_can_reenter_reservation_inspection(tmp_path, monkeypatch, surface):
    release, finished = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    real_release = reconciliation._release_worker
    holder = []
    calls = []

    def clock():
        # Fail promptly rather than hanging the test if caller code is invoked
        # under the registry's non-reentrant lock.
        assert reconciliation._workers_lock.acquire(blocking=False)
        reconciliation._workers_lock.release()
        if holder and holder[0]._store_identity is not None:
            holder[0]._pending()
        calls.append(True)
        return 100.0

    def complete(worker):
        real_release(worker)
        if worker.key[1] == "reentrant-clock":
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr(reconciliation, "_release_worker", complete)

    async def hook():
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return DeliveryOutcome.FAILURE

    async def prepare(_row, _db):
        if surface == "native":
            return PreparedDelivery(hook)
        return PreparedDelivery(lambda: hook())

    async with StateDB(tmp_path / "reentrant.db") as db:
        await _seed(db, 1)
        reconciler = TerminalReconciler(
            "reentrant-clock", prepare, batch_size=1, timeout=0.02, clock=clock
        )
        holder.append(reconciler)
        try:
            result = await reconciler.run_pass(db)
            assert result["saturated"] == 1 and result["outstanding"] == 1
            assert len(calls) >= 2
        finally:
            release.set()
            await asyncio.wait_for(finished.wait(), timeout=3)


@pytest.mark.asyncio
async def test_clock_churn_is_bounded_and_next_stable_snapshot_warns(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger=reconciliation.__name__)
    consumer = "clock-churn"
    holder = {}
    armed = [False]
    steady_clock = lambda: 100.0

    def mutating_clock():
        if armed[0]:
            armed[0] = False
            reconciliation._release_worker(holder["old"])
            holder["new"] = reconciliation._reserve_worker(
                holder["store"],
                holder["db"],
                consumer,
                "new",
                1,
                started_at=100.0,
                timeout_seconds=1,
                clock=steady_clock,
            )
            assert holder["new"] is not None
        return 100.0

    async with StateDB(tmp_path / "churn.db") as db:
        store = reconciliation._store_identity(db)
        holder.update(store=store, db=db)
        old = reconciliation._reserve_worker(
            store,
            db,
            consumer,
            "old",
            1,
            started_at=100.0,
            timeout_seconds=1,
            clock=mutating_clock,
        )
        assert old is not None
        holder["old"] = old
        reconciler = TerminalReconciler(
            consumer, lambda _row, _db: None, batch_size=1, clock=mutating_clock
        )
        armed[0] = True
        try:
            snapshot = {"saturated": 0}
            reconciler._report_capacity(store, snapshot)
            # The reported membership is the atomic pre-clock snapshot; the
            # changed episode must not install a stale warning or retry forever.
            assert snapshot["outstanding"] == 1 and snapshot["saturated"] == 1
            assert _warnings(caplog, consumer) == []
            with reconciliation._workers_lock:
                assert (store, consumer) not in reconciliation._saturation_warnings
            stable = {"saturated": 0}
            reconciler._report_capacity(store, stable)
            assert stable["outstanding"] == 1 and stable["saturated"] == 1
            assert len(_warnings(caplog, consumer)) == 1
        finally:
            reconciliation._release_worker(old)
            if holder.get("new") is not None:
                reconciliation._release_worker(holder["new"])


@pytest.mark.asyncio
async def test_stale_retired_snapshot_cannot_clear_new_saturated_episode(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger=reconciliation.__name__)
    consumer = "retired-churn"
    holder = {}
    armed = [False]

    def steady_clock():
        return 100.0

    def interleaving_clock():
        if armed[0]:
            armed[0] = False
            # Recovery followed by a new episode can happen while this stale
            # retired snapshot samples its injected clock outside the lock.
            reconciliation._release_worker(holder["old"])
            holder["new"] = reconciliation._reserve_worker(
                holder["store"],
                holder["db"],
                consumer,
                "new",
                1,
                started_at=100.0,
                timeout_seconds=1,
                clock=steady_clock,
            )
            assert holder["new"] is not None
            holder["new_reconciler"]._report_capacity(holder["store"], {"saturated": 0})
            with reconciliation._workers_lock:
                holder["new_warning"] = reconciliation._saturation_warnings[
                    (holder["store"], consumer)
                ]
        return 100.0

    async with StateDB(tmp_path / "retired-churn.db") as db:
        store = reconciliation._store_identity(db)
        holder.update(store=store, db=db)
        old = reconciliation._reserve_worker(
            store,
            db,
            consumer,
            "old",
            1,
            started_at=100.0,
            timeout_seconds=1,
            clock=interleaving_clock,
        )
        assert old is not None
        holder["old"] = old
        stale = TerminalReconciler(
            consumer, lambda _row, _db: None, batch_size=1, clock=interleaving_clock
        )
        current = TerminalReconciler(
            consumer, lambda _row, _db: None, batch_size=1, clock=steady_clock
        )
        holder["new_reconciler"] = current
        stale._report_capacity(store, {"saturated": 0})
        assert len(_warnings(caplog, consumer)) == 1
        armed[0] = True
        try:
            stale._report_capacity(store, {"saturated": 0}, retired=True)
            with reconciliation._workers_lock:
                assert (
                    reconciliation._saturation_warnings[(store, consumer)] is holder["new_warning"]
                )
            assert len(_warnings(caplog, consumer)) == 2
            current._report_capacity(store, {"saturated": 0})
            assert len(_warnings(caplog, consumer)) == 2
        finally:
            reconciliation._release_worker(old)
            if holder.get("new") is not None:
                reconciliation._release_worker(holder["new"])


@pytest.mark.asyncio
async def test_stale_retired_snapshot_cannot_clear_reobserved_same_episode(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger=reconciliation.__name__)
    consumer = "retired-same-episode"
    holder = {}
    armed = [False]

    def steady_clock():
        return 100.0

    def interleaving_clock():
        if armed[0]:
            armed[0] = False
            # The reactivated consumer observes the same worker and warning
            # episode while the stale retired pass samples its clock.
            holder["current"]._report_capacity(holder["store"], {"saturated": 0})
        return 100.0

    async with StateDB(tmp_path / "retired-same.db") as db:
        store = reconciliation._store_identity(db)
        holder["store"] = store
        worker = reconciliation._reserve_worker(
            store,
            db,
            consumer,
            "held",
            1,
            started_at=100.0,
            timeout_seconds=1,
            clock=interleaving_clock,
        )
        assert worker is not None
        stale = TerminalReconciler(
            consumer, lambda _row, _db: None, batch_size=1, clock=interleaving_clock
        )
        current = TerminalReconciler(
            consumer, lambda _row, _db: None, batch_size=1, clock=steady_clock
        )
        holder["current"] = current
        stale._report_capacity(store, {"saturated": 0})
        assert len(_warnings(caplog, consumer)) == 1
        with reconciliation._workers_lock:
            episode = reconciliation._saturation_warnings[(store, consumer)]
            old_version = episode.version
        armed[0] = True
        try:
            stale._report_capacity(store, {"saturated": 0}, retired=True)
            with reconciliation._workers_lock:
                assert reconciliation._saturation_warnings[(store, consumer)] is episode
                assert episode.version == old_version + 1
            current._report_capacity(store, {"saturated": 0})
            assert len(_warnings(caplog, consumer)) == 1
        finally:
            reconciliation._release_worker(worker)
