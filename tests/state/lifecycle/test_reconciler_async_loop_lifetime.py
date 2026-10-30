# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Async delivery reservations follow the loop that actually owns the task."""

from __future__ import annotations

import asyncio
import threading
import uuid

import pytest

from lionagi.state.db import StateDB
from lionagi.state.lifecycle.deliveries import is_acknowledged, retire_consumer
from lionagi.state.lifecycle.reconciler import DeliveryOutcome, PreparedDelivery, TerminalReconciler


async def _seed(db):
    session, progression = str(uuid.uuid4()), str(uuid.uuid4())
    await db.create_progression(progression)
    await db.create_session({"id": session, "progression_id": progression, "status": "running"})
    await db.update_status(
        "session", session, new_status="failed", reason_code="session.stale.no_heartbeat"
    )
    row = await db.fetch_one(
        "SELECT id FROM status_transitions WHERE entity_type = ? AND entity_id = ? AND status = ?",
        ("session", session, "failed"),
    )
    return row["id"]


async def _eventually(predicate):
    async def wait():
        while not await predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout=2)


@pytest.mark.asyncio
async def test_foreign_loop_task_retains_slot_and_acknowledges_on_its_own_loop(tmp_path):
    loop = asyncio.new_event_loop()
    ready, started, release = (threading.Event() for _ in range(3))
    task_holder = []
    calls = []

    def run_loop():
        asyncio.set_event_loop(loop)
        ready.set()
        loop.run_forever()
        loop.close()

    thread = threading.Thread(target=run_loop, daemon=True)
    thread.start()
    assert ready.wait(2)

    async def hook():
        calls.append("started")
        started.set()
        while not release.is_set():
            try:
                await asyncio.sleep(0.005)
            except asyncio.CancelledError:
                pass
        return DeliveryOutcome.SUCCESS

    def create_task():
        task_holder.append(loop.create_task(hook()))

    loop.call_soon_threadsafe(create_task)
    try:
        async with StateDB(tmp_path / "foreign.db") as db:
            transition = await _seed(db)
            # Wait without blocking the loop that owns the StateDB operations.
            while not started.is_set():
                await asyncio.sleep(0.005)

            async def prepare(_row, _db):
                return PreparedDelivery(lambda: task_holder[0])

            first = TerminalReconciler("foreign-loop", prepare, batch_size=1, timeout=0.05)
            second = TerminalReconciler("foreign-loop", prepare, batch_size=1, timeout=0.05)
            assert (await first.run_pass(db))["failed"] == 1
            blocked = await second.run_pass(db)
            assert blocked["saturated"] == 1 and blocked["attempted"] == 0
            assert calls == ["started"]
            release.set()

            async def acknowledged():
                return await is_acknowledged(db, transition, "foreign-loop")

            await _eventually(acknowledged)
            assert calls == ["started"]
    finally:
        release.set()
        # Wait for the task to finish before stopping its event loop.
        if task_holder:

            async def completed():
                return task_holder[0].done()

            await _eventually(completed)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_closed_loop_unfinished_delivery_stays_reserved_and_unacknowledged(tmp_path):
    from lionagi.state.lifecycle import reconciler as reconciliation

    path = tmp_path / "closed-loop.db"
    loop = asyncio.new_event_loop()
    tasks = []
    calls = []

    async def stalled():
        tasks.append(asyncio.current_task())
        calls.append("stalled")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.Event().wait()
        return DeliveryOutcome.SUCCESS

    async def run_first():
        async with StateDB(path) as db:
            transition = await _seed(db)

            async def prepare(_row, _db):
                return PreparedDelivery(stalled)

            reconciler = TerminalReconciler("closed-loop", prepare, batch_size=1, timeout=0.05)
            assert (await reconciler.run_pass(db))["failed"] == 1
            assert not await is_acknowledged(db, transition, "closed-loop")
            return transition

    try:
        transition = loop.run_until_complete(run_first())
    finally:
        loop.close()
        # The closed loop cannot finish the delivery. Close the suspended
        # coroutine explicitly so the test itself leaves no pending task.
        for task in tasks:
            task._log_destroy_pending = False
            task.get_coro().close()

    async def retry():
        async with StateDB(path) as db:
            assert not await is_acknowledged(db, transition, "closed-loop")

            async def prepare(_row, _db):
                async def replacement():
                    calls.append("retried")
                    return DeliveryOutcome.SUCCESS

                return PreparedDelivery(replacement)

            reconciler = TerminalReconciler("closed-loop", prepare, batch_size=1, timeout=1)
            result = await reconciler.run_pass(db)
            assert result["attempted"] == 0 and result["saturated"] == 1
            assert not await is_acknowledged(db, transition, "closed-loop")

    try:
        asyncio.run(retry())
        assert calls == ["stalled"]
    finally:
        with reconciliation._workers_lock:
            workers = [
                w
                for k, w in reconciliation._workers.items()
                if k[1:] == ("closed-loop", transition)
            ]
        for worker in workers:
            reconciliation._release_worker(worker)


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition", ["resume", "close"])
async def test_detached_factory_keeps_foreign_future_on_stopped_loop(tmp_path, disposition):
    loop = asyncio.new_event_loop()
    started, release, factory_started, factory_release, stopped = (
        threading.Event() for _ in range(5)
    )
    task_holder, calls = [], []

    def run_loop():
        asyncio.set_event_loop(loop)
        loop.run_forever()
        stopped.set()

    thread = threading.Thread(target=run_loop, daemon=True)
    thread.start()

    async def hook():
        started.set()
        while not release.is_set():
            try:
                await asyncio.sleep(0.005)
            except asyncio.CancelledError:
                pass
        return DeliveryOutcome.SUCCESS

    loop.call_soon_threadsafe(lambda: task_holder.append(loop.create_task(hook())))

    async def started_async():
        return started.is_set()

    await _eventually(started_async)

    def factory():
        calls.append("factory")
        factory_started.set()
        assert factory_release.wait(2)
        return task_holder[0]

    async def prepare(_row, _db):
        return PreparedDelivery(factory)

    try:
        async with StateDB(tmp_path / "stopped.db") as db:
            transition = await _seed(db)
            first = TerminalReconciler("stopped-loop", prepare, batch_size=1, timeout=0.05)
            second = TerminalReconciler("stopped-loop", prepare, batch_size=1, timeout=0.05)
            assert (await first.run_pass(db))["failed"] == 1
            assert factory_started.is_set()
            loop.call_soon_threadsafe(loop.stop)

            async def stopped_async():
                return stopped.is_set()

            await _eventually(stopped_async)
            factory_release.set()
            # The detached completion thread has time to encounter the stopped
            # loop. It must not treat a restartable loop as a dead operation.
            await asyncio.sleep(0.2)
            blocked = await second.run_pass(db)
            assert blocked["saturated"] == 1 and blocked["attempted"] == 0
            assert calls == ["factory"]
            assert not await is_acknowledged(db, transition, "stopped-loop")
            if disposition == "resume":
                stopped.clear()
                thread = threading.Thread(target=run_loop, daemon=True)
                thread.start()
                release.set()

                async def acknowledged():
                    return await is_acknowledged(db, transition, "stopped-loop")

                await _eventually(acknowledged)
            else:
                loop.close()

                async def abandoned():
                    from lionagi.state.lifecycle import reconciler as reconciliation

                    with reconciliation._workers_lock:
                        return any(
                            worker.abandoned
                            for key, worker in reconciliation._workers.items()
                            if key[1:] == ("stopped-loop", transition)
                        )

                await _eventually(abandoned)
                assert second._pending()[1] == 1
                assert not await is_acknowledged(db, transition, "stopped-loop")
    finally:
        factory_release.set()
        release.set()
        if loop.is_running():
            if task_holder:

                async def task_finished():
                    return task_holder[0].done()

                await _eventually(task_finished)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=2)
        if not loop.is_closed():
            loop.close()
        for task in task_holder:
            if not task.done():
                task._log_destroy_pending = False
                task.get_coro().close()
        if disposition == "close":
            from lionagi.state.lifecycle import reconciler as reconciliation

            with reconciliation._workers_lock:
                workers = [
                    w
                    for k, w in reconciliation._workers.items()
                    if k[1:] == ("stopped-loop", transition)
                ]
            for worker in workers:
                reconciliation._release_worker(worker)


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition", ["resume", "close"])
async def test_detached_coroutine_waits_on_original_stopped_loop(monkeypatch, disposition):
    import concurrent.futures
    import gc
    import warnings

    from lionagi.state.lifecycle import reconciler as reconciliation

    loop = asyncio.new_event_loop()
    captured = loop.create_future()
    completed: concurrent.futures.Future = concurrent.futures.Future()
    started, finished = threading.Event(), threading.Event()
    outcome = []

    async def hook():
        started.set()
        await captured
        return DeliveryOutcome.SUCCESS

    completed.set_result(hook())
    worker = reconciliation._Worker(
        ("sqlite-test", "loop-bound-coro", str(uuid.uuid4())),
        None,
        loop,
        completed,
        1,
        None,
    )
    with reconciliation._workers_lock:
        reconciliation._workers[worker.key] = worker

    def record_result(item, result):
        outcome.append(result)
        reconciliation._release_worker(item)
        finished.set()

    monkeypatch.setattr(reconciliation, "_finish_late_result", record_result)

    def run_loop():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=reconciliation._finish_late, args=(worker,), daemon=True)
    thread.start()
    loop_thread = None
    try:

        async def queued():
            return bool(loop._ready)

        await _eventually(queued)
        assert not started.is_set() and thread.is_alive()
        with reconciliation._workers_lock:
            assert reconciliation._workers.get(worker.key) is worker
        if disposition == "resume":
            loop_thread = threading.Thread(target=run_loop, daemon=True)
            loop_thread.start()

            async def hook_started():
                return started.is_set()

            await _eventually(hook_started)
            loop.call_soon_threadsafe(captured.set_result, None)

            async def hook_finished():
                return finished.is_set()

            await _eventually(hook_finished)
            assert outcome == [DeliveryOutcome.SUCCESS]
        else:
            with warnings.catch_warnings(record=True) as seen:
                warnings.simplefilter("always", RuntimeWarning)
                loop.close()
                thread.join(timeout=2)
                assert not thread.is_alive()
                gc.collect()
                assert not any("was never awaited" in str(item.message) for item in seen)
            assert outcome == []
            with reconciliation._workers_lock:
                assert reconciliation._workers.get(worker.key) is worker
                assert worker.abandoned
    finally:
        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
            assert loop_thread is not None
            loop_thread.join(timeout=2)
        if not loop.is_closed():
            loop.close()
        thread.join(timeout=2)
        reconciliation._release_worker(worker)


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_closed", [False, True])
async def test_private_memory_late_ack_is_released_when_origin_loop_closes(initially_closed):
    from lionagi.state.lifecycle import reconciler as reconciliation

    origin = asyncio.new_event_loop()
    if initially_closed:
        origin.close()
    async with StateDB(url="sqlite+aiosqlite:///:memory:") as db:
        transition = await _seed(db)
        await reconciliation.register_consumer(db, "closed-private", start="all")
        store = reconciliation._store_identity(db)
        worker = reconciliation._reserve_worker(store, db, "closed-private", transition, 1)
        assert worker is not None
        worker.loop = origin
        try:
            reconciliation._finish_late_result(worker, DeliveryOutcome.SUCCESS)
            with reconciliation._workers_lock:
                assert (worker.key in reconciliation._workers) is (not initially_closed)
                assert worker.deferred is (not initially_closed)
            if not initially_closed:
                # The temporarily stopped loop is still eligible to resume;
                # once closed it can no longer complete the deferred ACK.
                origin.close()
                reconciliation._release_closed_loop_workers(store, "closed-private")
            with reconciliation._workers_lock:
                assert worker.key not in reconciliation._workers
            assert not await is_acknowledged(db, transition, "closed-private")
        finally:
            if not origin.is_closed():
                origin.close()
            reconciliation._release_worker(worker)


