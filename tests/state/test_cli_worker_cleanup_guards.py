# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Read-only signal admission and generation-bound lifecycle cancellation."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from contextlib import asynccontextmanager, closing
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from lionagi.state import db as db_module
from lionagi.state.compat import CompatibilityProfile, StateCompatibilityRefused, read_closure
from lionagi.state.db import StateDB
from lionagi.state.lifecycle import LifecycleValidationError, TerminalCallbackRegistry
from lionagi.state.lifecycle.models import ActorRecord, ReasonRecord, TransitionCommand
from lionagi.state.reasons import RunReasons

pytestmark = pytest.mark.asyncio(loop_scope="function")


@pytest_asyncio.fixture(params=["sqlite", "postgresql"], loop_scope="function")
async def store(request, tmp_path):
    if request.param == "postgresql":
        state = StateDB(url=request.getfixturevalue("pg_url"))
    else:
        state = StateDB(tmp_path / "cleanup.db")
    async with state:
        yield state


async def _seed(db):
    sid, generation, progression = (str(uuid.uuid4()) for _ in range(3))
    markers = {"pid": 32123, "pid_create_time": 123.5, "pid_host": "fixture-host"}
    await db.create_progression(progression)
    await db.create_session(
        {
            "id": sid,
            "progression_id": progression,
            "status": "running",
            "cli_worker_generation": generation,
            "node_metadata": markers,
            "artifacts_path": "/fixture/artifacts",
            "source_kind": "live",
        }
    )
    record = {
        "spawn_id": str(uuid.uuid4()),
        "session_id": sid,
        "generation_id": generation,
        "pid": 32123,
        "pgid": 32123,
        "pid_create_time": 123.5,
        "pid_host": "fixture-host",
        "pid_boot_time": 100.0,
        "process_identity_mode": "local",
    }
    await db.register_cli_worker_spawn(record)
    return sid, generation, record, markers


async def _state(db, sid):
    return {
        "session": await db.get_session(sid),
        "records": await db.fetch_all(
            "SELECT * FROM cli_worker_spawns WHERE session_id = :id ORDER BY spawn_id", {"id": sid}
        ),
        "history": await db.fetch_all(
            "SELECT * FROM status_transitions WHERE entity_type = 'session' AND entity_id = :id "
            "ORDER BY created_at, id",
            {"id": sid},
        ),
        "audit": await db.fetch_all(
            "SELECT * FROM admin_events WHERE target_id = :id ORDER BY created_at, id", {"id": sid}
        ),
    }


_RUNNER_FIELDS = ("pid", "pid_create_time", "pid_host", "pid_boot_time", "process_identity_mode")


def _identity(metadata):
    return {key: metadata[key] for key in _RUNNER_FIELDS if key in metadata}


async def _cancel(db, sid, generation, expected_runner_identity):
    return await db.cancel_session_if_worker_generation(
        sid,
        generation,
        expected_runner_identity=expected_runner_identity,
        reason_code=RunReasons.CANCELLED_STALE_AUTO,
        reason_summary="runner is no longer live",
        evidence={"check": "runner-not-live"},
    )


@pytest.mark.parametrize("status", ["running", "failed", "cancelled"])
async def test_signal_guard_is_read_only_and_keeps_old_record_generations(store, status):
    sid, old_generation, record, _ = await _seed(store)
    generation = str(uuid.uuid4())
    await store.update_session(sid, cli_worker_generation=generation)
    if status != "running":
        reason = RunReasons.FAILED_EXCEPTION if status == "failed" else RunReasons.CANCELLED_SYSTEM
        await store.update_status("session", sid, new_status=status, reason_code=reason)
    before = await _state(store, sid)
    actions = []
    async with store.guard_cli_worker_signal(sid, status, generation, record["spawn_id"]) as state:
        if state == "unchanged":
            actions.append("signal")
    assert actions == ["signal"]
    assert old_generation != generation
    assert await _state(store, sid) == before


