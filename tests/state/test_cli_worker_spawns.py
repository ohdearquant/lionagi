# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Durable worker registration, coherent snapshots and exact settlement."""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from contextlib import asynccontextmanager, closing
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text

from lionagi.state import db as db_module
from lionagi.state.compat import CompatibilityProfile, StateCompatibilityRefused, read_closure
from lionagi.state.db import StateDB

pytestmark = pytest.mark.asyncio(loop_scope="function")


@pytest_asyncio.fixture(loop_scope="function")
async def db(tmp_path):
    async with StateDB(tmp_path / "workers.db") as state:
        yield state


async def _owner(db, *, session_id="Opaque/Owner", generation=None, source_kind="live"):
    generation = str(uuid.uuid4()) if generation is None else generation
    progression = str(uuid.uuid4())
    await db.create_progression(progression)
    await db.create_session(
        {
            "id": session_id,
            "progression_id": progression,
            "status": "running",
            "invocation_kind": "agent",
            "source_kind": source_kind,
            "cli_worker_generation": generation,
        }
    )
    return session_id, generation


def _record(session_id, generation, /, **changes):
    record = {
        "spawn_id": str(uuid.uuid4()),
        "session_id": session_id,
        "generation_id": generation,
        "pid": 32123,
        "pgid": 32123,
        "pid_create_time": 123.5,
        "pid_host": "synthetic-host",
        "pid_boot_time": 100.0,
        "process_identity_mode": "local",
    }
    record.update(changes)
    return record


async def _owner_state(db, session_id):
    return {
        "session": await db.get_session(session_id),
        "history": await db.fetch_all(
            "SELECT * FROM status_transitions WHERE entity_type = 'session' "
            "AND entity_id = :id ORDER BY created_at, id",
            {"id": session_id},
        ),
    }


async def _state(db, session_id):
    return {
        **await _owner_state(db, session_id),
        "workers": await db.fetch_all("SELECT * FROM cli_worker_spawns ORDER BY spawn_id"),
    }


async def _terminal(db, session_id):
    from lionagi.state.reasons import RunReasons

    assert await db.update_status(
        "session", session_id, new_status="failed", reason_code=RunReasons.FAILED_EXCEPTION
    )


async def test_register_assigns_store_time_and_preserves_session_and_history(db, monkeypatch):
    sid, generation = await _owner(db)
    record = _record(sid, generation)
    before = await _owner_state(db, sid)
    with monkeypatch.context() as patch:
        patch.setattr(db_module, "time", SimpleNamespace(time=lambda: 456.25))
        assert await db.register_cli_worker_spawn(record) is None
    snapshot = await db.snapshot_cli_worker_spawns(sid)
    row = snapshot["spawns"][0]
    assert row["recorded_at"] == 456.25
    assert row["confirmed_empty_at"] is None
    assert all(row[key] == value for key, value in record.items())
    assert (row["run_id"], row["engine_run_id"], row["branch_id"]) == (None, None, None)
    assert await _owner_state(db, sid) == before


@pytest.mark.parametrize("state", ["running", "terminal", "new-generation"])
async def test_identical_replay_changes_no_worker_session_or_history(db, monkeypatch, state):
    sid, generation = await _owner(db)
    record = _record(sid, generation)
    await db.register_cli_worker_spawn(record)
    if state == "terminal":
        await _terminal(db, sid)
    elif state == "new-generation":
        await db.update_session(sid, cli_worker_generation=str(uuid.uuid4()))
    before = await _state(db, sid)

    def forbidden_clock():
        raise AssertionError("a durable replay must not assign a second store timestamp")

    with monkeypatch.context() as patch:
        patch.setattr(db_module, "time", SimpleNamespace(time=forbidden_clock))
        assert await db.register_cli_worker_spawn(record) is None
    assert await _state(db, sid) == before