@pytest.mark.asyncio
async def test_detached_factory_hook_timeout_error_releases_reservation(tmp_path):
    factory_started, factory_release = threading.Event(), threading.Event()

    async def hook():
        raise TimeoutError("hook failure")

    def factory():
        factory_started.set()
        assert factory_release.wait(2)
        return hook()

    async def prepare(_row, _db):
        return PreparedDelivery(factory)

    async with StateDB(tmp_path / "timeout-exception.db") as db:
        transition = await _seed(db)
        reconciler = TerminalReconciler("hook-timeout", prepare, batch_size=1, timeout=0.05)
        try:
            assert (await reconciler.run_pass(db))["failed"] == 1
            assert factory_started.is_set()
            factory_release.set()

            async def released():
                return reconciler._pending()[1] == 0

            await _eventually(released)
            assert not await is_acknowledged(db, transition, "hook-timeout")
        finally:
            factory_release.set()


@pytest.mark.parametrize("hook_raises_timeout", [False, True])
def test_late_result_after_poll_timeout_distinguishes_success_from_hook_timeout(
    monkeypatch, hook_raises_timeout
):
    import concurrent.futures

    from lionagi.state.lifecycle import reconciler as reconciliation

    loop = asyncio.new_event_loop()
    captured = []

    async def hook():
        return DeliveryOutcome.SUCCESS

    coroutine = hook()
    future: concurrent.futures.Future = concurrent.futures.Future()
    future.set_result(coroutine)
    worker = reconciliation._Worker(
        ("sqlite-test", "poll-race", str(uuid.uuid4())), None, loop, future, 1, None
    )
    with reconciliation._workers_lock:
        reconciliation._workers[worker.key] = worker

    class CompletionAtTimeout:
        calls = 0

        def result(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                # The wait expired, but the actual completion won the race
                # before the caller checks done().
                raise concurrent.futures.TimeoutError()
            if hook_raises_timeout:
                raise TimeoutError("the hook itself timed out")
            return DeliveryOutcome.SUCCESS

        def done(self):
            return True

    pending = CompletionAtTimeout()

    def submit(wrapper, submitted_loop):
        assert submitted_loop is loop
        wrapper.close()
        coroutine.close()
        return pending

    def record_result(item, result):
        captured.append(result)
        reconciliation._release_worker(item)

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", submit)
    monkeypatch.setattr(reconciliation, "_finish_late_result", record_result)
    try:
        reconciliation._finish_late(worker)
        assert pending.calls == 2
        assert captured == ([] if hook_raises_timeout else [DeliveryOutcome.SUCCESS])
        with reconciliation._workers_lock:
            assert worker.key not in reconciliation._workers
    finally:
        loop.close()
        reconciliation._release_worker(worker)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [DeliveryOutcome.SUCCESS, DeliveryOutcome.FAILURE])