@pytest.mark.parametrize(
    "change", ["generation", "status", "settled", "missing-record", "missing-parent"]
)
async def test_signal_guard_refuses_a_changed_snapshot_without_writes(store, change):
    sid, generation, record, _ = await _seed(store)
    if change == "generation":
        await store.update_session(sid, cli_worker_generation=str(uuid.uuid4()))
    elif change == "status":
        await store.update_status(
            "session", sid, new_status="failed", reason_code=RunReasons.FAILED_EXCEPTION
        )
    elif change == "settled":
        await store.settle_cli_worker_spawn(record["spawn_id"], confirmed_empty_at=456.0)
    elif change == "missing-record":
        await store.execute(
            "DELETE FROM cli_worker_spawns WHERE spawn_id = :id",
            {"id": record["spawn_id"]},
        )
    else:
        await store.execute("DELETE FROM sessions WHERE id = :id", {"id": sid})
    before = await _state(store, sid)
    actions = []
    async with store.guard_cli_worker_signal(
        sid, "running", generation, record["spawn_id"]
    ) as state:
        assert state == "changed"
        if state == "unchanged":
            actions.append("signal")
    assert actions == []
    assert await _state(store, sid) == before


async def test_signal_guard_does_not_adopt_another_sessions_record(store):
    sid, generation, _, _ = await _seed(store)
    other_sid, _, other_record, _ = await _seed(store)
    before = await _state(store, sid)
    other_before = await _state(store, other_sid)
    async with store.guard_cli_worker_signal(
        sid, "running", generation, other_record["spawn_id"]
    ) as state:
        assert state == "changed"
    assert await _state(store, sid) == before
    assert await _state(store, other_sid) == other_before


async def test_cancellation_owns_one_transition_and_one_terminal_event(store):
    sid, generation, _, markers = await _seed(store)
    await store.update_session(sid, current_phase="executing")
    received = []
    registry = TerminalCallbackRegistry()
    registry.register("collector", lambda envelope: received.append(envelope))
    store._lifecycle_service()._terminal_callbacks = registry
    before = await _state(store, sid)
    assert await _cancel(store, sid, generation, _identity(markers)) == "applied"
    after = await _state(store, sid)
    assert after["session"]["status"] == "cancelled"
    assert after["session"]["current_phase"] is None
    assert after["session"]["cli_worker_generation"] == generation
    assert after["session"]["status_reason_code"] == RunReasons.CANCELLED_STALE_AUTO
    assert after["session"]["status_evidence_refs"] == [{"check": "runner-not-live"}]
    assert after["records"] == before["records"]
    assert len(after["history"]) == len(before["history"]) + 1
    assert len(received) == 1
    assert received[0].previous_status == "running"
    assert received[0].terminal_status == "cancelled"
    assert {row["id"] for row in after["history"]} - {row["id"] for row in before["history"]} == {
        received[0].event_id
    }
    # The same execution is already recorded as ended: nothing is written a second time.
    assert await _cancel(store, sid, generation, _identity(markers)) == "already_terminal"
    assert await _state(store, sid) == after
    assert len(received) == 1


async def test_resume_between_runner_snapshot_and_cancel_loses_without_a_write(store):
    sid, generation, record, markers = await _seed(store)
    snapshot = await store.snapshot_cli_worker_spawns(sid)
    assert snapshot["session"]["node_metadata"] == markers
    other = StateDB(url=store.url)
    async with other:
        resumed_generation = str(uuid.uuid4())
        await other.update_session(sid, cli_worker_generation=resumed_generation)
        before = await _state(other, sid)
        assert (
            await _cancel(
                store,
                sid,
                snapshot["session"]["cli_worker_generation"],
                _identity(snapshot["session"]["node_metadata"]),
            )
            == "conflict"
        )
        actions = []
        async with store.guard_cli_worker_signal(
            sid, "running", generation, record["spawn_id"]
        ) as state:
            if state == "unchanged":
                actions.append("signal")
        assert actions == []
        assert await _state(other, sid) == before


