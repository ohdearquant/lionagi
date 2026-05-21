# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for Batch 2 DB/Schema fixes: #990 status_source migration."""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# #990 — status_source column migration and round-trip
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestStatusSourceMigration:
    def test_new_db_has_status_source_column(self, tmp_path):
        """A freshly created StateDB must have status_source on the shows table."""
        from lionagi.state.db import StateDB

        db_path = tmp_path / "state.db"

        async def _check():
            async with StateDB(db_path) as db:
                cur = await db.db.execute("PRAGMA table_info(shows)")
                cols = {row["name"] for row in await cur.fetchall()}
            return cols

        cols = _run(_check())
        assert "status_source" in cols, (
            "shows table missing status_source column after fresh StateDB init (#990)"
        )

    def test_existing_db_migrated_to_add_status_source(self, tmp_path):
        """An existing DB without status_source must gain it after StateDB open."""
        import aiosqlite as aio

        db_path = tmp_path / "state.db"

        # Create a legacy shows table WITHOUT status_source
        async def _create_legacy():
            async with aio.connect(str(db_path)) as db:
                await db.execute("PRAGMA journal_mode = WAL")
                await db.execute(
                    """CREATE TABLE IF NOT EXISTS shows (
                        id          TEXT PRIMARY KEY,
                        topic       TEXT NOT NULL UNIQUE,
                        goal        TEXT,
                        repo        TEXT,
                        base_branch TEXT,
                        integration_branch TEXT,
                        status      TEXT NOT NULL DEFAULT 'active',
                        show_dir    TEXT NOT NULL,
                        created_at  REAL NOT NULL,
                        updated_at  REAL NOT NULL
                    )"""
                )
                # Insert a legacy row (no status_source)
                await db.execute(
                    "INSERT INTO shows (id, topic, show_dir, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("old-id", "legacy-show", "/tmp/shows/legacy", time.time(), time.time()),
                )
                await db.commit()

        _run(_create_legacy())

        # Now open through StateDB — migration should add the column
        from lionagi.state.db import StateDB

        async def _migrate_and_check():
            async with StateDB(db_path) as db:
                cur = await db.db.execute("PRAGMA table_info(shows)")
                cols = {row["name"] for row in await cur.fetchall()}
                # Read back the legacy row — it should have status_source = 'unknown'
                cur2 = await db.db.execute(
                    "SELECT status_source FROM shows WHERE id = 'old-id'"
                )
                row = await cur2.fetchone()
            return cols, row

        cols, row = _run(_migrate_and_check())
        assert "status_source" in cols, "Migration must add status_source column"
        assert row is not None, "Legacy row must still exist after migration"
        assert row["status_source"] == "unknown", (
            f"Migrated legacy row must default to 'unknown', got {row['status_source']!r}"
        )

    def test_create_show_stores_status_source(self, tmp_path):
        """create_show() with status_source='db' must persist that value."""
        from lionagi.state.db import StateDB

        db_path = tmp_path / "state.db"
        show_id = str(uuid.uuid4())

        async def _insert_and_read():
            async with StateDB(db_path) as db:
                await db.create_show(
                    {
                        "id": show_id,
                        "topic": "test-show",
                        "show_dir": str(tmp_path / "shows" / "test-show"),
                        "status_source": "db",
                    }
                )
                show = await db.get_show(show_id)
            return show

        show = _run(_insert_and_read())
        assert show is not None
        assert show["status_source"] == "db"

    def test_create_show_defaults_status_source_unknown(self, tmp_path):
        """create_show() without status_source must default to 'unknown'."""
        from lionagi.state.db import StateDB

        db_path = tmp_path / "state.db"
        show_id = str(uuid.uuid4())

        async def _insert_and_read():
            async with StateDB(db_path) as db:
                await db.create_show(
                    {
                        "id": show_id,
                        "topic": "no-source-show",
                        "show_dir": str(tmp_path / "shows" / "no-source"),
                    }
                )
                return await db.get_show(show_id)

        show = _run(_insert_and_read())
        assert show["status_source"] == "unknown"

    def test_update_show_can_change_status_source(self, tmp_path):
        """update_show() must accept status_source as an updatable field."""
        from lionagi.state.db import StateDB

        db_path = tmp_path / "state.db"
        show_id = str(uuid.uuid4())

        async def _insert_update_read():
            async with StateDB(db_path) as db:
                await db.create_show(
                    {
                        "id": show_id,
                        "topic": "mutable-source",
                        "show_dir": str(tmp_path / "shows" / "mutable"),
                    }
                )
                await db.update_show(show_id, status_source="fs")
                return await db.get_show(show_id)

        show = _run(_insert_update_read())
        assert show["status_source"] == "fs"