@pytest.mark.parametrize("retired", [False, True])
async def test_completed_task_is_observed_when_done_callback_is_lost(tmp_path, outcome, retired):
    from lionagi.state.lifecycle import reconciler as reconciliation

    loop = asyncio.new_event_loop()
    started, cancelled, release, stopped = (threading.Event() for _ in range(4))
    tasks = []

    def run_loop():
        asyncio.set_event_loop(loop)
        loop.run_forever()
        stopped.set()

    thread = threading.Thread(target=run_loop, daemon=True)
    thread.start()

    async def hook():
        started.set()
        while not release.is_set():
            try:
                await asyncio.sleep(0.005)
            except asyncio.CancelledError:
                cancelled.set()
        # Stop the loop in the task itself. Its done callbacks are queued for
        # a later iteration and are never dispatched before the loop closes.
        loop.stop()
        return outcome

    loop.call_soon_threadsafe(lambda: tasks.append(loop.create_task(hook())))

    async def wait_started():
        return started.is_set()

    await _eventually(wait_started)

    async def prepare(_row, _db):
        return PreparedDelivery(lambda: tasks[0])

    try:
        async with StateDB(tmp_path / "completed-before-close.db") as db:
            transition = await _seed(db)
            reconciler = TerminalReconciler("completion-close", prepare, batch_size=1, timeout=0.05)
            assert (await reconciler.run_pass(db))["failed"] == 1

            async def cancellation_observed():
                return cancelled.is_set()

            await _eventually(cancellation_observed)
            release.set()

            async def stopped_async():
                return stopped.is_set()

            await _eventually(stopped_async)
            assert tasks[0].done()
            loop.close()
            if retired:
                assert await retire_consumer(db, "completion-close")
                # A retired pass must observe completion without attempting a
                # new delivery, even though the loop's callback was lost.
                assert (await reconciler.run_pass(db))["attempted"] == 0
            else:
                reconciler._pending()

            async def released():
                with reconciliation._workers_lock:
                    return not any(
                        key[1:] == ("completion-close", transition)
                        for key in reconciliation._workers
                    )

            await _eventually(released)
            assert await is_acknowledged(db, transition, "completion-close") is (
                outcome is DeliveryOutcome.SUCCESS
            )
    finally:
        release.set()
        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        if not loop.is_closed():
            loop.close()
        for task in tasks:
            if not task.done():
                task._log_destroy_pending = False
                task.get_coro().close()