@pytest.mark.parametrize("writer", ["update-metadata", "merge-metadata"])
@pytest.mark.parametrize("same_time", [False, True], ids=["later-time", "same-time"])
async def test_runner_marker_swap_conflicts_at_same_generation(
    store, monkeypatch, writer, same_time
):
    sid, generation, _, _ = await _seed(store)
    version = (await store.get_session(sid))["updated_at"]
    snapshot = await store.snapshot_cli_worker_spawns(sid)
    captured = _identity(snapshot["session"]["node_metadata"])
    replacement = {"pid": 45678, "pid_create_time": 444.5, "pid_host": "other-host"}
    # One arm keeps updated_at equal to the snapshot, so only the runner identity can refuse.
    with monkeypatch.context() as context:
        context.setattr(
            db_module, "time", SimpleNamespace(time=lambda: version if same_time else version + 1.0)
        )
        if writer == "update-metadata":
            await store.update_session(sid, node_metadata=json.dumps(replacement))
        else:
            await store.merge_session_node_metadata(sid, replacement)
    before = await _state(store, sid)
    assert before["session"]["updated_at"] == (version if same_time else version + 1.0)
    assert before["session"]["cli_worker_generation"] == generation
    assert before["session"]["node_metadata"]["pid"] == replacement["pid"]
    assert await _cancel(store, sid, generation, captured) == "conflict"
    assert await _state(store, sid) == before


async def test_unrelated_metadata_change_does_not_refuse_cancellation(store):
    sid, generation, _, markers = await _seed(store)
    captured = _identity(markers)
    await store.merge_session_node_metadata(sid, {"unrelated": "changed"})
    assert await _cancel(store, sid, generation, captured) == "applied"
    session = await store.get_session(sid)
    assert session["status"] == "cancelled"
    assert session["node_metadata"]["unrelated"] == "changed"


@pytest.mark.parametrize("change", ["add-null", "remove-null", "bool-for-number"])
async def test_absent_null_and_boolean_marker_changes_conflict(store, change):
    sid, generation, _, markers = await _seed(store)
    if change == "remove-null":
        markers["pid_boot_time"] = None
    if change == "bool-for-number":
        markers["pid_create_time"] = 1
    await store.update_session(sid, node_metadata=json.dumps(markers))
    captured = _identity(markers)
    replacement = dict(markers)
    if change == "add-null":
        replacement["pid_boot_time"] = None
    elif change == "remove-null":
        del replacement["pid_boot_time"]
    else:
        replacement["pid_create_time"] = True
    await store.update_session(sid, node_metadata=json.dumps(replacement))
    before = await _state(store, sid)
    assert await _cancel(store, sid, generation, captured) == "conflict"
    assert await _state(store, sid) == before


async def test_equivalent_json_integer_and_float_marker_is_accepted(store):
    sid, generation, _, markers = await _seed(store)
    markers["pid_create_time"] = 1
    await store.update_session(sid, node_metadata=json.dumps(markers))
    captured = _identity(markers)
    replacement = dict(markers, pid_create_time=1.0)
    await store.update_session(sid, node_metadata=json.dumps(replacement))
    assert await _cancel(store, sid, generation, captured) == "applied"


@pytest.mark.parametrize(
    ("dialect", "stored", "matches"),
    [
        ("sqlite", '{"pid": 123}', True),
        ("sqlite", json.dumps('{"pid": 123}'), False),
        ("postgresql", {"pid": 123}, True),
        ("postgresql", '{"pid": 123}', False),
    ],
)
async def test_runner_guard_respects_driver_json_decoding(dialect, stored, matches):
    from lionagi.state.lifecycle.service import _runner_identity_matches

    assert _runner_identity_matches({"pid": 123}, stored, dialect=dialect) is matches


@pytest.mark.parametrize(("dialect", "stored"), [("sqlite", "null"), ("postgresql", None)])
@pytest.mark.parametrize(("captured", "matches"), [({}, True), ({"pid": 123}, False)])
async def test_null_metadata_is_a_record_with_no_runner_identity(
    dialect, stored, captured, matches
):
    from lionagi.state.lifecycle.service import _runner_identity_matches

    assert _runner_identity_matches(captured, stored, dialect=dialect) is matches


