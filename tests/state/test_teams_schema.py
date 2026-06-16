# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0019 teams + team_messages schema tests — schema constraints, FK cascade, and import-teams backfill path."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import pytest

from lionagi.state.db import StateDB


@pytest.fixture
async def db():
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


# ── Schema invariants ─────────────────────────────────────────────────────────


async def test_teams_table_exists_with_status_check(db: StateDB):
    """Schema CHECK accepts 'active' / 'archived' and rejects others."""
    await db.db.execute(
        "INSERT INTO teams (id, name, created_at, updated_at, status) VALUES (?, ?, ?, ?, ?)",
        ("t1", "team-one", 1.0, 1.0, "active"),
    )
    await db.db.execute(
        "INSERT INTO teams (id, name, created_at, updated_at, status) VALUES (?, ?, ?, ?, ?)",
        ("t2", "team-two", 1.0, 1.0, "archived"),
    )

    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        await db.db.execute(
            "INSERT INTO teams (id, name, created_at, updated_at, status) VALUES (?, ?, ?, ?, ?)",
            ("t3", "team-three", 1.0, 1.0, "frozen"),
        )


async def test_team_messages_cascade_on_team_delete(db: StateDB):
    await db.db.execute(
        "INSERT INTO teams (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("t1", "team-one", 1.0, 1.0),
    )
    await db.db.execute(
        "INSERT INTO team_messages (id, team_id, created_at, sender, content) "
        "VALUES (?, ?, ?, ?, ?)",
        ("m1", "t1", 1.0, "alice", "hello"),
    )
    await db.db.commit()

    cur = await db.db.execute("SELECT COUNT(*) AS n FROM team_messages")
    assert (await cur.fetchone())["n"] == 1

    await db.db.execute("DELETE FROM teams WHERE id = ?", ("t1",))
    await db.db.commit()

    cur = await db.db.execute("SELECT COUNT(*) AS n FROM team_messages")
    assert (await cur.fetchone())["n"] == 0, (
        "team_messages should cascade-delete when their team is removed"
    )


async def test_team_messages_session_id_fk_to_sessions(db: StateDB):
    """team_messages.session_id may reference a session row (optional FK)."""
    prog_id = str(uuid.uuid4())
    sess_id = str(uuid.uuid4())
    await db.create_progression(prog_id)
    await db.create_session({"id": sess_id, "progression_id": prog_id, "status": "running"})

    await db.db.execute(
        "INSERT INTO teams (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("t1", "team-one", 1.0, 1.0),
    )
    await db.db.execute(
        "INSERT INTO team_messages (id, team_id, created_at, sender, content, session_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("m1", "t1", 1.0, "alice", "hello", sess_id),
    )
    await db.db.commit()

    cur = await db.db.execute("SELECT session_id FROM team_messages WHERE id = ?", ("m1",))
    row = await cur.fetchone()
    assert row["session_id"] == sess_id


# ── import-teams CLI helper ───────────────────────────────────────────────────


async def test_import_teams_loads_json_into_db(tmp_path: Path, monkeypatch):
    """JSON file → row in teams + per-message rows in team_messages."""
    teams_dir = tmp_path / "teams"
    teams_dir.mkdir()
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    state_db = tmp_path / "state.db"

    # Seed one JSON file mirroring the live li-team format.
    team_doc = {
        "id": "abc123",
        "name": "review-team",
        "members": ["alice", "bob"],
        "messages": [
            {
                "id": "m1",
                "from": "alice",
                "to": ["bob"],
                "content": "what's the verdict?",
                "timestamp": "2026-05-21T15:00:00+00:00",
                "read_by": {},
            },
            {
                "id": "m2",
                "from": "bob",
                "to": ["*"],
                "content": "approve",
                "timestamp": "2026-05-21T15:05:00+00:00",
                "read_by": {"alice": "2026-05-21T15:06:00+00:00"},
            },
        ],
    }
    (teams_dir / f"{team_doc['id']}.json").write_text(json.dumps(team_doc))

    # Redirect RUNS_ROOT so _import_teams looks at the temp teams_dir
    # (which is sibling to runs/ in the same temp parent).
    from lionagi.cli import state as state_mod

    monkeypatch.setattr(state_mod, "RUNS_ROOT", runs_dir)

    # Use a temp DB path for the import.
    from lionagi.state import db as db_mod

    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", state_db)

    counts = await state_mod._import_teams()
    assert counts["teams"] == 1
    assert counts["messages"] == 2
    assert counts["skipped_teams"] == 0
    assert counts["errors"] == 0

    # Verify rows landed correctly.
    async with StateDB(state_db) as db:
        cur = await db.db.execute("SELECT id, name, member_count, members, status FROM teams")
        team_row = await cur.fetchone()
        assert team_row["id"] == "abc123"
        assert team_row["name"] == "review-team"
        assert team_row["member_count"] == 2
        assert json.loads(team_row["members"]) == ["alice", "bob"]
        assert team_row["status"] == "active"

        cur = await db.db.execute(
            "SELECT id, sender, recipient, content FROM team_messages ORDER BY created_at"
        )
        msgs = await cur.fetchall()
        assert len(msgs) == 2
        assert msgs[0]["sender"] == "alice"
        assert msgs[0]["recipient"] == "bob"
        assert msgs[1]["recipient"] == "all"  # ["*"] → "all"


async def test_import_teams_is_idempotent(tmp_path: Path, monkeypatch):
    """Running twice imports the team once; second run skips."""
    teams_dir = tmp_path / "teams"
    teams_dir.mkdir()
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    state_db = tmp_path / "state.db"

    (teams_dir / "abc.json").write_text(
        json.dumps({"id": "abc", "name": "t", "members": [], "messages": []})
    )

    from lionagi.cli import state as state_mod
    from lionagi.state import db as db_mod

    monkeypatch.setattr(state_mod, "RUNS_ROOT", runs_dir)
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", state_db)

    first = await state_mod._import_teams()
    second = await state_mod._import_teams()

    assert first["teams"] == 1
    assert second["teams"] == 0
    assert second["skipped_teams"] == 1