@pytest.mark.asyncio
async def test_shutdown_cancellation_holds_late_factory_slot_until_delivery_finishes(monkeypatch):
    import concurrent.futures

    from lionagi.state.lifecycle import reconciler as reconciliation

    loop = asyncio.new_event_loop()
    started, cancelled, finished, released = (threading.Event() for _ in range(4))
    releases = []
    release = asyncio.Event()
    completion: concurrent.futures.Future = concurrent.futures.Future()

    async def hook():
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        finally:
            finished.set()
        return DeliveryOutcome.FAILURE

    completion.set_result(hook())
    worker = reconciliation._Worker(
        ("sqlite-test", "shutdown-cancel", str(uuid.uuid4())), None, loop, completion, 1, None
    )
    with reconciliation._workers_lock:
        reconciliation._workers[worker.key] = worker

    original_release = reconciliation._release_worker

    def record_release(item):
        releases.append((item, finished.is_set()))
        original_release(item)
        released.set()

    monkeypatch.setattr(reconciliation, "_release_worker", record_release)

    def run_loop():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    loop_thread = threading.Thread(target=run_loop, daemon=True)
    loop_thread.start()
    worker_thread = threading.Thread(
        target=reconciliation._finish_late, args=(worker,), daemon=True
    )
    worker_thread.start()
    try:

        async def delivery_started():
            return started.is_set()

        await _eventually(delivery_started)

        def cancel_loop_tasks():
            # asyncio.run shutdown cancels the wrapper and delivery separately.
            for task in asyncio.all_tasks(loop):
                task.cancel()

        loop.call_soon_threadsafe(cancel_loop_tasks)

        async def cancellation_observed():
            return cancelled.is_set()

        await _eventually(cancellation_observed)
        # Allow the waiter thread to process wrapper cancellation; the child is
        # still inside its cancellation handler and cannot finish before release.
        await asyncio.sleep(0.2)
        assert not released.is_set()
        with reconciliation._workers_lock:
            assert reconciliation._workers.get(worker.key) is worker
        assert not finished.is_set()

        loop.call_soon_threadsafe(release.set)

        async def slot_released():
            return released.is_set()

        await _eventually(slot_released)
        assert releases == [(worker, True)]
        with reconciliation._workers_lock:
            assert worker.key not in reconciliation._workers
    finally:
        loop.call_soon_threadsafe(release.set)

        async def delivery_finished():
            return finished.is_set() and worker.late_task is not None and worker.late_task.done()

        if started.is_set():
            await _eventually(delivery_finished)
        worker_thread.join(timeout=2)
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2)
        if not loop.is_closed():
            loop.close()
        original_release(worker)


