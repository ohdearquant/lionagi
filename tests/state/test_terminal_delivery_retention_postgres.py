# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Pruning respects terminal delivery debt on a live PostgreSQL store.

Each test runs in a database of its own, because the age-based prune considers every
session in the store and registration applies to every eligible transition.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager, contextmanager, suppress
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import make_url

import lionagi.state.db as state_db
from lionagi.state.lifecycle.deliveries import (
    ack_delivery,
    get_owed_transition,
    reconcile_unacknowledged,
    register_consumer,
    retire_consumer,
)
from lionhub.orchestration.scheduler import db_maintenance, settings
from tests.state.test_terminal_delivery_retention import CONSUMER, _mixed_session, _terminal
from tests.state.test_terminal_delivery_retention import store as sqlite_store  # noqa: F401

_DANGLING_ACKS = (
    "SELECT td.transition_id FROM terminal_deliveries td "
    "LEFT JOIN status_transitions st ON st.id = td.transition_id WHERE st.id IS NULL"
)
_WAITING_LOCK = (
    "SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
    "WHERE NOT l.granted AND c.relname = $1 AND l.mode = $2 "
    "AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
    "AND $3 = ANY(pg_blocking_pids(l.pid))"
)


@contextmanager
def _capture_prune_delete_plans(monkeypatch, table, *, work_mem, anchor=""):
    plans = []

    def capture(conn, _cursor, statement, parameters, _context, _executemany):
        if not statement.startswith(f"DELETE FROM {table} ") or anchor not in statement:
            return
        conn.exec_driver_sql(f"SET LOCAL work_mem = '{work_mem}'")
        # ANALYZE executes the DELETE. Roll the rehearsal back before executing the original
        # statement so the temporary tables and the actual prune result are both available.
        rehearsal = conn.begin_nested()
        try:
            raw = conn.exec_driver_sql(
                "EXPLAIN (ANALYZE, FORMAT JSON, TIMING OFF) " + statement, parameters
            ).scalar_one()
        finally:
            rehearsal.rollback()
        plan = json.loads(raw) if isinstance(raw, str) else raw
        plans.append(plan[0]["Plan"])

    class ObservedStore(state_db.StateDB):
        async def __aenter__(self):
            await super().__aenter__()
            event.listen(self._engine.sync_engine, "before_cursor_execute", capture)
            return self

    with monkeypatch.context() as patch:
        patch.setattr(db_maintenance, "StateDB", ObservedStore)
        yield plans


def _plan_nodes(node):
    yield node
    for child in node.get("Plans", ()):
        yield from _plan_nodes(child)


def _assert_no_repeated_survivor_scans(plan, relations):
    for node in _plan_nodes(plan):
        loops = node["Actual Loops"]
        if loops <= 1:
            continue
        node_type = node["Node Type"]
        if node.get("Relation Name") in relations and (
            node_type == "Seq Scan"
            or (node_type in {"Index Scan", "Index Only Scan"} and "Index Cond" not in node)
        ):
            pytest.fail(f"repeated full survivor scan ({loops} loops): {node}")
        # A nested loop can reread a cached survivor relation while its base scan still
        # reports one loop. A selective index probe is fine. Reading a large set again is not.
        if node_type == "Materialize" and node["Actual Rows"] >= 100:
            if any(child.get("Relation Name") in relations for child in _plan_nodes(node)):
                pytest.fail(f"repeated materialized survivor scan ({loops} loops): {node}")
        if node_type == "CTE Scan" and node["Actual Rows"] >= 100:
            pytest.fail(f"repeated CTE survivor scan ({loops} loops): {node}")


@asynccontextmanager
async def _fresh_store(pg_url, monkeypatch):
    asyncpg = pytest.importorskip("asyncpg")
    url = make_url(pg_url)
    admin = url.set(drivername="postgresql").render_as_string(hide_password=False)
    name = f"retention_{uuid.uuid4().hex[:12]}"
    conn = await asyncpg.connect(admin, timeout=5, command_timeout=5)
    created = False
    try:
        await conn.execute(f'CREATE DATABASE "{name}"')
        created = True
        store_url = url.set(database=name).render_as_string(hide_password=False)
        raw_url = url.set(database=name, drivername="postgresql").render_as_string(
            hide_password=False
        )
        monkeypatch.setattr(
            state_db,
            "settings",
            state_db.settings.model_copy(update={"LIONAGI_STATE_DB_URL": store_url}),
        )
        monkeypatch.setattr(settings, "PRUNE_ARCHIVE_DIR", None)
        monkeypatch.setattr(settings, "PRUNE_CHUNK_ROWS", 5)
        yield store_url, raw_url
    finally:
        try:
            if created:
                await conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        finally:
            await conn.close()


