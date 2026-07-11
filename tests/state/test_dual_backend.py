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

    # A zero-session invocation must still appear (LEFT JOIN, not INNER) with a
    # NULL project — the ROW_NUMBER subquery matches nothing for it.
    empty_inv = _uid()
    await db.create_invocation({"id": empty_inv, "skill": "parity", "started_at": now})
    empties = [r for r in await db.list_invocations() if r["id"] == empty_inv]
    assert len(empties) == 1, f"zero-session invocation must appear once: {empties!r}"
    assert empties[0]["project"] is None, f"zero-session project must be None: {empties[0]!r}"


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


# ── Postgres leg: lifecycle service load-bearing contract (ADR-0058 Phase 2) ──
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
