# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Dual-backend parity tests: SQLite (in-memory) + PostgreSQL.

SQLite leg always runs. The Postgres leg uses LIONAGI_TEST_PG_URL when set,
otherwise it auto-provisions a throwaway Postgres via testcontainers (Docker).
It is skipped locally only when neither is available, and is required to run in
CI (a missing backend there is a hard failure, never a silent skip).

Both legs run the same contract: create session, insert messages, check
progression, run update_status with reason, verify transition row written.
"""

from __future__ import annotations

import time
import uuid

import pytest

from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons

# ── Shared helpers ────────────────────────────────────────────────────────────


def _uid() -> str:
    return str(uuid.uuid4())


async def _run_parity_suite(db: StateDB) -> None:
    """Core contract verified against a live StateDB regardless of dialect."""
    from sqlalchemy import text

    prog_id = _uid()
    session_id = _uid()
    now = time.time()

    # 1. create_progression + create_session
    await db.create_progression(prog_id)
    await db.create_session(
        {
            "id": session_id,
            "progression_id": prog_id,
            "status": "running",
            "created_at": now,
            "updated_at": now,
        }
    )

    row = await db.get_session(session_id)
    assert row is not None, "session must be retrievable after create"
    assert row["status"] == "running"
    assert row["id"] == session_id

    # 1b. append_to_progression is ordered and idempotent (the json-array path)
    await db.append_to_progression(prog_id, "m-a")
    await db.append_to_progression(prog_id, "m-b")
    await db.append_to_progression(prog_id, "m-a")  # duplicate must be a no-op
    coll = await db.get_progression(prog_id)
    assert coll == ["m-a", "m-b"], f"progression append/idempotency failed: {coll!r}"

    # 1c. touch_session_activity is monotonic (GREATEST on pg / scalar MAX on sqlite)
    await db.touch_session_activity(session_id, at=now + 1000)
    bumped = (await db.get_session(session_id))["last_message_at"]
    await db.touch_session_activity(session_id, at=now - 1000)  # older ts must not regress
    held = (await db.get_session(session_id))["last_message_at"]
    assert held == bumped, "touch_session_activity must be monotonic"

    # 1d. update a reserved-word column ("user") through the dynamic SET builder.
    # PostgreSQL rejects an unquoted `user` identifier; the builder must quote it.
    await db.update_session(session_id, user="alice")
    assert (await db.get_session(session_id))["user"] == "alice"

    # 1e. create_branch + get_branch round-trip (branches INSERT also names "user")
    branch_id = _uid()
    await db.create_branch(
        {
            "id": branch_id,
            "session_id": session_id,
            "progression_id": prog_id,
            "user": "alice",
            "name": "main",
        }
    )
    br = await db.get_branch(branch_id)
    assert br is not None and br["user"] == "alice" and br["name"] == "main"

    # 2. insert_message + get_message roundtrip
    msg_id = _uid()
    await db.insert_message(
        {
            "id": msg_id,
            "created_at": now,
            "node_metadata": {"key": "val"},
            "content": {"text": "hello dual-backend"},
            "role": "user",
            "sender": "test",
            "recipient": "test",
            "channel": "c",
        }
    )
    msg = await db.get_message(msg_id)
    assert msg is not None, "message must be retrievable after insert"
    content = msg["content"]
    if isinstance(content, str):
        import json

        content = json.loads(content)
    assert content["text"] == "hello dual-backend"

    # 3. update_status writes denormalized + transition row
    await db.update_status(
        entity_type="session",
        entity_id=session_id,
        new_status="completed",
        reason_code=RunReasons.COMPLETED_OK,
        reason_summary="parity test completed.",
        source="executor",
    )

    updated = await db.get_session(session_id)
    assert updated is not None
    assert updated["status"] == "completed"
    assert updated["status_reason_code"] == RunReasons.COMPLETED_OK

    # 4. status_transitions row was written
    async with db._read() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT entity_type, previous_status, status, reason_code "
                        "FROM status_transitions WHERE entity_id = :id"
                    ),
                    {"id": session_id},
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1
    t = dict(rows[0])
    assert t["entity_type"] == "session"
    assert t["previous_status"] == "running"
    assert t["status"] == "completed"
    assert t["reason_code"] == RunReasons.COMPLETED_OK

    # 5. get_session returns None for a missing id
    assert await db.get_session(_uid()) is None

    # 6. schema_version is '1'
    ver = await db.schema_version()
    assert ver == "1", f"schema_version must be '1', got {ver!r}"

    # 7. insert_session_signal assigns sequential seq (MAX+1 path / PG advisory lock)
    s1 = await db.insert_session_signal(
        session_id=session_id, kind="started", ts=now, payload={"a": 1}
    )
    s2 = await db.insert_session_signal(
        session_id=session_id, kind="progress", ts=now + 1, payload={"b": 2}
    )
    assert (s1, s2) == (1, 2), f"signal seq must be 1,2; got {(s1, s2)}"
    sigs = await db.get_session_signals_after(session_id, 0)
    assert [s["seq"] for s in sigs] == [1, 2], f"signals must be ordered: {sigs!r}"
    p0 = sigs[0]["payload"]
    if isinstance(p0, str):
        import json as _json

        p0 = _json.loads(p0)
    assert p0 == {"a": 1}, f"signal payload roundtrip failed: {p0!r}"

    # 8. list_invocations takes project from the latest-updated session (the
    #    ROW_NUMBER path that replaced a SQLite-only GROUP BY ... HAVING MAX),
    #    and list_projects groups by the projects PK. Both are PG-strict.
    inv_id = _uid()
    await db.create_invocation({"id": inv_id, "skill": "parity", "started_at": now})
    prog2 = _uid()
    await db.create_progression(prog2)
    await db.create_session(
        {
            "id": _uid(),
            "progression_id": prog2,
            "status": "running",
            "created_at": now,
            "updated_at": now,
            "invocation_id": inv_id,
            "project": "proj-old",
        }
    )
    await db.create_session(
        {
            "id": _uid(),
            "progression_id": prog2,
            "status": "running",
            "created_at": now,
            "updated_at": now + 100,  # newer → its project must win
            "invocation_id": inv_id,
            "project": "proj-new",
        }
    )
    mine = [r for r in await db.list_invocations() if r["id"] == inv_id]
    assert len(mine) == 1, f"invocation must appear exactly once: {mine!r}"
    assert mine[0]["project"] == "proj-new", f"latest session's project must win: {mine[0]!r}"

    # create_session upserts each session's project (register_project), so
    # list_projects exercises the GROUP BY p.name (projects PK) path on PG here
    # without a redundant create_project insert.
    listed = {p["name"] for p in await db.list_projects()}
    assert {"proj-old", "proj-new"} <= listed, f"both projects must be listed: {listed!r}"


# ── SQLite leg (always runs) ──────────────────────────────────────────────────


@pytest.fixture
async def sqlite_db():
    db = StateDB(":memory:")
    await db.open()
    yield db
    await db.close()


async def test_sqlite_parity(sqlite_db: StateDB):
    """Full parity suite against SQLite in-memory."""
    assert sqlite_db.dialect == "sqlite"
    await _run_parity_suite(sqlite_db)


# ── SQLite regression: singleton keying by URL ─────────────────────────────────


async def test_sqlite_singleton_keyed_by_url(tmp_path):
    """register_shared_db / get_shared_db round-trip uses URL string key."""
    from lionagi.state.db import get_shared_db, register_shared_db, unregister_shared_db

    db_path = tmp_path / "singleton.db"
    db = StateDB(db_path)
    await db.open()
    try:
        await register_shared_db(db)
        got = await get_shared_db(db_path)
        assert got is db, "get_shared_db must return the registered instance"
    finally:
        unregister_shared_db(db)
        await db.close()


# ── SQLite regression: multiple concurrent writes (WAL) ───────────────────────


async def test_sqlite_concurrent_writes(tmp_path):
    """50 concurrent insert_message calls on SQLite must all succeed."""
    import asyncio

    db_path = tmp_path / "concurrent.db"
    db = StateDB(db_path)
    await db.open()
    try:
        msgs = [
            {
                "id": _uid(),
                "created_at": time.time(),
                "node_metadata": {},
                "content": {"n": i},
                "role": "user",
                "sender": "x",
                "recipient": "y",
                "channel": "c",
            }
            for i in range(50)
        ]
        await asyncio.gather(*[db.insert_message(m) for m in msgs])

        from sqlalchemy import text

        async with db._read() as conn:
            count = (
                (await conn.execute(text("SELECT COUNT(*) AS n FROM messages")))
                .mappings()
                .first()["n"]
            )
        assert count == 50, f"expected 50 rows, got {count}"
    finally:
        await db.close()


# ── Postgres leg (pg_url fixture: testcontainers, or LIONAGI_TEST_PG_URL) ─────


async def test_postgres_parity(pg_url):
    """Full parity suite against a live PostgreSQL instance."""
    db = StateDB(url=pg_url)
    await db.open()
    try:
        assert db.dialect == "postgresql"
        await _run_parity_suite(db)
    finally:
        await db.close()


async def test_postgres_schema_creates_all_tables(pg_url):
    """metadata.create_all() produces the expected set of tables in Postgres."""
    import sqlalchemy as sa

    db = StateDB(url=pg_url)
    await db.open()
    try:
        async with db._read() as conn:
            tables = await conn.run_sync(
                lambda sync_conn: set(sa.inspect(sync_conn).get_table_names())
            )
        expected = {
            "messages",
            "message_types",
            "progressions",
            "sessions",
            "branches",
            "definitions",
            "schema_meta",
            "status_transitions",
        }
        missing = expected - tables
        assert not missing, f"Postgres missing tables: {missing}"
    finally:
        await db.close()


# ── Dialect SQL correctness (static — no live connection, always runs) ─────────
# Guards the Postgres-breaking SQL forms that the gated live leg above would only
# catch when LIONAGI_TEST_PG_URL is set.


def test_pg_progression_append_binds_v():
    """to_jsonb(CAST(:v AS text)) keeps :v bindable; :v::text would not."""
    from sqlalchemy import text

    sql = StateDB._progression_append_sql("postgresql")
    assert ":v::" not in sql, "':v::' prevents text() from binding :v"
    binds = text(sql).compile().params
    assert "v" in binds and "id" in binds, f"binds not recognized: {binds}"


def test_sqlite_progression_append_uses_json_insert():
    sql = StateDB._progression_append_sql("sqlite")
    assert "json_insert" in sql and "json_each" in sql


def test_pg_touch_activity_uses_greatest():
    """Postgres timestamp-monotonic update must use GREATEST, not scalar MAX()."""
    pg = StateDB._touch_activity_sql("postgresql")
    assert "GREATEST(" in pg and "MAX(" not in pg
    sqlite = StateDB._touch_activity_sql("sqlite")
    assert "MAX(" in sqlite and "GREATEST(" not in sqlite


def test_to_named_skips_question_mark_in_string_literal():
    sql, params = StateDB._to_named("SELECT '?' AS q, ? AS v", ["x"])
    assert sql == "SELECT '?' AS q, :p0 AS v"
    assert params == {"p0": "x"}


def test_to_named_skips_question_mark_in_like_pattern():
    sql, params = StateDB._to_named("SELECT * FROM t WHERE name LIKE '%?%' AND id = ?", [5])
    assert sql == "SELECT * FROM t WHERE name LIKE '%?%' AND id = :p0"
    assert params == {"p0": 5}


def test_to_named_doubled_quote_escape():
    sql, params = StateDB._to_named("SELECT 'a''?b', ?", ["v"])
    assert sql == "SELECT 'a''?b', :p0"
    assert params == {"p0": "v"}


def test_to_named_count_mismatch_raises():
    with pytest.raises(ValueError, match="param count mismatch"):
        StateDB._to_named("SELECT ?, ?", [1])


def test_to_named_named_dict_passthrough():
    sql, params = StateDB._to_named("SELECT :a", {"a": 1})
    assert sql == "SELECT :a"
    assert params == {"a": 1}