@asynccontextmanager
async def _pg_connection(raw_url):
    asyncpg = pytest.importorskip("asyncpg")
    conn = await asyncpg.connect(raw_url, timeout=5, command_timeout=5)
    try:
        yield conn
    finally:
        await conn.close()


async def _wait_for_blocked(probe, writer, relation, mode, task):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if task.done():
            raise AssertionError(f"prune finished before waiting for {mode}: {task.result()}")
        if await probe.fetchval(_WAITING_LOCK, relation, mode, writer.get_server_pid()):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"prune never waited for {mode} on {relation}")


@asynccontextmanager
async def _pruning(session_id):
    task = asyncio.create_task(db_maintenance.prune_terminal_sessions_by_id([session_id]))
    try:
        yield task
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(task, 5)


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("entry", ["explicit", "age"])
async def test_postgresql_prune_retains_owed_history_and_removes_ack_children(
    pg_url, entry, monkeypatch, caplog
):
    async with _fresh_store(pg_url, monkeypatch) as (store_url, _raw):
        async with state_db.StateDB(url=store_url) as db:
            await register_consumer(db, CONSUMER, start="all")
            owed_only = await _terminal(db, "owed")
            acknowledged = await _terminal(db, "acked")
            await ack_delivery(db, acknowledged, CONSUMER)
            mixed_ack, mixed_owed = await _mixed_session(db)
            await register_consumer(db, "other", start="now")

        with caplog.at_level(logging.INFO, logger=db_maintenance._log.name):
            if entry == "age":
                assert await db_maintenance.prune_old_data(keep_days=30, actor="test") == {
                    "sessions_pruned": 1,
                    "runs_pruned": 0,
                    "dispatch_purged": 0,
                }
                assert "retained_for_delivery=2" in caplog.text
            else:
                assert (
                    await db_maintenance.prune_terminal_sessions_by_id(["owed", "acked", "mixed"])
                    == 1
                )
                assert "Retained 2 terminal sessions with owed deliveries" in caplog.text

        async with state_db.StateDB(url=store_url) as db:
            assert db.dialect == "postgresql"
            assert await db.get_session("owed") is not None
            assert await db.get_session("mixed") is not None
            assert await db.get_session("acked") is None
            assert {r["transition_id"] for r in await reconcile_unacknowledged(db, CONSUMER)} == {
                owed_only,
                mixed_owed,
            }
            assert await get_owed_transition(db, CONSUMER, owed_only) is not None
            assert await get_owed_transition(db, CONSUMER, mixed_owed) is not None
            remaining = await db.fetch_all(
                "SELECT transition_id FROM terminal_deliveries WHERE consumer = ?", (CONSUMER,)
            )
            assert {r["transition_id"] for r in remaining} == {mixed_ack}
            assert not await db.fetch_one(
                "SELECT id FROM status_transitions WHERE id = ?", (acknowledged,)
            )
            assert await db.fetch_all(_DANGLING_ACKS) == []
            if entry == "age":
                entries = await db.list_admin_events(action="prune")
                assert len(entries) == 1
                details = entries[0]["details"]
                if isinstance(details, str):
                    details = json.loads(details)
                assert details["sessions_retained_for_delivery"] == 2


@pytest.mark.asyncio(loop_scope="function")
async def test_postgresql_retired_consumer_does_not_block_prune(pg_url, monkeypatch):
    async with _fresh_store(pg_url, monkeypatch) as (store_url, _raw):
        async with state_db.StateDB(url=store_url) as db:
            await register_consumer(db, CONSUMER, start="all")
            _acked, owed = await _mixed_session(db)
            assert await get_owed_transition(db, CONSUMER, owed) is not None
            assert await retire_consumer(db, CONSUMER)
        assert await db_maintenance.prune_terminal_sessions_by_id(["mixed"]) == 1
        async with state_db.StateDB(url=store_url) as db:
            assert await db.get_session("mixed") is None
            assert await db.fetch_all("SELECT * FROM terminal_deliveries") == []
            assert (
                await db.fetch_all("SELECT * FROM status_transitions WHERE entity_id = 'mixed'")
                == []
            )


