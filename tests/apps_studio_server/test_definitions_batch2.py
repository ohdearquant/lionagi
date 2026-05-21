# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for Batch 2 DB/Schema fixes: #989."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")

from tests.apps_studio_server._helpers import run_async as _run  # noqa: E402

# ---------------------------------------------------------------------------
# Shared fake DB plumbing
# ---------------------------------------------------------------------------


class _FakeCursor:
    async def fetchall(self):
        return []

    async def fetchone(self):
        return None


class _FakeDB:
    row_factory = None

    async def execute(self, sql, params=None):
        return _FakeCursor()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


# ---------------------------------------------------------------------------
# #989 — list_definitions uses ONE DB connection for N definition files
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestListDefinitionsNPlusOne:
    def _setup(self, tmp_path, monkeypatch, n_agents=3):
        import apps.studio.server.services.definitions as defs_mod
        import lionagi.state.db as state_db_mod

        fake_home = tmp_path / "lionagi_home"
        agents_dir = fake_home / "agents"
        agents_dir.mkdir(parents=True)
        for i in range(n_agents):
            (agents_dir / f"agent{i}.md").write_text(f"# Agent {i}\ncontent")

        fake_db = tmp_path / "state.db"
        fake_db.touch()  # exists → _ensure_db() returns True

        monkeypatch.setattr(defs_mod, "LIONAGI_HOME", fake_home)
        monkeypatch.setattr(defs_mod, "AGENTS_DIR", agents_dir)
        monkeypatch.setattr(defs_mod, "PLAYBOOKS_DIR", fake_home / "playbooks")
        monkeypatch.setattr(defs_mod, "KIND_DIRS", {"agent": agents_dir})
        monkeypatch.setattr(defs_mod, "_DB", str(fake_db))
        # Patch DEFAULT_DB_PATH on BOTH the source module and definitions'
        # local import — _ensure_db() uses the latter (from-import binding).
        monkeypatch.setattr(defs_mod, "DEFAULT_DB_PATH", fake_db)
        monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)

        return defs_mod

    def test_single_db_connect_for_multiple_definitions(self, tmp_path, monkeypatch):
        """list_definitions() must open exactly one DB connection regardless
        of how many definition files exist on disk (#989 N+1 fix)."""
        defs_mod = self._setup(tmp_path, monkeypatch, n_agents=3)

        connect_count = 0

        def fake_connect(path):
            nonlocal connect_count
            connect_count += 1
            return _FakeDB()

        monkeypatch.setattr("aiosqlite.connect", fake_connect)

        result = _run(defs_mod.list_definitions("agent"))

        assert len(result) == 3, f"Expected 3 definitions, got {len(result)}"
        assert connect_count == 1, (
            f"Expected exactly 1 DB connection for {len(result)} definitions, "
            f"got {connect_count} — #989 N+1 regression"
        )

    def test_no_db_connect_when_no_definitions(self, tmp_path, monkeypatch):
        """list_definitions() must not open any DB connection when there are
        no definition files to enrich."""
        defs_mod = self._setup(tmp_path, monkeypatch, n_agents=0)

        connect_count = 0

        def fake_connect(path):
            nonlocal connect_count
            connect_count += 1
            return _FakeDB()

        monkeypatch.setattr("aiosqlite.connect", fake_connect)

        result = _run(defs_mod.list_definitions("agent"))
        assert result == []
        assert connect_count == 0, "No DB connect expected when no definitions found"

    def test_version_info_populated_from_batch_query(self, tmp_path, monkeypatch):
        """Batch query results must be mapped back to the correct entry."""
        import apps.studio.server.services.definitions as defs_mod
        import lionagi.state.db as state_db_mod

        fake_home = tmp_path / "lionagi_home"
        agents_dir = fake_home / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "myagent.md").write_text("# Agent\ncontent")

        fake_db = tmp_path / "state.db"
        fake_db.touch()

        monkeypatch.setattr(defs_mod, "LIONAGI_HOME", fake_home)
        monkeypatch.setattr(defs_mod, "AGENTS_DIR", agents_dir)
        monkeypatch.setattr(defs_mod, "PLAYBOOKS_DIR", fake_home / "playbooks")
        monkeypatch.setattr(defs_mod, "KIND_DIRS", {"agent": agents_dir})
        monkeypatch.setattr(defs_mod, "_DB", str(fake_db))
        monkeypatch.setattr(defs_mod, "DEFAULT_DB_PATH", fake_db)
        monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)


        class _RowLike:
            def __init__(self, data):
                self._data = data

            def __getitem__(self, key):
                return self._data[key]

        class _CursorWithRow:
            async def fetchall(self):
                return [_RowLike({"kind": "agent", "name": "myagent", "v": 7, "ts": 9999.0})]

        class _DBWithRow:
            row_factory = None

            async def execute(self, sql, params=None):
                return _CursorWithRow()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        monkeypatch.setattr("aiosqlite.connect", lambda path: _DBWithRow())

        result = _run(defs_mod.list_definitions("agent"))
        assert len(result) == 1
        assert result[0]["has_versions"] is True
        assert result[0]["version"] == 7
        assert result[0]["updated_at"] == 9999.0
