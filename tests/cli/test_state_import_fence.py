# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Atomic run imports preserve shared messages and concurrent admission."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from contextlib import closing

import pytest
from sqlalchemy import event, text

from lionagi.cli import state as state_mod
from lionagi.state import db as db_mod
from lionagi.state import engine as engine_mod
from lionagi.state.compat import CompatibilityProfile, StateCompatibilityRefused, read_closure
from lionagi.state.db import StateDB


def _message(message_id: str, value: str, created_at: float = 1.0) -> dict:
    return {
        "id": message_id,
        "created_at": created_at,
        "role": "user",
        "content": {"text": value},
    }


def _run(root, *, branches=None, status="completed"):
    run = root / "run-1"
    branch_dir = run / "branches"
    branch_dir.mkdir(parents=True)
    manifest = {"run_id": "run-1", "kind": "agent", "status": status}
    (run / "run.json").write_text(json.dumps(manifest))
    if branches is None:
        branches = [[_message("fresh", "new")], [_message("shared", "replacement")]]
    for i, messages in enumerate(branches):
        path = branch_dir / f"branch-{i}.json"
        path.write_text(json.dumps({"id": f"branch-{i}", "messages": {"collections": messages}}))
        os.utime(path, (1_000_000 + i, 1_000_000 + i))
    return run, manifest


async def _seed_keeper(db):
    await db.create_progression("keeper-progress")
    await db.create_session(
        {"id": "keeper", "progression_id": "keeper-progress", "status": "running"}
    )
    await db.insert_message(_message("shared", "original"))


async def _snapshot(db):
    return {
        table: await db.fetch_all(f"SELECT * FROM {table} ORDER BY id")  # noqa: S608
        for table in ("sessions", "branches", "progressions", "messages", "status_transitions")
    }


@pytest.mark.asyncio
async def test_conflict_leaves_no_run_and_can_be_retried(tmp_path):
    run, manifest = _run(tmp_path / "runs")
    async with StateDB(tmp_path / "state.db") as db:
        await _seed_keeper(db)
        await db.append_to_progression("keeper-progress", "shared")
        before = await _snapshot(db)
        with pytest.raises(state_mod.ImportMessageConflictError) as caught:
            await state_mod._import_one_run(db, "run-1", run, manifest)
        assert caught.value.message_id == "shared"
        assert await _snapshot(db) == before
        assert (await db.get_message("shared"))["content"] == {"text": "original"}

        await db.set_progression("keeper-progress", [])
        assert await state_mod._import_one_run(db, "run-1", run, manifest) == (1, 2, 2)
        assert (await db.get_message("shared"))["content"] == {"text": "replacement"}
        session = await db.get_session("run-1")
        assert await db.get_progression(session["progression_id"]) == ["fresh", "shared"]
        assert await db.get_branch("branch-0") is not None
        assert await db.get_branch("branch-1") is not None


@pytest.mark.asyncio
async def test_existing_run_skips_before_a_shared_conflict_and_writes_nothing(tmp_path):
    run, manifest = _run(tmp_path / "runs")
    async with StateDB(tmp_path / "state.db") as db:
        await _seed_keeper(db)
        await db.append_to_progression("keeper-progress", "shared")
        await db.create_progression("existing-run-progress")
        await db.create_session(
            {"id": "run-1", "progression_id": "existing-run-progress", "status": "running"}
        )
        before = await _snapshot(db)
        writes = []

        def record_write(_conn, _cursor, statement, _parameters, _context, _executemany):
            if re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE)\b", statement, re.I):
                writes.append(statement)

        event.listen(db._engine.sync_engine, "before_cursor_execute", record_write)
        try:
            assert await state_mod._import_one_run(db, "run-1", run, manifest) == (0, 0, 0)
        finally:
            event.remove(db._engine.sync_engine, "before_cursor_execute", record_write)
        assert writes == []
        assert await _snapshot(db) == before
        assert (await db.get_message("shared"))["content"] == {"text": "original"}


