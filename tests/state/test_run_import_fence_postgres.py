# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Cross-connection PostgreSQL coverage for run import ownership and admission."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text

from lionagi.cli import state as state_mod
from lionagi.state import db as db_mod
from lionagi.state.db import StateDB
from tests.state._pg_cleanup import delete_imported_session_with_retry

_WAIT = 10.0
_HOLD_WAIT = 4 * _WAIT


def _id() -> str:
    return str(uuid.uuid4())


def _message(message_id: str, value: str) -> dict:
    return {"id": message_id, "created_at": 1.0, "role": "user", "content": {"text": value}}


def _write_run(root: Path, run_id: str, branch_id: str, messages: list[dict]) -> tuple:
    run = root / run_id
    branches = run / "branches"
    branches.mkdir(parents=True)
    manifest = {"run_id": run_id, "kind": "agent", "status": "running"}
    (run / "run.json").write_text(json.dumps(manifest))
    path = branches / "branch.json"
    path.write_text(json.dumps({"id": branch_id, "messages": {"collections": messages}}))
    os.utime(path, (1_000_000, 1_000_000))
    return run, manifest


@asynccontextmanager
async def _stores(pg_url, monkeypatch):
    """Track only this test's generated keys so cleanup cannot sweep another run."""
    generated = []
    real_uuid4 = uuid.uuid4

    def track_uuid():
        value = real_uuid4()
        generated.append(str(value))
        return value

    monkeypatch.setattr(state_mod, "uuid", SimpleNamespace(uuid4=track_uuid))
    keeper_id, keeper_progression, run_id, branch_id = (_id() for _ in range(4))
    message_ids = []
    async with StateDB(url=pg_url) as importer, StateDB(url=pg_url) as writer:
        assert importer.dialect == writer.dialect == "postgresql"
        try:
            await writer.create_progression(keeper_progression)
            await writer.create_session(
                {
                    "id": keeper_id,
                    "progression_id": keeper_progression,
                    "source_kind": "imported_fs",
                    "status": "running",
                }
            )
            yield (
                importer,
                writer,
                keeper_id,
                keeper_progression,
                run_id,
                branch_id,
                message_ids,
                generated,
            )
        finally:
            # Each ID comes from this test; do not touch message type rows or other sessions.
            # Session deletion takes NOWAIT table locks, so a backend that holds a conflicting
            # lock for an instant (autovacuum, a rollback still finishing) refuses it. The helper
            # waits such a holder out for a bounded time and fails with the holders listed if a
            # lock stays held.
            for session_id in (run_id, keeper_id):
                if await writer.get_session(session_id) is not None:
                    assert await delete_imported_session_with_retry(
                        writer, session_id, require_source_kind="imported_fs"
                    )
            for message_id in message_ids:
                await writer.execute("DELETE FROM messages WHERE id = :id", {"id": message_id})
            for progression_id in [keeper_progression, *generated]:
                await writer.execute(
                    "DELETE FROM progressions WHERE id = :id", {"id": progression_id}
                )


async def _is_blocked(observer: StateDB, waiting: int, blocker: int) -> None:
    """Wait for the server to report the particular transaction blocking the writer."""
    deadline = time.monotonic() + _WAIT
    while time.monotonic() < deadline:
        row = await observer.fetch_one(
            "SELECT :blocker = ANY(pg_blocking_pids(:waiting)) AS blocked",
            {"blocker": blocker, "waiting": waiting},
        )
        if row["blocked"]:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the expected PostgreSQL row-lock wait was not observed")


async def _pid(conn) -> int:
    return int((await conn.exec_driver_sql("SELECT pg_backend_pid()")).scalar_one())


@contextmanager
def _membership_expansions(db):
    """Record executed membership SQL and the candidate IDs bound to each expansion."""
    calls = []

    def record(_conn, _cursor, statement, _params, context, _executemany):
        if "jsonb_array_elements_text(p.collection::jsonb)" in statement:
            calls.append(json.loads(context.compiled_parameters[0]["candidate_ids"]))

    event.listen(db._engine.sync_engine, "before_cursor_execute", record)
    try:
        yield calls
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", record)


