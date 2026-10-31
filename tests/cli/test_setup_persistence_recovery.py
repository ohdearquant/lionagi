# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Recovery of a session committed during interrupted setup."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.fixture
def isolated_setup(monkeypatch):
    from lionagi.cli import _runs
    from lionagi.session import session as session_module
    from lionagi.state import cli_workers, compat, provenance

    class Session:
        def __init__(self, *, name, default_branch):
            self.id = uuid4()

        def to_dict(self, *, mode):
            return {"created_at": 0.0, "node_metadata": {}}

        def remove_branch(self, branch):
            pass

    class Owner:
        def __init__(self, *_args, **_kwargs):
            pass

        async def aclose(self):
            pass

    monkeypatch.setattr(session_module, "Session", Session)
    monkeypatch.setattr(cli_workers, "_WorkerSpawnOwner", Owner)
    monkeypatch.setattr(compat, "is_compatibility_store", lambda _db: False)
    monkeypatch.setattr("lionagi.ln._proc.current_pid_markers", lambda: {})
    monkeypatch.setattr(provenance, "agent_definition_hash", lambda _name: None)
    monkeypatch.setattr(_runs, "_resolve_project", lambda _project: (None, None))
    return _runs


def fake_branch():
    return SimpleNamespace(
        _owning_session_id=None,
        id=uuid4(),
        system=None,
        to_dict=lambda **_: {"created_at": 0.0, "node_metadata": {}},
    )


@pytest.mark.parametrize("point", ["during_create", "before_read", "after_admission"])
@pytest.mark.asyncio
async def test_cancellation_recovers_committed_session(
    isolated_setup, monkeypatch, tmp_path, point
):
    from lionagi.state.db import StateDB
    from lionagi.state.reasons import RunReasons

    db = StateDB(tmp_path / "state.db")
    await db.open()
    entered, release = asyncio.Event(), asyncio.Event()
    original_create, original_read, original_branch = (
        db.create_session,
        db.get_session,
        db.create_branch,
    )
    observed = {}
    read_blocked = False

    async def create(row):
        observed["id"] = row["id"]
        await original_create(row)
        if point == "during_create":
            entered.set()
            await release.wait()

    async def read(identifier):
        nonlocal read_blocked
        if observed and point == "before_read" and not read_blocked:
            read_blocked = True
            entered.set()
            await release.wait()
        return await original_read(identifier)

    async def create_branch(row):
        if point == "after_admission":
            entered.set()
            await release.wait()
        await original_branch(row)

    async def open_db(*, register):
        return db

    monkeypatch.setattr(db, "create_session", create)
    monkeypatch.setattr(db, "get_session", read)
    monkeypatch.setattr(db, "create_branch", create_branch)
    monkeypatch.setattr(isolated_setup, "_open_shared_db", open_db)
    task = asyncio.create_task(isolated_setup.setup_agent_persist(fake_branch(), share_db=False))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        async with StateDB(tmp_path / "state.db") as check:
            row = await check.get_session(observed["id"])
        assert row["status"] == "cancelled"
        assert row["status_reason_code"] == RunReasons.CANCELLED_SYSTEM
        assert db._engine is None
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if db._engine is not None:
            await db.close()