@pytest.mark.asyncio
async def test_attach_waits_for_open_import_transaction(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_mod, "SQLITE_BUSY_TIMEOUT_MS", 2_000)
    run, manifest = _run(tmp_path / "runs")
    async with StateDB(tmp_path / "state.db") as importer, StateDB(tmp_path / "state.db") as writer:
        await _seed_keeper(writer)
        paused = asyncio.Event()
        resume = asyncio.Event()
        attempted = asyncio.Event()
        original = importer._insert_import_message_if_absent_in_tx

        async def pause_first_insert(conn, message):
            if not paused.is_set():
                paused.set()
                await asyncio.wait_for(resume.wait(), 5)
            return await original(conn, message)

        monkeypatch.setattr(importer, "_insert_import_message_if_absent_in_tx", pause_first_insert)

        def observe_begin(_conn, _cursor, statement, _parameters, _context, _executemany):
            if statement.lstrip().upper().startswith("BEGIN IMMEDIATE"):
                attempted.set()

        event.listen(writer._engine.sync_engine, "before_cursor_execute", observe_begin)
        import_task = asyncio.create_task(
            state_mod._import_one_run(importer, "run-1", run, manifest)
        )
        writer_task = None
        try:
            await asyncio.wait_for(paused.wait(), 5)
            writer_task = asyncio.create_task(
                writer.append_to_progression("keeper-progress", "shared")
            )
            await asyncio.wait_for(attempted.wait(), 5)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(writer_task), 0.1)
            resume.set()
            assert await asyncio.wait_for(import_task, 5) == (1, 2, 2)
            await asyncio.wait_for(writer_task, 5)
        finally:
            resume.set()
            for task in (import_task, writer_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(t for t in (import_task, writer_task) if t is not None), return_exceptions=True
            )
            event.remove(writer._engine.sync_engine, "before_cursor_execute", observe_begin)
        assert await writer.get_progression("keeper-progress") == ["shared"]
        assert (await writer.get_message("shared"))["content"] == {"text": "replacement"}


@pytest.mark.asyncio
async def test_second_message_failure_rolls_back_run_and_lifecycle(tmp_path):
    run, manifest = _run(
        tmp_path / "runs",
        branches=[[_message("first", "one"), _message("second", "two")]],
        status="running",
    )

    class InjectedFailure(Exception):
        pass

    async with StateDB(tmp_path / "state.db") as db:
        before = await _snapshot(db)
        inserts = 0

        def fail_second_message(_conn, _cursor, statement, _parameters, _context, _executemany):
            nonlocal inserts
            if re.match(r"\s*INSERT(?:\s+OR\s+\w+)?\s+INTO\s+messages\b", statement, re.I):
                inserts += 1
                if inserts == 2:
                    raise InjectedFailure("second message")

        event.listen(db._engine.sync_engine, "before_cursor_execute", fail_second_message)
        try:
            with pytest.raises(InjectedFailure):
                await state_mod._import_one_run(db, "run-1", run, manifest)
        finally:
            event.remove(db._engine.sync_engine, "before_cursor_execute", fail_second_message)
        assert inserts == 2
        assert await _snapshot(db) == before


@pytest.mark.asyncio
async def test_late_conflict_rolls_back_admission_then_retry_imports(tmp_path, monkeypatch):
    run, manifest = _run(tmp_path / "runs", status="running")
    async with StateDB(tmp_path / "state.db") as db:
        before = await _snapshot(db)
        original = db._insert_import_message_if_absent_in_tx
        conflict_reached = False

        async def refuse_second(conn, message):
            nonlocal conflict_reached
            if message["id"] == "shared":
                assert (
                    await conn.execute(text("SELECT id FROM sessions WHERE id = 'run-1'"))
                ).first()
                assert (
                    await conn.execute(
                        text("SELECT id FROM status_transitions WHERE entity_id = 'run-1'")
                    )
                ).first()
                assert (
                    await conn.execute(text("SELECT id FROM messages WHERE id = 'fresh'"))
                ).first()
                assert (
                    await conn.execute(text("SELECT id FROM branches WHERE id = 'branch-0'"))
                ).first()
                conflict_reached = True
                raise state_mod.ImportMessageConflictError("shared", ("content",))
            return await original(conn, message)

        monkeypatch.setattr(db, "_insert_import_message_if_absent_in_tx", refuse_second)
        with pytest.raises(state_mod.ImportMessageConflictError) as caught:
            await state_mod._import_one_run(db, "run-1", run, manifest)
        assert conflict_reached
        assert caught.value.message_id == "shared"
        assert await _snapshot(db) == before

        monkeypatch.setattr(db, "_insert_import_message_if_absent_in_tx", original)
        assert await state_mod._import_one_run(db, "run-1", run, manifest) == (1, 2, 2)
        session = await db.get_session("run-1")
        assert await db.get_progression(session["progression_id"]) == ["fresh", "shared"]
        assert await db.get_branch("branch-0") is not None
        assert await db.get_branch("branch-1") is not None
        assert await db.fetch_one(
            "SELECT id FROM status_transitions WHERE entity_type = 'session' AND entity_id = ?",
            ("run-1",),
        )


@pytest.mark.asyncio
async def test_insert_if_absent_preserves_existing_message(tmp_path):
    async with StateDB(tmp_path / "state.db") as db:
        await db.insert_message(_message("shared", "original"))
        before = await db.get_message("shared")
        async with db._tx() as conn:
            inserted = await db._insert_import_message_if_absent_in_tx(
                conn, _message("shared", "replacement", created_at=23)
            )
        assert inserted is False
        assert await db.get_message("shared") == before


