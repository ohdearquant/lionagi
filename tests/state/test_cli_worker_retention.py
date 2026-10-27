# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Unresolved worker evidence protects the whole session before retention effects."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

import lionagi.state.db as state_db_mod
from lionagi.state.db import StateDB


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.delenv("LIONAGI_STATE_DB_URL", raising=False)
    return path


async def _seed(
    db: StateDB,
    *,
    settled: bool | None,
    older_generation: bool = False,
    source_kind: str = "live",
    age_days: int = 40,
    with_artifact: bool = True,
) -> dict[str, Any]:
    sid, generation, mid, prog, bprog, branch, artifact, spawn = (
        str(uuid.uuid4()) for _ in range(8)
    )
    old = time.time() - age_days * 86400
    await db.insert_message(
        {"id": mid, "role": "user", "content": {"text": sid}, "created_at": old}
    )
    await db.create_progression(prog, [mid])
    await db.create_progression(bprog, [mid])
    await db.create_session(
        {
            "id": sid,
            "progression_id": prog,
            "status": "running",
            "started_at": old,
            "updated_at": old,
            "source_kind": source_kind,
            "cli_worker_generation": generation,
        }
    )
    await db.create_branch(
        {"id": branch, "session_id": sid, "progression_id": bprog, "created_at": old}
    )
    if with_artifact:
        await db.execute(
            "INSERT INTO artifacts (id, session_id, created_at, updated_at, kind, name, content)"
            " VALUES (?, ?, ?, ?, 'file', 'retained.txt', '{}')",
            (artifact, sid, old, old),
        )
    worker = {
        "spawn_id": spawn,
        "session_id": sid,
        "generation_id": generation,
        "run_id": "retention-run",
        "engine_run_id": None,
        "branch_id": branch,
        "pid": 2048,
        "pgid": 2048,
        "pid_create_time": 10.0,
        "pid_host": "retention-test",
        "pid_boot_time": 1.0,
        "process_identity_mode": "local",
    }
    if settled is not None:
        await db.register_cli_worker_spawn(worker)
        if settled:
            assert await db.settle_cli_worker_spawn(spawn, confirmed_empty_at=time.time()) is True
    if older_generation:
        await db.update_session(sid, cli_worker_generation=str(uuid.uuid4()))
    assert (
        await db.update_status(
            "session", sid, new_status="completed", reason_code="run.completed.ok"
        )
        is True
    )
    await db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (old, sid))
    return {
        "sid": sid,
        "message": mid,
        "progressions": (prog, bprog),
        "branch": branch,
        "artifact": artifact,
        "worker": worker,
    }


async def _graph(db: StateDB, item: dict[str, Any]) -> dict[str, Any]:
    return {
        "session": await db.fetch_one("SELECT * FROM sessions WHERE id = ?", (item["sid"],)),
        "branch": await db.fetch_one("SELECT * FROM branches WHERE id = ?", (item["branch"],)),
        "progressions": await db.fetch_all(
            "SELECT * FROM progressions WHERE id IN (?, ?) ORDER BY id", item["progressions"]
        ),
        "message": await db.fetch_one("SELECT * FROM messages WHERE id = ?", (item["message"],)),
        "artifact": await db.fetch_one("SELECT * FROM artifacts WHERE id = ?", (item["artifact"],)),
        "history": await db.fetch_all(
            "SELECT * FROM status_transitions WHERE entity_type = 'session' AND entity_id = ?"
            " ORDER BY id",
            (item["sid"],),
        ),
    }


async def _worker(db: StateDB, item: dict[str, Any]) -> dict[str, Any]:
    row = await db.fetch_one(
        "SELECT * FROM cli_worker_spawns WHERE spawn_id = ?", (item["worker"]["spawn_id"],)
    )
    assert row is not None
    return row


def _archive_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from lionagi.studio import config

    archive = tmp_path / "archive"
    archive.mkdir()
    monkeypatch.setattr(config, "PRUNE_ARCHIVE_DIR", archive)
    monkeypatch.setattr(config, "PRUNE_CHUNK_ROWS", 2)
    return archive