@pytest.mark.parametrize(
    "mode",
    [
        "before_insert",
        "failure",
        "other_generation",
        "compat",
        "race",
        "missing_version",
        "missing_close",
        "unexpected_lookup",
        "timeout",
        "repeat_cancel",
        "read_timeout",
        "read_error",
    ],
)
@pytest.mark.asyncio
async def test_fresh_recovery_guards(isolated_setup, monkeypatch, tmp_path, caplog, mode):
    from lionagi.state import compat
    from lionagi.state.db import StateDB
    from lionagi.state.lifecycle import LifecycleNotFoundError
    from lionagi.state.reasons import RunReasons

    db = StateDB(tmp_path / "state.db")
    await db.open()
    entered, release = asyncio.Event(), asyncio.Event()
    cleanup_entered = asyncio.Event()
    original_create, original_read, original_update = (
        db.create_session,
        db.get_session,
        db.update_status,
    )
    observed = {}
    calls = 0
    other_generation = str(uuid4())

    async def create(row):
        observed["id"] = row["id"]
        if mode == "before_insert":
            entered.set()
            await release.wait()
        if mode == "other_generation":
            row = {**row, "cli_worker_generation": other_generation}
        await original_create(row)
        if mode == "failure":
            raise ValueError("synthetic setup failure")
        entered.set()
        await release.wait()

    async def read(identifier):
        nonlocal calls
        if observed and mode == "read_timeout":
            await asyncio.Event().wait()
        if observed and mode == "read_error":
            raise RuntimeError("synthetic read failure")
        row = await original_read(identifier)
        if observed and row is not None:
            calls += 1
            if mode == "race":
                await db.update_session(identifier, cli_worker_generation=other_generation)
            elif mode == "missing_version":
                row["updated_at"] = None
        return row

    async def update(*args, **kwargs):
        if mode == "missing_close":
            raise LookupError("already removed") from LifecycleNotFoundError("gone")
        if mode == "unexpected_lookup":
            raise KeyError("unexpected")
        if mode == "timeout":
            await asyncio.Event().wait()
        if mode == "repeat_cancel":
            cleanup_entered.set()
            await asyncio.Event().wait()
        return await original_update(*args, **kwargs)

    async def open_db(*, register):
        return db

    monkeypatch.setattr(db, "create_session", create)
    monkeypatch.setattr(db, "get_session", read)
    monkeypatch.setattr(db, "update_status", update)
    monkeypatch.setattr(isolated_setup, "_open_shared_db", open_db)
    monkeypatch.setattr(isolated_setup, "_SETUP_RECOVERY_TIMEOUT_SECONDS", 0.05)
    if mode == "compat":
        monkeypatch.setattr(compat, "is_compatibility_store", lambda _db: True)
    task = asyncio.create_task(isolated_setup.setup_agent_persist(fake_branch(), share_db=False))
    try:
        if mode == "failure":
            assert await asyncio.wait_for(task, 5) is None
        else:
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel("first")
            if mode == "repeat_cancel":
                await asyncio.wait_for(cleanup_entered.wait(), 5)
                task.cancel("second")
            done, _ = await asyncio.wait({task}, timeout=5)
            assert task in done
            with pytest.raises(asyncio.CancelledError) as error:
                await task
            assert error.value.args != ("second",)
        async with StateDB(tmp_path / "state.db") as check:
            row = await check.get_session(observed["id"])
        if mode == "before_insert":
            assert row is None
            assert "session setup recovery did not complete" not in caplog.text
        elif mode == "failure":
            assert row["status"] == "failed"
            assert row["status_reason_code"] == RunReasons.FAILED_EXCEPTION
        else:
            assert row["status"] == "running"
            if mode == "missing_close":
                assert "session setup recovery did not complete" not in caplog.text
            else:
                assert "session setup recovery did not complete" in caplog.text
        if mode == "compat":
            assert calls == 0
        if mode == "race":
            assert row["cli_worker_generation"] == other_generation
            assert "row changed" in caplog.text
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if db._engine is not None:
            await db.close()