@pytest.mark.asyncio
async def test_identical_duplicates_compare_and_insert_once(tmp_path, monkeypatch):
    run, manifest = _run(
        tmp_path / "runs",
        branches=[
            [_message("shared", "original"), _message("fresh", "new")],
            [_message("shared", "original"), _message("fresh", "new")],
        ],
    )
    async with StateDB(tmp_path / "state.db") as db:
        await _seed_keeper(db)
        await db.append_to_progression("keeper-progress", "shared")
        shared_before = await db.get_message("shared")
        insert_ids = []
        compare_ids = []
        original_insert = db._insert_import_message_if_absent_in_tx
        original_compare = state_mod._import_message_differences

        async def record_insert(conn, message):
            insert_ids.append(message["id"])
            return await original_insert(conn, message)

        def record_compare(existing, message):
            compare_ids.append(message["id"])
            return original_compare(existing, message)

        monkeypatch.setattr(db, "_insert_import_message_if_absent_in_tx", record_insert)
        monkeypatch.setattr(state_mod, "_import_message_differences", record_compare)
        assert await state_mod._import_one_run(db, "run-1", run, manifest) == (1, 2, 4)
        assert insert_ids == ["fresh"]
        assert compare_ids == ["shared"]
        assert await db.get_message("shared") == shared_before
        session = await db.get_session("run-1")
        assert await db.get_progression(session["progression_id"]) == [
            "shared",
            "fresh",
            "shared",
            "fresh",
        ]


@pytest.mark.asyncio
async def test_duplicate_ids_preserve_positions_and_upsert_order(tmp_path):
    run, manifest = _run(
        tmp_path / "runs",
        branches=[[_message("dup", "first", 11)], [_message("dup", "last", 22)]],
    )
    async with StateDB(tmp_path / "state.db") as db:
        assert await state_mod._import_one_run(db, "run-1", run, manifest) == (1, 2, 2)
        message = await db.get_message("dup")
        assert message["created_at"] == 11
        assert message["content"] == {"text": "last"}
        session = await db.get_session("run-1")
        assert await db.get_progression(session["progression_id"]) == ["dup", "dup"]
        for branch_id in ("branch-0", "branch-1"):
            branch = await db.get_branch(branch_id)
            assert await db.get_progression(branch["progression_id"]) == ["dup"]


@pytest.mark.asyncio
async def test_ordered_duplicates_select_last_collection_entry(tmp_path):
    run, manifest = _run(
        tmp_path / "runs",
        branches=[
            [_message("dup", "first", 11), _message("other", "middle"), _message("dup", "last", 22)]
        ],
    )
    path = run / "branches" / "branch-0.json"
    data = json.loads(path.read_text())
    data["messages"]["progression"] = {"order": ["dup", "other", "dup"]}
    path.write_text(json.dumps(data))
    async with StateDB(tmp_path / "state.db") as db:
        assert await state_mod._import_one_run(db, "run-1", run, manifest) == (1, 1, 3)
        message = await db.get_message("dup")
        assert message["created_at"] == 22
        assert message["content"] == {"text": "last"}
        session = await db.get_session("run-1")
        branch = await db.get_branch("branch-0")
        assert await db.get_progression(session["progression_id"]) == ["dup", "other", "dup"]
        assert await db.get_progression(branch["progression_id"]) == ["dup", "other", "dup"]


@pytest.mark.asyncio
async def test_concurrent_imports_admit_one_run_and_skip_the_other(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_mod, "SQLITE_BUSY_TIMEOUT_MS", 2_000)
    runs = tmp_path / "runs"
    _run(runs, status="running")
    path = tmp_path / "state.db"
    async with StateDB(path):
        pass
    monkeypatch.setattr(state_mod, "RUNS_ROOT", runs)
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    original = StateDB.get_session
    both_checked = asyncio.Event()
    read_count = 0

    async def simultaneous_preflight(self, session_id):
        nonlocal read_count
        result = await original(self, session_id)
        if session_id == "run-1" and result is None and read_count < 2:
            read_count += 1
            if read_count == 2:
                both_checked.set()
            await asyncio.wait_for(both_checked.wait(), 5)
        return result

    monkeypatch.setattr(StateDB, "get_session", simultaneous_preflight)
    results = await asyncio.wait_for(
        asyncio.gather(state_mod._import_runs(), state_mod._import_runs()), 10
    )
    assert read_count == 2
    assert sorted(result["sessions"] for result in results) == [0, 1]
    assert sorted(result["skipped"] for result in results) == [0, 1]
    assert all(result["errors"] == 0 for result in results)
    assert sum(result["branches"] for result in results) == 2
    assert sum(result["messages"] for result in results) == 2

    async with StateDB(path) as db:
        sessions = await db.fetch_all("SELECT id, progression_id FROM sessions")
        branches = await db.fetch_all("SELECT id, progression_id FROM branches ORDER BY id")
        progressions = await db.fetch_all("SELECT id FROM progressions")
        messages = await db.fetch_all("SELECT id FROM messages ORDER BY id")
        transitions = await db.fetch_all(
            "SELECT entity_id FROM status_transitions WHERE entity_type = 'session'"
        )
        assert [row["id"] for row in sessions] == ["run-1"]
        assert [row["id"] for row in branches] == ["branch-0", "branch-1"]
        assert {row["id"] for row in progressions} == {
            sessions[0]["progression_id"],
            *(row["progression_id"] for row in branches),
        }
        assert len(progressions) == 3
        assert [row["id"] for row in messages] == ["fresh", "shared"]
        assert transitions == [{"entity_id": "run-1"}]
        assert await db.get_progression(sessions[0]["progression_id"]) == ["fresh", "shared"]


