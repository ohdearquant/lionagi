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

import json
import time
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from lionagi.state.db import SCHEMA_VERSION, StateDB
from lionagi.state.reasons import RunReasons

# Shared helpers


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
    embedding = [0.125, -0.25, 1.5, 0.0]
    await db.insert_message(
        {
            "id": msg_id,
            "created_at": now,
            "node_metadata": {"key": "val"},
            "content": {"text": "hello dual-backend"},
            "embedding": embedding,
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
    assert msg["embedding"] == pytest.approx(embedding)
    async with db._read() as conn:
        stored_embedding = (
            await conn.execute(
                text("SELECT embedding FROM messages WHERE id = :id"),
                {"id": msg_id},
            )
        ).first()[0]
    assert isinstance(stored_embedding, bytes)
    assert len(stored_embedding) == 4 * len(embedding)

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

    # 4. Creation and transition history were written.
    async with db._read() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT entity_type, previous_status, status, reason_code "
                        "FROM status_transitions WHERE entity_id = :id "
                        "ORDER BY created_at"
                    ),
                    {"id": session_id},
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 2
    initial = dict(rows[0])
    assert initial == {
        "entity_type": "session",
        "previous_status": None,
        "status": "running",
        "reason_code": RunReasons.STARTED_OK,
    }
    t = dict(rows[1])
    assert t["entity_type"] == "session"
    assert t["previous_status"] == "running"
    assert t["status"] == "completed"
    assert t["reason_code"] == RunReasons.COMPLETED_OK

    # 5. get_session returns None for a missing id
    assert await db.get_session(_uid()) is None

    # 6. schema_version is the version this code applies
    ver = await db.schema_version()
    assert ver == SCHEMA_VERSION, f"schema_version must be {SCHEMA_VERSION!r}, got {ver!r}"

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

    # A zero-session invocation must still appear (LEFT JOIN, not INNER) with a
    # NULL project — the ROW_NUMBER subquery matches nothing for it.
    empty_inv = _uid()
    await db.create_invocation({"id": empty_inv, "skill": "parity", "started_at": now})
    empties = [r for r in await db.list_invocations() if r["id"] == empty_inv]
    assert len(empties) == 1, f"zero-session invocation must appear once: {empties!r}"
    assert empties[0]["project"] is None, f"zero-session project must be None: {empties[0]!r}"


# SQLite leg (always runs)


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


# SQLite regression: singleton keying by URL


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


# SQLite regression: multiple concurrent writes (WAL)


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


# Postgres leg (pg_url fixture: testcontainers, or LIONAGI_TEST_PG_URL)


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


async def test_postgres_open_widens_a_sessions_check_that_predates_a_value(pg_url):
    """An existing Postgres store keeps whatever CHECK its sessions table was
    created with, because ``create_all`` only creates missing tables. SQLite
    gets the widening by rebuilding the table, and that path returns early on
    every other dialect, so a value added to the declared vocabulary was
    rejected by exactly the store that had been running longest.

    The narrow definition is written back here rather than assumed, so the
    test starts from the state it is about, and the rows and the constraint
    are put back on the way out: ``pg_url`` is session-scoped, so anything
    left behind here is the next test's starting state.

    Every arm matters. ``agent`` says the widened constraint did not lose the
    values the store already had, and ``not-a-kind`` says it is still a
    constraint rather than a vacuous one that would pass by accepting
    everything.
    """
    from sqlalchemy import text

    narrow = (
        "invocation_kind IS NULL OR invocation_kind IN ('agent','play','flow','fanout','show-play')"
    )
    written: list[str] = []

    async def insert(db: StateDB, kind: str | None) -> bool:
        """INSERT below create_session, whose own validation would answer first."""
        prog_id, session_id = _uid(), _uid()
        await db.create_progression(prog_id)
        try:
            async with db._engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO sessions "
                        "(id, progression_id, created_at, updated_at, invocation_kind) "
                        "VALUES (:i, :p, 1.0, 1.0, :k)"
                    ),
                    {"i": session_id, "p": prog_id, "k": kind},
                )
        except IntegrityError:
            return False
        written.append(session_id)
        return True

    async def set_constraint(db: StateDB, body: str) -> None:
        async with db._engine.begin() as conn:
            await conn.execute(
                text("ALTER TABLE sessions DROP CONSTRAINT IF EXISTS ck_sessions_invocation_kind")
            )
            await conn.execute(
                text(
                    "ALTER TABLE sessions ADD CONSTRAINT ck_sessions_invocation_kind "
                    f"CHECK ({body})"
                )
            )

    db = StateDB(url=pg_url)
    await db.open()
    try:
        async with db._read() as conn:
            declared = (
                await conn.execute(
                    text(
                        "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
                        "JOIN pg_class t ON t.oid = c.conrelid "
                        "WHERE t.relname = 'sessions' "
                        "AND c.conname = 'ck_sessions_invocation_kind'"
                    )
                )
            ).scalar()
        assert declared and "engine" in declared, f"store did not start widened: {declared!r}"
        await set_constraint(db, narrow)
        assert not await insert(db, "engine"), "the narrow constraint was not in place"
    finally:
        await db.close()

    db = StateDB(url=pg_url)
    await db.open()
    try:
        assert await insert(db, "engine"), "open() did not widen the constraint"
        assert await insert(db, "agent"), "widening dropped a value the store already had"
        assert await insert(db, None), "widening stopped admitting NULL"
        assert not await insert(db, "not-a-kind"), "the replacement constraint is vacuous"
    finally:
        # Rows first: a row the narrow constraint rejects blocks re-narrowing,
        # which is what a later run of this same test starts by doing.
        if written:
            async with db._engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM sessions WHERE id = ANY(:ids)"), {"ids": written}
                )
        await db.close()


