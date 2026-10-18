# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""
Comprehensive tests for lionagi.state.db.StateDB.

All tests use in-memory SQLite (:memory:) for speed and isolation.
tests/conftest.py marks coroutine tests for pytest-asyncio — no @pytest.mark.asyncio needed.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
import uuid
from types import SimpleNamespace
from unittest import mock

import pytest
import pytest_asyncio
from sqlalchemy import text

from lionagi.state.db import SCHEMA_VERSION, StateDB
from lionagi.state.reasons import RunReasons
from tests._scheduler_claims import claim_and_advance

# Fixtures


@pytest_asyncio.fixture
async def db():
    """Fresh in-memory StateDB for each test."""
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


# Helpers


def uid() -> str:
    return str(uuid.uuid4())


def make_message(*, role: str = "user", lion_class: str = "") -> dict:
    node_meta = {"lion_class": lion_class} if lion_class else {}
    return {
        "id": uid(),
        "created_at": time.time(),
        "node_metadata": node_meta,
        "content": {"text": "hello"},
        "role": role,
        "sender": "test-sender",
        "recipient": "test-recipient",
        "channel": "test-channel",
        "embedding": None,
    }


async def _make_session(db: StateDB, *, status: str | None = None) -> dict:
    """Create a progression + session, return the session dict."""
    prog_id = uid()
    await db.create_progression(prog_id)
    session = {
        "id": uid(),
        "progression_id": prog_id,
        "status": status,
    }
    await db.create_session(session)
    return session


async def _make_show(db: StateDB, *, status: str = "active") -> dict:
    show = {
        "id": uid(),
        "topic": f"topic-{uid()}",
        "show_dir": f"/tmp/show-{uid()}",
        "status": status,
    }
    await db.create_show(show)
    return show


async def _make_live_persist_fixture(db: StateDB) -> tuple[str, str, str, str]:
    session_id = "live-persist-session"
    session_prog_id = "live-persist-session-progression"
    branch_id = "live-persist-branch"
    branch_prog_id = "live-persist-branch-progression"
    await db.create_progression(session_prog_id)
    await db.create_progression(branch_prog_id)
    await db.create_session(
        {
            "id": session_id,
            "created_at": 100.0,
            "progression_id": session_prog_id,
            "status": "running",
            "started_at": 100.0,
            "last_message_at": 100.0,
            "updated_at": 100.0,
        }
    )
    await db.create_branch(
        {
            "id": branch_id,
            "created_at": 100.0,
            "session_id": session_id,
            "progression_id": branch_prog_id,
        }
    )
    return session_id, session_prog_id, branch_id, branch_prog_id


# Connection lifecycle


async def test_open_close():
    """Open connects and applies pragmas; close nulls the internal connection.

    Note: in-memory SQLite ignores WAL mode (always returns 'memory') — WAL is
    a file-system-level feature.  We verify pragmas were applied by issuing a
    read-back of foreign_keys (which works in-memory) and that the schema is
    accessible.
    """
    from sqlalchemy import text

    state = StateDB(":memory:")
    await state.open()

    # foreign_keys pragma is set to ON in _apply_pragmas — verify round-trip
    async with state._read() as conn:
        row = (await conn.execute(text("PRAGMA foreign_keys"))).first()
    assert row[0] == 1  # 1 = ON

    # Schema is available after open
    version = await state.schema_version()
    assert version == SCHEMA_VERSION

    await state.close()
    assert state._engine is None


async def test_context_manager():
    """async with opens and closes cleanly."""
    async with StateDB(":memory:") as state:
        version = await state.schema_version()
        assert version == SCHEMA_VERSION
    assert state._engine is None


async def test_managed_entity_creations_write_initial_lifecycle_history(db: StateDB):
    progression_id = uid()
    await db.create_progression(progression_id)
    session_id = uid()
    await db.create_session(
        {"id": session_id, "progression_id": progression_id, "status": "running"}
    )

    invocation_id = uid()
    await db.create_invocation({"id": invocation_id, "skill": "test", "started_at": time.time()})

    show_id = uid()
    await db.create_show({"id": show_id, "topic": "creation-history", "show_dir": "shows/history"})
    play_id = uid()
    await db.create_play({"id": play_id, "show_id": show_id, "name": "first"})

    schedule_id = uid()
    await db.create_schedule(
        {
            "id": schedule_id,
            "name": "creation-history",
            "trigger_type": "interval",
            "interval_sec": 60,
            "action_kind": "agent",
        }
    )
    schedule_run_id = uid()
    await db.create_schedule_run(
        {
            "id": schedule_run_id,
            "schedule_id": schedule_id,
            "trigger_context": {},
            "action_kind": "agent",
            "action_args": [],
            "status": "running",
            "fired_at": time.time(),
        }
    )
    advanced_run_id = uid()
    advanced_run = {
        "id": advanced_run_id,
        "schedule_id": schedule_id,
        "trigger_context": {},
        "action_kind": "agent",
        "action_args": [],
        "status": "running",
        "fired_at": time.time(),
    }
    await claim_and_advance(
        db,
        advanced_run,
        schedule_id=schedule_id,
        schedule_fields={"last_fired_at": time.time()},
    )
    replacement_run_id = uid()
    replacement_run = {**advanced_run, "id": replacement_run_id}
    assert await db.tombstone_and_replace_schedule_run(
        schedule_run_id,
        replacement_run,
    )

    # Idempotent creation retries do not append another creation event.
    await db.create_session(
        {"id": session_id, "progression_id": progression_id, "status": "running"}
    )

    expected = {
        session_id: ("session", "running"),
        invocation_id: ("invocation", "running"),
        show_id: ("show", "active"),
        play_id: ("play", "pending"),
        schedule_run_id: ("schedule_run", "running"),
        advanced_run_id: ("schedule_run", "running"),
        replacement_run_id: ("schedule_run", "running"),
    }
    placeholders = ", ".join(f":id{i}" for i in range(len(expected)))
    params = {f"id{i}": entity_id for i, entity_id in enumerate(expected)}
    async with db._read() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT entity_type, entity_id, previous_status, status "
                        f"FROM status_transitions WHERE entity_id IN ({placeholders})"
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )

    initial_rows = [row for row in rows if row["previous_status"] is None]
    assert len(initial_rows) == len(expected)
    for row in initial_rows:
        assert row["previous_status"] is None
        assert (row["entity_type"], row["status"]) == expected[row["entity_id"]]

    tombstones = [
        row
        for row in rows
        if row["entity_id"] == schedule_run_id and row["previous_status"] == "running"
    ]
    assert len(rows) == len(expected) + 1
    assert [(row["status"], row["entity_type"]) for row in tombstones] == [
        ("failed", "schedule_run")
    ]


async def test_creation_history_failure_rolls_back_managed_entity(
    db: StateDB, monkeypatch: pytest.MonkeyPatch
):
    from lionagi.state.lifecycle.service import SQLAlchemyLifecycleService

    async def _fail_initial_history(self, connection, command):
        raise RuntimeError("forced history failure")

    monkeypatch.setattr(
        SQLAlchemyLifecycleService,
        "initialize_in_transaction",
        _fail_initial_history,
    )
    invocation_id = uid()
    with pytest.raises(RuntimeError, match="forced history failure"):
        await db.create_invocation(
            {"id": invocation_id, "skill": "test", "started_at": time.time()}
        )

    assert await db.get_invocation(invocation_id) is None


async def test_engine_is_none_when_closed():
    """_engine is None before open() is called."""
    state = StateDB(":memory:")
    assert state._engine is None


async def test_context_open_failure_disposes_partial_engine(monkeypatch):
    """A context-entry error must not leave the driver's worker alive."""
    from unittest.mock import AsyncMock, MagicMock

    import lionagi.state.db as db_mod

    engine = MagicMock()
    engine.dispose = AsyncMock()
    monkeypatch.setattr(db_mod, "make_engine", lambda *_args, **_kwargs: engine)
    monkeypatch.setattr(db_mod, "_install_begin_immediate", lambda *_args: None)

    state = StateDB(":memory:")

    async def fail_schema() -> None:
        raise RuntimeError("schema is locked")

    monkeypatch.setattr(state, "_apply_schema", fail_schema)

    with pytest.raises(RuntimeError, match="schema is locked"):
        async with state:
            raise AssertionError("context body must not run")

    engine.dispose.assert_awaited_once()
    assert state._engine is None


async def test_context_cancelled_open_shields_partial_engine_disposal(monkeypatch):
    """Cancellation during context entry must not interrupt engine disposal."""
    import anyio

    import lionagi.state.db as db_mod

    dispose_started = anyio.Event()
    dispose_finished = anyio.Event()

    class FakeEngine:
        sync_engine = object()

        async def dispose(self) -> None:
            dispose_started.set()
            await anyio.lowlevel.checkpoint()
            dispose_finished.set()

    engine = FakeEngine()
    monkeypatch.setattr(db_mod, "make_engine", lambda *_args, **_kwargs: engine)
    monkeypatch.setattr(db_mod, "_install_begin_immediate", lambda *_args: None)

    state = StateDB(":memory:")

    async def hang_schema() -> None:
        await anyio.sleep_forever()

    monkeypatch.setattr(state, "_apply_schema", hang_schema)

    with pytest.raises(TimeoutError):
        with anyio.fail_after(0.01):
            async with state:
                raise AssertionError("context body must not run")

    assert dispose_started.is_set()
    assert dispose_finished.is_set()
    assert state._engine is None


async def test_a_cancelled_context_entry_leaves_the_open_it_waited_on_usable(tmp_path, monkeypatch):
    """A second ``async with`` on one handle waits behind the first open. Its cancellation must
    undo only what its own call opened: the holder's open ends with a usable engine, not one the
    cancelled entry's cleanup disposed after waiting for the holder to finish."""
    original = StateDB._apply_schema
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow(self):
        entered.set()
        await release.wait()
        await original(self)

    store = tmp_path / "state.db"
    seed = StateDB(store)
    await seed.open()
    await seed.close()
    monkeypatch.setattr(StateDB, "_apply_schema", slow)

    handle = StateDB(store)
    holder = asyncio.ensure_future(handle.open())
    await asyncio.wait_for(entered.wait(), timeout=10)

    async def enter():
        async with handle:
            raise AssertionError("the cancelled entry's body must not run")

    waiter = asyncio.ensure_future(enter())
    await asyncio.sleep(0.1)
    assert handle._open_lock_users == 2, "the second entry is waiting on the first open"
    waiter.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(waiter, timeout=10)
    await asyncio.wait_for(holder, timeout=10)

    assert handle._engine is not None, "the cancelled entry disposed the open it was waiting on"
    assert await handle.fetch_all("SELECT 1")
    await handle.close()


async def test_a_cancelled_context_entry_leaves_a_queued_open_its_engine(tmp_path, monkeypatch):
    """A second open queued behind a context entry that is then cancelled keeps the engine it
    builds: the entry's failure cleanup disposes only what its own entry opened."""
    store = tmp_path / "state.db"
    seed = StateDB(store)
    await seed.open()
    await seed.close()
    original = StateDB._apply_schema
    entered = asyncio.Event()
    calls: list[int] = []

    async def schema(self):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            await asyncio.sleep(60)
        await original(self)

    monkeypatch.setattr(StateDB, "_apply_schema", schema)
    handle = StateDB(store)
    entry = asyncio.ensure_future(handle.__aenter__())
    await asyncio.wait_for(entered.wait(), timeout=10)
    queued = asyncio.ensure_future(handle.open())
    await asyncio.sleep(0.2)
    assert handle._open_lock_users == 2, "the second open is waiting on the entry's open"

    entry.cancel()
    with pytest.raises(asyncio.CancelledError):
        await entry
    await asyncio.wait_for(queued, timeout=10)

    assert handle._engine is not None, (
        "the cancelled entry disposed the engine the open queued behind it then built"
    )
    assert await handle.fetch_all("SELECT 1")
    await handle.close()
    assert handle._engine is None


