# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Teams + team_messages schema tests — schema constraints, FK cascade, and import-teams backfill path."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

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
    async with db._tx() as conn:
        await conn.execute(
            text(
                "INSERT INTO teams (id, name, created_at, updated_at, status) "
                "VALUES (:id, :name, :ca, :ua, :st)"
            ),
            {"id": "t1", "name": "team-one", "ca": 1.0, "ua": 1.0, "st": "active"},
        )
        await conn.execute(
            text(
                "INSERT INTO teams (id, name, created_at, updated_at, status) "
                "VALUES (:id, :name, :ca, :ua, :st)"
            ),
            {"id": "t2", "name": "team-two", "ca": 1.0, "ua": 1.0, "st": "archived"},
        )

    with pytest.raises(IntegrityError):
        async with db._tx() as conn:
            await conn.execute(
                text(
                    "INSERT INTO teams (id, name, created_at, updated_at, status) "
                    "VALUES (:id, :name, :ca, :ua, :st)"
                ),
                {"id": "t3", "name": "team-three", "ca": 1.0, "ua": 1.0, "st": "frozen"},
            )


async def test_team_messages_cascade_on_team_delete(db: StateDB):
    async with db._tx() as conn:
        await conn.execute(
            text(
                "INSERT INTO teams (id, name, created_at, updated_at) VALUES (:id, :name, :ca, :ua)"
            ),
            {"id": "t1", "name": "team-one", "ca": 1.0, "ua": 1.0},
        )
        await conn.execute(
            text(
                "INSERT INTO team_messages (id, team_id, created_at, sender, content) "
                "VALUES (:id, :tid, :ca, :sender, :content)"
            ),
            {"id": "m1", "tid": "t1", "ca": 1.0, "sender": "alice", "content": "hello"},
        )

    async with db._read() as conn:
        count = (
            (await conn.execute(text("SELECT COUNT(*) AS n FROM team_messages"))).mappings().first()
        )
    assert count["n"] == 1

    async with db._tx() as conn:
        await conn.execute(text("DELETE FROM teams WHERE id = :id"), {"id": "t1"})

    async with db._read() as conn:
        count = (
            (await conn.execute(text("SELECT COUNT(*) AS n FROM team_messages"))).mappings().first()
        )
    assert count["n"] == 0, "team_messages should cascade-delete when their team is removed"


async def test_team_messages_session_id_fk_to_sessions(db: StateDB):
    """team_messages.session_id may reference a session row (optional FK)."""
    prog_id = str(uuid.uuid4())
    sess_id = str(uuid.uuid4())
    await db.create_progression(prog_id)
    await db.create_session({"id": sess_id, "progression_id": prog_id, "status": "running"})

    async with db._tx() as conn:
        await conn.execute(
            text(
                "INSERT INTO teams (id, name, created_at, updated_at) VALUES (:id, :name, :ca, :ua)"
            ),
            {"id": "t1", "name": "team-one", "ca": 1.0, "ua": 1.0},
        )
        await conn.execute(
            text(
                "INSERT INTO team_messages (id, team_id, created_at, sender, content, session_id) "
                "VALUES (:id, :tid, :ca, :sender, :content, :sid)"
            ),
            {
                "id": "m1",
                "tid": "t1",
                "ca": 1.0,
                "sender": "alice",
                "content": "hello",
                "sid": sess_id,
            },
        )

    async with db._read() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT session_id FROM team_messages WHERE id = :id"), {"id": "m1"}
                )
            )
            .mappings()
            .first()
        )
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
        async with db._read() as conn:
            team_row = (
                (
                    await conn.execute(
                        text("SELECT id, name, member_count, members, status FROM teams")
                    )
                )
                .mappings()
                .first()
            )
        assert team_row["id"] == "abc123"
        assert team_row["name"] == "review-team"
        assert team_row["member_count"] == 2
        members = team_row["members"]
        if isinstance(members, str):
            members = json.loads(members)
        assert members == ["alice", "bob"]
        assert team_row["status"] == "active"

        async with db._read() as conn:
            msgs = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, sender, recipient, content "
                            "FROM team_messages ORDER BY created_at"
                        )
                    )
                )
                .mappings()
                .all()
            )
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


async def test_import_teams_skips_a_torn_file_via_shared_lock_read(tmp_path: Path, monkeypatch):
    """A corrupt/torn team file must count as an error, not crash the
    import — read_team_json (the same shared-flock reader every other
    team-file consumer uses), not a raw json.loads(read_text())."""
    teams_dir = tmp_path / "teams"
    teams_dir.mkdir()
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    state_db = tmp_path / "state.db"

    (teams_dir / "good.json").write_text(
        json.dumps({"id": "good", "name": "t", "members": [], "messages": []})
    )
    (teams_dir / "torn.json").write_text("{not valid json")

    from lionagi.cli import state as state_mod
    from lionagi.state import db as db_mod

    monkeypatch.setattr(state_mod, "RUNS_ROOT", runs_dir)
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", state_db)

    counts = await state_mod._import_teams()

    assert counts["teams"] == 1
    assert counts["errors"] == 1