@pytest.mark.asyncio
async def test_cancelled_late_wrapper_does_not_ack_success_when_loop_closes(monkeypatch):
    import concurrent.futures

    from lionagi.state.lifecycle import reconciler as reconciliation

    loop = asyncio.new_event_loop()
    started, cancelled, wrapper_waiting, stopped, released = (threading.Event() for _ in range(5))
    release = asyncio.Event()
    acknowledgements = []
    releases = []
    cancelled_tasks = []
    completion: concurrent.futures.Future = concurrent.futures.Future()

    async def hook():
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        # The delivery is done, but the wrapper cannot resume on this loop.
        loop.stop()
        return DeliveryOutcome.SUCCESS

    completion.set_result(hook())
    worker = reconciliation._Worker(
        ("sqlite-test", "cancel-close", str(uuid.uuid4())), None, loop, completion, 1, None
    )
    with reconciliation._workers_lock:
        reconciliation._workers[worker.key] = worker

    original_release = reconciliation._release_worker

    async def record_ack(item):
        acknowledgements.append(item)

    def record_release(item):
        releases.append(item)
        original_release(item)
        released.set()

    monkeypatch.setattr(reconciliation, "_acknowledge_late", record_ack)
    monkeypatch.setattr(reconciliation, "_release_worker", record_release)

    def run_loop():
        asyncio.set_event_loop(loop)
        loop.run_forever()
        stopped.set()

    loop_thread = threading.Thread(target=run_loop, daemon=True)
    loop_thread.start()
    worker_thread = threading.Thread(
        target=reconciliation._finish_late, args=(worker,), daemon=True
    )
    worker_thread.start()
    try:

        async def delivery_started():
            return started.is_set()

        await _eventually(delivery_started)

        def cancel_loop_tasks():
            cancelled_tasks.extend(asyncio.all_tasks(loop))
            for task in cancelled_tasks:
                task.cancel()
            # Both cancellation wakeups precede this loop barrier.
            loop.call_soon(wrapper_waiting.set)

        loop.call_soon_threadsafe(cancel_loop_tasks)

        async def cancellation_observed():
            return cancelled.is_set() and wrapper_waiting.is_set()

        await _eventually(cancellation_observed)
        with reconciliation._workers_lock:
            assert reconciliation._workers.get(worker.key) is worker
        loop.call_soon_threadsafe(release.set)

        async def loop_stopped():
            return stopped.is_set()

        await _eventually(loop_stopped)
        assert worker.late_task is not None and worker.late_task.done()
        loop.close()

        async def slot_released():
            return released.is_set()

        await _eventually(slot_released)
        worker_thread.join(timeout=2)
        assert not worker_thread.is_alive()
        assert acknowledgements == []
        assert releases == [worker]
        with reconciliation._workers_lock:
            assert worker.key not in reconciliation._workers
    finally:
        if loop.is_running():
            loop.call_soon_threadsafe(release.set)
            loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2)
        if not loop.is_closed():
            loop.close()
        worker_thread.join(timeout=2)
        for task in cancelled_tasks:
            if not task.done():
                task._log_destroy_pending = False
        original_release(worker)