async def _assert_no_run(db, run_id, branch_id, message_ids, progressions):
    assert await db.get_session(run_id) is None
    assert await db.get_branch(branch_id) is None
    for message_id in message_ids:
        assert await db.get_message(message_id) is None
    for progression_id in progressions:
        assert (
            await db.fetch_one("SELECT id FROM progressions WHERE id = :id", {"id": progression_id})
            is None
        )
    assert (
        await db.fetch_one(
            "SELECT id FROM status_transitions WHERE entity_type = 'session' AND entity_id = :id",
            {"id": run_id},
        )
        is None
    )


@pytest.mark.asyncio
async def test_postgres_existing_session_skips_before_shared_conflict_and_any_write(
    pg_url, tmp_path, monkeypatch
):
    async with _stores(pg_url, monkeypatch) as setup:
        importer, writer, _keeper, prog, run_id, branch_id, ids, generated = setup
        shared_id, run_progression = _id(), _id()
        ids.append(shared_id)
        generated.append(run_progression)
        await writer.insert_message(
            state_mod._msg_from_collection_entry(_message(shared_id, "old"))
        )
        await writer.append_to_progression(prog, shared_id)
        await writer.create_progression(run_progression)
        await writer.create_session(
            {
                "id": run_id,
                "progression_id": run_progression,
                "source_kind": "imported_fs",
                "status": "running",
            }
        )
        run, manifest = _write_run(tmp_path, run_id, branch_id, [_message(shared_id, "new")])
        before_session = await writer.get_session(run_id)
        statements = []

        def record(_conn, _cursor, statement, _params, _context, _executemany):
            statements.append(statement.lstrip().upper())

        event.listen(importer._engine.sync_engine, "before_cursor_execute", record)
        try:
            assert await state_mod._import_one_run(importer, run_id, run, manifest) == (0, 0, 0)
        finally:
            event.remove(importer._engine.sync_engine, "before_cursor_execute", record)
        assert not any(s.startswith(("INSERT", "UPDATE", "DELETE")) for s in statements)
        assert await writer.get_session(run_id) == before_session
        assert await writer.get_progression(run_progression) == []
        assert await writer.get_progression(prog) == [shared_id]
        assert (await writer.get_message(shared_id))["content"] == {"text": "old"}
        assert await writer.get_branch(branch_id) is None
        for progression_id in generated:
            if progression_id != run_progression:
                assert (
                    await writer.fetch_one(
                        "SELECT id FROM progressions WHERE id = :id", {"id": progression_id}
                    )
                    is None
                )


@pytest.mark.asyncio
async def test_postgres_committed_attachment_refuses_without_run_writes(
    pg_url, tmp_path, monkeypatch
):
    async with _stores(pg_url, monkeypatch) as setup:
        importer, writer, _keeper, prog, run_id, branch_id, ids, generated = setup
        shared_id, fresh_id = _id(), _id()
        ids.extend((shared_id, fresh_id))
        await writer.insert_message(
            state_mod._msg_from_collection_entry(_message(shared_id, "old"))
        )
        await writer.append_to_progression(prog, shared_id)
        run, manifest = _write_run(
            tmp_path, run_id, branch_id, [_message(fresh_id, "fresh"), _message(shared_id, "new")]
        )

        with _membership_expansions(importer) as expansions:
            with pytest.raises(state_mod.ImportMessageConflictError) as caught:
                await state_mod._import_one_run(importer, run_id, run, manifest)
        assert expansions == [[shared_id]]
        assert caught.value.message_id == shared_id
        assert (await writer.get_message(shared_id))["content"] == {"text": "old"}
        assert await writer.get_progression(prog) == [shared_id]
        await _assert_no_run(writer, run_id, branch_id, [fresh_id], generated)


