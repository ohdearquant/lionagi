# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Recover completed foreign-loop deliveries without restarting that loop."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import uuid

import pytest

from lionagi.state.db import StateDB
from lionagi.state.lifecycle import reconciler as reconciliation
from lionagi.state.lifecycle.deliveries import is_acknowledged, retire_consumer
from lionagi.state.lifecycle.reconciler import DeliveryOutcome, PreparedDelivery, TerminalReconciler


async def _eventually(predicate, timeout=3):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout)


async def _seed(db):
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
    return row["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["immediate", "detached"])
@pytest.mark.parametrize("outcome", ["success", "failure", "false", "exception", "cancelled"])
@pytest.mark.parametrize("retired", [False, True], ids=["active", "retired"])
async def test_pass_observes_completed_future_on_stopped_foreign_loop_once(
    tmp_path, monkeypatch, timing, outcome, retired
):
    foreign_loop = asyncio.new_event_loop()
    future = foreign_loop.create_future()
    started, factory_release, finish_started, finish_thread = (threading.Event() for _ in range(4))
    ack_keys, releases = [], []
    counts_lock = threading.Lock()
    real_ack = reconciliation.ack_delivery
    real_release = reconciliation._release_worker
    real_finish = reconciliation._finish_late

    async def tracked_ack(db, transition, consumer):
        with counts_lock:
            ack_keys.append((transition, consumer))
        await real_ack(db, transition, consumer)

    def tracked_release(worker):
        if worker.key[1] == "stopped-foreign":
            with counts_lock:
                releases.append(worker.key)
        real_release(worker)

    def tracked_finish(worker):
        finish_started.set()
        try:
            real_finish(worker)
        finally:
            finish_thread.set()

    monkeypatch.setattr(reconciliation, "ack_delivery", tracked_ack)
    monkeypatch.setattr(reconciliation, "_release_worker", tracked_release)
    monkeypatch.setattr(reconciliation, "_finish_late", tracked_finish)

    def factory():
        started.set()
        if timing == "detached" and not factory_release.wait(3):
            return DeliveryOutcome.FAILURE
        return future

    async def prepare(_row, _db):
        return PreparedDelivery(factory)

    loop_thread = None
    async with StateDB(tmp_path / "foreign.db") as db:
        transition = await _seed(db)
        first = TerminalReconciler(
            "stopped-foreign", prepare, batch_size=1, timeout=0.05, clock=lambda: 100.0
        )
        second = TerminalReconciler(
            "stopped-foreign", prepare, batch_size=1, timeout=0.05, clock=lambda: 100.0
        )
        try:
            initial = await first.run_pass(db)
            assert initial["attempted"] == initial["failed"] == 1 and started.is_set()
            factory_release.set()

            def reserved():
                with reconciliation._workers_lock:
                    return any(
                        key[1:] == ("stopped-foreign", transition)
                        and (worker.async_task is future or worker.late_task is future)
                        for key, worker in reconciliation._workers.items()
                    )

            await _eventually(reserved)
            if timing == "detached":
                await _eventually(finish_started.is_set)
            await _eventually(lambda: bool(foreign_loop._ready))
            assert not future.done()
            blocked = await second.run_pass(db)
            assert blocked["saturated"] == 1 and blocked["attempted"] == 0
            if outcome == "success":
                future.set_result(DeliveryOutcome.SUCCESS)
            elif outcome == "failure":
                future.set_result(DeliveryOutcome.FAILURE)
            elif outcome == "false":
                future.set_result(False)
            elif outcome == "exception":
                future.set_exception(ValueError("hook failed"))
            else:
                future.cancel()
            assert future.done() and not foreign_loop.is_running()
            queued_before = len(foreign_loop._ready)
            if retired:
                assert await retire_consumer(db, "stopped-foreign")

            # The next pass must claim the already-finished result without
            # restarting or scheduling new work on the foreign loop.
            pass_result = await first.run_pass(db)
            assert pass_result["attempted"] == 0
            assert len(foreign_loop._ready) == queued_before

            def released():
                with reconciliation._workers_lock:
                    return not any(
                        key[1:] == ("stopped-foreign", transition)
                        for key in reconciliation._workers
                    )

            await _eventually(released)
            assert await is_acknowledged(db, transition, "stopped-foreign") is (
                outcome == "success"
            )
            if timing == "detached":
                # The polling thread must not remain alive until loop resume.
                await _eventually(finish_thread.is_set)
            assert (await first.run_pass(db))["attempted"] == 0
            with counts_lock:
                assert len(releases) == 1
                assert ack_keys == (
                    [(transition, "stopped-foreign")] if outcome == "success" else []
                )

            def run_loop():
                asyncio.set_event_loop(foreign_loop)
                foreign_loop.run_forever()

            loop_thread = threading.Thread(target=run_loop, daemon=True)
            loop_thread.start()
            await _eventually(foreign_loop.is_running)
            # Drain registrations queued before observation. They must not
            # acknowledge or release this already-claimed execution again.
            drained = threading.Event()
            foreign_loop.call_soon_threadsafe(drained.set)
            await _eventually(drained.is_set)
            await asyncio.sleep(0.05)
            with counts_lock:
                assert len(releases) == 1
                assert ack_keys == (
                    [(transition, "stopped-foreign")] if outcome == "success" else []
                )
        finally:
            factory_release.set()
            if not future.done():
                future.cancel()
            if foreign_loop.is_running():
                foreign_loop.call_soon_threadsafe(foreign_loop.stop)
            if loop_thread is not None:
                loop_thread.join(timeout=2)
            if not foreign_loop.is_closed() and not foreign_loop.is_running():
                foreign_loop.close()
            with reconciliation._workers_lock:
                workers = [
                    worker
                    for key, worker in reconciliation._workers.items()
                    if key[1:] == ("stopped-foreign", transition)
                ]
            for worker in workers:
                real_release(worker)


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["immediate", "detached"])
async def test_resumed_callback_cannot_release_while_pass_ack_is_in_flight(
    tmp_path, monkeypatch, timing
):
    foreign_loop = asyncio.new_event_loop()
    future = foreign_loop.create_future()
    started, factory_release, ack_entered, ack_release, poller_finished = (
        threading.Event() for _ in range(5)
    )
    ack_calls, releases, record_calls = [], [], []
    lock = threading.Lock()
    real_ack = reconciliation.ack_delivery
    real_release = reconciliation._release_worker
    real_finish = reconciliation._finish_late
    real_record = reconciliation._record_claimed_task

    async def hold_ack(db, transition, consumer):
        with lock:
            ack_calls.append((transition, consumer))
        ack_entered.set()
        while not ack_release.is_set():
            await asyncio.sleep(0.005)
        await real_ack(db, transition, consumer)

    def count_release(worker):
        if worker.key[1] == "concurrent-ack":
            with lock:
                releases.append(worker.key)
        real_release(worker)

    def observe_poller(worker):
        try:
            real_finish(worker)
        finally:
            poller_finished.set()

    def count_record(worker, task):
        with lock:
            record_calls.append((worker, task))
        real_record(worker, task)

    monkeypatch.setattr(reconciliation, "ack_delivery", hold_ack)
    monkeypatch.setattr(reconciliation, "_release_worker", count_release)
    monkeypatch.setattr(reconciliation, "_finish_late", observe_poller)
    monkeypatch.setattr(reconciliation, "_record_claimed_task", count_record)

    def factory():
        started.set()
        if timing == "detached" and not factory_release.wait(3):
            return DeliveryOutcome.FAILURE
        return future

    async def prepare(_row, _db):
        return PreparedDelivery(factory)

    def run_loop():
        asyncio.set_event_loop(foreign_loop)
        foreign_loop.run_forever()

    loop_thread = None
    async with StateDB(tmp_path / "in-flight.db") as db:
        transition = await _seed(db)
        reconciler = TerminalReconciler(
            "concurrent-ack", prepare, batch_size=1, timeout=0.05, clock=lambda: 100.0
        )
        try:
            initial = await reconciler.run_pass(db)
            assert initial["attempted"] == initial["failed"] == 1 and started.is_set()
            factory_release.set()

            def observed():
                with reconciliation._workers_lock:
                    return any(
                        key[1:] == ("concurrent-ack", transition)
                        and (worker.async_task is future or worker.late_task is future)
                        for key, worker in reconciliation._workers.items()
                    )

            await _eventually(observed)
            await _eventually(lambda: bool(foreign_loop._ready))
            future.set_result(DeliveryOutcome.SUCCESS)
            assert (await reconciler.run_pass(db))["attempted"] == 0
            await _eventually(ack_entered.is_set)
            assert reconciler._pending()[1] == 1
            if timing == "detached":
                await _eventually(poller_finished.is_set)
                with reconciliation._workers_lock:
                    worker = next(
                        item
                        for key, item in reconciliation._workers.items()
                        if key[1:] == ("concurrent-ack", transition)
                    )
                # A competing late observer must lose while ACK is in flight.
                reconciliation._late_delivery_task_done(worker, future)
                with lock:
                    assert record_calls == [(worker, future)]

            # The original loop resumes while the winning acknowledgement is
            # deliberately blocked. Queued callbacks must lose the claim.
            loop_thread = threading.Thread(target=run_loop, daemon=True)
            loop_thread.start()
            await _eventually(foreign_loop.is_running)

            async def drain_callbacks():
                for _ in range(5):
                    await asyncio.sleep(0)

            drained = asyncio.run_coroutine_threadsafe(drain_callbacks(), foreign_loop)
            await asyncio.wait_for(asyncio.wrap_future(drained), timeout=2)
            assert reconciler._pending()[1] == 1
            with lock:
                assert ack_calls == [(transition, "concurrent-ack")]
                assert releases == []
            assert not await is_acknowledged(db, transition, "concurrent-ack")

            ack_release.set()

            def released():
                with reconciliation._workers_lock:
                    return not any(
                        key[1:] == ("concurrent-ack", transition) for key in reconciliation._workers
                    )

            await _eventually(released)
            assert await is_acknowledged(db, transition, "concurrent-ack")
            with lock:
                assert ack_calls == [(transition, "concurrent-ack")]
                assert len(releases) == 1
        finally:
            factory_release.set()
            ack_release.set()
            if ack_entered.is_set():

                def ack_finished():
                    with reconciliation._workers_lock:
                        return not any(
                            key[1:] == ("concurrent-ack", transition)
                            for key in reconciliation._workers
                        )

                await _eventually(ack_finished)
            if not future.done():
                future.cancel()
            if foreign_loop.is_running():
                foreign_loop.call_soon_threadsafe(foreign_loop.stop)
            if loop_thread is not None:
                loop_thread.join(timeout=2)
            if not foreign_loop.is_closed() and not foreign_loop.is_running():
                foreign_loop.close()
            with reconciliation._workers_lock:
                workers = [
                    worker
                    for key, worker in reconciliation._workers.items()
                    if key[1:] == ("concurrent-ack", transition)
                ]
            for worker in workers:
                real_release(worker)


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["immediate", "detached"])
@pytest.mark.parametrize("retired", [False, True], ids=["active", "retired"])
async def test_pass_reobserves_future_completed_after_closed_loop_scan(
    tmp_path, monkeypatch, timing, retired
):
    foreign_loop = asyncio.new_event_loop()
    future = foreign_loop.create_future()
    started, factory_release, poller_finished = (threading.Event() for _ in range(3))
    ack_calls, releases = [], []
    lock = threading.Lock()
    real_ack, real_release, real_finish = (
        reconciliation.ack_delivery,
        reconciliation._release_worker,
        reconciliation._finish_late,
    )

    async def tracked_ack(db, transition, consumer):
        with lock:
            ack_calls.append((transition, consumer))
        await real_ack(db, transition, consumer)

    def tracked_release(worker):
        if worker.key[1] == "closed-future":
            with lock:
                releases.append(worker.key)
        real_release(worker)

    def tracked_finish(worker):
        try:
            real_finish(worker)
        finally:
            poller_finished.set()

    monkeypatch.setattr(reconciliation, "ack_delivery", tracked_ack)
    monkeypatch.setattr(reconciliation, "_release_worker", tracked_release)
    monkeypatch.setattr(reconciliation, "_finish_late", tracked_finish)

    def factory():
        started.set()
        if timing == "detached" and not factory_release.wait(3):
            return DeliveryOutcome.FAILURE
        return future

    async def prepare(_row, _db):
        return PreparedDelivery(factory)

    async with StateDB(tmp_path / "closed-future.db") as db:
        transition = await _seed(db)
        reconciler = TerminalReconciler(
            "closed-future", prepare, batch_size=1, timeout=0.05, clock=lambda: 100.0
        )
        try:
            result = await reconciler.run_pass(db)
            assert result["failed"] == 1 and started.is_set()
            factory_release.set()

            def tracked():
                with reconciliation._workers_lock:
                    return any(
                        key[1:] == ("closed-future", transition)
                        and (worker.async_task is future or worker.late_task is future)
                        for key, worker in reconciliation._workers.items()
                    )

            await _eventually(tracked)
            await _eventually(lambda: bool(foreign_loop._ready))
            foreign_loop.close()
            if timing == "detached":
                await _eventually(poller_finished.is_set)
            if retired:
                assert await retire_consumer(db, "closed-future")
            first_scan = await reconciler.run_pass(db)
            assert first_scan["attempted"] == 0 and not future.done()
            assert not await is_acknowledged(db, transition, "closed-future")

            # The externally controlled Future can become done after the loop
            # was closed and after a pass already observed it as unfinished.
            try:
                future.set_result(DeliveryOutcome.SUCCESS)
            except RuntimeError:
                # asyncio may reject scheduling its callbacks on a closed loop
                # after storing the result; the state itself is observable.
                assert future.done()
            assert future.done()
            assert (await reconciler.run_pass(db))["attempted"] == 0

            def released():
                with reconciliation._workers_lock:
                    return not any(
                        key[1:] == ("closed-future", transition) for key in reconciliation._workers
                    )

            await _eventually(released)
            assert await is_acknowledged(db, transition, "closed-future")
            assert (await reconciler.run_pass(db))["attempted"] == 0
            with lock:
                assert ack_calls == [(transition, "closed-future")]
                assert len(releases) == 1
        finally:
            factory_release.set()
            if not future.done():
                try:
                    future.cancel()
                except RuntimeError:
                    pass
            if not foreign_loop.is_closed():
                foreign_loop.close()
            with reconciliation._workers_lock:
                workers = [
                    worker
                    for key, worker in reconciliation._workers.items()
                    if key[1:] == ("closed-future", transition)
                ]
            for worker in workers:
                real_release(worker)