@pytest.mark.asyncio
async def test_membership_expands_other_progressions_once_for_the_run(tmp_path):
    messages = [_message(f"candidate-{i:04}", "same") for i in range(1001)]
    run, manifest = _run(tmp_path / "runs", branches=[messages])
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_progression("keeper-progress", ["unrelated"])
        await db.create_session({"id": "keeper", "progression_id": "keeper-progress"})
        await db.insert_messages(messages)
        expansions = []
        rewrites = []

        def count_expansion(value):
            expansions.append(value)
            return value

        async with db._engine.connect() as conn:
            raw = await conn.get_raw_connection()
            await raw.driver_connection.create_function("count_import_json", 1, count_expansion)

        pattern = re.compile(r"json_each\(\s*([a-z_]\w*\.collection)\s*\)", re.I)

        def observe_expansion(_conn, _cursor, statement, parameters, _context, _executemany):
            statement, count = pattern.subn(r"json_each(count_import_json(\1))", statement)
            if count:
                rewrites.append(count)
            return statement, parameters

        event.listen(
            db._engine.sync_engine, "before_cursor_execute", observe_expansion, retval=True
        )
        try:
            assert await state_mod._import_one_run(db, "run-1", run, manifest) == (1, 1, 1001)
        finally:
            event.remove(db._engine.sync_engine, "before_cursor_execute", observe_expansion)
        assert rewrites == [1]
        assert len(expansions) == 1
        assert json.loads(expansions[0]) == ["unrelated"]


@pytest.mark.asyncio
async def test_compatibility_handle_refuses_import_before_open_or_sql(tmp_path):
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE TABLE managed (id TEXT PRIMARY KEY)")
        closure = read_closure(conn, ["managed"])
    profile = CompatibilityProfile(
        name="managed", version=1, closure=closure, writes={}, operations={"create_progression"}
    )
    path = tmp_path / "must-not-be-created.db"
    db = StateDB(path, compatibility=profile)
    with pytest.raises(StateCompatibilityRefused) as caught:
        await state_mod._import_one_run(db, "run-1", tmp_path / "absent-run", {})
    assert caught.value.operation == "import_runs"
    assert caught.value.committed == ()
    assert not path.exists()


@pytest.mark.asyncio
async def test_open_compatibility_store_refuses_import_without_sql_or_changes(tmp_path):
    path = tmp_path / "managed.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO schema_meta VALUES ('lineage', 'manager');"
            "CREATE TABLE managed (id TEXT PRIMARY KEY, value TEXT);"
            "INSERT INTO managed VALUES ('keep', 'unchanged');"
            "CREATE TABLE unrelated (id TEXT PRIMARY KEY, value TEXT);"
            "INSERT INTO unrelated VALUES ('other', 'preserved');"
        )
        closure = read_closure(conn, ["managed"])

    profile = CompatibilityProfile(
        name="managed", version=1, closure=closure, writes={}, operations={"create_progression"}
    )

    def snapshot():
        with closing(sqlite3.connect(path)) as conn:
            return list(conn.iterdump())

    async with StateDB(path, compatibility=profile) as db:
        before = snapshot()
        statements = []

        def record_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(db._engine.sync_engine, "before_cursor_execute", record_sql)
        try:
            with pytest.raises(StateCompatibilityRefused) as caught:
                await state_mod._import_one_run(db, "run-1", tmp_path / "absent-run", {})
        finally:
            event.remove(db._engine.sync_engine, "before_cursor_execute", record_sql)
        assert caught.value.operation == "import_runs"
        assert caught.value.committed == ()
        assert statements == []
        assert snapshot() == before