async def test_postgres_open_does_not_add_a_sessions_check_that_was_absent(pg_url):
    """A column with no CHECK already accepts every value, so there is nothing
    to widen and a store that dropped one deliberately does not get it back.
    Same reading the SQLite rebuild applies."""
    from sqlalchemy import text

    async def constraint_def(db: StateDB) -> str | None:
        async with db._read() as conn:
            return (
                await conn.execute(
                    text(
                        "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
                        "JOIN pg_class t ON t.oid = c.conrelid "
                        "WHERE t.relname = 'sessions' "
                        "AND c.conname = 'ck_sessions_invocation_kind'"
                    )
                )
            ).scalar()

    db = StateDB(url=pg_url)
    await db.open()
    declared = await constraint_def(db)
    try:
        assert declared is not None, "nothing to drop; test is inert"
        async with db._engine.begin() as conn:
            await conn.execute(
                text("ALTER TABLE sessions DROP CONSTRAINT ck_sessions_invocation_kind")
            )
    finally:
        await db.close()

    db = StateDB(url=pg_url)
    await db.open()
    try:
        assert await constraint_def(db) is None
    finally:
        async with db._engine.begin() as conn:
            await conn.execute(
                text("ALTER TABLE sessions DROP CONSTRAINT IF EXISTS ck_sessions_invocation_kind")
            )
            await conn.execute(
                text(f"ALTER TABLE sessions ADD CONSTRAINT ck_sessions_invocation_kind {declared}")
            )
        await db.close()


# Dialect SQL correctness (static — no live connection, always runs)


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


def test_pg_merge_node_metadata_sql_never_strips_the_whole_document():
    """jsonb_strip_nulls over the entire merged document deletes nulls that
    predate the patch. Cheap and always-run (no live Postgres needed) so a
    reintroduction fails fast,
    ahead of the live-Postgres parity test in test_merge_node_metadata_dialect_parity."""
    sql = StateDB._merge_node_metadata_sql("postgresql")
    assert "jsonb_strip_nulls" not in sql, sql


def test_sqlite_merge_node_metadata_sql_uses_json_patch():
    sql = StateDB._merge_node_metadata_sql("sqlite")
    assert "json_patch" in sql


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


async def test_postgres_capability_claim(pg_url):
    """A capability-bearing queued task is claimable on live Postgres.

    Exercises the two dialect-sensitive seams in the worker claim path at
    once: JSON columns come back as native Python values (not strings) on
    Postgres, and the keyset pager's cursor-less first page must not send a
    nullable bind parameter asyncpg cannot type.
    """
    from lionagi.studio.scheduler.worker import claim_and_execute, register_heartbeat
    from lionagi.studio.services.task_applications import TaskApplication, submit_task

    db = StateDB(url=pg_url)
    await db.open()
    try:
        run_id = await submit_task(
            db,
            TaskApplication(
                action_kind="agent",
                args={"prompt": "x"},
                execution_target="host",
                required_capabilities=["lean-toolchain"],
            ),
        )
        await register_heartbeat(
            db,
            worker_id="w-pg",
            advertised_capabilities=["lean-toolchain"],
            execution_targets=["host"],
        )

        async def execute(row):
            return 0, ""

        claimed = await claim_and_execute(
            db,
            worker_id="w-pg",
            execute=execute,
            advertised_capabilities=["lean-toolchain"],
            execution_targets=["host"],
        )
        assert claimed == 1
        async with db._read() as conn:
            from sqlalchemy import text as sa_text

            status = (
                (
                    await conn.execute(
                        sa_text("SELECT status FROM schedule_runs WHERE id = :id"),
                        {"id": run_id},
                    )
                )
                .mappings()
                .first()["status"]
            )
        assert status == "completed"
    finally:
        await db.close()


# merge_session_node_metadata: dialect-parity table
# Postgres's jsonb_strip_nulls, applied to the *entire* merged document,
# deletes nulls that predate the patch and were never touched by it --
# SQLite's json_patch never removes them. A real NodeStarted flow segment
# (lionagi/cli/orchestrate/flow.py) carries ended_at: None and
# last_heartbeat_at: None inside a "segments" array patch value, so this was
# reachable from production, not hypothetical. The fix computes the
# null-deletion set from the patch alone instead of stripping the whole
# document; this table pins every divergent case a live sqlite/postgres
# comparison surfaces, plus the SQL-NULL/JSON-null/non-object forensic arms,
# against both backends and asserts they land on the identical value.


async def _seed_node_metadata(db: StateDB, session_id: str, value) -> None:
    """A bare session with node_metadata set to *value* verbatim -- the JSON
    bind type accepts a dict, list, scalar, or None as-is."""
    prog_id = _uid()
    await db.create_progression(prog_id)
    await db.create_session(
        {"id": session_id, "progression_id": prog_id, "status": "running", "node_metadata": value}
    )


def _node_metadata(row: dict) -> object:
    v = row["node_metadata"]
    if isinstance(v, str):
        v = json.loads(v)
    return v


def _pop_discarded_at(d: object) -> object:
    """_discarded_at is time.time() at merge time -- assert its shape, not
    its value, and hand back the rest of the document for an exact compare."""
    if isinstance(d, dict) and "_discarded_at" in d:
        d = dict(d)
        ts = d.pop("_discarded_at")
        assert isinstance(ts, float), f"_discarded_at must be a float timestamp, got {ts!r}"
    return d


# (name, initial node_metadata, patch, expected merged result)
_MERGE_PARITY_CASES = [
    ("flat_key_collision", {"a": 1}, {"a": 2}, {"a": 2}),
    ("disjoint_top_level_key", {"a": 1}, {"b": 2}, {"a": 1, "b": 2}),
    ("root_patch_null_removes_key", {"a": 1, "b": 2}, {"b": None}, {"a": 1}),
    ("root_patch_null_on_absent_key_is_noop", {"a": 1}, {"c": None}, {"a": 1}),
    (
        "existing_untouched_nested_null_survives",
        {"nested": {"nullable": None, "keep": 1}},
        {"other": 2},
        {"nested": {"nullable": None, "keep": 1}, "other": 2},
    ),
    (
        # The production shape: a top-level array patch value whose elements
        # carry nulls (flow.py's segment records), rather than a synthetic
        # nested-object patch.
        "patch_array_with_nested_nulls_survives",
        {},
        {"segments": [{"op_id": "x", "ended_at": None, "last_heartbeat_at": None}]},
        {"segments": [{"op_id": "x", "ended_at": None, "last_heartbeat_at": None}]},
    ),
]

# (name, initial non-object node_metadata, patch, expected result minus _discarded_at)
_MERGE_PARITY_NONOBJECT_CASES = [
    (
        "existing_array_wrapped_nested_null_survives",
        [1, {"nested_null": None}],
        {"added": 2},
        {"_discarded_node_metadata": [1, {"nested_null": None}], "added": 2},
    ),
    (
        "existing_scalar_wrapped",
        42,
        {"x": 1},
        {"_discarded_node_metadata": 42, "x": 1},
    ),
]