@pytest.mark.asyncio
async def test_postgres_fresh_run_does_not_expand_memberships(pg_url, tmp_path, monkeypatch):
    async with _stores(pg_url, monkeypatch) as setup:
        importer, writer, _keeper, _prog, run_id, branch_id, ids, _generated = setup
        first_id, second_id = _id(), _id()
        ids.extend((first_id, second_id))
        run, manifest = _write_run(
            tmp_path, run_id, branch_id, [_message(first_id, "one"), _message(second_id, "two")]
        )
        with _membership_expansions(importer) as expansions:
            assert await state_mod._import_one_run(importer, run_id, run, manifest) == (1, 1, 2)
        assert expansions == []
        assert (await writer.get_message(first_id))["content"] == {"text": "one"}
        assert (await writer.get_message(second_id))["content"] == {"text": "two"}


@pytest.mark.asyncio
async def test_postgres_success_expands_existing_memberships_once(pg_url, tmp_path, monkeypatch):
    async with _stores(pg_url, monkeypatch) as setup:
        importer, writer, _keeper, _prog, run_id, branch_id, ids, _generated = setup
        existing_id, absent_id = _id(), _id()
        ids.extend((existing_id, absent_id))
        await writer.insert_message(
            state_mod._msg_from_collection_entry(_message(existing_id, "old"))
        )
        run, manifest = _write_run(
            tmp_path,
            run_id,
            branch_id,
            [_message(existing_id, "new"), _message(absent_id, "fresh")],
        )
        with _membership_expansions(importer) as expansions:
            assert await state_mod._import_one_run(importer, run_id, run, manifest) == (1, 1, 2)
        assert expansions == [[existing_id]]
        assert (await writer.get_message(existing_id))["content"] == {"text": "new"}
        assert (await writer.get_message(absent_id))["content"] == {"text": "fresh"}


@pytest.mark.asyncio
async def test_postgres_attachment_waits_until_import_commits(pg_url, tmp_path, monkeypatch):
    async with _stores(pg_url, monkeypatch) as setup:
        importer, writer, _keeper, prog, run_id, branch_id, ids, _generated = setup
        shared_id = _id()
        ids.append(shared_id)
        raw = _message(shared_id, "new")
        await writer.insert_message(
            state_mod._msg_from_collection_entry(_message(shared_id, "old"))
        )
        run, manifest = _write_run(tmp_path, run_id, branch_id, [raw])
        imported = asyncio.Event()
        release = asyncio.Event()
        import_pid = asyncio.get_running_loop().create_future()
        writer_pid = asyncio.get_running_loop().create_future()
        original_import = importer._insert_message_in_tx
        original_writer = writer._insert_message_in_tx

        async def pause_import(conn, msg):
            await original_import(conn, msg)
            if msg["id"] == shared_id:
                import_pid.set_result(await _pid(conn))
                imported.set()
                await asyncio.wait_for(release.wait(), _HOLD_WAIT)

        async def identify_writer(conn, msg):
            if msg["id"] == shared_id:
                writer_pid.set_result(await _pid(conn))
            await original_writer(conn, msg)

        monkeypatch.setattr(importer, "_insert_message_in_tx", pause_import)
        monkeypatch.setattr(writer, "_insert_message_in_tx", identify_writer)
        import_task = asyncio.create_task(
            state_mod._import_one_run(importer, run_id, run, manifest)
        )
        writer_task = None
        try:
            await asyncio.wait_for(imported.wait(), _WAIT)
            writer_task = asyncio.create_task(
                writer.insert_messages(
                    [state_mod._msg_from_collection_entry(raw)], progression_ids=[prog]
                )
            )
            await _is_blocked(
                importer,
                await asyncio.wait_for(writer_pid, _WAIT),
                await asyncio.wait_for(import_pid, _WAIT),
            )
            assert not writer_task.done()
            release.set()
            assert await asyncio.wait_for(import_task, _WAIT) == (1, 1, 1)
            await asyncio.wait_for(writer_task, _WAIT)
        finally:
            release.set()
            for task in (import_task, writer_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(t for t in (import_task, writer_task) if t), return_exceptions=True
            )
        assert await writer.get_progression(prog) == [shared_id]
        assert (await writer.get_message(shared_id))["content"] == {"text": "new"}
        assert await writer.get_session(run_id) is not None