@pytest.mark.asyncio(loop_scope="function")
async def test_postgresql_prune_waits_for_an_uncommitted_acknowledgement(pg_url, monkeypatch):
    """The ACK ledger lock makes the debt check see an ACK that commits while prune waits."""
    async with _fresh_store(pg_url, monkeypatch) as (store_url, raw_url):
        async with state_db.StateDB(url=store_url) as db:
            await register_consumer(db, CONSUMER, start="all")
            transition = await _terminal(db, "late-ack")

        async with _pg_connection(raw_url) as probe, _pg_connection(raw_url) as writer:
            tx = writer.transaction()
            await tx.start()
            await writer.execute(
                "INSERT INTO terminal_deliveries (transition_id, consumer, acked_at) "
                "VALUES ($1, $2, $3)",
                transition,
                CONSUMER,
                time.time(),
            )
            async with _pruning("late-ack") as prune:
                await _wait_for_blocked(
                    probe, writer, "terminal_deliveries", "ShareRowExclusiveLock", prune
                )
                await tx.commit()
                assert await asyncio.wait_for(prune, 5) == 1

        async with state_db.StateDB(url=store_url) as db:
            assert await db.get_session("late-ack") is None
            assert await db.fetch_all("SELECT * FROM terminal_deliveries") == []
            assert await db.fetch_all(_DANGLING_ACKS) == []


@pytest.mark.asyncio(loop_scope="function")
async def test_postgresql_prune_waits_for_an_uncommitted_registration(pg_url, monkeypatch):
    """The consumer lock makes the debt check see a consumer registered while prune waits."""
    async with _fresh_store(pg_url, monkeypatch) as (store_url, raw_url):
        async with state_db.StateDB(url=store_url) as db:
            transition = await _terminal(db, "late-consumer")

        async with _pg_connection(raw_url) as probe, _pg_connection(raw_url) as writer:
            tx = writer.transaction()
            await tx.start()
            await writer.execute(
                "INSERT INTO terminal_consumers (name, registered_at, retired_at) "
                "VALUES ($1, $2, NULL)",
                CONSUMER,
                time.time(),
            )
            async with _pruning("late-consumer") as prune:
                await _wait_for_blocked(probe, writer, "terminal_consumers", "ShareLock", prune)
                await tx.commit()
                assert await asyncio.wait_for(prune, 5) == 0

        async with state_db.StateDB(url=store_url) as db:
            assert await db.get_session("late-consumer") is not None
            assert await get_owed_transition(db, CONSUMER, transition) is not None


async def _prune_sessions(entry, ids):
    if entry == "explicit":
        return await db_maintenance.prune_terminal_sessions_by_id(ids)
    result = await db_maintenance.prune_old_data(keep_days=30, actor="test")
    assert result["runs_pruned"] == result["dispatch_purged"] == 0
    return result["sessions_pruned"]


async def _check_message_references(entry):
    async with state_db.StateDB() as db:
        await _terminal(db, "victim")
        await _terminal(db, "empty-victim")
        await _terminal(db, "keeper")
        for message_id in ("victim-listed", "victim-direct", "shared-listed", "shared-direct"):
            await db.insert_message(
                {
                    "id": message_id,
                    "created_at": time.time(),
                    "role": "user",
                    "content": {"text": message_id},
                }
            )
        await db.set_progression("victim", ["victim-listed", "shared-listed", "shared-direct"])
        await db.execute(
            "UPDATE progressions SET collection = ? WHERE id = 'keeper'",
            ('[null, "shared-listed"]',),
        )
        await db.execute(
            "UPDATE sessions SET first_msg_id = ? WHERE id = 'victim'", ("victim-direct",)
        )
        await db.create_progression("keeper-branch")
        await db.create_branch(
            {"id": "keeper-branch", "session_id": "keeper", "progression_id": "keeper-branch"}
        )
        await db.execute(
            "UPDATE branches SET system_msg_id = ? WHERE id = 'keeper-branch'", ("shared-direct",)
        )
        await db.execute("UPDATE sessions SET updated_at = ? WHERE id = 'keeper'", (time.time(),))
        # The prune must use a temporary table even when a permanent table has this name.
        await db.execute("CREATE TABLE prune_candidate_messages (id TEXT PRIMARY KEY)")
        await db.execute("INSERT INTO prune_candidate_messages VALUES ('permanent-table-marker')")

    assert await _prune_sessions(entry, ["victim", "empty-victim"]) == 2
    async with state_db.StateDB() as db:
        assert await db.fetch_all("SELECT id FROM sessions") == [{"id": "keeper"}]
        assert await db.get_progression("keeper") == [None, "shared-listed"]
        assert {row["id"] for row in await db.fetch_all("SELECT id FROM progressions")} == {
            "keeper",
            "keeper-branch",
        }
        assert {row["id"] for row in await db.fetch_all("SELECT id FROM messages")} == {
            "shared-listed",
            "shared-direct",
        }
        assert await db.fetch_all("SELECT id FROM prune_candidate_messages") == [
            {"id": "permanent-table-marker"}
        ]