async def test_merge_node_metadata_dialect_parity(sqlite_db: StateDB, pg_url):
    """The same patch table lands on the identical value on SQLite and
    PostgreSQL. Manually verified to discriminate: reintroducing
    jsonb_strip_nulls over the whole merged document in the Postgres branch of
    _merge_node_metadata_sql turns existing_untouched_nested_null_survives and
    patch_array_with_nested_nulls_survives red on the Postgres leg."""
    from sqlalchemy import text

    pg = StateDB(url=pg_url)
    await pg.open()
    executed = 0
    try:
        assert pg.dialect == "postgresql"
        for name, initial, patch, expected in _MERGE_PARITY_CASES:
            for db in (sqlite_db, pg):
                sid = _uid()
                await _seed_node_metadata(db, sid, initial)
                await db.merge_session_node_metadata(sid, patch)
                got = _node_metadata(await db.get_session(sid))
                assert got == expected, (
                    f"{name} on {db.dialect}: expected {expected!r}, got {got!r}"
                )
                executed += 1

        for name, initial, patch, expected in _MERGE_PARITY_NONOBJECT_CASES:
            for db in (sqlite_db, pg):
                sid = _uid()
                await _seed_node_metadata(db, sid, initial)
                await db.merge_session_node_metadata(sid, patch)
                got = _pop_discarded_at(_node_metadata(await db.get_session(sid)))
                assert got == expected, (
                    f"{name} on {db.dialect}: expected {expected!r}, got {got!r}"
                )
                executed += 1

        # True SQL NULL (the column's state before any session ever set it) is
        # treated as an absent object to merge into, on both dialects.
        for db in (sqlite_db, pg):
            sid = _uid()
            await _seed_node_metadata(db, sid, {"tmp": 1})
            async with db._tx() as conn:
                await conn.execute(
                    text("UPDATE sessions SET node_metadata = NULL WHERE id = :id"), {"id": sid}
                )
            await db.merge_session_node_metadata(sid, {"x": 1})
            got = _node_metadata(await db.get_session(sid))
            assert got == {"x": 1}, f"SQL NULL on {db.dialect}: got {got!r}"
            executed += 1

        # JSON null (create_session's default when node_metadata is omitted --
        # SQLAlchemy's JSON bind serializes Python None to the JSON null
        # literal, not SQL NULL) is likewise absent, not a foreign shape.
        for db in (sqlite_db, pg):
            sid = _uid()
            prog_id = _uid()
            await db.create_progression(prog_id)
            await db.create_session({"id": sid, "progression_id": prog_id, "status": "running"})
            await db.merge_session_node_metadata(sid, {"x": 1})
            got = _node_metadata(await db.get_session(sid))
            assert got == {"x": 1}, f"JSON null default on {db.dialect}: got {got!r}"
            executed += 1
    finally:
        await pg.close()

    # An empty case table would pass silently and look identical to a real
    # comparison run -- assert the exact population this test is derived
    # from, computed independently of the loops above, and print it so a
    # future reader (or CI log) can tell "compared N cases" from "compared
    # nothing" without re-deriving the arithmetic by hand.
    expected_executed = (
        len(_MERGE_PARITY_CASES) * 2 + len(_MERGE_PARITY_NONOBJECT_CASES) * 2 + 2 + 2
    )
    print(f"merge_node_metadata_dialect_parity: {executed} dialect comparisons executed")
    assert executed == expected_executed, (
        f"expected {expected_executed} dialect comparisons "
        f"(({len(_MERGE_PARITY_CASES)} + {len(_MERGE_PARITY_NONOBJECT_CASES)}) * 2 dialects "
        f"+ 2 SQL-NULL + 2 JSON-null), got {executed} -- a case table shrank or a loop stopped "
        "short without failing an assertion above"
    )


# (name, initial node_metadata, patch) -- every row here must raise ValueError
# on both dialects before any SQL runs.
_MERGE_REJECTS_NESTED_CASES = [
    ("synthetic_nested_object", {}, {"nested": {"a": 1}}),
    (
        # The exact input measured to diverge between dialects while this fix
        # was developed: sqlite's json_patch would merge
        # {"nullable": None, "value": 1} into the stored "nested" key
        # recursively, while Postgres's jsonb `||` would replace any existing
        # "nested" key with it shallowly instead. Permanently pinned here so
        # the refusal contract keeps covering the exact input that motivated
        # it, not just a synthetic stand-in for the same shape.
        "first_measured_divergence",
        {},
        {"nested": {"nullable": None, "value": 1}},
    ),
]


async def test_merge_node_metadata_rejects_nested_object_patch_on_both_dialects(
    sqlite_db: StateDB, pg_url
):
    """A patch value that is itself a dict is refused on every dialect,
    identically, before any SQL runs -- sqlite's json_patch would merge it
    recursively and Postgres's jsonb `||` would replace it shallowly, so
    allowing it would persist different state per backend."""
    pg = StateDB(url=pg_url)
    await pg.open()
    try:
        for name, initial, patch in _MERGE_REJECTS_NESTED_CASES:
            for db in (sqlite_db, pg):
                sid = _uid()
                await _seed_node_metadata(db, sid, initial)
                with pytest.raises(ValueError, match="nested object patch"):
                    await db.merge_session_node_metadata(sid, patch)
    finally:
        await pg.close()


async def test_merge_node_metadata_malformed_existing_text_sqlite_only(sqlite_db: StateDB):
    """Non-JSON text in node_metadata is a sqlite-only state: Postgres's typed
    json column rejects invalid text at write time (asserted in
    test_postgres_json_column_rejects_non_json_text below), so this case can
    only be reached on sqlite. There, merge preserves it verbatim under
    _discarded_node_metadata instead of dropping it."""
    from sqlalchemy import bindparam, text
    from sqlalchemy.types import String

    sid = _uid()
    prog_id = _uid()
    await sqlite_db.create_progression(prog_id)
    await sqlite_db.create_session({"id": sid, "progression_id": prog_id, "status": "running"})
    async with sqlite_db._tx() as conn:
        await conn.execute(
            text("UPDATE sessions SET node_metadata = :raw WHERE id = :id").bindparams(
                bindparam("raw", type_=String)
            ),
            {"raw": "not json {{{", "id": sid},
        )

    await sqlite_db.merge_session_node_metadata(sid, {"x": 1})
    got = _pop_discarded_at(_node_metadata(await sqlite_db.get_session(sid)))
    assert got == {"_discarded_node_metadata": "not json {{{", "x": 1}