async def test_json_scalar_containing_marker_object_never_authorizes_cancellation(store):
    sid, generation, _, markers = await _seed(store)
    captured = _identity(markers)
    # The stored JSON value is a string containing valid object text, not the
    # object itself. Asyncpg decodes the string once before SQLAlchemy sees it.
    object_text = json.dumps(markers)
    await store.update_session(sid, node_metadata=json.dumps(object_text))
    async with store.read_snapshot() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT node_metadata FROM sessions WHERE id = :id"), {"id": sid}
                )
            )
            .mappings()
            .first()
        )
    if store.dialect == "postgresql":
        assert row["node_metadata"] == object_text
    else:
        assert json.loads(row["node_metadata"]) == object_text
    before = await _state(store, sid)
    assert await _cancel(store, sid, generation, captured) == "conflict"
    assert await _state(store, sid) == before


@pytest.mark.parametrize("stored", [json.dumps("not-json"), json.dumps([]), json.dumps(None)])
async def test_nonobject_metadata_cannot_authorize_cancellation(store, stored):
    sid, generation, _, markers = await _seed(store)
    captured = _identity(markers)
    await store.update_session(sid, node_metadata=stored)
    before = await _state(store, sid)
    assert await _cancel(store, sid, generation, captured) == "conflict"
    assert await _state(store, sid) == before


async def test_sqlite_raw_invalid_json_cannot_authorize_cancellation(tmp_path):
    async with StateDB(tmp_path / "invalid-marker.db") as db:
        sid, generation, _, markers = await _seed(db)
        captured = _identity(markers)
        await db.update_session(sid, node_metadata="{invalid")
        before = await _state(db, sid)
        assert await _cancel(db, sid, generation, captured) == "conflict"
        assert await _state(db, sid) == before


@pytest.mark.parametrize("bad", [None, [], "not-a-mapping"])
async def test_invalid_captured_identity_cannot_disable_guard(store, bad):
    sid, generation, _, _ = await _seed(store)
    before = await _state(store, sid)
    with pytest.raises(ValueError, match="expected_runner_identity"):
        await _cancel(store, sid, generation, bad)
    assert await _state(store, sid) == before


async def test_empty_identity_means_all_marker_keys_absent(store):
    sid, generation, _, _ = await _seed(store)
    before = await _state(store, sid)
    assert await _cancel(store, sid, generation, {}) == "conflict"
    assert await _state(store, sid) == before
    await store.update_session(sid, node_metadata=json.dumps({"unrelated": "retained"}))
    assert await _cancel(store, sid, generation, {}) == "applied"


async def test_unknown_captured_marker_field_is_rejected(store):
    sid, generation, _, markers = await _seed(store)
    before = await _state(store, sid)
    captured = dict(_identity(markers), unknown_marker="cannot-authorize")
    assert await _cancel(store, sid, generation, captured) == "rejected"
    assert await _state(store, sid) == before


@pytest.mark.parametrize("entity_type", ["schedule", "show"])
async def test_runner_guard_never_silently_ignores_a_non_session_command(store, entity_type):
    sid, _, _, _ = await _seed(store)
    before = await _state(store, sid)
    command = TransitionCommand(
        entity_type=entity_type,
        entity_id="unused",
        to_status="cancelled",
        reason=ReasonRecord(code=RunReasons.CANCELLED_STALE_AUTO),
        actor=ActorRecord(type="admin", id="user"),
    )
    with pytest.raises(LifecycleValidationError, match="requires a session"):
        await store._lifecycle_service()._transition(command, expected_runner_identity={})
    assert await _state(store, sid) == before


async def test_missing_and_typed_rejection_do_not_write(store, monkeypatch):
    sid, generation, _, markers = await _seed(store)
    assert await _cancel(store, "absent-" + sid, generation, {}) == "missing"
    before = await _state(store, sid)

    async def rejected(*args, **kwargs):
        raise LifecycleValidationError("the command is rejected")

    monkeypatch.setattr(store._lifecycle_service(), "_transition", rejected)
    assert await _cancel(store, sid, generation, _identity(markers)) == "rejected"
    assert await _state(store, sid) == before


