# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the shared _db helper: WAL mode, busy_timeout, row_factory."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")

from tests.apps_studio_server._helpers import run_async as _run  # noqa: E402

# ---------------------------------------------------------------------------
# open_db() configures busy_timeout, WAL, and row_factory
# ---------------------------------------------------------------------------


class TestOpenDb:
    @pytest.mark.integration
    def test_open_db_sets_wal_mode(self, tmp_path):
        """open_db() must switch the connection to WAL journal mode."""
        from lionagi.studio.services._db import open_db

        db_path = str(tmp_path / "test.db")

        async def _check():
            async with open_db(db_path) as db:
                cur = await db.execute("PRAGMA journal_mode")
                row = await cur.fetchone()
            return row[0]

        mode = _run(_check())
        assert mode == "wal", f"Expected WAL journal mode, got {mode!r}"

    @pytest.mark.integration
    def test_open_db_sets_busy_timeout(self, tmp_path):
        """open_db() must set busy_timeout = 5000 ms."""
        from lionagi.studio.services._db import open_db

        db_path = str(tmp_path / "test.db")

        async def _check():
            async with open_db(db_path) as db:
                cur = await db.execute("PRAGMA busy_timeout")
                row = await cur.fetchone()
            return row[0]

        timeout = _run(_check())
        assert timeout == 5000, f"Expected busy_timeout=5000, got {timeout!r}"

    @pytest.mark.integration
    def test_open_db_sets_row_factory(self, tmp_path):
        """open_db() must set row_factory so rows are accessible by column name."""
        from lionagi.studio.services._db import open_db

        db_path = str(tmp_path / "test.db")

        async def _check():
            async with open_db(db_path) as db:
                await db.execute("CREATE TABLE t (a TEXT, b INTEGER)")
                await db.execute("INSERT INTO t VALUES ('hello', 42)")
                await db.commit()
                cur = await db.execute("SELECT a, b FROM t")
                row = await cur.fetchone()
            return row

        row = _run(_check())
        assert row is not None
        assert row["a"] == "hello"
        assert row["b"] == 42

    def test_sessions_service_uses_open_db(self):
        """sessions.py must import and use _open_db (not bare aiosqlite.connect)."""
        import inspect

        import lionagi.studio.services.sessions as sessions_mod

        src = inspect.getsource(sessions_mod)
        # Verify _open_db is imported
        assert "_open_db" in src, "sessions.py must import open_db as _open_db"
        # Verify the module no longer uses bare aiosqlite.connect() calls
        # (the only remaining reference to aiosqlite should be for the Row
        # sentinel or type annotations, not for .connect())
        for line in src.splitlines():
            stripped = line.strip()
            if "aiosqlite.connect(" in stripped and not stripped.startswith("#"):
                raise AssertionError(f"sessions.py still uses bare aiosqlite.connect(): {line!r}")

    def test_shows_service_uses_open_db(self):
        """shows.py must import and use _open_db (not bare aiosqlite.connect)."""
        import inspect

        import lionagi.studio.services.shows as shows_mod

        src = inspect.getsource(shows_mod)
        assert "_open_db" in src, "shows.py must import open_db as _open_db"
        for line in src.splitlines():
            stripped = line.strip()
            if "aiosqlite.connect(" in stripped and not stripped.startswith("#"):
                raise AssertionError(f"shows.py still uses bare aiosqlite.connect(): {line!r}")
