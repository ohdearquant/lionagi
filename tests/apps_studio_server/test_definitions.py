# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for save_definition() atomicity and DB-first ordering (H-BE-3)."""

from __future__ import annotations

import asyncio

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# H-BE-3: save_definition() writes DB first, then disk
# ---------------------------------------------------------------------------


def test_save_definition_creates_db_row_and_file(tmp_path, monkeypatch):
    """save_definition() with a missing (fresh) DB path must create the DB,
    insert a row, then write the file.  It must NOT return success without a
    row in the definitions table.
    """
    import apps.studio.server.services.definitions as defs_mod
    import lionagi.cli._runs as cli_runs_mod
    import lionagi.state.db as state_db_mod

    # Redirect LIONAGI_HOME → tmp dirs so no real agent/playbook dirs are needed
    fake_home = tmp_path / "lionagi_home"
    fake_home.mkdir()
    agents_dir = fake_home / "agents"
    playbooks_dir = fake_home / "playbooks"
    agents_dir.mkdir()
    playbooks_dir.mkdir()

    fake_db = tmp_path / "state.db"  # does NOT exist yet

    monkeypatch.setattr(cli_runs_mod, "LIONAGI_HOME", fake_home)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "_DB", str(fake_db))
    monkeypatch.setattr(defs_mod, "AGENTS_DIR", agents_dir)
    monkeypatch.setattr(defs_mod, "PLAYBOOKS_DIR", playbooks_dir)
    monkeypatch.setattr(defs_mod, "KIND_DIRS", {"agent": agents_dir, "playbook": playbooks_dir})

    result = _run(
        defs_mod.save_definition(
            "agent", "test-agent", "# Test Agent\nGuidance here.", "initial save"
        )
    )

    # DB file must exist now (StateDB created it)
    assert fake_db.exists(), "DB file must be created by StateDB on first use"

    # Result must carry a valid version number
    assert result["version"] >= 1
    assert result["kind"] == "agent"
    assert result["name"] == "test-agent"
    assert "saved_at" in result

    # Disk file must also exist
    agent_file = agents_dir / "test-agent.md"
    assert agent_file.exists(), "Disk file must be written after DB row is committed"
    assert agent_file.read_text() == "# Test Agent\nGuidance here."

    # Verify the DB row was actually inserted
    import aiosqlite

    async def _check_db():
        async with aiosqlite.connect(str(fake_db)) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT version, name, kind FROM definitions WHERE kind = 'agent' AND name = 'test-agent'"
            )
            rows = await cur.fetchall()
        return rows

    rows = _run(_check_db())
    assert len(rows) == 1, "Exactly one DB row must exist after save"
    assert rows[0]["version"] == 1


def test_save_definition_increments_version(tmp_path, monkeypatch):
    """Calling save_definition() twice for the same (kind, name) must increment version."""
    import apps.studio.server.services.definitions as defs_mod
    import lionagi.cli._runs as cli_runs_mod
    import lionagi.state.db as state_db_mod

    fake_home = tmp_path / "lionagi_home"
    fake_home.mkdir()
    agents_dir = fake_home / "agents"
    agents_dir.mkdir()
    playbooks_dir = fake_home / "playbooks"
    playbooks_dir.mkdir()
    fake_db = tmp_path / "state.db"

    monkeypatch.setattr(cli_runs_mod, "LIONAGI_HOME", fake_home)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "_DB", str(fake_db))
    monkeypatch.setattr(defs_mod, "AGENTS_DIR", agents_dir)
    monkeypatch.setattr(defs_mod, "PLAYBOOKS_DIR", playbooks_dir)
    monkeypatch.setattr(defs_mod, "KIND_DIRS", {"agent": agents_dir, "playbook": playbooks_dir})

    r1 = _run(defs_mod.save_definition("agent", "my-agent", "v1 content"))
    r2 = _run(defs_mod.save_definition("agent", "my-agent", "v2 content"))

    assert r1["version"] == 1
    assert r2["version"] == 2


def test_save_definition_unknown_kind_raises(tmp_path, monkeypatch):
    """save_definition() with an unknown kind must raise ValueError (not return success)."""
    import apps.studio.server.services.definitions as defs_mod
    import lionagi.cli._runs as cli_runs_mod

    fake_home = tmp_path / "lionagi_home"
    fake_home.mkdir()
    (fake_home / "agents").mkdir()
    (fake_home / "playbooks").mkdir()

    monkeypatch.setattr(cli_runs_mod, "LIONAGI_HOME", fake_home)
    monkeypatch.setattr(
        defs_mod, "KIND_DIRS", {"agent": fake_home / "agents", "playbook": fake_home / "playbooks"}
    )

    with pytest.raises(ValueError, match="Unknown kind"):
        _run(defs_mod.save_definition("skill", "my-skill", "content"))