@pytest.mark.parametrize("state", ["terminal", "new-generation", "unknown-generation", "missing"])
async def test_new_spawn_requires_running_current_owner(db, state):
    sid, generation = await _owner(db)
    if state == "terminal":
        await _terminal(db, sid)
    elif state == "new-generation":
        await db.update_session(sid, cli_worker_generation=str(uuid.uuid4()))
    elif state == "unknown-generation":
        await db.update_session(sid, cli_worker_generation=None)
    else:
        sid = "missing-owner"
    before = await _state(db, sid)
    with pytest.raises(ValueError):
        await db.register_cli_worker_spawn(_record(sid, generation))
    assert await _state(db, sid) == before


@pytest.mark.parametrize("terminal", [False, True], ids=["running", "terminal"])
async def test_conflicting_replay_is_refused_without_any_write(db, terminal):
    sid, generation = await _owner(db)
    record = _record(sid, generation)
    await db.register_cli_worker_spawn(record)
    if terminal:
        await _terminal(db, sid)
    before = await _state(db, sid)
    with pytest.raises(ValueError, match="different worker contents"):
        await db.register_cli_worker_spawn({**record, "pid_host": "different-host"})
    assert await _state(db, sid) == before


@pytest.mark.parametrize("field", ["recorded_at", "confirmed_empty_at"])
async def test_store_managed_fields_are_never_registration_inputs(db, field):
    sid, generation = await _owner(db)
    before = await _state(db, sid)
    with pytest.raises(ValueError):
        await db.register_cli_worker_spawn(_record(sid, generation, **{field: None}))
    assert await _state(db, sid) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("spawn_id", "not-uuid"),
        ("generation_id", None),
        ("session_id", ""),
        ("pid", True),
        ("pid", 2.0),
        ("pid", 1),
        ("pgid", 2),
        ("pid_create_time", float("nan")),
        ("pid_create_time", float("inf")),
        ("pid_create_time", "12"),
        ("pid_boot_time", False),
        ("pid_boot_time", float("-inf")),
        ("pid_host", " "),
        ("process_identity_mode", "remote"),
        ("run_id", ""),
        ("engine_run_id", 3),
        ("branch_id", []),
        ("unexpected", None),
    ],
    ids=[
        "bad-spawn",
        "missing-generation",
        "empty-owner",
        "bool-pid",
        "float-pid",
        "pid-one",
        "different-pgid",
        "nan-start",
        "infinite-start",
        "text-start",
        "bool-boot",
        "infinite-boot",
        "blank-host",
        "foreign-mode",
        "empty-run",
        "numeric-engine",
        "list-branch",
        "unknown-field",
    ],
)
async def test_malformed_worker_evidence_never_changes_storage(db, field, value):
    sid, generation = await _owner(db)
    before = await _state(db, sid)
    with pytest.raises(ValueError):
        await db.register_cli_worker_spawn(_record(sid, generation, **{field: value}))
    assert await _state(db, sid) == before


async def test_uuid_normalization_keeps_the_session_key_opaque(db):
    sid, generation = await _owner(db, session_id="NOT/a/UUID:Case-Sensitive")
    record = _record(sid, generation.upper(), spawn_id=str(uuid.uuid4()).upper())
    await db.register_cli_worker_spawn(record)
    snapshot = await db.snapshot_cli_worker_spawns(sid)
    assert snapshot["session"]["id"] == sid
    assert snapshot["spawns"][0]["session_id"] == sid
    assert snapshot["spawns"][0]["spawn_id"] == str(uuid.UUID(record["spawn_id"]))
    assert snapshot["spawns"][0]["generation_id"] == generation
    assert await db.snapshot_cli_worker_spawns(sid.lower()) is None