async def test_postgres_json_column_rejects_non_json_text(pg_url):
    """The dialect difference the sqlite-only arm above depends on: Postgres's
    typed json column refuses invalid text before merge_session_node_metadata
    is ever reached, so the malformed-text arm cannot occur on that backend."""
    from sqlalchemy import bindparam, text
    from sqlalchemy.types import String

    db = StateDB(url=pg_url)
    await db.open()
    try:
        sid = _uid()
        prog_id = _uid()
        await db.create_progression(prog_id)
        await db.create_session({"id": sid, "progression_id": prog_id, "status": "running"})
        with pytest.raises(Exception):  # noqa: B017, PT011 -- backend-specific DBAPI error
            async with db._tx() as conn:
                await conn.execute(
                    text("UPDATE sessions SET node_metadata = :raw WHERE id = :id").bindparams(
                        bindparam("raw", type_=String)
                    ),
                    {"raw": "not json {{{", "id": sid},
                )
    finally:
        await db.close()


# Postgres leg: lifecycle service load-bearing contract (ADR-0058 Phase 2)
# The applied/conflict/rejected/rollback/parity cases pinned against SQLite in
# tests/state/lifecycle/test_service.py and test_wrapper_parity.py must hold
# identically on PostgreSQL (FOR UPDATE locking, JSON binding, transaction
# rollback, and guarded-update rowcount are backend-specific). These live in
# this module — which already owns the `pg_url` fixture — rather than adding
# a second and third module's own `pg_url` consumer: multiple modules
# requesting the session-scoped `pg_url` fixture in the same run corrupts
# asyncpg's event-loop-bound connection state across module boundaries
# (reproducible: "attached to a different loop" RuntimeError on the second
# module's first checkout) — a pre-existing fragility of this fixture
# combination that a second file should not paper over by working around it.


async def _pg_make_session(db: StateDB, *, status: str = "running") -> str:
    prog_id = _uid()
    await db.create_progression(prog_id)
    sid = _uid()
    await db.create_session({"id": sid, "progression_id": prog_id, "status": status})
    return sid


async def _pg_make_schedule_run(db: StateDB, *, status: str = "queued") -> str:
    sched_id = _uid()
    await db.create_schedule(
        {
            "id": sched_id,
            "name": f"sched-{sched_id}",
            "trigger_type": "interval",
            "interval_sec": 60,
            "action_kind": "agent",
        }
    )
    run_id = _uid()
    await db.create_schedule_run(
        {
            "id": run_id,
            "schedule_id": sched_id,
            "trigger_context": {},
            "action_kind": "agent",
            "action_args": [],
            "status": status,
            "fired_at": time.time(),
        }
    )
    return run_id


async def test_postgres_lifecycle_service_applied_conflict_rejected(pg_url):
    """Load-bearing D1 applied/conflict/rejected contract, on Postgres —
    mirrors tests/state/lifecycle/test_service.py's SQLite-only cases."""
    from lionagi.state.lifecycle import ActorRecord, ReasonRecord, TransitionCommand
    from lionagi.state.lifecycle.service import SQLAlchemyLifecycleService

    def _command(**overrides):
        base = dict(
            entity_type="session",
            entity_id="",
            to_status="completed",
            reason=ReasonRecord(code="session.stale.no_heartbeat"),
            actor=ActorRecord(type="executor", id="executor"),
        )
        base.update(overrides)
        return TransitionCommand(**base)

    db = StateDB(url=pg_url)
    await db.open()
    try:
        service = SQLAlchemyLifecycleService(db)

        # applied
        sid = await _pg_make_session(db, status="running")
        outcome = await service.transition(_command(entity_id=sid, to_status="completed"))
        assert outcome.result == "applied"
        assert outcome.previous_status == "running"
        assert outcome.current_status == "completed"
        assert outcome.transition_id is not None

        # conflict
        sid2 = await _pg_make_session(db, status="running")
        outcome = await service.transition(
            _command(
                entity_id=sid2,
                to_status="completed",
                expected_statuses=frozenset({"failed"}),
            )
        )
        assert outcome.result == "conflict"
        assert outcome.previous_status == "running"
        assert outcome.current_status == "running"
        assert outcome.transition_id is None

        # rejected: terminal exit without override
        sid3 = await _pg_make_session(db, status="completed")
        outcome = await service.transition(_command(entity_id=sid3, to_status="running"))
        assert outcome.result == "rejected"
        assert outcome.previous_status == "completed"
        assert outcome.current_status == "completed"
        assert outcome.transition_id is None
    finally:
        await db.close()


async def test_postgres_lifecycle_service_history_insert_failure_rolls_back(pg_url):
    """Load-bearing rollback contract, on Postgres — mirrors
    tests/state/lifecycle/test_service.py's SQLite-only rollback case: a
    history-append failure inside the same transaction must roll back the
    entity UPDATE that already "succeeded"."""
    from unittest.mock import patch

    from sqlalchemy import text

    from lionagi.state.lifecycle import ActorRecord, ReasonRecord, TransitionCommand
    from lionagi.state.lifecycle.service import SQLAlchemyLifecycleService

    def _command(**overrides):
        base = dict(
            entity_type="session",
            entity_id="",
            to_status="completed",
            reason=ReasonRecord(code="session.stale.no_heartbeat"),
            actor=ActorRecord(type="executor", id="executor"),
        )
        base.update(overrides)
        return TransitionCommand(**base)

    db = StateDB(url=pg_url)
    await db.open()
    try:
        sid = await _pg_make_session(db, status="running")
        service = SQLAlchemyLifecycleService(db)

        async def _write_then_break_history_insert(self, conn, table, command, **kwargs):
            set_clauses = ["status = :status", "updated_at = :now"]
            result = await conn.execute(
                text(
                    f"UPDATE {table} SET {', '.join(set_clauses)} "  # noqa: S608
                    "WHERE id = :id AND status = :previous_status"
                ),
                {
                    "status": command.to_status,
                    "now": kwargs["now"],
                    "id": command.entity_id,
                    "previous_status": kwargs["previous_status"],
                },
            )
            assert result.rowcount == 1
            await conn.execute(
                text("INSERT INTO nonexistent_history_table (id) VALUES (:id)"), {"id": "x"}
            )
            return "unreachable"

        with patch.object(SQLAlchemyLifecycleService, "_write", _write_then_break_history_insert):
            with pytest.raises(Exception):  # noqa: B017, PT011 -- backend-specific DBAPI error
                await service.transition(_command(entity_id=sid, to_status="completed"))

        row = await db.get_session(sid)
        assert row["status"] == "running"  # rolled back
    finally:
        await db.close()