@pytest.mark.usefixtures("sqlite_store")
@pytest.mark.parametrize("entry", ["explicit", "age"])
async def test_sqlite_shared_prune_preserves_surviving_message_references(entry):
    await _check_message_references(entry)


@pytest.mark.parametrize("entry", ["explicit", "age"])
async def test_postgresql_shared_prune_preserves_surviving_message_references(
    pg_url, monkeypatch, entry
):
    async with _fresh_store(pg_url, monkeypatch):
        await _check_message_references(entry)


async def test_postgresql_shared_prune_removes_an_empty_victim_progression(pg_url, monkeypatch):
    async with _fresh_store(pg_url, monkeypatch):
        async with state_db.StateDB() as db:
            await _terminal(db, "empty-victim")

        assert await db_maintenance.prune_terminal_sessions_by_id(["empty-victim"]) == 1
        async with state_db.StateDB() as db:
            assert await db.get_session("empty-victim") is None
            assert await db.fetch_all("SELECT id FROM progressions") == []


async def test_shared_prune_refuses_an_unknown_dialect():
    conn = SimpleNamespace(dialect=SimpleNamespace(name="unknown"))
    with pytest.raises(ValueError, match="Session prune does not support 'unknown'"):
        await db_maintenance._prune_session_chunk(
            conn,
            ["victim"],
            sess_ph="",
            retention_sql="",
            retention_params=(),
            archive_destination=None,
            archive_id="",
        )


async def test_postgresql_shared_prune_plan_avoids_rescanning_retained_members(pg_url, monkeypatch):
    retained, victims = 4_000, 3_000
    now = time.time()
    async with _fresh_store(pg_url, monkeypatch) as (store_url, _raw):
        async with state_db.StateDB(url=store_url) as db:
            await _terminal(db, "victim")
            async with db.transaction() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO messages (id, created_at, content, role, lion_class) "
                        "SELECT 'm-' || n, :now, '{}', 'user', 0 "
                        "FROM generate_series(1, CAST(:total AS integer)) AS g(n)"
                    ),
                    {"now": now, "total": retained + victims},
                )
                await conn.execute(
                    text(
                        "INSERT INTO progressions (id, created_at, collection) "
                        "SELECT 'p-' || n, :now, jsonb_build_array(NULL, 'm-' || n)::text "
                        "FROM generate_series(1, CAST(:retained AS integer)) AS g(n)"
                    ),
                    {"now": now, "retained": retained},
                )
                await conn.execute(
                    text(
                        "UPDATE progressions SET collection = ("
                        "  SELECT jsonb_agg('m-' || n)::text "
                        "  FROM generate_series(CAST(:first AS integer), CAST(:last AS integer)) AS g(n)"
                        ") WHERE id = 'victim'"
                    ),
                    {"first": retained, "last": retained + victims},
                )
                await conn.execute(text("ANALYZE messages"))
                await conn.execute(text("ANALYZE progressions"))

        with _capture_prune_delete_plans(
            monkeypatch, "messages", work_mem="4MB", anchor="retained_messages"
        ) as plans:
            assert await db_maintenance.prune_terminal_sessions_by_id(["victim"]) == 1
        assert len(plans) == 1
        plan = plans[0]
        _assert_no_repeated_survivor_scans(plan, {"progressions"})
        progression_scans = [
            node
            for node in _plan_nodes(plan)
            if node.get("Relation Name") == "progressions" and node["Actual Loops"]
        ]
        assert sum(node["Actual Loops"] for node in progression_scans) == 1, plan
        assert sum(node["Actual Rows"] for node in progression_scans) == retained, plan
        async with state_db.StateDB(url=store_url) as db:
            rows = await db.fetch_all("SELECT id FROM messages")
            assert {row["id"] for row in rows} == {f"m-{n}" for n in range(1, retained + 1)}