async def test_snapshot_includes_only_this_sessions_unresolved_generations(db):
    sid, old_generation = await _owner(db)
    old = _record(sid, old_generation)
    await db.register_cli_worker_spawn(old)
    new_generation = str(uuid.uuid4())
    await db.update_session(sid, cli_worker_generation=new_generation)
    current = _record(sid, new_generation)
    settled = _record(sid, new_generation)
    await db.register_cli_worker_spawn(current)
    await db.register_cli_worker_spawn(settled)
    assert await db.settle_cli_worker_spawn(settled["spawn_id"], confirmed_empty_at=500.0)
    other_sid, other_generation = await _owner(db, session_id="other-owner")
    await db.register_cli_worker_spawn(_record(other_sid, other_generation))
    snapshot = await db.snapshot_cli_worker_spawns(sid)
    assert snapshot["session"] == {
        "id": sid,
        "status": "running",
        "cli_worker_generation": new_generation,
    }
    assert {row["spawn_id"] for row in snapshot["spawns"]} == {old["spawn_id"], current["spawn_id"]}
    assert [(row["recorded_at"], row["spawn_id"]) for row in snapshot["spawns"]] == sorted(
        (row["recorded_at"], row["spawn_id"]) for row in snapshot["spawns"]
    )


async def test_legacy_null_generation_remains_unknown(db):
    sid, _ = await _owner(db)
    await db.update_session(sid, cli_worker_generation=None)
    assert await db.snapshot_cli_worker_spawns(sid) == {
        "session": {"id": sid, "status": "running", "cli_worker_generation": None},
        "spawns": [],
    }


async def test_settlement_writes_once_and_replay_preserves_both_store_timestamps(db):
    sid, generation = await _owner(db)
    record = _record(sid, generation)
    await db.register_cli_worker_spawn(record)
    owner = await _owner_state(db, sid)
    assert await db.settle_cli_worker_spawn(record["spawn_id"], confirmed_empty_at=-1.0) is True
    before = await _state(db, sid)
    assert await db.settle_cli_worker_spawn(record["spawn_id"], confirmed_empty_at=999.0) is False
    assert await db.settle_cli_worker_spawn(str(uuid.uuid4()), confirmed_empty_at=999.0) is False
    assert await db.register_cli_worker_spawn(record) is None
    assert await _state(db, sid) == before
    assert await _owner_state(db, sid) == owner
    assert (await db.snapshot_cli_worker_spawns(sid))["spawns"] == []


async def test_settled_replay_survives_parent_deletion(db):
    sid, generation = await _owner(db, source_kind="imported_fs")
    record = _record(sid, generation)
    await db.register_cli_worker_spawn(record)
    assert await db.settle_cli_worker_spawn(record["spawn_id"], confirmed_empty_at=500.0)
    assert await db.delete_imported_session(sid, require_source_kind="imported_fs")
    before = await _state(db, sid)
    assert before["session"] is None and len(before["workers"]) == 1
    assert await db.snapshot_cli_worker_spawns(sid) is None
    assert await db.register_cli_worker_spawn(record) is None
    assert await _state(db, sid) == before
    with pytest.raises(ValueError, match="different worker contents"):
        await db.register_cli_worker_spawn({**record, "pid_host": "different-host"})
    assert await _state(db, sid) == before


@pytest.mark.parametrize(
    "bad", [float("nan"), float("inf"), True, "12"], ids=["nan", "infinity", "bool", "text"]
)
async def test_invalid_settlement_timestamp_leaves_every_row_unchanged(db, bad):
    sid, generation = await _owner(db)
    record = _record(sid, generation)
    await db.register_cli_worker_spawn(record)
    before = await _state(db, sid)
    with pytest.raises(ValueError, match="confirmed_empty_at"):
        await db.settle_cli_worker_spawn(record["spawn_id"], confirmed_empty_at=bad)
    assert await _state(db, sid) == before