async def test_a_failed_context_entry_leaves_a_queued_open_its_engine(tmp_path, monkeypatch):
    """The same when the entry's schema raises instead of being cancelled."""
    store = tmp_path / "state.db"
    seed = StateDB(store)
    await seed.open()
    await seed.close()
    original = StateDB._apply_schema
    calls: list[int] = []

    async def schema(self):
        calls.append(1)
        if len(calls) == 1:
            await asyncio.sleep(0.3)
            raise RuntimeError("the entry's schema failed")
        await original(self)

    monkeypatch.setattr(StateDB, "_apply_schema", schema)
    handle = StateDB(store)
    entry = asyncio.ensure_future(handle.__aenter__())
    await asyncio.sleep(0.05)
    queued = asyncio.ensure_future(handle.open())
    with pytest.raises(RuntimeError, match="the entry's schema failed"):
        await entry
    await asyncio.wait_for(queued, timeout=10)

    assert handle._engine is not None, (
        "the failed entry disposed the engine the open queued behind it then built"
    )
    assert await handle.fetch_all("SELECT 1")
    await handle.close()
    assert handle._engine is None


def _close_on_another_loop(handle: StateDB) -> object:
    """Run ``handle.close()`` on a fresh event loop in a worker thread; return what it did."""
    outcome: dict = {}

    def body():
        try:
            asyncio.run(asyncio.wait_for(handle.close(), timeout=10))
            outcome["result"] = "returned"
        except BaseException as exc:  # noqa: BLE001 -- the test reads what the call raised
            outcome["result"] = exc

    worker = threading.Thread(target=body, daemon=True)
    worker.start()
    worker.join(30)
    assert not worker.is_alive()
    return outcome["result"]


def _name_the_manager(store) -> None:
    """Write the ``lineage`` row a store's managing application writes (ADR-0128 D7)."""
    conn = sqlite3.connect(store)
    try:
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('lineage', 'manager') "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value"
        )
        conn.commit()
    finally:
        conn.close()


async def test_a_close_from_another_loop_after_a_refused_open_closes_the_handle(tmp_path):
    """The open lock's user count is lowered on every way out of an open, so an open that refused
    leaves nothing behind and a close from another loop is not refused for a call that is over."""
    from lionagi.state.db import ForeignLineageError

    store = tmp_path / "managed.db"
    async with StateDB(store):
        pass
    _name_the_manager(store)

    handle = StateDB(store)
    with pytest.raises(ForeignLineageError, match="'manager' as the application"):
        await handle.open()

    assert handle._open_lock_users == 0
    assert _close_on_another_loop(handle) == "returned"
    assert handle._engine is None


async def test_a_close_from_another_loop_after_a_cancelled_open_closes_the_handle(
    tmp_path, monkeypatch
):
    """A cancelled open leaves the lock its call held, so the count returns to zero and a close
    from another loop is not refused for a holder that no longer exists."""
    store = tmp_path / "state.db"
    seed = StateDB(store)
    await seed.open()
    await seed.close()

    async def stall(self):
        await asyncio.sleep(60)

    monkeypatch.setattr(StateDB, "_apply_schema", stall)
    handle = StateDB(store)
    opener = asyncio.ensure_future(handle.open())
    await asyncio.sleep(0.1)
    opener.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opener

    assert handle._open_lock_users == 0
    assert _close_on_another_loop(handle) == "returned"


async def test_a_sweep_after_a_refused_close_still_takes_the_lock_a_lookup_meets(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
):
    """A refused close leaves its handle registered and resets the shared lock. The next sweep
    takes that lock all the same, so a lookup made during it meets the lock and the teardown
    refusal instead of registering a second handle that the sweep then returns over."""
    import lionagi.state.db as db_mod
    from lionagi.state.db import close_shared_db, get_shared_db, register_shared_db

    monkeypatch.setattr(db_mod, "_SHARED", {})
    monkeypatch.setattr(db_mod, "_SHARED_OPEN_LOCK", None)
    monkeypatch.setattr(db_mod, "_SHARED_LOCK_USERS", 0)
    monkeypatch.setattr(db_mod, "_SHARED_LOCK_LOOP", None)
    store = tmp_path / "state.db"
    handle = StateDB(store)
    await handle.open()
    await register_shared_db(handle)

    async def refused_close():
        raise OSError("dispose failed")

    with mock.patch.object(handle, "close", refused_close):
        with pytest.raises(RuntimeError, match="could not be closed"):
            await close_shared_db()
    assert db_mod._SHARED.get(handle.url) is handle, "the refused handle stays registered"

    async def slow_close():
        await asyncio.sleep(0.3)
        await StateDB.close(handle)

    racing = None
    try:
        with mock.patch.object(handle, "close", slow_close):
            sweep = asyncio.ensure_future(close_shared_db())
            await asyncio.sleep(0.1)
            with pytest.raises(RuntimeError, match="torn down"):
                racing = await get_shared_db(store)
            await sweep
        assert not [db for db in db_mod._SHARED.values() if db._engine is not None], (
            "the sweep returned with an open handle still in the registry"
        )
    finally:
        if racing is not None and racing._engine is not None:
            await racing.close()
        if handle._engine is not None:
            await handle.close()


async def test_a_cancelled_context_entry_that_read_the_store_disposes_its_own_engine(
    tmp_path, monkeypatch
):
    """An entry whose connection read the store keeps its engine when it is cancelled, since only
    an open whose connection never read the store is undone; the entry's own cleanup disposes it
    under the open lock, so no engine outlives the entry."""
    store = tmp_path / "state.db"
    seed = StateDB(store)
    await seed.open()
    await seed.close()
    entered = asyncio.Event()

    async def schema(self):
        await self.fetch_all("SELECT 1")
        entered.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(StateDB, "_apply_schema", schema)
    handle = StateDB(store)
    entry = asyncio.ensure_future(handle.__aenter__())
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        assert handle._connection_read, "the entry's connection must have read the store"
        entry.cancel()
        with pytest.raises(asyncio.CancelledError):
            await entry
        assert handle._engine is None, "the cancelled entry left its engine open"
    finally:
        if handle._engine is not None:
            await handle.close()


async def test_a_failed_context_entry_that_read_the_store_disposes_its_own_engine(
    tmp_path, monkeypatch
):
    """The same when the entry's schema raises after its connection read the store."""
    store = tmp_path / "state.db"
    seed = StateDB(store)
    await seed.open()
    await seed.close()

    async def schema(self):
        await self.fetch_all("SELECT 1")
        raise RuntimeError("the entry's schema failed")

    monkeypatch.setattr(StateDB, "_apply_schema", schema)
    handle = StateDB(store)

    async def enter():
        async with handle:
            raise AssertionError("the failed entry's body must not run")

    try:
        with pytest.raises(RuntimeError, match="the entry's schema failed"):
            await enter()
        assert handle._engine is None, "the failed entry left its engine open"
    finally:
        if handle._engine is not None:
            await handle.close()


async def test_a_queued_open_behind_an_entry_that_failed_after_its_read_builds_its_own_engine(
    tmp_path, monkeypatch
):
    """The failed entry's cleanup disposes only what its own call opened, so the open queued
    behind it builds an engine of its own and runs the schema on that one."""
    import lionagi.state.db as db_mod

    store = tmp_path / "state.db"
    seed = StateDB(store)
    await seed.open()
    await seed.close()
    original = StateDB._apply_schema
    entered = asyncio.Event()
    calls: list[int] = []

    async def schema(self):
        calls.append(1)
        if len(calls) == 1:
            await self.fetch_all("SELECT 1")
            entered.set()
            await asyncio.sleep(0.3)
            raise RuntimeError("the entry's schema failed")
        await original(self)

    monkeypatch.setattr(StateDB, "_apply_schema", schema)
    built: list = []
    make_engine = db_mod.make_engine

    def counting(url, **kwargs):
        engine = make_engine(url, **kwargs)
        built.append(engine)
        return engine

    monkeypatch.setattr(db_mod, "make_engine", counting)
    handle = StateDB(store)
    entry = asyncio.ensure_future(handle.__aenter__())
    await asyncio.wait_for(entered.wait(), timeout=10)
    queued = asyncio.ensure_future(handle.open())
    with pytest.raises(RuntimeError, match="the entry's schema failed"):
        await entry
    await asyncio.wait_for(queued, timeout=10)
    try:
        assert len(built) == 2, "the queued open found the failed entry's engine still open"
        assert handle._engine is built[-1]
        assert await handle.fetch_all("SELECT 1")
    finally:
        await handle.close()
    assert handle._engine is None


async def test_a_queued_entry_after_a_plain_open_that_failed_completes_the_store_schema(
    tmp_path, monkeypatch
):
    """A plain open that fails after its connection read the store keeps its engine, and its
    schema never ran. A later ``async with`` on the handle must not return over that store: it
    completes the open, the schema included."""
    store = tmp_path / "fresh.db"
    original = StateDB._apply_schema
    calls: list[int] = []

    async def schema(self):
        calls.append(1)
        if len(calls) == 1:
            await self.fetch_all("SELECT 1")
            await asyncio.sleep(0.3)
            raise RuntimeError("the first open's schema failed")
        await original(self)

    monkeypatch.setattr(StateDB, "_apply_schema", schema)
    handle = StateDB(store)
    opener = asyncio.ensure_future(handle.open())
    await asyncio.sleep(0.1)
    with pytest.raises(RuntimeError, match="the first open's schema failed"):
        await opener
    assert handle._engine is not None, "a plain open keeps the engine it built"

    try:
        async with handle:
            rows = await handle.fetch_all(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'sessions'"
            )
            assert rows, "the entry returned over a store whose schema never ran"
        assert len(calls) == 2
    finally:
        if handle._engine is not None:
            await handle.close()