async def test_postgres_wrapper_parity_cas_conflict_and_same_status_append(pg_url):
    """Load-bearing wrapper-parity contract, on Postgres — mirrors
    tests/state/lifecycle/test_wrapper_parity.py's SQLite-only cases:
    StateDB.update_status() and lionagi.state.transitions.transition() must
    behave identically (CAS conflict is a clean skip; same-status write
    appends) since both delegate through the same lifecycle service."""
    from lionagi.state.reasons import RunReasons
    from lionagi.state.transitions import Actor, StateReason, TransitionRequest, transition

    db = StateDB(url=pg_url)
    await db.open()
    try:
        # CAS conflict: update_status()
        run_id = await _pg_make_schedule_run(db, status="queued")
        applied = await db.update_status(
            "schedule_run",
            run_id,
            new_status="running",
            reason_code=RunReasons.STARTED_OK,
            source="executor",
            expected_statuses={"running"},  # actual status is "queued"
        )
        assert applied is False
        row = await db.get_schedule_run(run_id)
        assert row["status"] == "queued"

        # CAS conflict: transitions.transition()
        run_id2 = await _pg_make_schedule_run(db, status="queued")
        result = await transition(
            db,
            TransitionRequest(
                entity_type="schedule_run",
                entity_id=run_id2,
                from_state="running",  # actual status is "queued"
                to_state="completed",
                reason=StateReason(code=RunReasons.COMPLETED_OK),
                actor=Actor(type="system", id="w1"),
                idempotency_key=_uid(),
            ),
        )
        assert result.applied is False
        assert result.conflict is True
        row = await db.get_schedule_run(run_id2)
        assert row["status"] == "queued"

        # same-status append: update_status()
        run_id3 = await _pg_make_schedule_run(db, status="running")
        applied = await db.update_status(
            "schedule_run",
            run_id3,
            new_status="running",
            reason_code=RunReasons.STARTED_OK,
            source="executor",
        )
        assert applied is True
        row = await db.get_schedule_run(run_id3)
        assert row["status"] == "running"

        # same-status append: transitions.transition()
        run_id4 = await _pg_make_schedule_run(db, status="running")
        result = await transition(
            db,
            TransitionRequest(
                entity_type="schedule_run",
                entity_id=run_id4,
                from_state="running",
                to_state="running",
                reason=StateReason(code=RunReasons.STARTED_OK),
                actor=Actor(type="system", id="w1"),
                idempotency_key=_uid(),
            ),
        )
        assert result.applied is True
        row = await db.get_schedule_run(run_id4)
        assert row["status"] == "running"
    finally:
        await db.close()


# session-control admission takes the session row lock on PostgreSQL
#
# SQLite serialises writers, so evaluating the running-session condition inside
# the insert statement is decisive there. PostgreSQL runs two clients at once
# and evaluates that condition against a READ COMMITTED snapshot, so an
# admission can pass while another transaction is terminalizing the same
# session and commit after that run's teardown sweep has already looked. The
# row then exists, is pending, and has no consumer. The admission therefore
# locks the session row, which makes a concurrent terminal transition wait for
# it rather than pass it.


async def _seed_running_agent_session(db) -> str:
    sid = uuid.uuid4().hex[:12]
    pid = uuid.uuid4().hex
    await db.create_progression(pid)
    await db.create_session(
        {
            "id": sid,
            "progression_id": pid,
            "status": "running",
            "invocation_kind": "agent",
            "run_id": "20260802T000000-lockprobe",
            "started_at": time.time(),
        }
    )
    return sid


@pytest.mark.asyncio
async def test_postgres_control_admission_waits_on_a_locked_session_row(pg_url):
    """A terminalizing transaction's lock on the session row must block the admission.

    That ordering is the mechanism. Without it the admission reads its own
    snapshot, passes, and can commit after the terminalizing run's sweep has
    already looked, leaving a committed pending row with no consumer.

    The holder takes FOR NO KEY UPDATE because that is what a plain status
    UPDATE takes, and because it is the mode that discriminates: the control's
    foreign key already takes FOR KEY SHARE on the same row, and FOR KEY SHARE
    conflicts with FOR UPDATE but not with FOR NO KEY UPDATE. A holder taking
    FOR UPDATE would block the insert through the foreign key alone and would
    pass against the defect. Two arms: the unheld admission must succeed, or a
    blocked insert would prove nothing; the held one must not complete.
    """
    import asyncio

    from sqlalchemy import text as _text
    from sqlalchemy.ext.asyncio import create_async_engine

    db = StateDB(url=pg_url)
    async with db:
        sid = await _seed_running_agent_session(db)

        # Control arm: nothing holds the row, so admission works normally.
        unlocked = await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "unblocked"}
        )
        assert unlocked is not None, "the admission failed for a reason unrelated to locking"

        engine = create_async_engine(pg_url)
        try:
            async with engine.connect() as holder:
                async with holder.begin():
                    # FOR NO KEY UPDATE is exactly the lock a plain
                    # `UPDATE sessions SET status = ...` takes, so this holder
                    # stands in for a run terminalizing underneath the
                    # admission. The lock mode is the whole test: FOR NO KEY
                    # UPDATE does NOT conflict with the FOR KEY SHARE the
                    # control's foreign key takes on the same row, so an
                    # admission that only reads the session sails past it and
                    # the wrong lock mode here would pass against the defect.
                    await holder.execute(
                        _text("SELECT 1 FROM sessions WHERE id = :sid FOR NO KEY UPDATE"),
                        {"sid": sid},
                    )
                    with pytest.raises(asyncio.TimeoutError):
                        await asyncio.wait_for(
                            db.insert_session_control(
                                session_id=sid, verb="message", payload={"text": "blocked"}
                            ),
                            timeout=3,
                        )
        finally:
            await engine.dispose()

        # Released: the same admission now goes through, so the block above was
        # the lock and not a dead connection.
        after = await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "after release"}
        )
        assert after is not None


# a hand resolution that loses its race reports nothing
#
# `resolve_claimed_session_control` reads the current claim, decides from it,
# then writes under a compare-and-set. On PostgreSQL the claimant can commit its
# own outcome between those two statements. The CAS correctly refuses; what the
# operator must not get is a receipt for the write that did not happen.
#
# The interleave is injected rather than raced for. An earlier version of this
# test held the row from a second connection and waited for the resolver to be
# observably blocked, which passed alone and timed out inside the full module:
# waiting on a lock is a property of the whole cluster, not of this call, so the
# arrangement was a timing dependency wearing a synchronisation primitive. Here
# the competing finalize is committed by a proxy sitting between the resolver's
# two statements, so the ordering the test is about is the only ordering it can
# produce.


