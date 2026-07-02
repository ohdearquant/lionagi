# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for concurrent writer contention on StateDB (WAL + busy_timeout=5000; file-backed DBs only — in-memory can't share)."""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from lionagi.state.db import StateDB


def _make_msg(role: str = "user") -> dict:
    return {
        "id": str(uuid.uuid4()),
        "created_at": time.time(),
        "node_metadata": {},
        "content": {"text": "x"},
        "role": role,
        "sender": "a",
        "recipient": "b",
        "channel": "c",
    }


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "shared.db"


# ── Multi-connection: concurrent insert_message ──────────────────────────────


async def test_two_connections_can_insert_concurrently(db_path: Path):
    """Two StateDB instances on the same file can interleave inserts without raising."""
    db1 = StateDB(db_path)
    db2 = StateDB(db_path)
    await db1.open()
    await db2.open()
    try:
        msgs = [_make_msg() for _ in range(20)]

        async def insert_all(db: StateDB, batch: list[dict]):
            for m in batch:
                await db.insert_message(m)

        await asyncio.gather(
            insert_all(db1, msgs[:10]),
            insert_all(db2, msgs[10:]),
        )

        db3 = StateDB(db_path)
        await db3.open()
        try:
            async with db3._read() as conn:
                row = (
                    (await conn.execute(text("SELECT COUNT(*) AS n FROM messages")))
                    .mappings()
                    .first()
                )
            assert row["n"] == 20
        finally:
            await db3.close()
    finally:
        await db1.close()
        await db2.close()


async def test_concurrent_resolve_lion_class_no_unique_error(db_path: Path):
    """_resolve_lion_class uses INSERT ON CONFLICT DO NOTHING + SELECT — two concurrent connections on the same novel class must not raise UNIQUE constraint failure."""
    db1 = StateDB(db_path)
    db2 = StateDB(db_path)
    await db1.open()
    await db2.open()
    try:
        novel = "test.NovelClass" + str(uuid.uuid4())

        # Both resolve the same brand-new class concurrently.
        res = await asyncio.gather(
            db1._resolve_lion_class(novel),
            db2._resolve_lion_class(novel),
        )
        assert res[0] == res[1]
        assert res[0] != db1._UNKNOWN_TYPE_ID
    finally:
        await db1.close()
        await db2.close()


# ── busy_timeout: long-held lock surfaces sensibly ────────────────────────────


async def test_busy_timeout_eventually_returns_locked_error(
    tmp_path: Path,
):
    """A connection holding an exclusive lock past busy_timeout surfaces OperationalError rather than hanging; uses a short timeout to keep test fast.

    The 'connection holding an exclusive lock' pattern is expressed by opening
    a raw aiosqlite connection and issuing BEGIN EXCLUSIVE directly, then
    attempting a write on a StateDB engine (which will time out according to
    busy_timeout PRAGMA).
    """
    import aiosqlite

    db_path = tmp_path / "locked.db"
    db2 = StateDB(db_path)
    await db2.open()
    try:
        # Tighten busy_timeout on db2 so the test runs fast.
        async with db2._engine.connect() as conn:
            await conn.execute(text("PRAGMA busy_timeout = 100"))

        # Grab an exclusive lock via a raw aiosqlite connection (bypasses SA hooks).
        raw_conn = await aiosqlite.connect(str(db_path))
        await raw_conn.execute("PRAGMA busy_timeout = 0")
        await raw_conn.execute("BEGIN EXCLUSIVE")
        try:
            from sqlalchemy.exc import OperationalError

            with pytest.raises(OperationalError):
                await db2.insert_message(_make_msg())
        finally:
            await raw_conn.execute("ROLLBACK")
            await raw_conn.close()
    finally:
        await db2.close()


# ── save_definition under contention ──────────────────────────────────────────


async def test_concurrent_save_definition_same_key_serialized(
    db_path: Path,
):
    """Concurrent save_definition for the same (kind, name) produces distinct monotonically-increasing versions via the per-key asyncio.Lock."""
    db = StateDB(db_path)
    await db.open()
    try:
        versions = await asyncio.gather(
            db.save_definition(
                kind="agent",
                name="rev",
                path="x.md",
                content="v1",
            ),
            db.save_definition(
                kind="agent",
                name="rev",
                path="x.md",
                content="v2",
            ),
        )
        assert sorted(versions) == [1, 2]
    finally:
        await db.close()


async def test_concurrent_save_definition_different_keys_parallel(
    db_path: Path,
):
    """Different (kind, name) pairs use independent asyncio.Locks and don't block each other."""
    db = StateDB(db_path)
    await db.open()
    try:
        versions = await asyncio.gather(
            db.save_definition(
                kind="agent",
                name="a1",
                path="a1.md",
                content="x",
            ),
            db.save_definition(
                kind="agent",
                name="a2",
                path="a2.md",
                content="y",
            ),
            db.save_definition(
                kind="playbook",
                name="p1",
                path="p1.md",
                content="z",
            ),
        )
        assert sorted(versions) == [1, 1, 1]
    finally:
        await db.close()