def _counting_dispose(monkeypatch) -> dict:
    """Patch ``AsyncEngine.dispose`` to count calls, stalling only the first in progress."""
    from sqlalchemy.ext.asyncio import AsyncEngine

    real = AsyncEngine.dispose
    state: dict = {"calls": 0, "stalled": asyncio.Event()}

    async def dispose(self, *args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            state["stalled"].set()
            await asyncio.sleep(10)
        await real(self, *args, **kwargs)

    monkeypatch.setattr(AsyncEngine, "dispose", dispose)
    return state


async def test_a_second_cancel_in_the_shielded_disposal_leaves_the_next_open_a_tracked_engine(
    tmp_path, monkeypatch
):
    """A second native cancel delivered while ``open()``'s shielded disposal is in progress escapes
    it, so the engine stays set and the handle is untracked. The interrupted close is recorded, so
    the next ``open()`` disposes that engine (once more) and opens a tracked native handle."""
    import lionagi.state.db as db_mod

    store = tmp_path / "state.db"
    seed = StateDB(store)
    await seed.open()
    await seed.close()

    dispose = _counting_dispose(monkeypatch)
    original_schema = StateDB._apply_schema
    entered = asyncio.Event()
    first_schema = True

    async def hang_once(self):
        nonlocal first_schema
        if first_schema:
            first_schema = False
            await self.fetch_all("SELECT 1")
            entered.set()
            await asyncio.sleep(60)
        await original_schema(self)

    monkeypatch.setattr(StateDB, "_apply_schema", hang_once)

    handle = StateDB(store)
    entry = asyncio.ensure_future(handle.__aenter__())
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        assert handle in db_mod._OPEN_NATIVE
        entry.cancel()
        await asyncio.wait_for(dispose["stalled"].wait(), timeout=10)
        entry.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(entry, timeout=10)

        assert handle._engine is not None
        assert dispose["calls"] == 1, "the disposal was not the one the cancel interrupted"

        await handle.open()
        assert dispose["calls"] == 2, (
            "the next open did not dispose the engine the interruption left set"
        )
        assert handle in db_mod._OPEN_NATIVE, "the native handle the next open built is untracked"
        assert await handle.fetch_all("SELECT 1")
    finally:
        if handle._engine is not None:
            await handle.close()


async def test_a_cancelled_close_leaves_the_next_open_a_tracked_engine(tmp_path, monkeypatch):
    """A ``close()`` cancelled while its disposal is in progress leaves the engine set and the
    handle untracked. The interrupted close is recorded, so the next ``open()`` disposes that engine
    (once more) and opens a tracked native handle."""
    import lionagi.state.db as db_mod

    store = tmp_path / "state.db"
    seed = StateDB(store)
    await seed.open()
    await seed.close()

    dispose = _counting_dispose(monkeypatch)
    handle = StateDB(store)
    try:
        await handle.open()
        assert handle in db_mod._OPEN_NATIVE

        closing = asyncio.ensure_future(handle.close())
        await asyncio.wait_for(dispose["stalled"].wait(), timeout=10)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing

        assert handle._engine is not None, "the cancel reached a disposal the close shielded"
        assert dispose["calls"] == 1

        await handle.open()
        assert dispose["calls"] == 2, (
            "the next open did not dispose the engine the cancelled close left set"
        )
        assert handle in db_mod._OPEN_NATIVE, "the open native handle is missing from _OPEN_NATIVE"
        assert await handle.fetch_all("SELECT 1")
    finally:
        if handle._engine is not None:
            await handle.close()


async def test_a_lookup_queued_behind_a_second_sweep_waits_for_it(tmp_path, monkeypatch):
    """A second sweep queued behind the first keeps the registry lock the first held. A lookup
    issued while that sweep awaits a close waits on the same lock rather than opening a handle the
    sweep then returns over, and the lock is released only once no sweep is left waiting on it."""
    import lionagi.state.db as db_mod
    from lionagi.state.db import close_shared_db, get_shared_db, register_shared_db

    monkeypatch.setattr(db_mod, "_SHARED", {})
    monkeypatch.setattr(db_mod, "_SHARED_OPEN_LOCK", None)
    monkeypatch.setattr(db_mod, "_SHARED_LOCK_USERS", 0)
    monkeypatch.setattr(db_mod, "_SHARED_LOCK_LOOP", None)
    store = tmp_path / "state.db"
    handle = StateDB(store)
    await handle.open()
    await register_shared_db(handle)

    first_call = asyncio.Event()
    sweeping = asyncio.Event()
    release = asyncio.Event()
    calls = {"n": 0}

    async def staged_close():
        calls["n"] += 1
        if calls["n"] == 1:
            # Held, so the second sweep queues on the lock this first one holds.
            first_call.set()
            await asyncio.sleep(0.3)
            raise OSError("the first sweep refused")
        sweeping.set()
        await release.wait()
        await StateDB.close(handle)

    outcome: dict = {}

    async def lookup():
        try:
            outcome["handle"] = await get_shared_db(store)
        except RuntimeError as exc:
            outcome["error"] = exc

    racing = None
    try:
        with mock.patch.object(handle, "close", staged_close):
            sweep1 = asyncio.ensure_future(close_shared_db())
            await asyncio.wait_for(first_call.wait(), timeout=10)
            # Queued on the lock the first sweep still holds.
            sweep2 = asyncio.ensure_future(close_shared_db())
            with pytest.raises(RuntimeError, match="could not be closed"):
                await sweep1

            # The second sweep now holds the lock, has emptied the registry and awaits a close.
            await asyncio.wait_for(sweeping.wait(), timeout=10)
            assert not db_mod._SHARED, "the second sweep's lookup would meet a registered handle"
            racing = asyncio.ensure_future(lookup())
            for _ in range(60):
                if racing.done():
                    break
                await asyncio.sleep(0.05)
            assert not racing.done(), (
                "a lookup ran beside the queued sweep instead of waiting on its lock"
            )
            release.set()
            await asyncio.wait_for(sweep2, timeout=10)
            await asyncio.wait_for(racing, timeout=10)
    finally:
        release.set()
        if racing is not None and not racing.done():
            racing.cancel()
        for db in list(db_mod._SHARED.values()):
            if db._engine is not None:
                await db.close()
        if handle._engine is not None:
            await handle.close()

    assert "handle" not in outcome, "a lookup beside the queued sweep opened its own handle"
    assert isinstance(outcome.get("error"), RuntimeError), outcome
    assert "torn down" in str(outcome["error"])
    assert not [db for db in db_mod._SHARED.values() if db._engine is not None], (
        "a sweep returned over a handle a lookup opened while it awaited a close"
    )


# Schema


async def test_schema_creates_all_tables(db: StateDB):
    """All 8 tables are present after open()."""
    from sqlalchemy import text

    expected = {
        "schema_meta",
        "message_types",
        "messages",
        "progressions",
        "sessions",
        "branches",
        "shows",
        "plays",
        "definitions",
    }
    async with db._read() as conn:
        rows = (
            await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        ).fetchall()
    names = {r[0] for r in rows}
    assert expected <= names, f"Missing tables: {expected - names}"


async def test_schema_version(db: StateDB):
    """schema_version() returns the version this code applies."""
    assert await db.schema_version() == SCHEMA_VERSION


def _stamp_version(path, value: str) -> None:
    """Write schema_meta.version directly, the way another release would have."""
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('version', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (value,),
        )
        conn.commit()
    finally:
        conn.close()


def _read_version(path) -> str | None:
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
    finally:
        conn.close()
    return row[0] if row else None


async def test_open_upgrades_an_older_recorded_schema_version(tmp_path):
    """A database stamped below SCHEMA_VERSION is migrated and re-stamped.

    The stamp records the shape the database has after the migrations that
    ``_apply_schema`` runs, so an older recording must move up to it.
    """
    path = tmp_path / "older.db"
    async with StateDB(path=path):
        pass
    _stamp_version(path, "1")
    assert _read_version(path) == "1"

    async with StateDB(path=path) as state:
        assert await state.schema_version() == SCHEMA_VERSION
    assert _read_version(path) == SCHEMA_VERSION


async def test_column_inspection_failure_does_not_advance_schema_version(
    tmp_path,
    monkeypatch,
    caplog,
):
    import logging

    import lionagi.state.db as db_mod

    path = tmp_path / "inspection_failure.db"
    async with StateDB(path=path):
        pass
    _stamp_version(path, "1")

    real_inspect = db_mod.inspect
    failure = RuntimeError("column inspection failed")
    failed = False

    class FailingInspector:
        def __init__(self, inspector):
            self._inspector = inspector

        def has_table(self, name):
            return self._inspector.has_table(name)

        def get_columns(self, name):
            nonlocal failed
            if name == "sessions" and not failed:
                failed = True
                raise failure
            return self._inspector.get_columns(name)

    def inspect_once(bind):
        inspector = real_inspect(bind)
        return inspector if failed else FailingInspector(inspector)

    monkeypatch.setattr(db_mod, "inspect", inspect_once)
    state = StateDB(path=path)
    state._MIGRATION_COLUMNS = {"sessions": [("inspection_probe", "TEXT")]}

    with caplog.at_level(logging.ERROR, logger="lionagi.state.db"):
        with pytest.raises(RuntimeError, match="column inspection failed") as excinfo:
            async with state:
                pass

    assert excinfo.value is failure
    assert failed is True
    assert _read_version(path) == "1"
    assert state._engine is None
    records = [item for item in caplog.records if item.name == "lionagi.state.db"]
    assert len(records) == 1
    record = records[0]
    assert record.getMessage() == (
        "failed to inspect migration columns for table 'sessions': "
        "RuntimeError('column inspection failed')"
    )
    assert record.exc_info is not None

    await state.open()
    try:
        assert await state.schema_version() == SCHEMA_VERSION
    finally:
        await state.close()
    assert _read_version(path) == SCHEMA_VERSION


async def test_open_refuses_a_newer_recorded_schema_version(tmp_path):
    """A database stamped above SCHEMA_VERSION is refused, not downgraded.

    A later release wrote that stamp, and this code cannot establish that its
    schema has the shape the migrations in ``_apply_schema`` assume. Opening
    for writing would run those migrations anyway and then record the lower
    version, leaving a database that reads as one this code understands.
    """
    from lionagi.state.db import SchemaTooNewError

    path = tmp_path / "newer.db"
    async with StateDB(path=path):
        pass
    newer = str(int(SCHEMA_VERSION) + 1)
    _stamp_version(path, newer)

    with pytest.raises(SchemaTooNewError) as excinfo:
        async with StateDB(path=path):
            pass

    # The refusal names both versions, so the caller can act on it.
    assert newer in str(excinfo.value)
    assert SCHEMA_VERSION in str(excinfo.value)
    # And the stamp it refused to write is still the one on disk.
    assert _read_version(path) == newer


async def test_schema_version_is_rechecked_after_migration_lock(tmp_path, monkeypatch):
    """A concurrent upgrade before the migration lock is refused before ALTER TABLE."""
    from lionagi.state.db import SchemaTooNewError

    path = tmp_path / "raced_newer.db"
    async with StateDB(path=path):
        pass

    state = StateDB(path=path)
    state._MIGRATION_COLUMNS = {"sessions": [("raced_column", "TEXT")]}
    reconcile = state._reconcile_columns
    newer = str(int(SCHEMA_VERSION) + 1)

    async def upgrade_then_reconcile():
        _stamp_version(path, newer)
        await reconcile()

    monkeypatch.setattr(state, "_reconcile_columns", upgrade_then_reconcile)

    with pytest.raises(SchemaTooNewError):
        async with state:
            pass

    assert _read_version(path) == newer
    conn = sqlite3.connect(path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    finally:
        conn.close()
    assert "raced_column" not in columns


async def test_postgres_schema_lock_rechecks_when_version_table_appears(monkeypatch):
    """A newer migrator may create and stamp schema_meta while this opener waits."""
    import lionagi.state.db as db_mod
    from lionagi.state.db import SchemaTooNewError

    state = StateDB(url="postgresql+asyncpg://user:pw@localhost/state")
    newer = str(int(SCHEMA_VERSION) + 1)
    race = {"table_exists": False}
    events: list[str] = []

    class FakeInspector:
        def has_table(self, name):
            assert name == "schema_meta"
            events.append("inspect")
            return race["table_exists"]

    class FakeResult:
        def __init__(self, rows=()):
            self.rows = list(rows)

        def all(self):
            return self.rows

    class FakeConnection:
        async def execute(self, statement, params=None):
            sql = str(statement)
            if "pg_advisory_xact_lock" in sql:
                events.append("lock")
                # The competing newer release commits before this transaction
                # obtains the lock, so the table absent at entry now exists.
                race["table_exists"] = True
                return FakeResult()
            if "FROM schema_meta" in sql and sql.endswith(" FOR UPDATE"):
                events.append("version")
                return FakeResult([("version", newer)])
            raise AssertionError(f"unexpected SQL: {sql}")

        async def run_sync(self, callback):
            return callback(object())

    monkeypatch.setattr(db_mod, "inspect", lambda _conn: FakeInspector())

    with pytest.raises(SchemaTooNewError):
        await state._refuse_newer_schema(FakeConnection())

    assert events == ["lock", "inspect", "version"]


async def test_readonly_open_reads_a_newer_recorded_schema_version(tmp_path):
    """Read-only opens apply no schema, so a newer database stays readable.

    This is the way out of the refusal above: inspecting a database written by
    a later release never rewrites it, so there is nothing to refuse.
    """
    path = tmp_path / "newer_readonly.db"
    async with StateDB(path=path):
        pass
    newer = str(int(SCHEMA_VERSION) + 1)
    _stamp_version(path, newer)

    async with StateDB(path=path, readonly=True) as state:
        assert await state.schema_version() == newer


async def test_open_stamps_over_an_unparsable_recorded_version(tmp_path):
    """A version this code cannot order against its own is replaced.

    Nothing can be said about whether such a value is newer, so there is no
    downgrade to prevent; the open records the shape it applied.
    """
    path = tmp_path / "garbage.db"
    async with StateDB(path=path):
        pass
    _stamp_version(path, "not-a-version")

    async with StateDB(path=path) as state:
        assert await state.schema_version() == SCHEMA_VERSION


async def test_apply_schema_adds_missing_columns_on_old_db(tmp_path):
    """Regression: an older state.db missing later-added
    columns must have them ADD COLUMN'd in by ``_reconcile_columns``.

    Without this migration, ``CREATE TABLE IF NOT EXISTS`` is a no-op
    on the existing tables, so ``create_session(status='running')``
    fails with ``OperationalError: table sessions has no column named
    status`` — the broad except in CLI live-persist setup swallows the
    error, returns ``None``, and leaks the aiosqlite worker thread.
    Resulting symptom: the CLI process hangs forever after the agent
    completes.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    path = tmp_path / "old.db"

    # Simulate an older DB: core columns are present, but the
    # provenance/lifecycle columns added later are missing.
    bootstrap = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with bootstrap.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE sessions ("
                "id TEXT PRIMARY KEY, created_at REAL, node_metadata TEXT, "
                "name TEXT, user TEXT, progression_id TEXT, "
                "first_msg_id TEXT, last_msg_id TEXT)"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE branches ("
                "id TEXT PRIMARY KEY, created_at REAL, node_metadata TEXT, "
                "user TEXT, name TEXT, session_id TEXT, progression_id TEXT)"
            )
        )
    await bootstrap.dispose()

    # Opening with the current StateDB must reconcile in the new columns
    # AND the index/trigger statements in schema.sql (which reference
    # those columns) must succeed.
    db = StateDB(str(path))
    await db.open()
    try:
        async with db._read() as conn:
            rows = (await conn.execute(text("PRAGMA table_info(sessions)"))).mappings().all()
        cols = {r["name"] for r in rows}
        for must_have in (
            "status",
            "started_at",
            "ended_at",
            "ended_at_is_approximate",
            "invocation_kind",
            "playbook_name",
            "agent_name",
            "artifacts_path",
            "source_kind",
            "updated_at",
            # ADR-0064: new artifact columns must be reconciled on old DBs.
            "artifact_contract_json",
            "artifact_verification_json",
            # live flow phase column for `li monitor`.
            "current_phase",
        ):
            assert must_have in cols, f"sessions.{must_have} not migrated"
        async with db._read() as conn:
            brows = (await conn.execute(text("PRAGMA table_info(branches)"))).mappings().all()
        bcols = {r["name"] for r in brows}
        assert "system_msg_id" in bcols
        # And the live-persist write path actually works against the
        # migrated DB (the symptom we're guarding against).
        prog_id = uid()
        await db.create_progression(prog_id)
        await db.create_session(
            {
                "id": uid(),
                "progression_id": prog_id,
                "created_at": time.time(),
                "status": "running",
                "started_at": time.time(),
            }
        )
    finally:
        await db.close()


async def test_message_types_seeded(db: StateDB):
    """6 message types pre-seeded (0 = __unknown__, 1-5 = known classes)."""
    from sqlalchemy import text

    async with db._read() as conn:
        row = (
            (await conn.execute(text("SELECT COUNT(*) AS n FROM message_types"))).mappings().first()
        )
    assert row["n"] == 6

    async with db._read() as conn:
        row = (
            (await conn.execute(text("SELECT lion_class FROM message_types WHERE type_id = 0")))
            .mappings()
            .first()
        )
    assert row["lion_class"] == "__unknown__"


# Messages


async def test_insert_and_get_message(db: StateDB):
    """Insert a message and retrieve it; all fields roundtrip."""
    msg = make_message(role="user")
    await db.insert_message(msg)

    retrieved = await db.get_message(msg["id"])
    assert retrieved is not None
    assert retrieved["id"] == msg["id"]
    assert retrieved["role"] == "user"
    assert retrieved["sender"] == "test-sender"
    assert retrieved["recipient"] == "test-recipient"
    assert retrieved["channel"] == "test-channel"
    # content was a dict — db round-trips it back to dict
    assert isinstance(retrieved["content"], dict)
    assert retrieved["content"]["text"] == "hello"


async def test_insert_message_idempotent(db: StateDB):
    """ON CONFLICT DO UPDATE: inserting the same id twice does not error."""
    from sqlalchemy import text

    msg = make_message()
    await db.insert_message(msg)
    # Second insert — same id, should silently be handled
    await db.insert_message(msg)

    async with db._read() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT COUNT(*) AS n FROM messages WHERE id = :id"), {"id": msg["id"]}
                )
            )
            .mappings()
            .first()
        )
    assert row["n"] == 1


async def test_persist_live_message_uses_one_transaction_and_preserves_rows(db: StateDB):
    """The live message bundle matches the prior writes in one SQLite transaction."""
    baseline = StateDB(":memory:")
    await baseline.open()
    try:
        session_id, session_prog_id, branch_id, branch_prog_id = await _make_live_persist_fixture(
            db
        )
        await _make_live_persist_fixture(baseline)
        msg = make_message(role="assistant")
        msg["id"] = "live-persist-message"
        msg["created_at"] = 200.0

        await baseline.insert_message(msg)
        await baseline.append_to_progression(branch_prog_id, msg["id"])
        await baseline.append_to_progression(session_prog_id, msg["id"])
        await baseline.touch_session_activity(session_id, at=msg["created_at"])

        statements: list[str] = []
        async with db._read() as conn:
            raw_conn = await conn.get_raw_connection()
            await raw_conn.driver_connection.set_trace_callback(statements.append)
        try:
            await db._persist_live_message(
                msg,
                session_id=session_id,
                branch_progression_id=branch_prog_id,
                session_progression_id=session_prog_id,
                activity_at=msg["created_at"],
            )
        finally:
            async with db._read() as conn:
                raw_conn = await conn.get_raw_connection()
                await raw_conn.driver_connection.set_trace_callback(None)

        tx_statements = [statement.strip().upper() for statement in statements]
        assert tx_statements.count("BEGIN IMMEDIATE") == 1
        assert tx_statements.count("COMMIT") == 1
        assert await db.get_message(msg["id"]) == await baseline.get_message(msg["id"])
        assert await db.get_progression(branch_prog_id) == await baseline.get_progression(
            branch_prog_id
        )
        assert await db.get_progression(session_prog_id) == await baseline.get_progression(
            session_prog_id
        )
        assert await db.get_session(session_id) == await baseline.get_session(session_id)
        assert await db.get_branch(branch_id) == await baseline.get_branch(branch_id)
    finally:
        await baseline.close()


async def test_persist_live_message_rolls_back_when_last_statement_fails(db: StateDB):
    """A failure on activity touch leaves no message or progression orphan."""
    from sqlalchemy import event

    session_id, session_prog_id, _branch_id, branch_prog_id = await _make_live_persist_fixture(db)
    msg = make_message(role="assistant")
    msg["id"] = "live-persist-rollback-message"
    msg["created_at"] = 200.0

    def fail_activity_touch(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE sessions SET last_message_at"):
            raise RuntimeError("simulated final live-persist statement failure")

    event.listen(db._engine.sync_engine, "before_cursor_execute", fail_activity_touch)
    try:
        with pytest.raises(RuntimeError, match="final live-persist statement"):
            await db._persist_live_message(
                msg,
                session_id=session_id,
                branch_progression_id=branch_prog_id,
                session_progression_id=session_prog_id,
                activity_at=msg["created_at"],
            )
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", fail_activity_touch)

    assert await db.get_message(msg["id"]) is None
    assert await db.get_progression(branch_prog_id) == []
    assert await db.get_progression(session_prog_id) == []
    session = await db.get_session(session_id)
    assert session is not None
    assert session["last_message_at"] == 100.0
    assert session["updated_at"] == 100.0


async def test_resolve_lion_class_known(db: StateDB):
    """A known lion_class string returns the correct seeded type_id."""
    from sqlalchemy import text

    known = "lionagi.protocols.messages.system.System"
    msg = make_message(lion_class=known)
    await db.insert_message(msg)

    async with db._read() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT lion_class FROM messages WHERE id = :id"), {"id": msg["id"]}
                )
            )
            .mappings()
            .first()
        )
    # type_id 1 maps to System
    assert row["lion_class"] == 1


async def test_resolve_lion_class_unknown_empty(db: StateDB):
    """Empty lion_class string returns sentinel type_id 0."""
    from sqlalchemy import text

    msg = make_message(lion_class="")
    await db.insert_message(msg)

    async with db._read() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT lion_class FROM messages WHERE id = :id"), {"id": msg["id"]}
                )
            )
            .mappings()
            .first()
        )
    assert row["lion_class"] == 0


async def test_resolve_lion_class_auto_register(db: StateDB):
    """Unknown non-empty class is auto-registered and gets a new type_id."""
    from sqlalchemy import text

    novel_class = "myapp.custom.CustomMessage"
    msg = make_message(lion_class=novel_class)
    await db.insert_message(msg)

    async with db._read() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT type_id FROM message_types WHERE lion_class = :lc"),
                    {"lc": novel_class},
                )
            )
            .mappings()
            .first()
        )
    assert row is not None
    # Must be > 5 (beyond the seeded range)
    assert row["type_id"] > 5


# Progressions


async def test_create_and_get_progression(db: StateDB):
    """Create with an initial collection; get returns the same list."""
    prog_id = uid()
    initial = [uid(), uid(), uid()]
    await db.create_progression(prog_id, initial)

    result = await db.get_progression(prog_id)
    assert result == initial


async def test_append_to_progression(db: StateDB):
    """Append preserves insertion order."""
    prog_id = uid()
    first, second, third = uid(), uid(), uid()
    await db.create_progression(prog_id, [first])
    await db.append_to_progression(prog_id, second)
    await db.append_to_progression(prog_id, third)

    result = await db.get_progression(prog_id)
    assert result == [first, second, third]


async def test_get_progression_missing(db: StateDB):
    """Fetching a non-existent progression returns an empty list."""
    result = await db.get_progression(uid())
    assert result == []


# Sessions


async def test_create_session_with_provenance(db: StateDB):
    """Create with all provenance + lifecycle columns and verify roundtrip."""
    prog_id = uid()
    await db.create_progression(prog_id)

    now = time.time()
    session = {
        "id": uid(),
        "progression_id": prog_id,
        "name": "test-session",
        "user": "ocean",
        "playbook_name": "my-playbook",
        "agent_name": "my-agent",
        "invocation_kind": "agent",
        "show_topic": "refactor-state",
        "show_play_name": "play-1",
        "artifacts_path": "/tmp/artifacts",
        "source_kind": "imported_fs",
        "status": "running",
        "started_at": now,
        "ended_at": None,
    }
    await db.create_session(session)

    retrieved = await db.get_session(session["id"])
    assert retrieved is not None
    assert retrieved["name"] == "test-session"
    assert retrieved["user"] == "ocean"
    assert retrieved["playbook_name"] == "my-playbook"
    assert retrieved["agent_name"] == "my-agent"
    assert retrieved["invocation_kind"] == "agent"
    assert retrieved["show_topic"] == "refactor-state"
    assert retrieved["show_play_name"] == "play-1"
    assert retrieved["source_kind"] == "imported_fs"
    assert retrieved["status"] == "running"
    assert retrieved["ended_at"] is None


async def test_create_session_minimal(db: StateDB):
    """Only required fields (id, progression_id) — no error."""
    prog_id = uid()
    await db.create_progression(prog_id)
    session = {"id": uid(), "progression_id": prog_id}
    await db.create_session(session)

    retrieved = await db.get_session(session["id"])
    assert retrieved is not None
    assert retrieved["id"] == session["id"]
    assert retrieved["name"] is None
    assert retrieved["status"] is None


async def test_update_session(db: StateDB):
    """update_session changes the given fields."""
    s = await _make_session(db, status="running")
    end_time = time.time()

    await db.update_session(s["id"], status="completed", ended_at=end_time)

    retrieved = await db.get_session(s["id"])
    assert retrieved["status"] == "completed"
    assert retrieved["ended_at"] == pytest.approx(end_time, abs=1e-3)


async def test_update_session_rejects_bad_columns(db: StateDB):
    """Passing an invalid column name to update_session raises ValueError."""
    s = await _make_session(db)
    with pytest.raises(ValueError, match="Invalid column"):
        await db.update_session(s["id"], nonexistent_column="boom")


async def _seed_node_metadata(db: StateDB, session_id: str, value: str) -> None:
    """Write a session's node_metadata verbatim, as another writer's raw SQL would."""
    async with db._tx() as conn:
        await conn.execute(
            text("UPDATE sessions SET node_metadata = :raw WHERE id = :id"),
            {"raw": value, "id": session_id},
        )


def _metadata_of(row: dict) -> object:
    """The stored node_metadata as JSON, whether the driver handed it back decoded or as text."""
    value = row["node_metadata"]
    return json.loads(value) if isinstance(value, str) else value


async def test_set_if_null_fills_a_stored_json_null(db: StateDB):
    """A stored JSON null is an absence, so set_if_null fills it rather than keeping the text."""
    s = await _make_session(db, status="running")
    await _seed_node_metadata(db, s["id"], "null")

    await db.update_session(
        s["id"], node_metadata=json.dumps({"run": 1}), set_if_null=frozenset({"node_metadata"})
    )

    row = await db.get_session(s["id"])
    assert _metadata_of(row) == {"run": 1}


async def test_set_if_null_keeps_a_stored_object(db: StateDB):
    s = await _make_session(db, status="running")
    await _seed_node_metadata(db, s["id"], '{"managed": "kept"}')

    await db.update_session(
        s["id"], node_metadata=json.dumps({"run": 1}), set_if_null=frozenset({"node_metadata"})
    )

    row = await db.get_session(s["id"])
    assert _metadata_of(row) == {"managed": "kept"}


async def test_set_if_null_still_fills_an_ordinary_null_column(db: StateDB):
    """The JSON-null comparison applies to node_metadata alone; a SQL NULL column keeps the
    plain COALESCE fill."""
    s = await _make_session(db, status="running")

    await db.update_session(s["id"], started_at=12.5, set_if_null=frozenset({"started_at"}))
    await db.update_session(s["id"], started_at=99.0, set_if_null=frozenset({"started_at"}))

    row = await db.get_session(s["id"])
    assert row["started_at"] == 12.5


async def test_a_status_change_and_node_metadata_land_together_natively(db: StateDB):
    """A status write that carries node_metadata writes the column whole, as before: the
    by-key merge is the compatibility-mode contract (ADR-0128 D6)."""
    s = await _make_session(db, status="running")
    await _seed_node_metadata(db, s["id"], '{"managed": "kept"}')

    await db.update_session(
        s["id"],
        status="completed",
        reason_code=RunReasons.COMPLETED_OK,
        reason_actor=s["id"],
        node_metadata=json.dumps({"run": 1}),
    )

    row = await db.get_session(s["id"])
    assert row["status"] == "completed"
    assert _metadata_of(row) == {"run": 1}


async def test_merge_session_node_metadata_treats_a_stored_json_null_as_absent(db: StateDB):
    """A stored JSON null holds no keys yet, so the patch lands in an object of its own."""
    s = await _make_session(db, status="running")
    await _seed_node_metadata(db, s["id"], "null")

    await db.merge_session_node_metadata(s["id"], {"linked": "engine-1"})

    row = await db.get_session(s["id"])
    assert _metadata_of(row) == {"linked": "engine-1"}


async def test_merge_session_node_metadata_replaces_under_a_top_level_key(db: StateDB):
    """The merge is the RFC 7396 merge patch: a scalar in the patch replaces the stored value under
    that key rather than merging into it, and a key the patch does not name keeps its stored value.
    A nested object in the patch merges into the stored object instead of replacing it."""
    s = await _make_session(db, status="running")
    await _seed_node_metadata(
        db, s["id"], '{"khive_injection": {"other": 1}, "untouched": {"keep": true}}'
    )

    await db.merge_session_node_metadata(s["id"], {"khive_injection": "flat"})

    row = await db.get_session(s["id"])
    assert _metadata_of(row) == {
        "khive_injection": "flat",
        "untouched": {"keep": True},
    }


async def test_merge_session_node_metadata_keeps_a_stored_non_object(db: StateDB):
    """A stored non-object is kept under _discarded_node_metadata, not lost."""
    s = await _make_session(db, status="running")
    await _seed_node_metadata(db, s["id"], "[1]")

    await db.merge_session_node_metadata(s["id"], {"linked": "engine-1"})

    row = await db.get_session(s["id"])
    stored = _metadata_of(row)
    assert stored["linked"] == "engine-1"
    assert stored["_discarded_node_metadata"] == [1]
    assert isinstance(stored["_discarded_at"], float)


async def test_update_session_current_phase(db: StateDB):
    """current_phase round-trips for the `li monitor` PHASE column."""
    s = await _make_session(db, status="running")
    assert (await db.get_session(s["id"]))["current_phase"] is None

    await db.update_session(s["id"], current_phase="executing")
    assert (await db.get_session(s["id"]))["current_phase"] == "executing"


async def test_list_sessions_by_status(db: StateDB):
    """list_sessions filters correctly by status."""
    await _make_session(db, status="running")
    await _make_session(db, status="running")
    await _make_session(db, status="completed")
    await _make_session(db, status="failed")

    running = await db.list_sessions(status="running")
    completed = await db.list_sessions(status="completed")
    failed = await db.list_sessions(status="failed")

    assert len(running) == 2
    assert len(completed) == 1
    assert len(failed) == 1
    assert all(s["status"] == "running" for s in running)


async def test_count_sessions(db: StateDB):
    """count_sessions returns correct total and per-status counts."""
    await _make_session(db, status="running")
    await _make_session(db, status="running")
    await _make_session(db, status="completed")

    total = await db.count_sessions()
    assert total == 3

    running = await db.count_sessions(status="running")
    assert running == 2

    completed = await db.count_sessions(status="completed")
    assert completed == 1

    failed = await db.count_sessions(status="failed")
    assert failed == 0


# Branches


async def test_create_and_get_branch(db: StateDB):
    """Full branch roundtrip."""
    s = await _make_session(db)
    prog_id = uid()
    await db.create_progression(prog_id)

    branch = {
        "id": uid(),
        "session_id": s["id"],
        "progression_id": prog_id,
        "user": "ocean",
        "name": "main",
        "node_metadata": {"model": "gpt-4.1", "provider": "openai"},
    }
    await db.create_branch(branch)

    retrieved = await db.get_branch(branch["id"])
    assert retrieved is not None
    assert retrieved["id"] == branch["id"]
    assert retrieved["user"] == "ocean"
    assert retrieved["name"] == "main"
    assert retrieved["session_id"] == s["id"]
    # node_metadata deserialised back to dict
    assert isinstance(retrieved["node_metadata"], dict)
    assert retrieved["node_metadata"]["model"] == "gpt-4.1"


async def test_create_branch_idempotent(db: StateDB):
    """INSERT OR IGNORE: second insert with same id is a no-op; original preserved."""
    s = await _make_session(db)
    prog_id = uid()
    await db.create_progression(prog_id)

    branch_id = uid()
    original = {
        "id": branch_id,
        "session_id": s["id"],
        "progression_id": prog_id,
        "name": "original-name",
    }
    await db.create_branch(original)

    # Attempt to overwrite with different name — should be silently ignored
    duplicate = {
        "id": branch_id,
        "session_id": s["id"],
        "progression_id": prog_id,
        "name": "overwritten-name",
    }
    await db.create_branch(duplicate)

    retrieved = await db.get_branch(branch_id)
    assert retrieved["name"] == "original-name"


async def test_list_branches(db: StateDB):
    """list_branches returns all branches for a session ordered by created_at."""
    s = await _make_session(db)

    branch_ids = []
    for i in range(3):
        prog_id = uid()
        await db.create_progression(prog_id)
        b = {
            "id": uid(),
            "session_id": s["id"],
            "progression_id": prog_id,
            "name": f"branch-{i}",
            "created_at": time.time() + i,  # ensure distinct ordering
        }
        await db.create_branch(b)
        branch_ids.append(b["id"])

    branches = await db.list_branches(s["id"])
    assert len(branches) == 3
    assert [b["id"] for b in branches] == branch_ids


async def test_get_branch_messages(db: StateDB):
    """get_branch_messages returns messages in progression order."""
    s = await _make_session(db)
    prog_id = uid()
    await db.create_progression(prog_id)

    # Insert three messages in order
    msgs = [
        make_message(role="user"),
        make_message(role="assistant"),
        make_message(role="user"),
    ]
    for m in msgs:
        await db.insert_message(m)
        await db.append_to_progression(prog_id, m["id"])

    branch = {
        "id": uid(),
        "session_id": s["id"],
        "progression_id": prog_id,
    }
    await db.create_branch(branch)

    result = await db.get_branch_messages(branch["id"])
    assert len(result) == 3
    # Order must match progression order
    assert [r["id"] for r in result] == [m["id"] for m in msgs]


# finalize_branch (BRANCH_END guarded terminal write)


async def _make_branch(db: StateDB, *, status: str | None = None) -> dict:
    s = await _make_session(db)
    prog_id = uid()
    await db.create_progression(prog_id)
    branch = {
        "id": uid(),
        "session_id": s["id"],
        "progression_id": prog_id,
        "name": "leg",
    }
    await db.create_branch(branch)
    if status is not None:
        await db.update_branch(branch["id"], status=status)
    return branch


async def test_finalize_branch_stamps_status_and_ended_at_when_null(db: StateDB):
    """A branch row that never had a status written (the single-branch agent
    path's gap) is finalized on the first BRANCH_END-equivalent call."""
    branch = await _make_branch(db)
    assert (await db.get_branch(branch["id"]))["status"] is None

    updated = await db.finalize_branch(branch["id"], status="completed", ended_at=123.0)

    assert updated is True
    row = await db.get_branch(branch["id"])
    assert row["status"] == "completed"
    assert row["ended_at"] == 123.0


async def test_finalize_branch_stamps_failed_status(db: StateDB):
    """A raising operation must not leave the branch row 'running' forever."""
    branch = await _make_branch(db, status="running")

    updated = await db.finalize_branch(branch["id"], status="failed", ended_at=456.0)

    assert updated is True
    row = await db.get_branch(branch["id"])
    assert row["status"] == "failed"
    assert row["ended_at"] == 456.0


async def test_finalize_branch_skips_already_completed(db: StateDB):
    """A per-op writer's 'completed' must not be clobbered by a coarser run-level finalize."""
    branch = await _make_branch(db, status="completed")
    await db.update_branch(branch["id"], ended_at=100.0)

    updated = await db.finalize_branch(branch["id"], status="failed", ended_at=999.0)

    assert updated is False
    row = await db.get_branch(branch["id"])
    assert row["status"] == "completed"
    assert row["ended_at"] == 100.0


async def test_finalize_branch_skips_already_failed(db: StateDB):
    """Same guard, the other terminal value: 'failed' is never overwritten either."""
    branch = await _make_branch(db, status="failed")
    await db.update_branch(branch["id"], ended_at=200.0)

    updated = await db.finalize_branch(branch["id"], status="completed", ended_at=999.0)

    assert updated is False
    row = await db.get_branch(branch["id"])
    assert row["status"] == "failed"
    assert row["ended_at"] == 200.0


async def test_finalize_branch_missing_row_is_noop(db: StateDB):
    """A branch id with no row (e.g. a DAG leg that never emitted a first
    message, so create_branch() never ran) matches zero rows — harmless."""
    updated = await db.finalize_branch(uid(), status="completed")
    assert updated is False


async def test_finalize_branch_defaults_ended_at_to_now(
    db: StateDB, monkeypatch: pytest.MonkeyPatch
):
    import lionagi.state.db as state_db_mod

    fixed_now = 1_000_000.0
    monkeypatch.setattr(state_db_mod, "time", SimpleNamespace(time=lambda: fixed_now))
    branch = await _make_branch(db)

    await db.finalize_branch(branch["id"], status="completed")

    row = await db.get_branch(branch["id"])
    assert row["ended_at"] == fixed_now


@pytest.mark.parametrize(
    "existing_status",
    ["completed", "completed_empty", "failed", "timed_out", "aborted", "cancelled"],
)
async def test_finalize_branch_skips_every_terminal_status(db: StateDB, existing_status: str):
    """Every value in the session terminal-status vocabulary is immutable on a
    branch row, not just 'completed'/'failed' — a branch already at
    'cancelled', 'timed_out', 'aborted', or 'completed_empty' must survive a
    later run-level finalize carrying a different terminal status exactly
    like 'completed'/'failed' already did."""
    branch = await _make_branch(db, status=existing_status)
    await db.update_branch(branch["id"], ended_at=555.0)

    updated = await db.finalize_branch(branch["id"], status="completed", ended_at=999.0)

    assert updated is False
    row = await db.get_branch(branch["id"])
    assert row["status"] == existing_status
    assert row["ended_at"] == 555.0


@pytest.mark.parametrize(
    "terminal_status",
    ["completed", "completed_empty", "failed", "timed_out", "aborted", "cancelled"],
)
async def test_finalize_branch_stamps_every_terminal_status_from_null(
    db: StateDB, terminal_status: str
):
    """The full session terminal-status vocabulary (not just completed/failed)
    is a legitimate finalize target from a fresh (NULL-status) row."""
    branch = await _make_branch(db)

    updated = await db.finalize_branch(branch["id"], status=terminal_status, ended_at=42.0)

    assert updated is True
    row = await db.get_branch(branch["id"])
    assert row["status"] == terminal_status
    assert row["ended_at"] == 42.0


async def test_finalize_branch_rejects_running_payload(db: StateDB):
    """'running' is not a terminal outcome -- BRANCH_END must never be able to
    stamp a branch 'ended' with a non-terminal status, even against a fresh
    (NULL-status) row that would otherwise pass the existing-row guard."""
    branch = await _make_branch(db)

    updated = await db.finalize_branch(branch["id"], status="running", ended_at=42.0)

    assert updated is False
    row = await db.get_branch(branch["id"])
    assert row["status"] is None
    assert row["ended_at"] is None


async def test_finalize_branch_rejects_running_payload_against_running_row(db: StateDB):
    """Same rejection when the existing row is itself 'running' (the state a
    running payload would otherwise cleanly match against)."""
    branch = await _make_branch(db, status="running")

    updated = await db.finalize_branch(branch["id"], status="running", ended_at=42.0)

    assert updated is False
    row = await db.get_branch(branch["id"])
    assert row["status"] == "running"
    assert row["ended_at"] is None


async def test_finalize_branch_repeated_call_does_not_flap_terminal_status(db: StateDB):
    """A repeated (or concurrent-race-losing) finalization attempt carrying a
    DIFFERENT terminal status than the one that already landed must not flap
    either status or ended_at — the first genuine terminal write wins."""
    branch = await _make_branch(db)

    first = await db.finalize_branch(branch["id"], status="cancelled", ended_at=100.0)
    assert first is True

    second = await db.finalize_branch(branch["id"], status="completed", ended_at=200.0)
    assert second is False

    row = await db.get_branch(branch["id"])
    assert row["status"] == "cancelled"
    assert row["ended_at"] == 100.0


# Shows


async def test_create_and_get_show(db: StateDB):
    """Full show roundtrip."""
    show = {
        "id": uid(),
        "topic": "add-feature-x",
        "goal": "Implement X end to end",
        "repo": "owner/repo",
        "base_branch": "main",
        "integration_branch": "integrate/x",
        "status": "active",
        "show_dir": "/tmp/shows/x",
    }
    await db.create_show(show)

    retrieved = await db.get_show(show["id"])
    assert retrieved is not None
    assert retrieved["topic"] == "add-feature-x"
    assert retrieved["goal"] == "Implement X end to end"
    assert retrieved["repo"] == "owner/repo"
    assert retrieved["base_branch"] == "main"
    assert retrieved["integration_branch"] == "integrate/x"
    assert retrieved["status"] == "active"
    assert retrieved["show_dir"] == "/tmp/shows/x"


async def test_get_show_by_topic(db: StateDB):
    """get_show_by_topic finds a show by its unique topic field."""
    topic = f"unique-topic-{uid()}"
    show = {"id": uid(), "topic": topic, "show_dir": "/tmp/x", "status": "active"}
    await db.create_show(show)

    retrieved = await db.get_show_by_topic(topic)
    assert retrieved is not None
    assert retrieved["id"] == show["id"]

    # Non-existent topic returns None
    assert await db.get_show_by_topic("no-such-topic") is None


async def test_list_shows_by_status(db: StateDB):
    """list_shows filters correctly by status."""
    await _make_show(db, status="active")
    await _make_show(db, status="active")
    await _make_show(db, status="completed")

    active = await db.list_shows(status="active")
    completed = await db.list_shows(status="completed")
    all_shows = await db.list_shows()

    assert len(active) == 2
    assert len(completed) == 1
    assert len(all_shows) == 3


async def test_update_show(db: StateDB):
    """update_show changes the given fields."""
    show = await _make_show(db, status="active")

    await db.update_show(show["id"], status="completed")

    retrieved = await db.get_show(show["id"])
    assert retrieved["status"] == "completed"


async def test_update_show_rejects_bad_columns(db: StateDB):
    """Passing an invalid column name to update_show raises ValueError."""
    show = await _make_show(db)
    with pytest.raises(ValueError, match="Invalid column"):
        await db.update_show(show["id"], not_a_column="boom")


# Plays


async def test_create_and_get_play(db: StateDB):
    """Full play roundtrip including depends_on JSON."""
    show = await _make_show(db)

    dep1, dep2 = uid(), uid()
    play = {
        "id": uid(),
        "show_id": show["id"],
        "name": "play-alpha",
        "playbook": "review-flow",
        "effort": "medium",
        "status": "pending",
        "attempt": 1,
        "sort_order": 10,
        "depends_on": [dep1, dep2],
        "worktree": "/tmp/wt/alpha",
        "branch": "show/alpha",
    }
    await db.create_play(play)

    retrieved = await db.get_play(play["id"])
    assert retrieved is not None
    assert retrieved["name"] == "play-alpha"
    assert retrieved["playbook"] == "review-flow"
    assert retrieved["effort"] == "medium"
    assert retrieved["sort_order"] == 10
    assert retrieved["worktree"] == "/tmp/wt/alpha"
    assert retrieved["branch"] == "show/alpha"
    # depends_on deserialized back to list
    assert isinstance(retrieved["depends_on"], list)
    assert retrieved["depends_on"] == [dep1, dep2]


async def test_list_plays_ordered(db: StateDB):
    """list_plays returns plays sorted by sort_order then created_at."""
    show = await _make_show(db)
    t0 = time.time()

    plays = [
        {
            "id": uid(),
            "show_id": show["id"],
            "name": "p3",
            "sort_order": 30,
            "created_at": t0,
        },
        {
            "id": uid(),
            "show_id": show["id"],
            "name": "p1",
            "sort_order": 10,
            "created_at": t0 + 1,
        },
        {
            "id": uid(),
            "show_id": show["id"],
            "name": "p2",
            "sort_order": 20,
            "created_at": t0 + 2,
        },
    ]
    for p in plays:
        await db.create_play(p)

    result = await db.list_plays(show["id"])
    assert [r["name"] for r in result] == ["p1", "p2", "p3"]


async def test_update_play(db: StateDB):
    """update_play changes status and exit_code."""
    show = await _make_show(db)
    play = {"id": uid(), "show_id": show["id"], "name": "update-me"}
    await db.create_play(play)

    end_time = time.time()
    # ADR-0057 vocab: plays use ``running_complete`` (not ``completed``)
    # for the "finished running" terminal — ``completed`` belongs to the
    # sessions vocabulary (ADR-0057), not plays.
    # ADR-0057 Phase 2: `running_complete` has no canonical default
    # reason_code (the gate hasn't run yet at that point — the caller
    # has the context to choose between PENDING_READY / GATE_FAILED_
    # VERDICT / etc.), so we must pass reason_code explicitly.
    from lionagi.state.reasons import PlayReasons

    await db.update_play(
        play["id"],
        status="running_complete",
        exit_code=0,
        ended_at=end_time,
        reason_code=PlayReasons.PENDING_READY,
        reason_summary="Test fixture: play marked running_complete.",
    )

    retrieved = await db.get_play(play["id"])
    assert retrieved["status"] == "running_complete"
    assert retrieved["exit_code"] == 0
    assert retrieved["ended_at"] == pytest.approx(end_time, abs=1e-3)


async def test_update_play_rejects_bad_columns(db: StateDB):
    """Passing an invalid column name to update_play raises ValueError."""
    show = await _make_show(db)
    play = {"id": uid(), "show_id": show["id"], "name": "bad-col-test"}
    await db.create_play(play)

    with pytest.raises(ValueError, match="Invalid column"):
        await db.update_play(play["id"], hacker_column="evil")


# Definitions


async def test_save_and_get_definition(db: StateDB):
    """save_definition returns version 1; get_definition returns latest."""
    version = await db.save_definition(
        kind="agent",
        name="analyst",
        path=".lionagi/agents/analyst.yaml",
        content="role: analyst\nmodel: gpt-4.1",
        message="initial",
    )
    assert version == 1

    defn = await db.get_definition("agent", "analyst")
    assert defn is not None
    assert defn["version"] == 1
    assert defn["kind"] == "agent"
    assert defn["name"] == "analyst"
    assert defn["content"] == "role: analyst\nmodel: gpt-4.1"
    assert defn["message"] == "initial"


async def test_definition_versioning(db: StateDB):
    """save_definition auto-increments; get_definition fetches by exact version."""
    v1 = await db.save_definition(
        kind="playbook",
        name="review-flow",
        path=".lionagi/playbooks/review-flow.yaml",
        content="v1 content",
    )
    v2 = await db.save_definition(
        kind="playbook",
        name="review-flow",
        path=".lionagi/playbooks/review-flow.yaml",
        content="v2 content",
        message="update instructions",
    )

    assert v1 == 1
    assert v2 == 2

    defn_v1 = await db.get_definition("playbook", "review-flow", version=1)
    defn_v2 = await db.get_definition("playbook", "review-flow", version=2)

    assert defn_v1["content"] == "v1 content"
    assert defn_v2["content"] == "v2 content"
    assert defn_v2["message"] == "update instructions"

    # get_definition without version returns latest
    latest = await db.get_definition("playbook", "review-flow")
    assert latest["version"] == 2


async def test_list_definition_versions(db: StateDB):
    """list_definition_versions returns all versions in descending order."""
    for i in range(3):
        await db.save_definition(
            kind="agent",
            name="reviewer",
            path=".lionagi/agents/reviewer.md",
            content=f"version {i + 1}",
        )

    versions = await db.list_definition_versions("agent", "reviewer")
    assert len(versions) == 3
    # Descending version order
    assert [v["version"] for v in versions] == [3, 2, 1]


async def test_save_definition_rejects_non_editable_kind(db: StateDB):
    """ADR-0077: arbitrary kinds outside the editable set (agent, playbook,
    skill) are read-only and must be rejected."""
    import pytest

    for bad_kind in ("plugin", "something_else"):
        with pytest.raises(ValueError, match="Invalid definition kind"):
            await db.save_definition(
                kind=bad_kind,
                name="x",
                path=".lionagi/x",
                content="content",
            )


async def test_save_definition_accepts_skill_kind(db: StateDB):
    """Skills joined the editable set alongside agent/playbook (skill editor)."""
    version = await db.save_definition(
        kind="skill",
        name="x",
        path=".lionagi/skills/x/SKILL.md",
        content="content",
    )
    assert version == 1


async def test_get_definition_missing(db: StateDB):
    """get_definition returns None for a (kind, name) that doesn't exist."""
    result = await db.get_definition("agent", "nonexistent-agent")
    assert result is None

    # Also for an explicit version that doesn't exist
    result_versioned = await db.get_definition("agent", "nonexistent-agent", version=99)
    assert result_versioned is None


# Regression: SQL race + JSON roundtrip + provenance


async def test_resolve_lion_class_concurrent_race(tmp_path):
    """SELECT-then-INSERT raced on UNIQUE(message_types.lion_class).

    The fix uses INSERT OR IGNORE + SELECT so concurrent writers for the same
    novel ``lion_class`` no longer collide. Drive 20 concurrent insert_message
    calls registering the same new class — none should raise.
    """
    import asyncio

    path = tmp_path / "race.db"
    db = StateDB(str(path))
    await db.open()
    try:
        prog_id = uid()
        await db.create_progression(prog_id)

        async def insert_one(i):
            await db.insert_message(
                {
                    "id": f"raced-{i}",
                    "created_at": time.time(),
                    "node_metadata": {"lion_class": "test.race.NovelClass"},
                    "content": {"i": i},
                    "role": "user",
                }
            )

        # 20 concurrent inserts of the same novel class. Pre-fix this raised
        # ``sqlite3.IntegrityError: UNIQUE constraint failed`` for most.
        await asyncio.gather(*(insert_one(i) for i in range(20)))

        # Exactly one message_types row for the novel class.
        from sqlalchemy import text

        async with db._read() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT COUNT(*) AS n FROM message_types WHERE lion_class = :lc"),
                        {"lc": "test.race.NovelClass"},
                    )
                )
                .mappings()
                .first()
            )
        assert row["n"] == 1
    finally:
        await db.close()