@pytest.mark.parametrize("mode", ["refusal", "ordinary", "close"])
@pytest.mark.asyncio
async def test_direct_cancel_during_cleanup_keeps_first_failure(
    isolated_setup, monkeypatch, tmp_path, mode
):
    from lionagi._provider_errors import _WorkerSpawnAdmissionError
    from lionagi.state.db import StateDB

    db = StateDB(tmp_path / "state.db")
    await db.open()
    entered, cleanup = asyncio.Event(), asyncio.Event()
    gate = asyncio.Event()
    original_create, original_update, original_close = db.create_session, db.update_status, db.close
    closes = 0
    failure = _WorkerSpawnAdmissionError("first failure")

    async def create(row):
        await original_create(row)
        if mode == "refusal":
            raise failure
        if mode == "ordinary":
            raise ValueError("first failure")
        entered.set()
        await gate.wait()

    async def update(*args, **kwargs):
        if mode in {"refusal", "ordinary"}:
            cleanup.set()
            await gate.wait()
        return await original_update(*args, **kwargs)

    async def close():
        nonlocal closes
        closes += 1
        if mode == "close" and closes == 1:
            cleanup.set()
            await gate.wait()
        await original_close()

    async def open_db(*, register):
        return db

    monkeypatch.setattr(db, "create_session", create)
    monkeypatch.setattr(db, "update_status", update)
    monkeypatch.setattr(db, "close", close)
    monkeypatch.setattr(isolated_setup, "_open_shared_db", open_db)
    task = asyncio.create_task(isolated_setup.setup_agent_persist(fake_branch(), share_db=False))
    try:
        if mode == "close":
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel("first")
        await asyncio.wait_for(cleanup.wait(), 5)
        task.cancel("second")
        done, _ = await asyncio.wait({task}, timeout=5)
        assert task in done
        if mode == "refusal":
            with pytest.raises(_WorkerSpawnAdmissionError) as raised:
                await task
            assert raised.value is failure
        elif mode == "ordinary":
            with pytest.raises(asyncio.CancelledError) as raised:
                await task
            # Python 3.10 can recreate a propagated cancellation without its message.
            if sys.version_info >= (3, 11):
                assert raised.value.args == ("second",)
        else:
            with pytest.raises(asyncio.CancelledError) as raised:
                await task
            assert raised.value.args != ("second",)
            assert closes == 2
        assert db._engine is None
    finally:
        gate.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if db._engine is not None:
            await original_close()


@pytest.mark.asyncio
async def test_cancel_during_fallback_close_after_ordinary_setup_failure(
    isolated_setup, monkeypatch, tmp_path
):
    from lionagi.state.db import StateDB

    db = StateDB(tmp_path / "state.db")
    await db.open()
    entered = asyncio.Event()
    gate = asyncio.Event()
    original_close = db.close
    closes = 0

    async def create(_row):
        raise ValueError("synthetic setup failure")

    async def close():
        nonlocal closes
        closes += 1
        if closes == 1:
            entered.set()
            await gate.wait()
        await original_close()

    async def open_db(*, register):
        return db

    monkeypatch.setattr(db, "create_session", create)
    monkeypatch.setattr(db, "close", close)
    monkeypatch.setattr(isolated_setup, "_open_shared_db", open_db)
    task = asyncio.create_task(isolated_setup.setup_agent_persist(fake_branch(), share_db=False))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel("second")
        with pytest.raises(asyncio.CancelledError) as raised:
            await asyncio.wait_for(task, 5)
        if sys.version_info >= (3, 11):
            assert raised.value.args == ("second",)
        assert closes == 2
        assert db._engine is None
    finally:
        gate.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if db._engine is not None:
            await original_close()


@pytest.mark.asyncio
async def test_cancel_before_create_never_starts_candidate_recovery(
    isolated_setup, monkeypatch, tmp_path, caplog
):
    from lionagi.state.db import StateDB

    db = StateDB(tmp_path / "state.db")
    await db.open()
    entered = asyncio.Event()
    gate = asyncio.Event()
    updates = []

    async def progression(_identifier):
        entered.set()
        await gate.wait()

    async def update(*args, **kwargs):
        updates.append((args, kwargs))

    async def open_db(*, register):
        return db

    monkeypatch.setattr(db, "create_progression", progression)
    monkeypatch.setattr(db, "update_status", update)
    monkeypatch.setattr(isolated_setup, "_open_shared_db", open_db)
    task = asyncio.create_task(isolated_setup.setup_agent_persist(fake_branch(), share_db=False))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        async with StateDB(tmp_path / "state.db") as check:
            assert await check.list_sessions() == []
        assert updates == []
        assert "session setup recovery did not complete" not in caplog.text
    finally:
        gate.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if db._engine is not None:
            await db.close()