@pytest.mark.asyncio(loop_scope="function")
async def test_postgresql_orphan_progression_delete_probes_instead_of_rescanning(
    pg_url, monkeypatch
):
    survivors, victims = 3_000, 200
    now = time.time()
    async with _fresh_store(pg_url, monkeypatch) as (store_url, _raw):
        async with state_db.StateDB(url=store_url) as db:
            await _terminal(db, "victim")
            await _terminal(db, "victim-shared-branch")
            await _terminal(db, "victim-shared-session")
            await db.create_progression("keeper")
            await db.create_session(
                {"id": "keeper", "progression_id": "keeper", "status": "running"}
            )
            async with db.transaction() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO progressions (id, created_at, collection) "
                        "SELECT 'kp-' || n, :now, '[]' "
                        "FROM generate_series(1, CAST(:total AS integer)) AS g(n)"
                    ),
                    {"now": now, "total": survivors},
                )
                await conn.execute(
                    text(
                        "INSERT INTO progressions (id, created_at, collection) "
                        "SELECT 'vp-' || n, :now, '[]' "
                        "FROM generate_series(1, CAST(:total AS integer)) AS g(n)"
                    ),
                    {"now": now, "total": victims},
                )
                await conn.execute(
                    text(
                        "INSERT INTO branches (id, created_at, session_id, progression_id) "
                        "SELECT 'kb-' || n, :now, 'keeper', 'kp-' || n "
                        "FROM generate_series(1, CAST(:total AS integer)) AS g(n)"
                    ),
                    {"now": now, "total": survivors},
                )
                await conn.execute(
                    text(
                        "INSERT INTO branches (id, created_at, session_id, progression_id) "
                        "SELECT 'vb-' || n, :now, 'victim', 'vp-' || n "
                        "FROM generate_series(1, CAST(:total AS integer)) AS g(n)"
                    ),
                    {"now": now, "total": victims},
                )
                await conn.execute(
                    text(
                        "INSERT INTO sessions (id, created_at, updated_at, progression_id, status) "
                        "SELECT 'ks-' || n, :now, :now, 'kp-' || n, 'running' "
                        "FROM generate_series(1, CAST(:total AS integer)) AS g(n)"
                    ),
                    {"now": now, "total": survivors},
                )
                await conn.execute(
                    text(
                        "INSERT INTO branches (id, created_at, session_id, progression_id) "
                        "VALUES ('kb-shared', :now, 'keeper', 'victim-shared-branch')"
                    ),
                    {"now": now},
                )
                await conn.execute(
                    text(
                        "INSERT INTO sessions (id, created_at, updated_at, progression_id, status) "
                        "VALUES ('ks-shared', :now, :now, 'victim-shared-session', 'running')"
                    ),
                    {"now": now},
                )
                await conn.execute(text("ANALYZE sessions"))
                await conn.execute(text("ANALYZE branches"))
                await conn.execute(text("ANALYZE progressions"))

        with _capture_prune_delete_plans(monkeypatch, "progressions", work_mem="64kB") as plans:
            assert (
                await db_maintenance.prune_terminal_sessions_by_id(
                    ["victim", "victim-shared-branch", "victim-shared-session"]
                )
                == 3
            )
        assert len(plans) == 1
        _assert_no_repeated_survivor_scans(plans[0], {"sessions", "branches"})
        async with state_db.StateDB(url=store_url) as db:
            rows = await db.fetch_all("SELECT id FROM progressions")
            remaining = {row["id"] for row in rows}
            assert remaining == {
                "keeper",
                "victim-shared-branch",
                "victim-shared-session",
                *(f"kp-{n}" for n in range(1, survivors + 1)),
            }