async def test_save_definition_concurrent_versions_are_unique(tmp_path):
    """R4-C HIGH-3: SELECT MAX(version) + INSERT raced under concurrent saves.

    The fix uses BEGIN IMMEDIATE + bounded retry on IntegrityError so all
    writers complete with unique, monotonically-increasing versions.
    """
    import asyncio

    path = tmp_path / "defrace.db"
    db = StateDB(str(path))
    await db.open()
    try:
        N = 10
        versions = await asyncio.gather(
            *(
                db.save_definition(
                    kind="agent",
                    name="race-agent",
                    path=".lionagi/agents/race-agent.md",
                    content=f"content-{i}",
                    message=f"save-{i}",
                )
                for i in range(N)
            )
        )
        # Every save returned a unique version, and the set is {1..N}.
        assert sorted(versions) == list(range(1, N + 1)), (
            f"Expected unique versions 1..{N}, got {sorted(versions)}"
        )

        # Database state matches the API return values.
        rows = await db.list_definition_versions("agent", "race-agent")
        assert sorted(r["version"] for r in rows) == list(range(1, N + 1))
    finally:
        await db.close()


async def test_message_content_string_roundtrips_as_string(db: StateDB):
    """R4-C MED-3: A literal string content used to round-trip as a dict
    because ``_row_to_dict`` ``json.loads()``'d every string column. The
    fix wraps strings in JSON via ``_to_json_column`` so loads is the
    exact inverse.
    """
    prog_id = uid()
    await db.create_progression(prog_id)

    # Cases that would have round-tripped as dict pre-fix.
    json_like_strings = [
        '{"text": "literal string"}',  # looks like a JSON object
        "[1, 2, 3]",  # looks like a JSON array
        '"already quoted"',  # already-quoted JSON string
        "plain text",  # not JSON at all
        "",  # empty string
        "42",  # JSON number
        "null",  # JSON null
    ]
    for i, raw in enumerate(json_like_strings):
        msg_id = f"str-{i}"
        await db.insert_message(
            {
                "id": msg_id,
                "created_at": time.time(),
                "content": raw,
                "role": "user",
            }
        )
        got = await db.get_message(msg_id)
        assert got is not None
        # Critical: type AND value preserved exactly.
        assert isinstance(got["content"], str), (
            f"case {i!r}: expected str, got {type(got['content']).__name__}"
        )
        assert got["content"] == raw, f"case {i!r}: value diverged"