@pytest.mark.asyncio
async def test_postgres_rolls_back_run_on_second_message_failure(pg_url, tmp_path, monkeypatch):
    async with _stores(pg_url, monkeypatch) as setup:
        importer, writer, _keeper, _prog, run_id, branch_id, ids, generated = setup
        first_id, second_id = _id(), _id()
        ids.extend((first_id, second_id))
        run, manifest = _write_run(
            tmp_path, run_id, branch_id, [_message(first_id, "one"), _message(second_id, "two")]
        )
        original = importer._insert_import_message_if_absent_in_tx
        calls = 0

        async def fail_second(conn, msg):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected message failure")
            return await original(conn, msg)

        monkeypatch.setattr(importer, "_insert_import_message_if_absent_in_tx", fail_second)
        with pytest.raises(RuntimeError, match="injected message failure"):
            await state_mod._import_one_run(importer, run_id, run, manifest)
        assert calls == 2
        await _assert_no_run(writer, run_id, branch_id, ids, generated)


@pytest.mark.asyncio
async def test_postgres_rolls_back_session_lifecycle_and_branch_on_late_failure(
    pg_url, tmp_path, monkeypatch
):
    async with _stores(pg_url, monkeypatch) as setup:
        importer, writer, _keeper, _prog, run_id, branch_id, ids, generated = setup
        message_id = _id()
        ids.append(message_id)
        run, manifest = _write_run(tmp_path, run_id, branch_id, [_message(message_id, "one")])
        original = importer._create_branch_in_tx
        observed = []

        async def fail_after_branch(conn, values):
            await original(conn, values)
            counts = await conn.execute(
                text(
                    "SELECT (SELECT count(*) FROM sessions WHERE id = :id), "
                    "(SELECT count(*) FROM status_transitions "
                    "WHERE entity_type = 'session' AND entity_id = :id), "
                    "(SELECT count(*) FROM branches WHERE id = :branch)"
                ),
                {"id": run_id, "branch": branch_id},
            )
            observed.append(tuple(counts.one()))
            raise RuntimeError("injected late failure")

        monkeypatch.setattr(importer, "_create_branch_in_tx", fail_after_branch)
        with pytest.raises(RuntimeError, match="injected late failure"):
            await state_mod._import_one_run(importer, run_id, run, manifest)
        assert observed == [(1, 1, 1)]
        await _assert_no_run(writer, run_id, branch_id, ids, generated)


@pytest.mark.asyncio
async def test_postgres_insert_and_attachment_after_import_lock_scan(pg_url, tmp_path, monkeypatch):
    async with _stores(pg_url, monkeypatch) as setup:
        importer, writer, _keeper, prog, run_id, branch_id, ids, generated = setup
        shared_id, existing_id = _id(), _id()
        ids.extend((shared_id, existing_id))
        await writer.insert_message(
            state_mod._msg_from_collection_entry(_message(existing_id, "existing"))
        )
        run, manifest = _write_run(
            tmp_path,
            run_id,
            branch_id,
            [_message(existing_id, "existing"), _message(shared_id, "new")],
        )
        scanned = asyncio.Event()
        release = asyncio.Event()
        original = state_mod._lock_import_messages_in_tx

        async def pause_after_scan(conn, message_ids):
            locked = await original(conn, message_ids)
            if shared_id in message_ids and shared_id not in locked:
                scanned.set()
                await asyncio.wait_for(release.wait(), _HOLD_WAIT)
            return locked

        monkeypatch.setattr(state_mod, "_lock_import_messages_in_tx", pause_after_scan)
        with _membership_expansions(importer) as expansions:
            task = asyncio.create_task(state_mod._import_one_run(importer, run_id, run, manifest))
            try:
                await asyncio.wait_for(scanned.wait(), _WAIT)
                await asyncio.wait_for(
                    writer.insert_messages(
                        [state_mod._msg_from_collection_entry(_message(shared_id, "old"))],
                        progression_ids=[prog],
                    ),
                    _WAIT,
                )
                assert await writer.get_progression(prog) == [shared_id]
                release.set()
                with pytest.raises(state_mod.ImportMessageConflictError) as caught:
                    await asyncio.wait_for(task, _WAIT)
                assert caught.value.message_id == shared_id
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert len(expansions) == 2
        assert set(expansions[0]) == {existing_id}
        assert expansions[1] == [shared_id]
        assert (await writer.get_message(shared_id))["content"] == {"text": "old"}
        assert (await writer.get_message(existing_id))["content"] == {"text": "existing"}
        await _assert_no_run(writer, run_id, branch_id, [], generated)