@pytest.mark.asyncio
async def test_postgres_resolve_reports_nothing_when_its_write_lost_the_race(pg_url):
    """A refused compare-and-set must not read back as a successful resolution."""
    from contextlib import asynccontextmanager

    from sqlalchemy import text as _text
    from sqlalchemy.ext.asyncio import create_async_engine

    db = StateDB(url=pg_url)
    async with db:
        sid = await _seed_running_agent_session(db)

        # Control arm: with no competing writer the resolve succeeds and keeps
        # the claim, so the None below means the race and not a broken call.
        free = await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "uncontended"}
        )
        await db.mark_session_control_applying(free, owner="leg-free")
        uncontended = await db.resolve_claimed_session_control(
            free, outcome="abandoned", actor="operator"
        )
        assert uncontended is not None and "applying:leg-free" in uncontended

        contested = await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "contested"}
        )
        await db.mark_session_control_applying(contested, owner="leg-slow")

        engine = create_async_engine(pg_url)
        try:

            class _ClaimantReportsBackAfterTheRead:
                """Commits the claimant's own outcome once the resolver has read.

                One connection, one interposition: the first execute is the
                resolver's SELECT, and the competing write lands on a separate
                connection immediately after it. The resolver's transaction has
                taken no lock on the row at that point, so the outside write
                commits without waiting and the CAS that follows is evaluated
                against a row that has moved.
                """

                def __init__(self, conn) -> None:
                    self._conn = conn
                    self._interposed = False

                def __getattr__(self, name):
                    return getattr(self._conn, name)

                async def execute(self, *args, **kwargs):
                    result = await self._conn.execute(*args, **kwargs)
                    if not self._interposed:
                        self._interposed = True
                        async with engine.connect() as claimant:
                            async with claimant.begin():
                                await claimant.execute(
                                    _text(
                                        "UPDATE session_controls SET applied_at = 1.0, "
                                        "result = 'applied' WHERE id = :cid"
                                    ),
                                    {"cid": contested},
                                )
                    return result

            real_tx = db._tx

            @asynccontextmanager
            async def _interposing_tx():
                async with real_tx() as conn:
                    yield _ClaimantReportsBackAfterTheRead(conn)

            db._tx = _interposing_tx
            try:
                stored = await db.resolve_claimed_session_control(
                    contested, outcome="abandoned", actor="operator"
                )
            finally:
                db._tx = real_tx
        finally:
            await engine.dispose()

        assert stored is None, (
            "the resolve returned a receipt after its conditional write matched "
            f"no rows: {stored!r}"
        )
        row = await db.get_session_control(contested)
        assert row["result"] == "applied", (
            "the claimant's own outcome was overwritten by a resolution that lost the race"
        )


# Postgres leg: the delete/writer race on delete_imported_session
# Lives in this module for the reason stated above: it needs the session-scoped
# `pg_url` fixture, and a second module requesting it in the same run breaks
# asyncpg's loop-bound connections.
#
# The race this pins is not hypothetical. At READ COMMITTED a transaction's
# retention check reads a snapshot, so a reference committed after that check is
# invisible to it while the delete authorised by it still proceeds — the delete
# destroys a message a survivor is by then pointing at. SQLite never had the
# exposure because `_tx()` holds a process write lock there; Postgres took a bare
# `engine.begin()`. The fix is a table lock taken as the transaction's first
# statement, which works because ordinary INSERT and UPDATE already hold ROW
# EXCLUSIVE and that conflicts with EXCLUSIVE.