async def test_cancellation_does_not_hide_storage_or_timeout_errors(store, monkeypatch):
    sid, generation, _, markers = await _seed(store)
    before = await _state(store, sid)

    async def failed(*args, **kwargs):
        raise TimeoutError("store operation timed out")

    monkeypatch.setattr(store._lifecycle_service(), "_transition", failed)
    with pytest.raises(TimeoutError, match="store operation timed out"):
        await _cancel(store, sid, generation, _identity(markers))
    assert await _state(store, sid) == before


async def test_sqlite_guard_budget_expires_before_its_body(tmp_path, monkeypatch):
    async with StateDB(tmp_path / "budget.db") as db:
        sid, generation, record, _ = await _seed(db)
        before = await _state(db, sid)
        entered = []

        async def waiting():
            async with db.guard_cli_worker_signal(sid, "running", generation, record["spawn_id"]):
                entered.append(True)

        monkeypatch.setattr(db_module, "_CLI_WORKER_OPERATION_TIMEOUT_SECONDS", 0.05)
        async with db._write_lock:
            task = asyncio.create_task(waiting())
            try:
                done, _ = await asyncio.wait({task}, timeout=0.5)
                assert task in done, "the guard did not enforce its own deadline"
                with pytest.raises(TimeoutError):
                    task.result()
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        assert entered == []
        assert await _state(db, sid) == before


async def test_postgres_guard_has_bounded_parent_lock_wait(pg_url):
    async with StateDB(url=pg_url) as holder, StateDB(url=pg_url) as waiting:
        sid, generation, record, _ = await _seed(holder)
        entered = []
        async with holder._tx() as conn:
            await conn.execute(
                text("SELECT id FROM sessions WHERE id = :id FOR UPDATE"), {"id": sid}
            )
            with pytest.raises(DBAPIError, match="lock timeout"):
                async with waiting.guard_cli_worker_signal(
                    sid, "running", generation, record["spawn_id"]
                ):
                    entered.append(True)
        assert entered == []
        async with waiting.guard_cli_worker_signal(
            sid, "running", generation, record["spawn_id"]
        ) as state:
            assert state == "unchanged"


async def test_guard_sql_uses_the_native_writer_lock_and_does_not_update(store):
    sid, generation, record, _ = await _seed(store)
    sql = []

    def observed(conn, cursor, statement, parameters, context, executemany):
        sql.append(statement.strip().upper())

    event.listen(store._engine.sync_engine, "before_cursor_execute", observed)
    try:
        async with store.guard_cli_worker_signal(
            sid, "running", generation, record["spawn_id"]
        ) as state:
            assert state == "unchanged"
    finally:
        event.remove(store._engine.sync_engine, "before_cursor_execute", observed)
    assert not any(item.startswith(("UPDATE", "INSERT", "DELETE")) for item in sql)
    if store.dialect == "sqlite":
        assert "BEGIN IMMEDIATE" in sql
    else:
        assert "SET LOCAL LOCK_TIMEOUT = '250MS'" in sql
        assert sum("FOR UPDATE" in item for item in sql) == 2


async def test_runner_markers_and_generation_share_the_record_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "snapshot.db"
    async with StateDB(path) as reader, StateDB(path) as writer:
        sid, generation, record, markers = await _seed(reader)
        original = reader.read_snapshot
        first_read, resume = asyncio.Event(), asyncio.Event()

        @asynccontextmanager
        async def interleaved():
            async with original() as connection:

                class Observed:
                    seen = False

                    async def execute(self, *args, **kwargs):
                        result = await connection.execute(*args, **kwargs)
                        if not self.seen:
                            self.seen = True
                            first_read.set()
                            await resume.wait()
                        return result

                yield Observed()

        monkeypatch.setattr(reader, "read_snapshot", interleaved)
        task = asyncio.create_task(reader.snapshot_cli_worker_spawns(sid))
        try:
            await asyncio.wait_for(first_read.wait(), 3)
            next_generation = str(uuid.uuid4())
            await writer.update_session(
                sid, cli_worker_generation=next_generation, node_metadata=json.dumps({"pid": 45678})
            )
            resume.set()
            snapshot = await asyncio.wait_for(task, 3)
            assert snapshot["session"]["cli_worker_generation"] == generation
            assert snapshot["session"]["node_metadata"] == markers
            assert snapshot["session"]["artifacts_path"] == "/fixture/artifacts"
            assert snapshot["session"]["source_kind"] == "live"
            assert [row["spawn_id"] for row in snapshot["spawns"]] == [record["spawn_id"]]
            current = await writer.get_session(sid)
            assert current["cli_worker_generation"] == next_generation
            assert current["node_metadata"] == {"pid": 45678}
        finally:
            resume.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("method", ["guard", "cancel"])
