# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A late result has only one acknowledgement and reservation release."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import uuid

from lionagi.state.lifecycle import reconciler as reconciliation
from lionagi.state.lifecycle.reconciler import DeliveryOutcome


def test_inline_late_observer_wins_before_poller_final_claim(monkeypatch):
    origin_loop = asyncio.new_event_loop()
    foreign_loop = asyncio.new_event_loop()
    hook = foreign_loop.create_future()
    returned = concurrent.futures.Future()
    returned.set_result(hook)
    worker = reconciliation._Worker(
        ("sqlite-memory:shared:fake", "race-consumer", str(uuid.uuid4())),
        None,
        origin_loop,
        returned,
        1,
        None,
        detached=True,
    )
    with reconciliation._workers_lock:
        reconciliation._workers[worker.key] = worker

    poll_entered, poll_continue, ack_entered, ack_continue, released = (
        threading.Event() for _ in range(5)
    )
    lock = threading.Lock()
    acknowledgements = []
    releases = []
    ack_waits = []
    original_release = reconciliation._release_worker

    class Pending:
        def result(self, timeout=None):
            poll_entered.set()
            if not poll_continue.wait(5):
                raise RuntimeError("poll was not released")
            return hook.result()

    def submit(wrapper, loop):
        assert loop is foreign_loop
        # The fake pending object supplies the result at the selected boundary.
        wrapper.close()
        return Pending()

    def acknowledge(item):
        with lock:
            acknowledgements.append(item.key)
            first = len(acknowledgements) == 1
        if first:
            ack_entered.set()
            ack_waits.append(ack_continue.wait(5))

    def release(item):
        with lock:
            releases.append(item.key)
        original_release(item)
        released.set()

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", submit)
    monkeypatch.setattr(reconciliation, "_keeper_ack", acknowledge)
    monkeypatch.setattr(reconciliation, "_release_worker", release)
    poller = threading.Thread(target=reconciliation._finish_late, args=(worker,), daemon=True)
    poller.start()
    try:
        assert poll_entered.wait(3)
        with reconciliation._workers_lock:
            assert worker.late_task is hook and not worker.late_claimed

        hook.set_result(DeliveryOutcome.SUCCESS)
        # A pass observes the completed task inline while the poller is about
        # to return its final result. Hold the winning ACK before it releases
        # the reservation, so the final claim must check late_claimed itself.
        reconciliation._late_delivery_task_done(worker, hook)
        assert ack_entered.wait(3)
        with reconciliation._workers_lock:
            assert reconciliation._workers.get(worker.key) is worker
            assert worker.late_claimed

        poll_continue.set()
        poller.join(timeout=3)
        assert not poller.is_alive()
        with lock:
            assert acknowledgements == [worker.key]
            assert releases == []

        ack_continue.set()
        assert released.wait(3)
        with lock:
            assert acknowledgements == [worker.key]
            assert releases == [worker.key]
        assert ack_waits == [True]
        with reconciliation._workers_lock:
            assert worker.key not in reconciliation._workers
    finally:
        poll_continue.set()
        ack_continue.set()
        poller.join(timeout=3)
        if ack_entered.is_set():
            released.wait(3)
        original_release(worker)
        foreign_loop.close()
        origin_loop.close()
