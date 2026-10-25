# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A failed SQLite connection is drained before its caller can close the loop."""

from __future__ import annotations

import asyncio
import functools
import logging
import sqlite3
import threading
import traceback

import aiosqlite
import aiosqlite.core as driver_core
import pytest
from sqlalchemy.exc import OperationalError

from lionagi.state import engine
from lionagi.state.db import StateDB

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.timeout(40)]

# aiosqlite 0.22.0 has no queued stop: its Connection._connect awaits
# _stop_running() before re-raising, so the worker has already exited when a
# failed open reaches the engine and the bounded join returns at once (checked on
# a 0.22.0 install, opening a directory as the database). The tests below hold
# the stop that 0.22.1 and later queue instead, so they skip on 0.22.0.
needs_stop = pytest.mark.skipif(
    not hasattr(aiosqlite.Connection, "stop"),
    reason="the stop future is only present in aiosqlite 0.22.1 and later",
)


def _hold_the_driver_stop(monkeypatch, loop, stopping, release_stop):
    """Record each driver connection and hold its worker inside the queued stop."""
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
    return connections, stopped_by


async def test_the_driver_still_has_the_private_worker_thread_the_wrapper_reads():
    # engine._SQLiteConnectionAwaitable reads Connection._thread, and pyproject
    # bounds aiosqlite to the releases that have it. A release without it fails here.
    connection = aiosqlite.Connection(functools.partial(sqlite3.connect, ":memory:"), 64)
    assert isinstance(getattr(connection, "_thread", None), threading.Thread), (
        "aiosqlite.Connection no longer has the private _thread the failed-open"
        " drain in lionagi/state/engine.py reads; re-check before widening the pin"
    )
    assert connection._thread.ident is None


async def test_each_engine_wraps_the_real_driver_module_once():
    first, second = {}, {}
    engine._configure_sqlite_module("sqlite+aiosqlite:///first.db", first)
    engine._configure_sqlite_module("sqlite+aiosqlite:///second.db", second)
    assert first["module"] is not second["module"]
    for kwargs in (first, second):
        wrapper = kwargs["module"].aiosqlite
        assert isinstance(wrapper, engine._OwnedSQLiteDriver)
        assert wrapper._driver is aiosqlite
    assert aiosqlite.connect is driver_core.connect


@needs_stop
@pytest.mark.parametrize("readonly", [False, True], ids=["writable", "readonly"])
async def test_failed_open_waits_for_the_actual_driver_worker(
    tmp_path, monkeypatch, readonly, request
):
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    release_stop = threading.Event()
    connections, stopped_by = _hold_the_driver_stop(monkeypatch, loop, stopping, release_stop)

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


@needs_stop
@pytest.mark.parametrize("cancel", [False, True], ids=["returns", "cancelled"])
async def test_failed_open_stops_waiting_for_a_worker_that_does_not_exit(
    tmp_path, monkeypatch, caplog, cancel
):
    monkeypatch.setattr(engine, "_FAILED_OPEN_WORKER_JOIN_SECONDS", 0.2)
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    release_stop = threading.Event()
    connections, _ = _hold_the_driver_stop(monkeypatch, loop, stopping, release_stop)
    store = tmp_path / "state.db"
    store.mkdir()
    state = StateDB(store)

    async def read_store():
        async with state:
            await state.fetch_all("SELECT 1")

    caplog.set_level(logging.WARNING, logger=engine.__name__)
    reading = asyncio.create_task(read_store())
    try:
        await asyncio.wait_for(stopping.wait(), 10)
        if cancel:
            reading.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else OperationalError):
            await asyncio.wait_for(reading, 5)
        assert connections[0]._thread.is_alive(), "the held stop did not keep the worker"
        assert any(
            "still running" in record.getMessage()
            for record in caplog.records
            if record.name == engine.__name__
        ), "a failed open past the bound did not say it left the worker running"
    finally:
        release_stop.set()
        for connection in connections:
            await asyncio.to_thread(connection._thread.join, 10)
            assert not connection._thread.is_alive(), "the test left a driver worker running"