def _claimed_worker_and_task(loop, strict, kind):
    worker = reconciliation._Worker(
        ("claimed-task", "consumer", "transition"),
        None,
        loop,
        concurrent.futures.Future(),
        1,
        None,
        strict_outcomes=strict,
    )
    task = loop.create_future()
    if kind == "cancelled":
        task.cancel()
    elif kind == "system_exit":
        task.set_exception(SystemExit(1))
    else:
        task.set_exception(RuntimeError("hook failed"))
    return worker, task


@pytest.mark.parametrize("strict", [False, True], ids=["non_strict", "strict"])
@pytest.mark.parametrize("kind", ["cancelled", "failed"])
def test_claimed_task_failure_paths_observe_then_release(monkeypatch, caplog, strict, kind):
    loop = asyncio.new_event_loop()
    events = []
    real_observe = reconciliation._observe_cancelled_hook

    def observe(task):
        events.append("observe")
        real_observe(task)

    monkeypatch.setattr(reconciliation, "_observe_cancelled_hook", observe)
    monkeypatch.setattr(reconciliation, "_release_worker", lambda _worker: events.append("release"))
    try:
        worker, task = _claimed_worker_and_task(loop, strict, kind)
        with caplog.at_level("WARNING", logger=reconciliation.logger.name):
            reconciliation._record_claimed_task(worker, task)
    finally:
        loop.close()
    records = [item for item in caplog.records if item.name == reconciliation.logger.name]
    if strict:
        # Strict workers never hand a failed or cancelled task to the observer.
        assert events == ["release"]
        if kind == "cancelled":
            assert records == []
        else:
            assert [item.getMessage() for item in records] == [
                "Late delivery failed for transition: RuntimeError"
            ]
            assert records[0].exc_info is None
        return
    assert events == ["observe", "release"]
    if kind == "cancelled":
        assert records == []
    else:
        # The observer is the only place that logs, so the failure appears once.
        assert [item.getMessage() for item in records] == [
            "terminal delivery hook failed after cancellation"
        ]
        assert records[0].exc_info and records[0].exc_info[0] is RuntimeError