async def test_compatibility_refuses_before_open_even_if_the_operation_is_named(tmp_path, method):
    with closing(sqlite3.connect(":memory:")) as reference:
        closure = read_closure(reference, [])
    operation = (
        "guard_cli_worker_signal" if method == "guard" else "cancel_session_if_worker_generation"
    )
    profile = CompatibilityProfile(
        name="cleanup-refusal",
        version=1,
        closure=closure,
        writes={},
        operations=frozenset({operation}),
    )
    state = StateDB(tmp_path / "never-created.db", compatibility=profile)
    with pytest.raises(StateCompatibilityRefused) as info:
        if method == "guard":
            async with state.guard_cli_worker_signal("session", "running", None, str(uuid.uuid4())):
                pytest.fail("compatibility storage admitted a signal")
        else:
            await _cancel(state, "session", None, {})
    assert info.value.operation == operation
    assert state._engine is None
    assert not (tmp_path / "never-created.db").exists()


async def test_readonly_snapshot_decodes_markers_but_cannot_authorize_a_signal(tmp_path):
    path = tmp_path / "readonly.db"
    async with StateDB(path) as writer:
        sid, generation, record, markers = await _seed(writer)
    async with StateDB(path, readonly=True) as reader:
        snapshot = await reader.snapshot_cli_worker_spawns(sid)
        assert snapshot["session"]["node_metadata"] == markers
        with pytest.raises(ValueError, match="writable"):
            async with reader.guard_cli_worker_signal(
                sid, "running", generation, record["spawn_id"]
            ):
                pytest.fail("a read-only handle admitted a signal")


async def test_sqlite_cancellation_budget_has_no_late_transition(tmp_path, monkeypatch):
    async with StateDB(tmp_path / "cancel-budget.db") as db:
        sid, generation, _, markers = await _seed(db)
        before = await _state(db, sid)
        monkeypatch.setattr(db_module, "_CLI_WORKER_OPERATION_TIMEOUT_SECONDS", 0.05)
        async with db._write_lock:
            task = asyncio.create_task(_cancel(db, sid, generation, _identity(markers)))
            try:
                done, _ = await asyncio.wait({task}, timeout=0.5)
                assert task in done, "the cancellation did not enforce its own deadline"
                with pytest.raises(TimeoutError):
                    task.result()
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        assert await _state(db, sid) == before


async def test_cancelled_guard_never_enters_its_body_or_writes(tmp_path):
    async with StateDB(tmp_path / "interrupted.db") as db:
        sid, generation, record, _ = await _seed(db)
        before = await _state(db, sid)
        entered = []

        async def waiting():
            async with db.guard_cli_worker_signal(sid, "running", generation, record["spawn_id"]):
                entered.append(True)

        async with db._write_lock:
            task = asyncio.create_task(waiting())
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert entered == []
        assert await _state(db, sid) == before


async def test_malformed_stored_record_is_not_signal_authority(store):
    sid, generation, record, _ = await _seed(store)
    await store.execute(
        "UPDATE cli_worker_spawns SET recorded_at = :at WHERE spawn_id = :id",
        {"at": float("inf"), "id": record["spawn_id"]},
    )
    before = await _state(store, sid)
    entered = []
    with pytest.raises(ValueError, match="recorded_at"):
        async with store.guard_cli_worker_signal(sid, "running", generation, record["spawn_id"]):
            entered.append(True)
    assert entered == []
    assert await _state(store, sid) == before