async def test_postgres_delete_imported_session_waits_for_an_open_writer(pg_url):
    """A writer holding an uncommitted reference blocks the delete, and the
    reference it commits is then honoured.

    Both directions are forced in one fixture, per the retention contract: the
    message the writer claims must survive, and the message nobody claims must
    go. Asserting only the first cannot tell a correct delete from one that
    retains everything.

    The discriminating step is what happens during the writer's open
    transaction. Without the table lock the delete does not notice the writer at
    all: it reads a snapshot that predates the uncommitted reference, concludes
    the message is an orphan, and removes it. With the lock the delete cannot
    begin reading, so the claimed message is still there for the writer to
    commit its reference against.
    """
    import asyncio

    from sqlalchemy import text

    from lionagi.state.db import _to_json_column

    db = StateDB(url=pg_url)
    await db.open()
    try:
        assert db.dialect == "postgresql"

        now = time.time()
        msg_claimed, msg_orphan = _uid(), _uid()
        for mid in (msg_claimed, msg_orphan):
            await db.insert_message(
                {"id": mid, "created_at": now, "content": {"text": "x"}, "role": "user"}
            )

        # The imported session holds both messages and is the delete's target.
        imported_prog, imported_sid = _uid(), _uid()
        await db.create_progression(imported_prog, [msg_claimed, msg_orphan])
        await db.create_session(
            {
                "id": imported_sid,
                "progression_id": imported_prog,
                "status": "completed",
                "created_at": now,
                "updated_at": now,
                "source_kind": "imported_codex",
            }
        )

        # The survivor starts out referencing neither message.
        survivor_prog, survivor_sid = _uid(), _uid()
        await db.create_progression(survivor_prog, [])
        await db.create_session(
            {
                "id": survivor_sid,
                "progression_id": survivor_prog,
                "status": "running",
                "created_at": now,
                "updated_at": now,
                "source_kind": "live",
            }
        )

        writer_holds_the_row = asyncio.Event()
        writer_may_commit = asyncio.Event()

        async def writer() -> None:
            """Claim msg_claimed for the survivor, then hold the transaction open."""
            async with db._tx() as conn:
                await conn.execute(
                    text("UPDATE progressions SET collection = :col WHERE id = :id"),
                    {"col": _to_json_column([msg_claimed]), "id": survivor_prog},
                )
                writer_holds_the_row.set()
                await writer_may_commit.wait()
            # commit happens on context exit

        writer_task = asyncio.create_task(writer())
        await asyncio.wait_for(writer_holds_the_row.wait(), timeout=10)

        # The lock is taken NOWAIT, so the contended attempt refuses instead of
        # queueing. What matters is the outcome, not which of the two shapes
        # produced it: while another transaction can still commit a new
        # reference, this delete must not run at all. Asserting the refusal
        # alone would be asserting the mechanism, so the retention arms below
        # are the real evidence — with no lock statement the delete completes
        # here and destroys the message the writer is about to claim.
        contended_error: Exception | None = None
        try:
            await db.delete_imported_session(imported_sid, require_source_kind="imported_codex")
        except Exception as exc:  # noqa: BLE001 — classified below
            contended_error = exc

        # The outcome is the evidence, so it is asserted before the shape of the
        # refusal: while another transaction can still commit a new reference,
        # this delete must not have run at all.
        assert await db.get_session(imported_sid) is not None, (
            "the delete ran while a writer held an uncommitted reference, so its "
            "retention check read a snapshot the writer was about to invalidate"
        )
        assert await db.get_message(msg_claimed) is not None, (
            "the message the open writer is in the middle of claiming was destroyed"
        )
        # Having established that, the refusal must come from the table lock and
        # not from something unrelated that would fake this result.
        assert contended_error is not None, "the contended attempt neither ran nor failed"
        assert getattr(contended_error.orig, "sqlstate", None) == "55P03", (
            "the delete failed for some reason other than the unavailable table "
            f"lock, so this test is no longer exercising the race: {contended_error!r}"
        )

        writer_may_commit.set()
        await asyncio.wait_for(writer_task, timeout=10)
        # Uncontended now, so the same call must go through.
        assert (
            await db.delete_imported_session(imported_sid, require_source_kind="imported_codex")
        ) is True

        assert await db.get_session(imported_sid) is None
        assert await db.get_session(survivor_sid) is not None
        # Direction one: claimed by a survivor, so retained.
        assert await db.get_progression(survivor_prog) == [msg_claimed]
        assert await db.get_message(msg_claimed) is not None
        # Direction two: claimed by nobody, so gone. Without this arm a
        # delete that retained every message would pass the assertion above.
        assert await db.get_message(msg_orphan) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_postgres_teardown_does_not_deadlock_the_maintenance_writer(pg_url):
    """The teardown and ``prune_old_data`` reach the same tables in opposite
    orders, which is the shape a lock cycle needs.

    ``prune_old_data`` updates ``sessions`` and then deletes from
    ``progressions``; the teardown's lock statement names ``branches`` and
    ``progressions`` before ``sessions``. A comma-separated ``LOCK TABLE`` takes
    those one at a time rather than atomically, so a blocking form holds the
    first two while waiting for the third and deadlocks against a maintenance
    pass already holding a ``sessions`` row. This drives that exact interleaving
    with the maintenance writer's real statement order.

    The discriminating assertion is that the maintenance transaction's second
    statement completes. Restore the blocking lock and PostgreSQL detects the
    cycle and aborts one of the two transactions with ``40P01``, which is a
    whole pass lost rather than a slow one.
    """
    import asyncio

    from sqlalchemy import text

    db = StateDB(url=pg_url)
    await db.open()
    try:
        assert db.dialect == "postgresql"

        now = time.time()
        msg = _uid()
        await db.insert_message(
            {"id": msg, "created_at": now, "content": {"text": "x"}, "role": "user"}
        )
        imported_prog, imported_sid = _uid(), _uid()
        await db.create_progression(imported_prog, [msg])
        await db.create_session(
            {
                "id": imported_sid,
                "progression_id": imported_prog,
                "status": "completed",
                "created_at": now,
                "updated_at": now,
                "source_kind": "imported_codex",
            }
        )
        # What the maintenance pass touches: a session row it locks first, and
        # an unrelated progression it deletes second.
        maint_prog, maint_sid = _uid(), _uid()
        maint_own_prog = _uid()
        await db.create_progression(maint_prog, [])
        await db.create_progression(maint_own_prog, [])
        await db.create_session(
            {
                "id": maint_sid,
                "progression_id": maint_own_prog,
                "status": "completed",
                "created_at": now,
                "updated_at": now,
                "source_kind": "live",
            }
        )

        holds_the_session_row = asyncio.Event()
        may_issue_second = asyncio.Event()
        outcome: dict[str, object] = {}

        async def maintenance() -> None:
            """prune_old_data's order: sessions first, then progressions."""
            async with db._tx() as conn:
                await conn.execute(
                    text("UPDATE sessions SET updated_at = updated_at WHERE id = :id"),
                    {"id": maint_sid},
                )
                holds_the_session_row.set()
                await may_issue_second.wait()
                try:
                    await conn.execute(
                        text("DELETE FROM progressions WHERE id = :p"), {"p": maint_prog}
                    )
                    outcome["second"] = "ok"
                except Exception as exc:  # noqa: BLE001 — the point of the test
                    outcome["second"] = exc

        maint_task = asyncio.create_task(maintenance())
        await asyncio.wait_for(holds_the_session_row.wait(), timeout=10)

        delete_task = asyncio.create_task(
            db.delete_imported_session(imported_sid, require_source_kind="imported_codex")
        )
        # Long enough for the teardown to reach its lock statement and either
        # refuse (NOWAIT) or settle into waiting (blocking).
        await asyncio.sleep(1.0)
        may_issue_second.set()
        await asyncio.wait_for(maint_task, timeout=30)

        second = outcome.get("second")
        assert second == "ok", (
            "the maintenance writer's second statement did not complete, so the "
            f"teardown's table lock is deadlocking a shipped writer: {second!r}"
        )

        delete_error = None
        try:
            await asyncio.wait_for(delete_task, timeout=30)
        except Exception as exc:  # noqa: BLE001 — inspected below
            delete_error = exc
        # Refusing the lock is fine and expected; being chosen as a deadlock
        # victim is the failure this test exists to catch, on either side.
        assert getattr(getattr(delete_error, "orig", None), "sqlstate", None) != "40P01", (
            f"the teardown was aborted as a deadlock victim: {delete_error!r}"
        )

        # The teardown is retried on a later sweep, and nothing above left the
        # row in a state that stops it going through.
        assert (
            await db.delete_imported_session(imported_sid, require_source_kind="imported_codex")
        ) is True
        assert await db.get_session(imported_sid) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_postgres_teardown_gives_up_rather_than_deadlocking_after_it_holds_the_locks(
    pg_url,
):
    """The table locks are not the only place this transaction can wait.

    Once the three EXCLUSIVE locks are held, the teardown still nulls the soft
    session FKs, and those rows can be held by someone else. A wait there is a
    wait while holding, which is what a deadlock cycle is made of, so NOWAIT on
    the acquisition does not by itself keep the teardown out of one. The bounded
    lock_timeout does: the wait gives up well inside PostgreSQL's default
    deadlock_timeout, so the cycle breaks before the detector runs.

    The discriminating assertion is the SQLSTATE. Remove the lock_timeout and
    this interleaving returns 40P01, a detected deadlock, instead of the
    retryable 55P03 both callers already handle.
    """
    import asyncio

    from sqlalchemy import text

    db = StateDB(url=pg_url)
    await db.open()
    try:
        assert db.dialect == "postgresql"

        now = time.time()
        msg = _uid()
        await db.insert_message(
            {"id": msg, "created_at": now, "content": {"text": "x"}, "role": "user"}
        )
        prog, sid = _uid(), _uid()
        await db.create_progression(prog, [msg])
        await db.create_session(
            {
                "id": sid,
                "progression_id": prog,
                "status": "completed",
                "created_at": now,
                "updated_at": now,
                "source_kind": "imported_codex",
            }
        )
        artifact_id = _uid()
        async with db._tx() as conn:
            await conn.execute(
                text(
                    "INSERT INTO artifacts (id, session_id, created_at, updated_at, "
                    "kind, name, content) VALUES (:i, :s, :c, :c, 'log', 'a', '{}')"
                ),
                {"i": artifact_id, "s": sid, "c": now},
            )

        holds_the_artifact = asyncio.Event()
        teardown_is_waiting = asyncio.Event()
        other_outcome: dict[str, object] = {}

        async def other_writer() -> None:
            """artifacts then sessions: the reverse of the teardown's order."""
            async with db._tx() as conn:
                await conn.execute(
                    text("UPDATE artifacts SET name = 'held' WHERE id = :i"),
                    {"i": artifact_id},
                )
                holds_the_artifact.set()
                await teardown_is_waiting.wait()
                try:
                    await conn.execute(
                        text("UPDATE sessions SET updated_at = updated_at WHERE id = :s"),
                        {"s": sid},
                    )
                    other_outcome["second"] = "ok"
                except Exception as exc:  # noqa: BLE001 — inspected below
                    other_outcome["second"] = exc

        writer_task = asyncio.create_task(other_writer())
        await asyncio.wait_for(holds_the_artifact.wait(), timeout=10)

        teardown = asyncio.create_task(
            db.delete_imported_session(sid, require_source_kind="imported_codex")
        )
        # Long enough for the teardown to take its three table locks and settle
        # into the artifacts wait, and short enough to be inside the 250ms bound.
        await asyncio.sleep(0.1)
        teardown_is_waiting.set()

        teardown_error = None
        try:
            await asyncio.wait_for(teardown, timeout=30)
        except Exception as exc:  # noqa: BLE001 — classified below
            teardown_error = exc
        await asyncio.wait_for(writer_task, timeout=30)

        sqlstate = getattr(getattr(teardown_error, "orig", None), "sqlstate", None)
        assert sqlstate != "40P01", (
            "the teardown was a deadlock participant after taking its table "
            f"locks, so bounding its later waits is not working: {teardown_error!r}"
        )
        assert sqlstate == "55P03", (
            "expected the teardown to give up on the contended soft-FK write; "
            f"got {teardown_error!r}"
        )
        assert other_outcome.get("second") == "ok", (
            f"the ordinary writer did not complete: {other_outcome.get('second')!r}"
        )
        # Nothing was half-torn-down: the whole transaction rolled back.
        assert await db.get_session(sid) is not None
        assert await db.get_message(msg) is not None
    finally:
        await db.close()


