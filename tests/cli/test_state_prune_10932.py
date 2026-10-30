# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A large prune keeps its preview and deletion atomic and preserves shared data."""

import sqlite3
import time

import pytest

from lionagi.cli.state import _prune
from lionagi.state import db as db_mod
from lionagi.state.db import StateDB


@pytest.mark.asyncio
async def test_prune_10932_sessions_preview_and_delete(tmp_path, monkeypatch, record_property):
    path = tmp_path / "state.db"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    async with StateDB(path):
        pass

    now = time.time()
    old = now - 60 * 86400
    with sqlite3.connect(path) as conn:
        limit = (
            conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
            if hasattr(conn, "getlimit")
            else "unavailable"
        )
        record_property("sqlite_variable_limit", str(limit))
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO progressions (id, created_at, collection) VALUES (?, ?, '[]')",
            ("shared-progress", old),
        )
        conn.executemany(
            "INSERT INTO sessions (id, created_at, updated_at, progression_id, status) "
            "VALUES (?, ?, ?, ?, ?)",
            ((f"victim-{i:05d}", old, old, "shared-progress", "completed") for i in range(10_932)),
        )
        conn.execute(
            "INSERT INTO sessions (id, created_at, updated_at, progression_id, status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("keeper", now, now, "shared-progress", "completed"),
        )

    expected = {"sessions": 10_932, "branches": 0, "messages": 0}
    assert await _prune(keep_days=30, keep_n=0, dry_run=True) == expected
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 10_933
        assert conn.execute("SELECT id FROM progressions").fetchall() == [("shared-progress",)]

    assert await _prune(keep_days=30, keep_n=0, dry_run=False) == expected
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT id FROM sessions").fetchall() == [("keeper",)]
        assert conn.execute("SELECT id FROM progressions").fetchall() == [("shared-progress",)]