async def test_locked_prune_candidates_exclude_all_unresolved_generations(db):
    from lionagi.state.db import _locked_cli_worker_prune_candidates

    protected, generation = await _owner(db, session_id="protected")
    await db.register_cli_worker_spawn(_record(protected, generation))
    await db.update_session(protected, cli_worker_generation=str(uuid.uuid4()))
    eligible, other_generation = await _owner(db, session_id="eligible")
    settled = _record(eligible, other_generation)
    await db.register_cli_worker_spawn(settled)
    assert await db.settle_cli_worker_spawn(settled["spawn_id"], confirmed_empty_at=500.0)
    before = await _state(db, protected)
    async with db._tx() as conn:
        selected = await _locked_cli_worker_prune_candidates(
            conn, [protected, eligible, protected, "absent"], dialect=db.dialect
        )
        assert selected == [eligible]
    assert await _state(db, protected) == before


async def test_snapshot_refuses_malformed_stored_timestamp_without_skipping_it(db):
    sid, generation = await _owner(db)
    record = _record(sid, generation)
    await db.register_cli_worker_spawn(record)
    async with db._tx() as conn:
        await conn.execute(
            text("UPDATE cli_worker_spawns SET recorded_at = :bad WHERE spawn_id = :id"),
            {"bad": float("inf"), "id": record["spawn_id"]},
        )
    with pytest.raises(ValueError, match="recorded_at"):
        await db.snapshot_cli_worker_spawns(sid)
    assert len(await db.fetch_all("SELECT * FROM cli_worker_spawns")) == 1


@pytest.mark.parametrize("conflict", [False, True], ids=["identical", "conflicting"])
async def test_two_handles_cannot_duplicate_or_reassociate_a_spawn(tmp_path, conflict):
    path = tmp_path / "shared.db"
    async with StateDB(path) as first, StateDB(path) as second:
        sid, generation = await _owner(first)
        record = _record(sid, generation)
        other = {**record, "pid_host": "another-host"} if conflict else dict(record)
        results = await asyncio.gather(
            first.register_cli_worker_spawn(record),
            second.register_cli_worker_spawn(other),
            return_exceptions=True,
        )
        if conflict:
            assert sum(isinstance(result, ValueError) for result in results) == 1
            assert results.count(None) == 1
        else:
            assert results == [None, None]
        rows = await first.fetch_all("SELECT * FROM cli_worker_spawns")
        assert len(rows) == 1
        assert rows[0]["pid_host"] in {record["pid_host"], other["pid_host"]}