async def test_message_content_dict_roundtrips_as_dict(db: StateDB):
    """And dicts still round-trip as dicts — the fix shouldn't regress
    the normal case."""
    prog_id = uid()
    await db.create_progression(prog_id)

    await db.insert_message(
        {
            "id": "dict-msg",
            "created_at": time.time(),
            "content": {"role": "assistant", "text": "hello"},
            "role": "assistant",
        }
    )
    got = await db.get_message("dict-msg")
    assert got is not None
    assert isinstance(got["content"], dict)
    assert got["content"] == {"role": "assistant", "text": "hello"}


async def test_create_session_rejects_invalid_invocation_kind(db: StateDB):
    """invocation_kind is a closed vocabulary; invalid kinds are rejected at creation."""
    prog_id = uid()
    await db.create_progression(prog_id)
    with pytest.raises(ValueError, match="invocation_kind"):
        await db.create_session(
            {
                "id": uid(),
                "progression_id": prog_id,
                "created_at": time.time(),
                "invocation_kind": "not-a-real-kind",
            }
        )


async def test_create_session_rejects_invalid_source_kind(db: StateDB):
    """source_kind ∈ {live, imported_fs}."""
    prog_id = uid()
    await db.create_progression(prog_id)
    with pytest.raises(ValueError, match="source_kind"):
        await db.create_session(
            {
                "id": uid(),
                "progression_id": prog_id,
                "created_at": time.time(),
                "source_kind": "remote_api",
            }
        )