async def _maintenance(kind: str, ids: list[str]) -> int:
    from lionagi.studio.services import db_maintenance as maint

    if kind == "explicit":
        return await maint.prune_terminal_sessions_by_id(ids)
    return (await maint.prune_old_data(keep_days=30, actor="test"))["sessions_pruned"]


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("kind", ["age", "explicit"])
async def test_maintenance_preserves_unresolved_graph_and_archives_only_settled_parent(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from lionagi.studio.services.retention_archive import read_archive_chunk

    archive = _archive_config(tmp_path, monkeypatch)
    async with StateDB(db_path) as db:
        blocked = await _seed(db, settled=False, older_generation=True)
        removable = await _seed(db, settled=True)
        graph_before = await _graph(db, blocked)
        blocked_worker = await _worker(db, blocked)
        settled_worker = await _worker(db, removable)

    assert await _maintenance(kind, [blocked["sid"], removable["sid"]]) == 1

    async with StateDB(db_path) as db:
        assert await _graph(db, blocked) == graph_before
        assert await _worker(db, blocked) == blocked_worker
        assert await db.get_session(removable["sid"]) is None
        assert await _worker(db, removable) == settled_worker
    archived = {
        row["id"]
        for path in archive.glob("*.zip")
        for row in read_archive_chunk(path)["tables"].get("sessions", [])
    }
    assert archived == {removable["sid"]}, "unresolved sessions must never enter an archive"


@pytest.mark.asyncio(loop_scope="function")
async def test_imported_deletion_preserves_unresolved_graph_and_settled_evidence(
    db_path: Path,
) -> None:
    async with StateDB(db_path) as db:
        blocked = await _seed(
            db, settled=False, older_generation=True, source_kind="imported_codex"
        )
        removable = await _seed(db, settled=True, source_kind="imported_codex")
        graph_before = await _graph(db, blocked)
        blocked_worker = await _worker(db, blocked)
        settled_worker = await _worker(db, removable)

        assert (
            await db.delete_imported_session(blocked["sid"], require_source_kind="imported_codex")
            is False
        )
        assert await _graph(db, blocked) == graph_before
        assert await _worker(db, blocked) == blocked_worker
        assert (
            await db.delete_imported_session(removable["sid"], require_source_kind="imported_codex")
            is True
        )
        assert await db.get_session(removable["sid"]) is None
        assert await _worker(db, removable) == settled_worker
        await db.register_cli_worker_spawn(removable["worker"])
        assert await _worker(db, removable) == settled_worker


@pytest.mark.asyncio(loop_scope="function")
async def test_cli_preview_and_recount_exclude_unresolved_workers(db_path: Path) -> None:
    from lionagi.cli import state

    async with StateDB(db_path) as db:
        blocked = await _seed(db, settled=False, older_generation=True, with_artifact=False)
        removable = await _seed(db, settled=True, with_artifact=False)
        graph_before = await _graph(db, blocked)
        settled_worker = await _worker(db, removable)
    assert (await state._prune_candidates(keep_days=30, keep_n=0))["candidates"] == 1
    preview = await state._prune(keep_days=30, keep_n=0, dry_run=True)
    assert preview["sessions"] == 1
    assert (await state._prune_candidates(keep_days=30, keep_n=0))["candidates"] == 1
    async with StateDB(db_path) as db:
        assert await _graph(db, blocked) == graph_before
        assert await db.get_session(removable["sid"]) is not None
        assert await _worker(db, removable) == settled_worker
    assert await state._prune(keep_days=30, keep_n=0, dry_run=False) == preview
    assert (await state._prune_candidates(keep_days=30, keep_n=0))["candidates"] == 0
    async with StateDB(db_path) as db:
        assert await _graph(db, blocked) == graph_before
        assert await db.get_session(removable["sid"]) is None
        assert await _worker(db, removable) == settled_worker


@pytest.mark.asyncio(loop_scope="function")
async def test_cli_keep_n_still_ranks_protected_sessions(db_path: Path) -> None:
    from lionagi.cli import state

    async with StateDB(db_path) as db:
        blocked = await _seed(db, settled=False, age_days=40, with_artifact=False)
        removable = await _seed(db, settled=True, age_days=50, with_artifact=False)
        settled_worker = await _worker(db, removable)
    assert (await state._prune_candidates(keep_days=30, keep_n=1))["candidates"] == 1
    assert (await state._prune(keep_days=30, keep_n=1, dry_run=False))["sessions"] == 1
    async with StateDB(db_path) as db:
        assert await db.get_session(blocked["sid"]) is not None
        assert await db.get_session(removable["sid"]) is None
        assert await _worker(db, removable) == settled_worker


async def _insert_unresolved(conn: AsyncConnection, worker: dict[str, Any]) -> None:
    """Place existing evidence at the retention transaction's recheck boundary."""
    await conn.execute(
        text(
            "INSERT INTO cli_worker_spawns (spawn_id, session_id, generation_id, run_id,"
            " engine_run_id, branch_id, pid, pgid, pid_create_time, pid_host, pid_boot_time,"
            " process_identity_mode, recorded_at, confirmed_empty_at)"
            " VALUES (:spawn_id, :session_id, :generation_id, :run_id, :engine_run_id, :branch_id,"
            " :pid, :pgid, :pid_create_time, :pid_host, :pid_boot_time, :process_identity_mode,"
            " :recorded_at, NULL)"
        ),
        {**worker, "recorded_at": time.time()},
    )


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("kind", ["age", "explicit"])
async def test_maintenance_rechecks_workers_after_candidate_selection(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from lionagi.studio.services import db_maintenance as maint

    archive = _archive_config(tmp_path, monkeypatch)
    async with StateDB(db_path) as db:
        blocked = await _seed(db, settled=None)
        graph_before = await _graph(db, blocked)
    original = maint._exec_chunked
    inserted = False

    async def before_lock(conn, sql_prefix, ids, extra_params=(), suffix="", suffix_params=()):
        nonlocal inserted
        if not inserted and sql_prefix.startswith("UPDATE sessions SET updated_at"):
            inserted = True
            await _insert_unresolved(conn, blocked["worker"])
        return await original(conn, sql_prefix, ids, extra_params, suffix, suffix_params)

    monkeypatch.setattr(maint, "_exec_chunked", before_lock)
    assert await _maintenance(kind, [blocked["sid"]]) == 0
    assert inserted, "the unresolved record must arrive after the initial candidate selection"
    assert not list(archive.glob("*.zip"))
    async with StateDB(db_path) as db:
        assert await _graph(db, blocked) == graph_before
        assert (await _worker(db, blocked))["confirmed_empty_at"] is None


@pytest.mark.asyncio(loop_scope="function")
async def test_cli_filters_a_late_worker_before_temp_tables(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lionagi.cli import state

    async with StateDB(db_path) as db:
        blocked = await _seed(db, settled=None)
        graph_before = await _graph(db, blocked)
    original_guard = state_db_mod._locked_cli_worker_prune_candidates
    original_execute = AsyncConnection.execute
    guard_calls: list[tuple[str, ...]] = []
    child_statements: list[str] = []
    inserted = False

    async def observe_guard(conn, ids, *, dialect):
        guard_calls.append(tuple(ids))
        return await original_guard(conn, ids, dialect=dialect)

    async def observe_execute(conn, statement, *args, **kwargs):
        nonlocal inserted
        sql = str(statement)
        if "prune_orphan_progressions" in sql or "prune_direct_messages" in sql:
            child_statements.append(sql)
        result = await original_execute(conn, statement, *args, **kwargs)
        if not inserted and sql.startswith("SELECT id FROM sessions ") and "LIMIT :keep_n" in sql:
            inserted = True
            await _insert_unresolved(conn, blocked["worker"])
        return result

    monkeypatch.setattr(state_db_mod, "_locked_cli_worker_prune_candidates", observe_guard)
    monkeypatch.setattr(AsyncConnection, "execute", observe_execute)
    assert (await state._prune(keep_days=30, keep_n=0, dry_run=False))["sessions"] == 0
    assert inserted
    assert guard_calls == [(blocked["sid"],)], "candidate sessions must use the shared lock guard"
    assert child_statements == [], "protected victims must be removed before temp-table work"
    async with StateDB(db_path) as db:
        assert await _graph(db, blocked) == graph_before
        # The empty prune exits by rollback, including the injected row in its transaction.
        assert (
            await db.fetch_one(
                "SELECT * FROM cli_worker_spawns WHERE spawn_id = ?",
                (blocked["worker"]["spawn_id"],),
            )
            is None
        )


@pytest.mark.asyncio(loop_scope="function")
async def test_cli_rechecks_age_after_locking_candidates(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lionagi.cli import state

    async with StateDB(db_path) as db:
        refreshed = await _seed(db, settled=True)
        graph_before = await _graph(db, refreshed)
        worker_before = await _worker(db, refreshed)
    original_execute = AsyncConnection.execute
    refreshed_at = time.time()
    invoked = False

    async def refresh_after_selection(conn, statement, *args, **kwargs):
        nonlocal invoked
        result = await original_execute(conn, statement, *args, **kwargs)
        sql = str(statement)
        if not invoked and sql.startswith("SELECT id FROM sessions ") and "LIMIT :keep_n" in sql:
            invoked = True
            await original_execute(
                conn,
                text("UPDATE sessions SET updated_at = :now WHERE id = :id"),
                {"now": refreshed_at, "id": refreshed["sid"]},
            )
        return result

    monkeypatch.setattr(AsyncConnection, "execute", refresh_after_selection)
    assert (await state._prune(keep_days=30, keep_n=0, dry_run=False))["sessions"] == 0
    assert invoked
    async with StateDB(db_path) as db:
        # The refresh shares the empty prune's rolled-back transaction.
        assert await _graph(db, refreshed) == graph_before
        assert await _worker(db, refreshed) == worker_before