@pytest.mark.parametrize("kind", ["cancelled", "failed"])
def test_claimed_task_releases_worker_when_observer_raises(monkeypatch, kind):
    loop = asyncio.new_event_loop()
    releases = []
    real_observe = reconciliation._observe_cancelled_hook

    def observe(task):
        real_observe(task)
        raise RuntimeError("observer broke")

    monkeypatch.setattr(reconciliation, "_observe_cancelled_hook", observe)
    monkeypatch.setattr(reconciliation, "_release_worker", releases.append)
    try:
        worker, task = _claimed_worker_and_task(loop, False, kind)
        with pytest.raises(RuntimeError, match="observer broke"):
            reconciliation._record_claimed_task(worker, task)
    finally:
        loop.close()
    assert releases == [worker]


def test_claimed_task_releases_worker_when_task_failed_with_system_exit(monkeypatch):
    # The observer consumes ordinary failures only, so a task that failed with a
    # process-exit exception makes it raise; the worker must still be released.
    loop = asyncio.new_event_loop()
    releases = []
    monkeypatch.setattr(reconciliation, "_release_worker", releases.append)
    try:
        worker, task = _claimed_worker_and_task(loop, False, "system_exit")
        with pytest.raises(SystemExit):
            reconciliation._record_claimed_task(worker, task)
    finally:
        loop.close()
    assert releases == [worker]