async def test_update_session_rejects_invalid_enums(db: StateDB):
    """Updates also validate — not just create."""
    prog_id = uid()
    await db.create_progression(prog_id)
    sid = uid()
    await db.create_session(
        {
            "id": sid,
            "progression_id": prog_id,
            "created_at": time.time(),
            "invocation_kind": "agent",
            "source_kind": "live",
        }
    )

    with pytest.raises(ValueError, match="invocation_kind"):
        await db.update_session(sid, invocation_kind="bogus")
    with pytest.raises(ValueError, match="source_kind"):
        await db.update_session(sid, source_kind="bogus")


async def test_create_play_rejects_invalid_status(db: StateDB):
    """ADR-0057: play status ∈ 11-vocabulary."""
    show = await _make_show(db)
    with pytest.raises(ValueError, match="play status"):
        await db.create_play(
            {
                "id": uid(),
                "show_id": show["id"],
                "name": "bad-status-play",
                "status": "completed",  # belongs to SESSIONS vocab
            }
        )


async def test_create_show_rejects_invalid_status(db: StateDB):
    """ADR-0057: show status ∈ {active, completed, aborted, imported}."""
    with pytest.raises(ValueError, match="show status"):
        await db.create_show(
            {
                "id": uid(),
                "topic": "bad-status",
                "show_dir": "/tmp/bad",
                "status": "running",  # not in show vocab
            }
        )