async def test_waiting_registration_rechecks_durable_replay_before_terminal_gate(
    tmp_path, monkeypatch
):
    path = tmp_path / "shared.db"
    async with StateDB(path) as waiting, StateDB(path) as winner:
        sid, generation = await _owner(waiting)
        record = _record(sid, generation)
        read_done = asyncio.Event()
        original_read = waiting._read

        @asynccontextmanager
        async def observed_read():
            async with original_read() as conn:
                yield conn
            read_done.set()

        monkeypatch.setattr(waiting, "_read", observed_read)
        task = None
        try:
            async with waiting._write_lock:
                task = asyncio.create_task(waiting.register_cli_worker_spawn(record))
                await asyncio.wait_for(read_done.wait(), timeout=3)
                await winner.register_cli_worker_spawn(record)
                await _terminal(winner, sid)
                before = await _state(winner, sid)
            assert await asyncio.wait_for(task, timeout=3) is None
            assert await _state(winner, sid) == before
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def test_snapshot_keeps_owner_and_records_in_one_committed_view(tmp_path, monkeypatch):
    path = tmp_path / "shared.db"
    async with StateDB(path) as reader, StateDB(path) as writer:
        sid, generation = await _owner(reader)
        first = _record(sid, generation)
        await reader.register_cli_worker_spawn(first)
        first_read = asyncio.Event()
        continue_read = asyncio.Event()
        original_snapshot = reader.read_snapshot

        @asynccontextmanager
        async def interleaved_snapshot():
            async with original_snapshot() as connection:

                class ObservedConnection:
                    seen = False

                    async def execute(self, *args, **kwargs):
                        result = await connection.execute(*args, **kwargs)
                        if not self.seen:
                            self.seen = True
                            first_read.set()
                            await asyncio.wait_for(continue_read.wait(), timeout=3)
                        return result

                yield ObservedConnection()

        task = None
        try:
            with monkeypatch.context() as patch:
                patch.setattr(reader, "read_snapshot", interleaved_snapshot)
                task = asyncio.create_task(reader.snapshot_cli_worker_spawns(sid))
                await asyncio.wait_for(first_read.wait(), timeout=3)
                next_generation = str(uuid.uuid4())
                await writer.update_session(sid, cli_worker_generation=next_generation)
                await writer.register_cli_worker_spawn(_record(sid, next_generation))
                continue_read.set()
                old = await asyncio.wait_for(task, timeout=3)
            assert old["session"]["cli_worker_generation"] == generation
            assert [row["spawn_id"] for row in old["spawns"]] == [first["spawn_id"]]
            current = await reader.snapshot_cli_worker_spawns(sid)
            assert current["session"]["cli_worker_generation"] == next_generation
            assert len(current["spawns"]) == 2
        finally:
            continue_read.set()
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def test_registration_budget_does_not_leave_a_late_write(db, monkeypatch):
    sid, generation = await _owner(db)
    record = _record(sid, generation)
    task = None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(db_module, "_CLI_WORKER_OPERATION_TIMEOUT_SECONDS", 0.05)
            async with db._write_lock:
                task = asyncio.create_task(db.register_cli_worker_spawn(record))
                done, _ = await asyncio.wait({task}, timeout=3)
                assert task in done, "registration must finish within its own operation budget"
                with pytest.raises(TimeoutError):
                    task.result()
        assert await db.fetch_all("SELECT * FROM cli_worker_spawns") == []
        assert await db.register_cli_worker_spawn(record) is None
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_cancelled_registration_preserves_cancellation_and_releases_waiter(db, monkeypatch):
    sid, generation = await _owner(db)
    record = _record(sid, generation)
    read_done = asyncio.Event()
    original_read = db._read

    @asynccontextmanager
    async def observed_read():
        async with original_read() as conn:
            yield conn
        read_done.set()

    monkeypatch.setattr(db, "_read", observed_read)
    task = None
    try:
        async with db._write_lock:
            task = asyncio.create_task(db.register_cli_worker_spawn(record))
            await asyncio.wait_for(read_done.wait(), timeout=3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert await db.fetch_all("SELECT * FROM cli_worker_spawns") == []
        assert await db.register_cli_worker_spawn(record) is None
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("listed", [False, True], ids=["unsupported-profile", "extended-whitelist"])
@pytest.mark.parametrize(
    "method", ["register_cli_worker_spawn", "snapshot_cli_worker_spawns", "settle_cli_worker_spawn"]
)
async def test_compatibility_refuses_before_any_connection_even_if_whitelisted(
    tmp_path, listed, method
):
    with closing(sqlite3.connect(":memory:")) as reference:
        closure = read_closure(reference, [])
    profile = CompatibilityProfile(
        name="worker-refusal",
        version=1,
        closure=closure,
        writes={},
        operations=frozenset({method} if listed else {"unrelated"}),
    )
    state = StateDB(tmp_path / "never-created.db", compatibility=profile)
    calls = {
        "register_cli_worker_spawn": lambda: state.register_cli_worker_spawn({"recorded_at": None}),
        "snapshot_cli_worker_spawns": lambda: state.snapshot_cli_worker_spawns("owner"),
        "settle_cli_worker_spawn": lambda: state.settle_cli_worker_spawn(
            str(uuid.uuid4()), confirmed_empty_at=1.0
        ),
    }
    with pytest.raises(StateCompatibilityRefused) as refused:
        await calls[method]()
    assert refused.value.operation == method
    assert state._engine is None
    assert not (tmp_path / "never-created.db").exists()