@pytest.mark.asyncio
async def test_postgres_concurrent_insert_and_attachment_are_rechecked(
    pg_url, tmp_path, monkeypatch
):
    async with _stores(pg_url, monkeypatch) as setup:
        importer, writer, _keeper, prog, run_id, branch_id, ids, generated = setup
        shared_id = _id()
        ids.append(shared_id)
        run, manifest = _write_run(tmp_path, run_id, branch_id, [_message(shared_id, "new")])
        import_pid = asyncio.get_running_loop().create_future()
        original = importer._insert_import_message_if_absent_in_tx

        async def identify_import(conn, msg):
            if msg["id"] == shared_id:
                import_pid.set_result(await _pid(conn))
            return await original(conn, msg)

        monkeypatch.setattr(importer, "_insert_import_message_if_absent_in_tx", identify_import)
        task = None
        try:
            async with writer._tx() as conn:
                writer_pid = await _pid(conn)
                await writer._insert_message_in_tx(
                    conn, state_mod._msg_from_collection_entry(_message(shared_id, "old"))
                )
                await writer._append_to_progression_in_tx(conn, prog, shared_id)
                task = asyncio.create_task(
                    state_mod._import_one_run(importer, run_id, run, manifest)
                )
                await _is_blocked(writer, await asyncio.wait_for(import_pid, _WAIT), writer_pid)
            with pytest.raises(state_mod.ImportMessageConflictError) as caught:
                await asyncio.wait_for(task, _WAIT)
            assert caught.value.message_id == shared_id
        finally:
            if task is not None and not task.done():
                task.cancel()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
        assert (await writer.get_message(shared_id))["content"] == {"text": "old"}
        assert await writer.get_progression(prog) == [shared_id]
        await _assert_no_run(writer, run_id, branch_id, [], generated)


@pytest.mark.asyncio
async def test_postgres_concurrent_same_run_imports_once(pg_url, tmp_path, monkeypatch):
    async with _stores(pg_url, monkeypatch) as setup:
        _importer, writer, _keeper, _prog, run_id, branch_id, ids, generated = setup
        message_id = _id()
        ids.append(message_id)
        _write_run(tmp_path, run_id, branch_id, [_message(message_id, "one")])
        monkeypatch.setattr(state_mod, "RUNS_ROOT", tmp_path)
        real_cls = db_mod.StateDB
        opened = asyncio.Lock()
        both_checked = asyncio.Event()
        checked = 0

        class ImportStore(real_cls):
            def __init__(self):
                super().__init__(url=pg_url)
                self._admission_checked = False

            async def open(self):
                async with opened:
                    await super().open()

            async def get_session(self, session_id):
                nonlocal checked
                row = await super().get_session(session_id)
                if session_id == run_id and not self._admission_checked:
                    self._admission_checked = True
                    assert row is None
                    checked += 1
                    if checked == 2:
                        both_checked.set()
                    await asyncio.wait_for(both_checked.wait(), _WAIT)
                return row

        monkeypatch.setattr(db_mod, "StateDB", ImportStore)
        results = await asyncio.wait_for(
            asyncio.gather(state_mod._import_runs(), state_mod._import_runs()), _WAIT * 3
        )
        assert sorted(
            (result["sessions"], result["skipped"], result["errors"]) for result in results
        ) == [
            (0, 1, 0),
            (1, 0, 0),
        ]
        session = await writer.get_session(run_id)
        branch = await writer.get_branch(branch_id)
        assert session is not None and branch is not None
        assert await writer.get_progression(session["progression_id"]) == [message_id]
        assert await writer.get_progression(branch["progression_id"]) == [message_id]
        surviving = set()
        for progression_id in generated:
            if await writer.fetch_one(
                "SELECT id FROM progressions WHERE id = :id", {"id": progression_id}
            ):
                surviving.add(progression_id)
        assert surviving == {session["progression_id"], branch["progression_id"]}