@pytest.mark.parametrize("entry", ["explicit", "age"])
async def test_postgresql_shared_prune_preserves_non_array_references_across_chunks(
    pg_url, monkeypatch, entry, caplog
):
    async with _fresh_store(pg_url, monkeypatch):
        monkeypatch.setattr(settings, "PRUNE_CHUNK_ROWS", 1)
        async with state_db.StateDB() as db:
            await _terminal(db, "deferred")
            await _terminal(db, "ordinary")
            ordinary_members = {"deferred-member", "direct", "object", "string", "1", "gone"}
            kept = (ordinary_members - {"gone"}) | {"shared-branch-only"}
            for id_ in sorted(kept | {"gone"}):
                await db.insert_message(
                    {"id": id_, "created_at": time.time(), "role": "user", "content": {}}
                )
            await db.set_progression("ordinary", sorted(ordinary_members))
            await db.create_progression("deferred-branch", ["shared-branch-only"])
            await db.create_branch(
                {
                    "id": "deferred-branch",
                    "session_id": "deferred",
                    "progression_id": "deferred-branch",
                    "system_msg_id": "direct",
                }
            )
            await db.create_branch(
                {
                    "id": "ordinary-branch",
                    "session_id": "ordinary",
                    "progression_id": "deferred-branch",
                }
            )
            for id_, collection in {
                "deferred": '{"ref":"deferred-member"}',
                "object-survivor": '{"ref":"object","number":1}',
                "string-survivor": '"string"',
                "null-survivor": "null",
            }.items():
                await db.create_progression(id_)
                await db.execute(
                    "UPDATE progressions SET collection = ? WHERE id = ?", (collection, id_)
                )

        with caplog.at_level(logging.INFO, logger="lionagi.studio.services.db_maintenance"):
            assert await _prune_sessions(entry, ["ordinary", "deferred"]) == 2

        async with state_db.StateDB() as db:
            assert await db.fetch_all("SELECT id FROM sessions") == []
            assert await db.fetch_all("SELECT id FROM branches") == []
            assert {row["id"] for row in await db.fetch_all("SELECT id FROM messages")} == kept
            assert await db.get_progression("deferred-branch") == ["shared-branch-only"]
            assert {row["id"] for row in await db.fetch_all("SELECT id FROM progressions")} == {
                "deferred",
                "deferred-branch",
                "object-survivor",
                "string-survivor",
                "null-survivor",
            }
        assert "deferred_victim_progressions=1 non_array_survivor_progressions=3" in caplog.text


@pytest.mark.parametrize("entry", ["explicit", "age"])
@pytest.mark.parametrize("invalid_progression", ["victim", "survivor"])
@pytest.mark.parametrize(
    ("collection", "error"),
    [
        ("[malformed", "invalid input syntax for type json"),
        (r'"\u0000"', "unsupported Unicode escape sequence"),
    ],
    ids=["malformed", "unrepresentable"],
)
async def test_postgresql_shared_prune_invalid_progression_rolls_back(
    pg_url, monkeypatch, entry, invalid_progression, collection, error
):
    from sqlalchemy.exc import DBAPIError

    async with _fresh_store(pg_url, monkeypatch):
        async with state_db.StateDB() as db:
            await _terminal(db, "victim")
            await db.create_progression("survivor")
            await db.insert_message(
                {"id": "victim-message", "created_at": time.time(), "role": "user", "content": {}}
            )
            await db.set_progression("victim", ["victim-message"])
            await db.execute(
                "UPDATE progressions SET collection = ? WHERE id = ?",
                (collection, invalid_progression),
            )
            await db.create_branch(
                {"id": "victim-branch", "session_id": "victim", "progression_id": "victim"}
            )

        async def snapshot():
            async with state_db.StateDB() as db:
                return (
                    await db.fetch_all("SELECT * FROM sessions ORDER BY id"),
                    await db.fetch_all("SELECT * FROM branches ORDER BY id"),
                    await db.fetch_all("SELECT * FROM progressions ORDER BY id"),
                    await db.fetch_all("SELECT * FROM messages ORDER BY id"),
                    await db.fetch_all("SELECT * FROM status_transitions ORDER BY id"),
                )

        before = await snapshot()
        with pytest.raises(DBAPIError, match=error):
            await _prune_sessions(entry, ["victim"])
        assert await snapshot() == before