@pytest.mark.asyncio
async def test_stopped_foreign_sweep_does_not_ack_after_wrapper_cancellation(monkeypatch):
    import concurrent.futures

    from lionagi.state.lifecycle import reconciler as reconciliation

    origin_loop, foreign_loop = asyncio.new_event_loop(), asyncio.new_event_loop()
    acknowledgements = []
    releases = []
    released = threading.Event()
    completion: concurrent.futures.Future = concurrent.futures.Future()

    # A foreign task has completed successfully, but its owner loop has stopped
    # before the cancelled wrapper can report its failure.
    task = foreign_loop.create_future()
    task.set_result(DeliveryOutcome.SUCCESS)
    completion.set_result(task)
    worker = reconciliation._Worker(
        ("sqlite-path:test", "cancel-foreign", str(uuid.uuid4())),
        None,
        origin_loop,
        completion,
        1,
        None,
        detached=True,
        late_task=task,
        late_cancelled=True,
    )
    with reconciliation._workers_lock:
        reconciliation._workers[worker.key] = worker

    original_release = reconciliation._release_worker

    async def record_ack(item):
        acknowledgements.append(item)

    def record_release(item):
        releases.append(item)
        original_release(item)
        released.set()

    monkeypatch.setattr(reconciliation, "_acknowledge_late", record_ack)
    monkeypatch.setattr(reconciliation, "_release_worker", record_release)
    try:
        reconciliation._release_closed_loop_workers(*worker.key[:2])

        async def slot_released():
            return released.is_set()

        await _eventually(slot_released)
        assert acknowledgements == []
        assert releases == [worker]
        with reconciliation._workers_lock:
            assert worker.key not in reconciliation._workers
    finally:
        original_release(worker)
        origin_loop.close()
        foreign_loop.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [DeliveryOutcome.SUCCESS, DeliveryOutcome.FAILURE])
async def test_completed_late_wrapper_is_observed_when_its_loop_closes(monkeypatch, outcome):
    import concurrent.futures

    from lionagi.state.lifecycle import reconciler as reconciliation

    loop = asyncio.new_event_loop()
    stopped = threading.Event()
    results = []
    completion: concurrent.futures.Future = concurrent.futures.Future()

    async def hook():
        return outcome

    completion.set_result(hook())
    worker = reconciliation._Worker(
        ("sqlite-test", "wrapper-close", str(uuid.uuid4())), None, loop, completion, 1, None
    )
    with reconciliation._workers_lock:
        reconciliation._workers[worker.key] = worker
    original = reconciliation._settle_late_awaitable

    async def settle_then_stop(item, awaitable):
        try:
            return await original(item, awaitable)
        finally:
            loop.stop()

    def record(item, result):
        results.append(result)
        reconciliation._release_worker(item)

    monkeypatch.setattr(reconciliation, "_settle_late_awaitable", settle_then_stop)
    monkeypatch.setattr(reconciliation, "_finish_late_result", record)

    def run_loop():
        asyncio.set_event_loop(loop)
        loop.run_forever()
        stopped.set()

    loop_thread = threading.Thread(target=run_loop, daemon=True)
    loop_thread.start()
    worker_thread = threading.Thread(
        target=reconciliation._finish_late, args=(worker,), daemon=True
    )
    worker_thread.start()
    try:

        async def loop_stopped():
            return stopped.is_set()

        await _eventually(loop_stopped)
        loop.close()
        worker_thread.join(timeout=2)
        assert not worker_thread.is_alive()
        assert results == [outcome]
        with reconciliation._workers_lock:
            assert worker.key not in reconciliation._workers
    finally:
        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2)
        if not loop.is_closed():
            loop.close()
        worker_thread.join(timeout=2)
        reconciliation._release_worker(worker)