@pytest.mark.parametrize("failure", ["error", "cancel"])
@pytest.mark.asyncio
async def test_recovery_disposes_engine_when_open_fails(
    isolated_setup, monkeypatch, tmp_path, failure
):
    from lionagi.state import db as db_module

    db = db_module.StateDB(tmp_path / "state.db")
    original_open = db.open
    entered = asyncio.Event()
    gate = asyncio.Event()
    error = ValueError("partial open")

    async def failed_open():
        await original_open()
        assert db._engine is not None
        if failure == "error":
            raise error
        entered.set()
        await gate.wait()

    monkeypatch.setattr(db, "open", failed_open)
    monkeypatch.setattr(db_module, "StateDB", lambda: db)
    task = asyncio.create_task(isolated_setup.find_incomplete_session_for_run("run"))
    try:
        if failure == "cancel":
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        else:
            with pytest.raises(ValueError) as raised:
                await asyncio.wait_for(task, 5)
            assert raised.value is error
        assert db._engine is None
    finally:
        gate.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if db._engine is not None:
            await db.close()


@pytest.mark.parametrize("open_fails", [False, True])
@pytest.mark.asyncio
async def test_recovery_close_is_bounded_and_preserves_open_error(
    isolated_setup, monkeypatch, tmp_path, caplog, open_fails
):
    from lionagi.state import db as db_module

    db = db_module.StateDB(tmp_path / "state.db")
    original_open, original_close = db.open, db.close
    error = ValueError("partial open")

    async def open_db():
        await original_open()
        if open_fails:
            raise error

    async def close():
        await asyncio.Event().wait()

    monkeypatch.setattr(db, "open", open_db)
    monkeypatch.setattr(db, "close", close)
    monkeypatch.setattr(db_module, "StateDB", lambda: db)
    monkeypatch.setattr(isolated_setup, "_SETUP_RECOVERY_TIMEOUT_SECONDS", 0.05)
    try:
        if open_fails:
            with pytest.raises(ValueError) as raised:
                await asyncio.wait_for(isolated_setup.find_incomplete_session_for_run("run"), 5)
            assert raised.value is error
            assert "database close did not complete" in caplog.text
        else:
            with pytest.raises(TimeoutError, match="database close did not complete"):
                await asyncio.wait_for(isolated_setup.find_incomplete_session_for_run("run"), 5)
    finally:
        await original_close()


@pytest.mark.asyncio
async def test_admission_refusal_remains_primary_when_owner_drain_is_cancelled(
    isolated_setup, monkeypatch, tmp_path
):
    from lionagi._provider_errors import _WorkerSpawnAdmissionError
    from lionagi.state import cli_workers
    from lionagi.state.db import StateDB

    db = StateDB(tmp_path / "state.db")
    await db.open()
    refusal = _WorkerSpawnAdmissionError("original admission refusal")
    drain_calls = []

    class CancellingOwner:
        def __init__(self, *_args, **_kwargs):
            pass

        async def aclose(self):
            drain_calls.append(True)
            if len(drain_calls) == 1:
                raise asyncio.CancelledError("later drain cancellation")

    async def fail_branch(_row):
        raise refusal

    async def open_db(*, register):
        return db

    monkeypatch.setattr(cli_workers, "_WorkerSpawnOwner", CancellingOwner)
    monkeypatch.setattr(db, "create_branch", fail_branch)
    monkeypatch.setattr(isolated_setup, "_open_shared_db", open_db)
    try:
        with pytest.raises(_WorkerSpawnAdmissionError) as raised:
            await asyncio.wait_for(
                isolated_setup.setup_agent_persist(fake_branch(), share_db=False), 5
            )
        assert raised.value is refusal
        assert len(drain_calls) == 2
        assert db._engine is None
    finally:
        if db._engine is not None:
            await db.close()


@pytest.mark.asyncio
async def test_timed_out_open_still_closes_once(isolated_setup, monkeypatch):
    from lionagi.ln.concurrency import move_on_after
    from lionagi.state import db as db_module

    class BlockedStore:
        def __init__(self):
            self.opened = False
            self.closes = 0

        async def open(self):
            self.opened = True
            await asyncio.Event().wait()

        async def close(self):
            self.closes += 1

    db = BlockedStore()
    monkeypatch.setattr(db_module, "StateDB", lambda: db)
    with move_on_after(0.05) as scope:
        await isolated_setup.find_incomplete_session_for_run("run")
    assert scope.cancel_called
    assert db.opened
    assert db.closes == 1
