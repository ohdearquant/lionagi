# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A failed SQLite connection is drained before its caller can close the loop."""

from __future__ import annotations

import asyncio
import threading
import traceback

import aiosqlite
import aiosqlite.core as driver_core
import pytest
from sqlalchemy.exc import OperationalError

from lionagi.state.db import StateDB

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.timeout(40)]


@pytest.mark.skipif(
    not hasattr(aiosqlite.Connection, "stop"),
    reason="the stop future is only present in aiosqlite 0.22.1 and later",
)
@pytest.mark.parametrize("readonly", [False, True], ids=["writable", "readonly"])
async def test_failed_open_waits_for_the_actual_driver_worker(
    tmp_path, monkeypatch, readonly, request
):
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    release_stop = threading.Event()
    connections = []
    stopped_by = []
    original_init = driver_core.Connection.__init__
    original_worker = driver_core._connection_worker_thread

    def record_connection(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        connections.append(self)

    def gated_worker(queue):
        class StopQueue:
            def get(self):
                future, function = queue.get()
                if getattr(function, "__name__", None) != "close_and_stop":
                    return future, function

                def finish_stop():
                    stopped_by.append((threading.current_thread(), future))
                    loop.call_soon_threadsafe(stopping.set)
                    if not release_stop.wait(10):
                        raise AssertionError("the test did not release the worker stop")
                    return function()

                return future, finish_stop

        return original_worker(StopQueue())

    monkeypatch.setattr(driver_core.Connection, "__init__", record_connection)
    monkeypatch.setattr(driver_core, "_connection_worker_thread", gated_worker)

    # A directory cannot be a SQLite database, so the real native connector
    # fails without relying on chmod behavior or replacing the DBAPI connection.
    store = tmp_path / "state.db"
    store.mkdir()
    state = StateDB(store, readonly=readonly)

    async def read_store():
        with pytest.raises(OperationalError) as failed_open:
            async with state:
                await state.fetch_all("SELECT 1")
        error = failed_open.value
        request.node.user_properties.append(
            (
                "failed_open_traceback",
                "".join(traceback.format_exception(type(error), error, error.__traceback__)),
            )
        )
        assert state._engine is None
        return connections[0]._thread.is_alive()

    reading = asyncio.create_task(read_store())
    try:
        await asyncio.wait_for(stopping.wait(), 10)
        # Cross a scheduled loop turn after the real stop has entered its
        # worker. This is an event handshake, not a timing or retry budget.
        turn = loop.create_future()
        loop.call_soon(turn.set_result, None)
        await turn

        assert len(connections) == 1
        worker, stop_future = stopped_by[0]
        assert worker is connections[0]._thread
        assert stop_future is not None
        assert stop_future.get_loop() is loop
        assert worker.is_alive(), "the controlled stop did not hold the actual worker"
        assert not reading.done(), "the failed read returned while its driver worker was stopping"
    finally:
        release_stop.set()
        try:
            alive_when_returned = await asyncio.wait_for(asyncio.shield(reading), 10)
        finally:
            for connection in connections:
                await asyncio.to_thread(connection._thread.join, 10)
                assert not connection._thread.is_alive(), "the test left a driver worker running"

    assert alive_when_returned is False, "the failed read returned before its worker exited"