async def test_session_delete_cascades_branches(db: StateDB):
    """R4-D MED-9: schema declares ON DELETE CASCADE for branches; verify."""
    prog_id = uid()
    await db.create_progression(prog_id)
    sid = uid()
    await db.create_session(
        {
            "id": sid,
            "progression_id": prog_id,
            "created_at": time.time(),
        }
    )
    bprog = uid()
    await db.create_progression(bprog)
    bid = uid()
    await db.create_branch(
        {
            "id": bid,
            "session_id": sid,
            "progression_id": bprog,
            "created_at": time.time(),
        }
    )

    assert await db.get_branch(bid) is not None
    async with db._tx() as conn:
        from sqlalchemy import text

        await conn.execute(text("DELETE FROM sessions WHERE id = :id"), {"id": sid})
    assert await db.get_branch(bid) is None


# ADR-0064: Artifact contract storage


async def test_create_session_with_artifact_contract(db: StateDB):
    """artifact_contract_json written at creation is decoded on fetch."""
    prog_id = uid()
    await db.create_progression(prog_id)
    contract = {"expected": [{"id": "report", "path": "report.md"}]}
    sid = uid()
    await db.create_session(
        {
            "id": sid,
            "progression_id": prog_id,
            "created_at": time.time(),
            "status": "running",
            "artifact_contract_json": contract,
        }
    )
    row = await db.get_session(sid)
    assert row is not None
    stored = row["artifact_contract_json"]
    assert isinstance(stored, dict), f"expected dict, got {type(stored)}"
    assert stored["expected"][0]["id"] == "report"


