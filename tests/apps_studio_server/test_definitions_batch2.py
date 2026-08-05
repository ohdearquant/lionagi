# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for list_definitions: single DB connection for N definition files."""

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
# list_definitions uses ONE DB connection for N definition files
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestListDefinitionsNPlusOne:
    def _setup(self, tmp_path, monkeypatch, n_agents=3):
        import lionagi.state.db as state_db_mod
        import lionagi.studio.services.definitions as defs_mod

        fake_home = tmp_path / "lionagi_home"
        agents_dir = fake_home / "agents"
        agents_dir.mkdir(parents=True)
        for i in range(n_agents):
            (agents_dir / f"agent{i}.md").write_text(f"# Agent {i}\ncontent")

        fake_db = tmp_path / "state.db"
        fake_db.touch()

        monkeypatch.setattr(defs_mod, "LIONAGI_HOME", fake_home)
        monkeypatch.setattr(defs_mod, "AGENTS_DIR", agents_dir)
        monkeypatch.setattr(defs_mod, "PLAYBOOKS_DIR", fake_home / "playbooks")
        monkeypatch.setattr(defs_mod, "KIND_DIRS", {"agent": agents_dir})
        monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)

        return defs_mod

    @staticmethod
    def _count_history_reads(monkeypatch, rows=()):
        """Count the history reads a listing makes, whatever the store is.

        Anchored to the portable reader rather than to a SQLite connection:
        history moved to StateDB so that a server-backed deployment answers
        from the store it is configured for, and a test that counts
        `aiosqlite.connect` calls stops measuring anything at that point while
        continuing to pass.
        """
        from lionagi.state.db import StateDB

        calls = []

        async def _fake(self):
            calls.append(1)
            return list(rows)

        monkeypatch.setattr(StateDB, "list_latest_definition_versions", _fake)
        return calls

    def test_single_history_read_for_multiple_definitions(self, tmp_path, monkeypatch):
        """One history read regardless of how many definitions are on disk."""
        defs_mod = self._setup(tmp_path, monkeypatch, n_agents=3)
        calls = self._count_history_reads(monkeypatch)

        result = _run(defs_mod.list_definitions("agent"))

        assert len(result) == 3, f"Expected 3 definitions, got {len(result)}"
        assert len(calls) == 1, (
            f"Expected exactly 1 history read for {len(result)} definitions, got {len(calls)}"
        )

    def test_no_history_read_when_no_definitions(self, tmp_path, monkeypatch):
        """Nothing on disk means nothing to enrich, so the store is not touched."""
        defs_mod = self._setup(tmp_path, monkeypatch, n_agents=0)
        calls = self._count_history_reads(monkeypatch)

        result = _run(defs_mod.list_definitions("agent"))
        assert result == []
        assert len(calls) == 0, "No history read expected when no definitions were found"

    def test_version_info_populated_from_batch_query(self, tmp_path, monkeypatch):
        """Batch query results must be mapped back to the correct entry."""
        defs_mod = self._setup(tmp_path, monkeypatch, n_agents=0)
        agents_dir = defs_mod.KIND_DIRS["agent"]
        (agents_dir / "myagent.md").write_text("# Agent\ncontent")

        self._count_history_reads(
            monkeypatch,
            rows=[{"kind": "agent", "name": "myagent", "version": 7, "created_at": 9999.0}],
        )

        result = _run(defs_mod.list_definitions("agent"))
        assert len(result) == 1
        assert result[0]["has_versions"] is True
        assert result[0]["version"] == 7
        assert result[0]["updated_at"] == 9999.0
        assert result[0]["history_available"] is True
