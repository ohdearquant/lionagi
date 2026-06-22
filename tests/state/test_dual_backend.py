# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Dual-backend parity tests: SQLite (in-memory) + PostgreSQL (gated).

SQLite leg always runs; Postgres leg requires LIONAGI_TEST_PG_URL env var.
Both legs run the same contract: create session, insert messages, check
progression, run update_status with reason, verify transition row written.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons

# ── Postgres gating ───────────────────────────────────────────────────────────

_PG_URL = os.environ.get("LIONAGI_TEST_PG_URL")
pg_skip = pytest.mark.skipif(not _PG_URL, reason="LIONAGI_TEST_PG_URL not set")

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


# ── Postgres leg (gated by LIONAGI_TEST_PG_URL) ───────────────────────────────


@pg_skip
async def test_postgres_parity():
    """Full parity suite against a live PostgreSQL instance."""
    assert _PG_URL is not None
    db = StateDB(url=_PG_URL)
    await db.open()
    try:
        assert db.dialect == "postgresql"
        await _run_parity_suite(db)
    finally:
        await db.close()


@pg_skip
async def test_postgres_schema_creates_all_tables():
    """metadata.create_all() produces the expected set of tables in Postgres."""
    import sqlalchemy as sa

    assert _PG_URL is not None
    db = StateDB(url=_PG_URL)
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
