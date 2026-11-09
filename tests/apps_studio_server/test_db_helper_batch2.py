# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the shared _db helper: WAL mode, busy_timeout, row_factory."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")


# open_db() configures busy_timeout, WAL, and row_factory


class TestOpenDb:
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
