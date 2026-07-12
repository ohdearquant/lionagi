# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for StateDB session_controls CRUD (ADR-0069 D1: live-control transport)."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")

from lionagi.state.db import StateDB  # noqa: E402


async def _make_session(db: StateDB) -> str:
    sid = uuid.uuid4().hex[:12]
    pid = uuid.uuid4().hex
    await db.create_progression(pid)
    await db.create_session(
        {
            "id": sid,
            "progression_id": pid,
            "status": "running",
            "invocation_kind": "flow",
            "started_at": time.time(),
        }
    )
    return sid


async def test_insert_and_get_session_control(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        sid = await _make_session(db)
        control_id = await db.insert_session_control(session_id=sid, verb="pause")
        row = await db.get_session_control(control_id)

    assert row is not None
    assert row["id"] == control_id
    assert row["session_id"] == sid
    assert row["verb"] == "pause"
    assert row["payload"] is None
    assert row["applied_at"] is None
    assert row["result"] is None
    assert row["created_at"] > 0


async def test_get_session_control_returns_none_for_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        result = await db.get_session_control("nonexistent-control-id")
    assert result is None


async def test_insert_session_control_with_payload_round_trips(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        sid = await _make_session(db)
        control_id = await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "hello there"}
        )
        row = await db.get_session_control(control_id)

    assert row is not None
    assert row["payload"] == {"text": "hello there"}


async def test_insert_session_control_uses_explicit_created_at(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        sid = await _make_session(db)
        control_id = await db.insert_session_control(
            session_id=sid, verb="resume", created_at=12345.5
        )
        row = await db.get_session_control(control_id)

    assert row["created_at"] == 12345.5


async def test_list_pending_session_controls_only_unapplied(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        sid = await _make_session(db)
        c1 = await db.insert_session_control(session_id=sid, verb="pause", created_at=1.0)
        c2 = await db.insert_session_control(session_id=sid, verb="resume", created_at=2.0)
        await db.finalize_session_control(c1, result="applied")

        pending = await db.list_pending_session_controls(sid)

    ids = [p["id"] for p in pending]
    assert c2 in ids
    assert c1 not in ids


async def test_list_pending_session_controls_ordered_oldest_first(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        sid = await _make_session(db)
        c_new = await db.insert_session_control(session_id=sid, verb="pause", created_at=100.0)
        c_old = await db.insert_session_control(session_id=sid, verb="resume", created_at=1.0)

        pending = await db.list_pending_session_controls(sid)

    assert [p["id"] for p in pending] == [c_old, c_new]


async def test_list_pending_session_controls_scoped_to_session(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        sid_a = await _make_session(db)
        sid_b = await _make_session(db)
        await db.insert_session_control(session_id=sid_a, verb="pause")
        c_b = await db.insert_session_control(session_id=sid_b, verb="resume")

        pending_b = await db.list_pending_session_controls(sid_b)

    assert [p["id"] for p in pending_b] == [c_b]


async def test_list_pending_session_controls_empty(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        sid = await _make_session(db)
        pending = await db.list_pending_session_controls(sid)
    assert pending == []


async def test_mark_session_control_applying_leaves_applied_at_null(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        sid = await _make_session(db)
        control_id = await db.insert_session_control(session_id=sid, verb="message")
        await db.mark_session_control_applying(control_id)
        row = await db.get_session_control(control_id)

    assert row["result"] == "applying"
    assert row["applied_at"] is None


async def test_mark_session_control_applying_still_listed_pending(tmp_path: Path) -> None:
    """result='applying' rows are still 'pending' (applied_at IS NULL) — the
    poller/status surface distinguish never-touched from mid-apply-crashed
    by inspecting the result field, not by excluding them from the list."""
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        sid = await _make_session(db)
        control_id = await db.insert_session_control(session_id=sid, verb="message")
        await db.mark_session_control_applying(control_id)
        pending = await db.list_pending_session_controls(sid)

    assert len(pending) == 1
    assert pending[0]["id"] == control_id
    assert pending[0]["result"] == "applying"


async def test_finalize_session_control_stamps_applied_at_and_result(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        sid = await _make_session(db)
        control_id = await db.insert_session_control(session_id=sid, verb="pause")
        await db.finalize_session_control(control_id, result="applied")
        row = await db.get_session_control(control_id)

    assert row["applied_at"] is not None
    assert row["result"] == "applied"


async def test_finalize_session_control_with_rejected_reason(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        sid = await _make_session(db)
        control_id = await db.insert_session_control(session_id=sid, verb="message")
        await db.finalize_session_control(control_id, result="rejected:no-pending-ops")
        row = await db.get_session_control(control_id)

    assert row["result"] == "rejected:no-pending-ops"
    assert row["applied_at"] is not None


async def test_session_controls_column_present(tmp_path: Path) -> None:
    """Confirm the table + partial index exist via raw PRAGMA (schema_meta.py parity)."""
    import aiosqlite

    db_path = tmp_path / "state.db"
    async with StateDB(db_path):
        pass  # schema-create only

    async with aiosqlite.connect(str(db_path)) as conn:
        async with conn.execute("PRAGMA table_info(session_controls)") as cur:
            cols = {r[1] async for r in cur}
    assert cols == {"id", "session_id", "verb", "payload", "created_at", "applied_at", "result"}

    async with aiosqlite.connect(str(db_path)) as conn:
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='session_controls'"
        ) as cur:
            index_names = {r[0] async for r in cur}
    assert "idx_session_controls_pending" in index_names


async def test_session_controls_verb_check_constraint_rejects_invalid(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        sid = await _make_session(db)
        with pytest.raises(Exception):  # noqa: B017, PT011 — sqlite raises IntegrityError via SQLAlchemy
            await db.insert_session_control(session_id=sid, verb="not-a-real-verb")


async def test_session_controls_cascades_on_session_delete(tmp_path: Path) -> None:
    from sqlalchemy import text

    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        sid = await _make_session(db)
        control_id = await db.insert_session_control(session_id=sid, verb="pause")

        async with db._tx() as conn:
            await conn.execute(text("DELETE FROM sessions WHERE id = :id"), {"id": sid})

        row = await db.get_session_control(control_id)

    assert row is None


async def test_concurrent_session_control_inserts_no_rows_dropped(tmp_path: Path) -> None:
    import asyncio

    db_path = tmp_path / "state.db"
    n = 25
    async with StateDB(db_path) as db:
        sid = await _make_session(db)
        await asyncio.gather(
            *[db.insert_session_control(session_id=sid, verb="pause") for _ in range(n)]
        )
        pending = await db.list_pending_session_controls(sid)

    assert len(pending) == n


async def test_identical_created_at_apply_order_is_deterministic(tmp_path: Path) -> None:
    """Two controls sharing one created_at float (rapid enqueues) must come
    back in the same order on every read — an unstable tie would let a pause
    and resume swap apply order between poll ticks."""
    from sqlalchemy import text

    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        sid = await _make_session(db)
        id_a = await db.insert_session_control(session_id=sid, verb="pause")
        id_b = await db.insert_session_control(session_id=sid, verb="resume")
        async with db._tx() as conn:
            await conn.execute(
                text("UPDATE session_controls SET created_at = :ts WHERE id IN (:a, :b)"),
                {"ts": 1000.0, "a": id_a, "b": id_b},
            )

        first = [r["id"] for r in await db.list_pending_session_controls(sid)]
        second = [r["id"] for r in await db.list_pending_session_controls(sid)]

    assert first == second == sorted([id_a, id_b])