@pytest.mark.asyncio
async def test_foreign_task_completing_during_loop_close_keeps_completed_outcome(monkeypatch):
    import concurrent.futures

    from lionagi.state.lifecycle import reconciler as reconciliation

    loop = asyncio.new_event_loop()
    task = loop.create_future()
    worker = reconciliation._Worker(
        ("sqlite-test", "close-race", str(uuid.uuid4())),
        None,
        asyncio.get_running_loop(),
        concurrent.futures.Future(),
        1,
        None,
    )

    def complete_close_then_fail(*_args):
        task.set_result(DeliveryOutcome.SUCCESS)
        loop.close()
        raise RuntimeError("loop closed")

    monkeypatch.setattr(loop, "call_soon_threadsafe", complete_close_then_fail)
    try:
        outcome, returned = await reconciliation._await_reserved_hook(worker, task, timeout=0.05)
        assert outcome is DeliveryOutcome.SUCCESS and returned is worker
        assert not worker.abandoned
    finally:
        if not loop.is_closed():
            loop.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [DeliveryOutcome.SUCCESS, DeliveryOutcome.FAILURE])
async def test_completed_late_hook_is_observed_before_its_wait_wrapper_resumes(
    monkeypatch, outcome
):
    import concurrent.futures

    from lionagi.state.lifecycle import reconciler as reconciliation

    loop = asyncio.new_event_loop()
    stopped = threading.Event()
    results = []
    completion: concurrent.futures.Future = concurrent.futures.Future()

    async def hook():
        # The hook finishes, but the wrapper waiting on the hook cannot run its
        # next queued callback after this loop stops.
        loop.stop()
        return outcome

    completion.set_result(hook())
    worker = reconciliation._Worker(
        ("sqlite-test", "hook-before-wrapper", str(uuid.uuid4())), None, loop, completion, 1, None
    )
    with reconciliation._workers_lock:
        reconciliation._workers[worker.key] = worker

    def record(item, result):
        results.append(result)
        reconciliation._release_worker(item)

    monkeypatch.setattr(reconciliation, "_finish_late_result", record)

    def run_loop():
        asyncio.set_event_loop(loop)
        loop.run_forever()
        stopped.set()

    loop_thread = threading.Thread(target=run_loop, daemon=True)
    loop_thread.start()
    worker_thread = threading.Thread(
        target=reconciliation._finish_late, args=(worker,), daemon=True
    )
    worker_thread.start()
    pending_tasks = set()
    try:

        async def loop_stopped():
            return stopped.is_set()

        await _eventually(loop_stopped)
        assert worker.late_task is not None and worker.late_task.done()
        assert worker.late_settled is None
        pending_tasks = asyncio.all_tasks(loop)
        loop.close()
        worker_thread.join(timeout=2)
        assert not worker_thread.is_alive()
        assert results == [outcome]
        with reconciliation._workers_lock:
            assert worker.key not in reconciliation._workers
    finally:
        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2)
        if not loop.is_closed():
            loop.close()
        worker_thread.join(timeout=2)
        for task in pending_tasks:
            if not task.done():
                task._log_destroy_pending = False
                task.get_coro().close()
        reconciliation._release_worker(worker)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [DeliveryOutcome.SUCCESS, DeliveryOutcome.FAILURE])