async def test_update_artifact_verification(db: StateDB):
    """update_artifact_verification() persists and round-trips the result."""
    prog_id = uid()
    await db.create_progression(prog_id)
    sid = uid()
    await db.create_session(
        {
            "id": sid,
            "progression_id": prog_id,
            "created_at": time.time(),
            "status": "running",
        }
    )
    verification = {
        "status": "passed",
        "checked_at": time.time(),
        "missing_required": [],
        "missing_optional": [],
        "produced": [{"id": "report", "path": "report.md", "size": 42, "present": True}],
    }
    await db.update_artifact_verification(sid, verification)
    row = await db.get_session(sid)
    assert row is not None
    stored = row["artifact_verification_json"]
    assert isinstance(stored, dict), f"expected dict, got {type(stored)}"
    assert stored["status"] == "passed"
    assert stored["produced"][0]["id"] == "report"


async def test_update_artifact_verification_none(db: StateDB):
    """update_artifact_verification(None) stores NULL without error."""
    prog_id = uid()
    await db.create_progression(prog_id)
    sid = uid()
    await db.create_session(
        {
            "id": sid,
            "progression_id": prog_id,
            "created_at": time.time(),
            "status": "running",
        }
    )
    await db.update_artifact_verification(sid, None)
    row = await db.get_session(sid)
    assert row is not None
    assert row["artifact_verification_json"] is None


async def test_new_db_has_artifact_columns(db: StateDB):
    """Fresh in-memory DB exposes both new columns via PRAGMA table_info."""
    from sqlalchemy import text

    async with db._read() as conn:
        rows = (await conn.execute(text("PRAGMA table_info(sessions)"))).mappings().all()
    cols = {r["name"] for r in rows}
    assert "artifact_contract_json" in cols
    assert "artifact_verification_json" in cols


# readonly=True: no schema application, no create-on-open


async def test_readonly_open_rejects_missing_file(tmp_path):
    """readonly=True against a path with no existing file raises loudly and
    never creates one — a read-only consumer must not have a database-creation
    side effect."""
    db_path = tmp_path / "does_not_exist.db"
    state = StateDB(db_path, readonly=True)
    with pytest.raises(FileNotFoundError):
        await state.open()
    assert not db_path.exists()


async def test_readonly_open_never_calls_apply_schema(tmp_path, monkeypatch):
    """readonly=True skips _apply_schema() entirely — the read-only open path
    must never reconcile columns, run metadata.create_all, or seed rows."""
    db_path = tmp_path / "seeded.db"
    # Seed a real file via a normal (writable) open first.
    seed = StateDB(db_path)
    await seed.open()
    prog_id = uid()
    await seed.create_progression(prog_id)
    await seed.create_session({"id": uid(), "progression_id": prog_id, "status": "completed"})
    await seed.close()

    async def _boom(self):
        raise AssertionError("_apply_schema() must not be called in readonly mode")

    monkeypatch.setattr(StateDB, "_apply_schema", _boom)

    ro = StateDB(db_path, readonly=True)
    async with ro as db:
        rows = await db.fetch_all("SELECT COUNT(*) AS n FROM sessions")
    assert rows[0]["n"] == 1


async def test_readonly_open_skips_write_lock_event(tmp_path):
    """readonly=True must not install the BEGIN IMMEDIATE connect-event —
    that event only exists to reserve a write lock, which a read-only
    accessor has no business taking."""
    db_path = tmp_path / "no_write_lock.db"
    seed = StateDB(db_path)
    await seed.open()
    await seed.close()

    ro = StateDB(db_path, readonly=True)
    await ro.open()
    try:
        async with ro._read() as conn:
            from sqlalchemy import text

            row = (await conn.execute(text("PRAGMA query_only"))).first()
            assert row[0] == 1
    finally:
        await ro.close()


async def test_readonly_rejects_write_attempt(tmp_path):
    """Defense in depth: even if a future code path tried to write through a
    readonly=True StateDB, SQLite itself refuses (mode=ro + query_only=1)."""
    db_path = tmp_path / "reject_write.db"
    seed = StateDB(db_path)
    await seed.open()
    await seed.close()

    ro = StateDB(db_path, readonly=True)
    await ro.open()
    try:
        with pytest.raises(Exception, match="readonly|read-only"):
            await ro.execute("INSERT INTO schema_meta (key, value) VALUES ('x', 'y')")
    finally:
        await ro.close()


async def test_writable_read_does_not_issue_begin_immediate(tmp_path):
    """StateDB._read() on a writable engine must not reserve the SQLite
    writer slot. Tracing the raw driver connection during a plain read must
    show no BEGIN IMMEDIATE -- only the SELECT."""
    db_path = tmp_path / "read_no_lock.db"
    db = StateDB(db_path)
    await db.open()
    try:
        statements: list[str] = []
        async with db._read() as conn:
            raw_conn = await conn.get_raw_connection()
            await raw_conn.driver_connection.set_trace_callback(statements.append)
            result = await conn.execute(text("SELECT 42 AS answer"))
            assert result.scalar() == 42
            await raw_conn.driver_connection.set_trace_callback(None)
        upper = [s.strip().upper() for s in statements]
        assert "BEGIN IMMEDIATE" not in upper, upper
    finally:
        await db.close()


async def test_writable_read_survives_a_concurrent_write_lock(tmp_path):
    """A read must complete while a separate connection holds SQLite's
    writer lock via BEGIN IMMEDIATE -- it must not wait out the busy
    timeout behind a lock it never needed to contend for."""
    db_path = tmp_path / "read_survives_lock.db"
    seed = StateDB(db_path)
    await seed.open()
    await seed.close()

    db = StateDB(db_path)
    await db.open()
    try:
        blocker = sqlite3.connect(str(db_path), timeout=0)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            start = time.monotonic()
            rows = await db.fetch_all("SELECT 42 AS answer")
            elapsed = time.monotonic() - start
        finally:
            blocker.rollback()
            blocker.close()
        assert rows == [{"answer": 42}]
        assert elapsed < 1.0, f"read waited {elapsed}s behind a lock it should not contend for"
    finally:
        await db.close()


# update_status extra_fields: schedule_run


async def _make_running_schedule_run(db) -> str:
    schedule_id = uid()
    await db.create_schedule(
        {
            "id": schedule_id,
            "name": "extras",
            "trigger_type": "interval",
            "interval_sec": 60,
            "action_kind": "agent",
        }
    )
    run_id = uid()
    await db.create_schedule_run(
        {
            "id": run_id,
            "schedule_id": schedule_id,
            "trigger_context": {},
            "action_kind": "agent",
            "action_args": [],
            "status": "running",
            "fired_at": time.time(),
        }
    )
    return run_id


async def test_schedule_run_status_write_carries_ended_at_and_error_detail(db):
    """A schedule_run's terminal write may set ended_at and error_detail in the
    same guarded transaction as the status, so the two can never disagree."""
    run_id = await _make_running_schedule_run(db)
    ended = time.time()

    assert await db.update_status(
        "schedule_run",
        run_id,
        new_status="failed",
        reason_code="run.failed.exception",
        reason_summary="RuntimeError: boom",
        source="executor",
        actor="test",
        expected_statuses={"running"},
        extra_fields={"ended_at": ended, "error_detail": "RuntimeError: boom"},
    )

    row = await db.get_schedule_run(run_id)
    assert row["status"] == "failed"
    assert row["error_detail"] == "RuntimeError: boom"
    assert row["ended_at"] == pytest.approx(ended)


async def test_schedule_run_status_write_rejects_an_unlisted_extra_field(db):
    """Only the columns declared for schedule_run ride a status write; anything
    else is refused rather than written."""
    run_id = await _make_running_schedule_run(db)

    with pytest.raises(ValueError, match="extra_fields"):
        await db.update_status(
            "schedule_run",
            run_id,
            new_status="failed",
            reason_code="run.failed.exception",
            reason_summary="RuntimeError: boom",
            source="executor",
            actor="test",
            expected_statuses={"running"},
            extra_fields={"stderr_tail": "nope"},
        )

    row = await db.get_schedule_run(run_id)
    assert row["status"] == "running"


async def test_the_imported_role_backfill_clears_only_imported_rows(tmp_path):
    """Stopping the write fixes future imports; rows already stored keep the
    engine label unless something clears them, and because the role tier sits
    ahead of the prompt tier in the name resolver they would render the engine
    forever while new imports rendered their prompt.

    The control is the point of this test. A live session is given the SAME
    `agent_name` value the imported rows carry, so a backfill scoped to the
    label rather than to `source_kind` passes every other assertion here and
    fails only this one.
    """
    path = tmp_path / "state.db"
    url = f"sqlite+aiosqlite:///{path}"
    prog = uid()
    imported_id, live_id, branch_id = uid(), uid(), uid()

    async with StateDB(url) as db:
        await db.create_progression(prog)
        await db.create_session(
            {
                "id": imported_id,
                "progression_id": prog,
                "name": "imported",
                "agent_name": "codex",
                "invocation_kind": "agent",
                "source_kind": "imported_codex",
                "status": "completed",
            }
        )
        await db.create_branch(
            {
                "id": branch_id,
                "session_id": imported_id,
                "progression_id": prog,
                "agent_name": "codex",
            }
        )
        # Control: a live row wearing the same label.
        await db.create_session(
            {
                "id": live_id,
                "progression_id": prog,
                "name": "live",
                "agent_name": "codex",
                "invocation_kind": "agent",
                "source_kind": "live",
                "status": "completed",
            }
        )

    # The first open already claimed the one-shot marker. Release it so the
    # reopen below exercises the migration against rows that now exist -- which
    # is the real upgrade order: rows first, migration after.
    conn = sqlite3.connect(path)
    try:
        conn.execute("DELETE FROM schema_meta WHERE key = 'migration.imported_role_label_backfill'")
        conn.commit()
    finally:
        conn.close()

    async with StateDB(url) as db:
        imported = await db.get_session(imported_id)
        live = await db.get_session(live_id)
        branch = await db.get_branch(branch_id)

    assert imported["agent_name"] is None, "imported session kept the engine label"
    assert branch["agent_name"] is None, "imported session was cleared but its branch was not"
    assert live["agent_name"] == "codex", (
        "a LIVE row was cleared -- the backfill is selecting on the label instead of source_kind"
    )


async def test_the_imported_role_backfill_runs_only_once(tmp_path):
    """A second open must not re-clear. The marker, not the data, is what makes
    this one-shot: a row legitimately re-labelled after the migration would be
    silently reverted on the next open if the guard were the data itself."""
    path = tmp_path / "state.db"
    url = f"sqlite+aiosqlite:///{path}"
    prog, sid = uid(), uid()

    async with StateDB(url) as db:
        await db.create_progression(prog)
        await db.create_session(
            {
                "id": sid,
                "progression_id": prog,
                "name": "imported",
                "invocation_kind": "agent",
                "source_kind": "imported_codex",
                "status": "completed",
            }
        )

    # Marker was claimed on the first open, so this label must survive.
    async with StateDB(url) as db:
        await db.update_session(sid, agent_name="re-labelled")

    async with StateDB(url) as db:
        again = await db.get_session(sid)
    assert again["agent_name"] == "re-labelled", (
        "the backfill ran a second time and reverted a post-migration write"
    )
