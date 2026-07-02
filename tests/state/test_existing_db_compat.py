# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Existing-data safety: the new SQLAlchemy StateDB must open a database built
by the previous raw-aiosqlite code (canonical schema.sql + JSON stored as text
strings) without destructive migration, read the old rows, and keep writing.

Guards the production guarantee that pointing the new code at an existing
~/.lionagi/state.db neither rewrites its schema nor loses its data.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons

_SCHEMA_SQL = Path(__file__).resolve().parents[2] / "lionagi" / "state" / "schema.sql"


def _schema_cols(db_path: Path) -> set[tuple[str, str, str]]:
    """(table, column, declared_type) for every table — affinity-bearing snapshot."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        out: set[tuple[str, str, str]] = set()
        for t in tables:
            for row in conn.execute(f"PRAGMA table_info({t})").fetchall():
                out.add((t, row[1], (row[2] or "").upper()))
        return out
    finally:
        conn.close()


def _seed_old_format_db(db_path: Path) -> tuple[str, str]:
    """Build the db the OLD way: schema.sql DDL + rows with JSON serialized as text."""
    sid, pid = str(uuid.uuid4()), str(uuid.uuid4())
    now = time.time()
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA_SQL.read_text())
        conn.execute(
            "INSERT INTO progressions(id, created_at, collection) VALUES (?, ?, ?)",
            (pid, now, json.dumps(["m1", "m2"])),  # JSON-as-text, the old representation
        )
        conn.execute(
            "INSERT INTO sessions(id, created_at, progression_id, updated_at, "
            "node_metadata, name, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                sid,
                now,
                pid,
                now,
                json.dumps({"lion_class": "Session", "k": 1}),
                "old-row",
                "running",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return sid, pid


async def test_open_existing_schema_db_no_destructive_migration_and_roundtrip(tmp_path):
    db_path = tmp_path / "existing.db"
    sid, pid = _seed_old_format_db(db_path)
    before = _schema_cols(db_path)

    db = StateDB(db_path)
    await db.open()
    try:
        after = _schema_cols(db_path)
        # Additive migration (new columns) is allowed; dropping or retyping an
        # existing column is the breakage we must never inflict on real data.
        lost = before - after
        assert not lost, f"open() dropped/retyped existing columns: {lost}"

        # Old-format rows are readable through the new code paths.
        session = await db.get_session(sid)
        assert session is not None and session["status"] == "running"
        md = session["node_metadata"]
        if isinstance(md, str):
            md = json.loads(md)
        assert md["k"] == 1

        coll = await db.get_progression(pid)
        assert coll == ["m1", "m2"], f"old collection must decode: {coll!r}"

        # The existing db keeps accepting writes through the new code.
        await db.update_status(
            entity_type="session",
            entity_id=sid,
            new_status="completed",
            reason_code=RunReasons.COMPLETED_OK,
            reason_summary="existing-db write path.",
            source="executor",
        )
        assert (await db.get_session(sid))["status"] == "completed"

        # A brand-new session created by the new code coexists with the old row.
        new_sid, new_pid = str(uuid.uuid4()), str(uuid.uuid4())
        await db.create_progression(new_pid)
        await db.create_session(
            {
                "id": new_sid,
                "progression_id": new_pid,
                "status": "running",
                "created_at": time.time(),
                "updated_at": time.time(),
            }
        )
        assert (await db.get_session(new_sid))["status"] == "running"
    finally:
        await db.close()