@pytest.mark.parametrize("completion", ["before", "queued", "registration"])
async def test_detached_factory_observes_completed_future_on_loop_closure(
    monkeypatch, outcome, completion
):
    import concurrent.futures

    from lionagi.state.lifecycle import reconciler as reconciliation

    loop = asyncio.new_event_loop()
    result = loop.create_future()
    returned: concurrent.futures.Future = concurrent.futures.Future()
    returned.set_result(result)
    worker = reconciliation._Worker(
        ("sqlite-test", "completed-future", str(uuid.uuid4())), None, loop, returned, 1, None
    )
    with reconciliation._workers_lock:
        reconciliation._workers[worker.key] = worker
    outcomes = []

    def record(item, value):
        outcomes.append(value)
        reconciliation._release_worker(item)

    monkeypatch.setattr(reconciliation, "_finish_late_result", record)
    if completion == "before":
        result.set_result(outcome)
    elif completion == "registration":

        def close_during_registration(*_args):
            result.set_result(outcome)
            loop.close()
            raise RuntimeError("closed while scheduling")

        monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", close_during_registration)

    thread = threading.Thread(target=reconciliation._finish_late, args=(worker,), daemon=True)
    thread.start()
    try:
        if completion == "queued":

            async def wrapper_queued():
                return bool(loop._ready)

            await _eventually(wrapper_queued)
            result.set_result(outcome)
            loop.close()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert outcomes == [outcome]
        with reconciliation._workers_lock:
            assert worker.key not in reconciliation._workers
    finally:
        if not loop.is_closed():
            loop.close()
        thread.join(timeout=2)
        reconciliation._release_worker(worker)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["hook", "wrapper"])
async def test_closed_loop_cleanup_exception_keeps_unfinished_reservation(monkeypatch, surface):
    import concurrent.futures

    from lionagi.state.lifecycle import reconciler as reconciliation

    class Pause:
        def __await__(self):
            yield

    async def resistant_cleanup():
        try:
            await Pause()
        finally:
            await Pause()

    loop = asyncio.new_event_loop()
    hook = resistant_cleanup()
    returned: concurrent.futures.Future = concurrent.futures.Future()
    returned.set_result(hook)
    worker = reconciliation._Worker(
        ("sqlite-test", "close-cleanup", str(uuid.uuid4())), None, loop, returned, 1, None
    )
    with reconciliation._workers_lock:
        reconciliation._workers[worker.key] = worker
    wrapper = None
    if surface == "hook":
        # Suspend in the body; closing now enters a finally that suspends too.
        hook.send(None)
        loop.close()
    else:
        wrapper = resistant_cleanup()
        wrapper.send(None)
        monkeypatch.setattr(reconciliation, "_settle_late_awaitable", lambda *_args: wrapper)

    thread = threading.Thread(target=reconciliation._finish_late, args=(worker,), daemon=True)
    thread.start()
    try:
        if surface == "wrapper":

            async def wrapper_queued():
                return bool(loop._ready)

            await _eventually(wrapper_queued)
            loop.close()
        thread.join(timeout=2)
        assert not thread.is_alive()
        with reconciliation._workers_lock:
            assert reconciliation._workers.get(worker.key) is worker
            assert worker.abandoned
    finally:
        if not loop.is_closed():
            loop.close()
        thread.join(timeout=2)
        reconciliation._release_worker(worker)
        for coroutine in (hook, wrapper):
            if coroutine is not None:
                try:
                    coroutine.close()
                except RuntimeError:
                    coroutine.close()


@pytest.mark.asyncio
async def test_retired_pass_releases_completed_private_ack_from_closed_loop(monkeypatch):
    from lionagi.state.lifecycle import reconciler as reconciliation

    origin = asyncio.new_event_loop()
    origin.close()
    async with StateDB(url="sqlite+aiosqlite:///:memory:") as db:
        transition = await _seed(db)
        consumer = "retired-private-closed"
        await reconciliation.register_consumer(db, consumer, start="all")
        assert await retire_consumer(db, consumer)
        store = reconciliation._store_identity(db)
        worker = reconciliation._reserve_worker(store, db, consumer, transition, 1)
        assert worker is not None
        worker.loop = origin
        worker.future.set_result(DeliveryOutcome.SUCCESS)
        worker.finished = True
        worker.deferred = True

        async def no_cross_loop_ack(*_args, **_kwargs):
            pytest.fail("retired pass tried to ACK on a different loop")

        monkeypatch.setattr(reconciliation, "ack_delivery", no_cross_loop_ack)

        async def prepare(_row, _db):
            pytest.fail("retired pass attempted a new delivery")

        reconciler = TerminalReconciler(consumer, prepare, batch_size=1)
        try:
            assert (await reconciler.run_pass(db))["attempted"] == 0
            with reconciliation._workers_lock:
                assert worker.key not in reconciliation._workers
            assert not await is_acknowledged(db, transition, consumer)
        finally:
            reconciliation._release_worker(worker)