# Postgres leg: attach_session_invocation's prior-invocation read can race
# a concurrent repoint. SQLite serializes this through its own write lock
# plus BEGIN IMMEDIATE; PostgreSQL at READ COMMITTED does not, so the
# prior-invocation SELECT takes FOR UPDATE there.


async def test_postgres_attach_session_invocation_decrements_off_the_value_a_concurrent_repoint_left(
    pg_url,
):
    """A concurrent attach decrements the invocation another attach left the session on, not the value it read before that transaction committed."""
    import asyncio

    from sqlalchemy import text as _text
    from sqlalchemy.ext.asyncio import create_async_engine

    db = StateDB(url=pg_url)
    await db.open()
    try:
        assert db.dialect == "postgresql"
        old, mid, new = _uid(), _uid(), _uid()
        now = time.time()
        for inv_id in (old, mid, new):
            await db.create_invocation({"id": inv_id, "skill": "show", "started_at": now})

        prog = _uid()
        await db.create_progression(prog)
        sid = _uid()
        await db.create_session(
            {"id": sid, "progression_id": prog, "status": "running", "invocation_id": old}
        )
        assert (await db.get_invocation(old))["session_count"] == 1

        engine = create_async_engine(pg_url)
        try:
            async with engine.connect() as holder:
                async with holder.begin():
                    await holder.execute(
                        _text("SELECT invocation_id FROM sessions WHERE id = :sid FOR UPDATE"),
                        {"sid": sid},
                    )
                    await holder.execute(
                        _text("UPDATE sessions SET invocation_id = :mid WHERE id = :sid"),
                        {"mid": mid, "sid": sid},
                    )
                    await holder.execute(
                        _text(
                            "UPDATE invocations SET session_count = "
                            "GREATEST(session_count - 1, 0) WHERE id = :old"
                        ),
                        {"old": old},
                    )
                    await holder.execute(
                        _text(
                            "UPDATE invocations SET session_count = session_count + 1 "
                            "WHERE id = :mid"
                        ),
                        {"mid": mid},
                    )

                    second = asyncio.create_task(db.attach_session_invocation(sid, new))
                    with pytest.raises(asyncio.TimeoutError):
                        await asyncio.wait_for(asyncio.shield(second), timeout=1.5)
                    assert not second.done(), (
                        "the concurrent attach did not block on the row lock — "
                        "the FOR UPDATE fix is not being taken"
                    )
                # holder's transaction commits here, releasing the lock the
                # blocked attach is waiting on.

                await asyncio.wait_for(second, timeout=10)
        finally:
            await engine.dispose()

        assert (await db.get_invocation(old))["session_count"] == 0
        assert (await db.get_invocation(mid))["session_count"] == 0
        assert (await db.get_invocation(new))["session_count"] == 1
        assert await db.list_sessions_for_invocation(old) == []
        assert await db.list_sessions_for_invocation(mid) == []
        assert [r["id"] for r in await db.list_sessions_for_invocation(new)] == [sid]
    finally:
        await db.close()
