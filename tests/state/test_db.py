# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""
Comprehensive tests for lionagi.state.db.StateDB.

All tests use in-memory SQLite (:memory:) for speed and isolation.
asyncio_mode = "auto" in pyproject.toml — no @pytest.mark.asyncio needed.
"""

from __future__ import annotations

import time
import uuid

import pytest

from lionagi.state.db import StateDB


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def db():
    """Fresh in-memory StateDB for each test."""
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


# ── Helpers ───────────────────────────────────────────────────────────────────


def uid() -> str:
    return str(uuid.uuid4())


def make_message(*, role: str = "user", lion_class: str = "") -> dict:
    node_meta = {"lion_class": lion_class} if lion_class else {}
    return {
        "id": uid(),
        "created_at": time.time(),
        "node_metadata": node_meta,
        "content": {"text": "hello"},
        "role": role,
        "sender": "test-sender",
        "recipient": "test-recipient",
        "channel": "test-channel",
        "embedding": None,
    }


async def _make_session(db: StateDB, *, status: str | None = None) -> dict:
    """Create a progression + session, return the session dict."""
    prog_id = uid()
    await db.create_progression(prog_id)
    session = {
        "id": uid(),
        "progression_id": prog_id,
        "status": status,
    }
    await db.create_session(session)
    return session


async def _make_show(db: StateDB, *, status: str = "active") -> dict:
    show = {
        "id": uid(),
        "topic": f"topic-{uid()}",
        "show_dir": f"/tmp/show-{uid()}",
        "status": status,
    }
    await db.create_show(show)
    return show


# ── Connection lifecycle ───────────────────────────────────────────────────────


async def test_open_close():
    """Open connects and applies pragmas; close nulls the internal connection.

    Note: in-memory SQLite ignores WAL mode (always returns 'memory') — WAL is
    a file-system-level feature.  We verify pragmas were applied by issuing a
    read-back of foreign_keys (which works in-memory) and that the schema is
    accessible.
    """
    state = StateDB(":memory:")
    await state.open()

    # foreign_keys pragma is set to ON in _apply_pragmas — verify round-trip
    cur = await state.db.execute("PRAGMA foreign_keys")
    row = await cur.fetchone()
    assert row[0] == 1  # 1 = ON

    # Schema is available after open
    version = await state.schema_version()
    assert version == "1"

    await state.close()
    assert state._db is None


async def test_context_manager():
    """async with opens and closes cleanly."""
    async with StateDB(":memory:") as state:
        version = await state.schema_version()
        assert version == "1"
    assert state._db is None


async def test_db_property_raises_when_closed():
    """Accessing .db before open() raises RuntimeError."""
    state = StateDB(":memory:")
    with pytest.raises(RuntimeError, match="not open"):
        _ = state.db


# ── Schema ─────────────────────────────────────────────────────────────────────


async def test_schema_creates_all_tables(db: StateDB):
    """All 8 tables are present after open()."""
    expected = {
        "schema_meta",
        "message_types",
        "messages",
        "progressions",
        "sessions",
        "branches",
        "shows",
        "plays",
        "definitions",
    }
    cur = await db.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    rows = await cur.fetchall()
    names = {r[0] for r in rows}
    assert expected <= names, f"Missing tables: {expected - names}"


async def test_schema_version(db: StateDB):
    """schema_version() returns '1'."""
    assert await db.schema_version() == "1"


async def test_apply_schema_adds_missing_columns_on_old_db(tmp_path):
    """Regression: an older state.db that pre-dates ADR-0012 / ADR-0017
    columns must have them ADD COLUMN'd in by ``_reconcile_columns``.

    Without this migration, ``CREATE TABLE IF NOT EXISTS`` is a no-op
    on the existing tables, so ``create_session(status='running')``
    fails with ``OperationalError: table sessions has no column named
    status`` — the broad except in CLI live-persist setup swallows the
    error, returns ``None``, and leaks the aiosqlite worker thread.
    Resulting symptom: the CLI process hangs forever after the agent
    completes.
    """
    import aiosqlite

    path = tmp_path / "old.db"

    # Simulate a real pre-PR-980 DB: ADR-0009 core columns are present
    # (since they shipped first), but the provenance/lifecycle columns
    # added later are missing.
    async with aiosqlite.connect(str(path)) as old:
        await old.execute(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, created_at REAL, node_metadata TEXT, "
            "name TEXT, user TEXT, progression_id TEXT, "
            "first_msg_id TEXT, last_msg_id TEXT)"
        )
        await old.execute(
            "CREATE TABLE branches ("
            "id TEXT PRIMARY KEY, created_at REAL, node_metadata TEXT, "
            "user TEXT, name TEXT, session_id TEXT, progression_id TEXT)"
        )
        await old.commit()

    # Opening with the current StateDB must reconcile in the new columns
    # AND the index/trigger statements in schema.sql (which reference
    # those columns) must succeed.
    db = StateDB(str(path))
    await db.open()
    try:
        cur = await db.db.execute("PRAGMA table_info(sessions)")
        cols = {r["name"] for r in await cur.fetchall()}
        for must_have in (
            "status", "started_at", "ended_at", "invocation_kind",
            "playbook_name", "agent_name", "artifacts_path",
            "source_kind", "updated_at",
        ):
            assert must_have in cols, f"sessions.{must_have} not migrated"
        cur = await db.db.execute("PRAGMA table_info(branches)")
        bcols = {r["name"] for r in await cur.fetchall()}
        assert "system_msg_id" in bcols
        # And the live-persist write path actually works against the
        # migrated DB (the symptom we're guarding against).
        prog_id = uid()
        await db.create_progression(prog_id)
        await db.create_session({
            "id": uid(), "progression_id": prog_id,
            "created_at": time.time(), "status": "running",
            "started_at": time.time(),
        })
    finally:
        await db.close()


async def test_message_types_seeded(db: StateDB):
    """6 message types pre-seeded (0 = __unknown__, 1-5 = known classes)."""
    cur = await db.db.execute("SELECT COUNT(*) AS n FROM message_types")
    row = await cur.fetchone()
    assert row["n"] == 6

    cur = await db.db.execute(
        "SELECT lion_class FROM message_types WHERE type_id = 0"
    )
    row = await cur.fetchone()
    assert row["lion_class"] == "__unknown__"


# ── Messages ───────────────────────────────────────────────────────────────────


async def test_insert_and_get_message(db: StateDB):
    """Insert a message and retrieve it; all fields roundtrip."""
    msg = make_message(role="user")
    await db.insert_message(msg)

    retrieved = await db.get_message(msg["id"])
    assert retrieved is not None
    assert retrieved["id"] == msg["id"]
    assert retrieved["role"] == "user"
    assert retrieved["sender"] == "test-sender"
    assert retrieved["recipient"] == "test-recipient"
    assert retrieved["channel"] == "test-channel"
    # content was a dict — db round-trips it back to dict
    assert isinstance(retrieved["content"], dict)
    assert retrieved["content"]["text"] == "hello"


async def test_insert_message_idempotent(db: StateDB):
    """INSERT OR IGNORE: inserting the same id twice does not error."""
    msg = make_message()
    await db.insert_message(msg)
    # Second insert — same id, should silently be ignored
    await db.insert_message(msg)

    cur = await db.db.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE id = ?", (msg["id"],)
    )
    row = await cur.fetchone()
    assert row["n"] == 1


async def test_resolve_lion_class_known(db: StateDB):
    """A known lion_class string returns the correct seeded type_id."""
    known = "lionagi.protocols.messages.system.System"
    msg = make_message(lion_class=known)
    await db.insert_message(msg)

    cur = await db.db.execute(
        "SELECT lion_class FROM messages WHERE id = ?", (msg["id"],)
    )
    row = await cur.fetchone()
    # type_id 1 maps to System
    assert row["lion_class"] == 1


async def test_resolve_lion_class_unknown_empty(db: StateDB):
    """Empty lion_class string returns sentinel type_id 0."""
    msg = make_message(lion_class="")
    await db.insert_message(msg)

    cur = await db.db.execute(
        "SELECT lion_class FROM messages WHERE id = ?", (msg["id"],)
    )
    row = await cur.fetchone()
    assert row["lion_class"] == 0


async def test_resolve_lion_class_auto_register(db: StateDB):
    """Unknown non-empty class is auto-registered and gets a new type_id."""
    novel_class = "myapp.custom.CustomMessage"
    msg = make_message(lion_class=novel_class)
    await db.insert_message(msg)

    cur = await db.db.execute(
        "SELECT type_id FROM message_types WHERE lion_class = ?",
        (novel_class,),
    )
    row = await cur.fetchone()
    assert row is not None
    # Must be > 5 (beyond the seeded range)
    assert row["type_id"] > 5


# ── Progressions ───────────────────────────────────────────────────────────────


async def test_create_and_get_progression(db: StateDB):
    """Create with an initial collection; get returns the same list."""
    prog_id = uid()
    initial = [uid(), uid(), uid()]
    await db.create_progression(prog_id, initial)

    result = await db.get_progression(prog_id)
    assert result == initial


async def test_append_to_progression(db: StateDB):
    """Append preserves insertion order."""
    prog_id = uid()
    first, second, third = uid(), uid(), uid()
    await db.create_progression(prog_id, [first])
    await db.append_to_progression(prog_id, second)
    await db.append_to_progression(prog_id, third)

    result = await db.get_progression(prog_id)
    assert result == [first, second, third]


async def test_get_progression_missing(db: StateDB):
    """Fetching a non-existent progression returns an empty list."""
    result = await db.get_progression(uid())
    assert result == []


# ── Sessions ───────────────────────────────────────────────────────────────────


async def test_create_session_with_provenance(db: StateDB):
    """Create with all provenance + lifecycle columns and verify roundtrip."""
    prog_id = uid()
    await db.create_progression(prog_id)

    now = time.time()
    session = {
        "id": uid(),
        "progression_id": prog_id,
        "name": "test-session",
        "user": "ocean",
        "playbook_name": "my-playbook",
        "agent_name": "my-agent",
        "invocation_kind": "agent",
        "show_topic": "refactor-state",
        "show_play_name": "play-1",
        "artifacts_path": "/tmp/artifacts",
        "source_kind": "imported_fs",
        "status": "running",
        "started_at": now,
        "ended_at": None,
    }
    await db.create_session(session)

    retrieved = await db.get_session(session["id"])
    assert retrieved is not None
    assert retrieved["name"] == "test-session"
    assert retrieved["user"] == "ocean"
    assert retrieved["playbook_name"] == "my-playbook"
    assert retrieved["agent_name"] == "my-agent"
    assert retrieved["invocation_kind"] == "agent"
    assert retrieved["show_topic"] == "refactor-state"
    assert retrieved["show_play_name"] == "play-1"
    assert retrieved["source_kind"] == "imported_fs"
    assert retrieved["status"] == "running"
    assert retrieved["ended_at"] is None


async def test_create_session_minimal(db: StateDB):
    """Only required fields (id, progression_id) — no error."""
    prog_id = uid()
    await db.create_progression(prog_id)
    session = {"id": uid(), "progression_id": prog_id}
    await db.create_session(session)

    retrieved = await db.get_session(session["id"])
    assert retrieved is not None
    assert retrieved["id"] == session["id"]
    assert retrieved["name"] is None
    assert retrieved["status"] is None


async def test_update_session(db: StateDB):
    """update_session changes the given fields."""
    s = await _make_session(db, status="running")
    end_time = time.time()

    await db.update_session(s["id"], status="completed", ended_at=end_time)

    retrieved = await db.get_session(s["id"])
    assert retrieved["status"] == "completed"
    assert retrieved["ended_at"] == pytest.approx(end_time, abs=1e-3)


async def test_update_session_rejects_bad_columns(db: StateDB):
    """Passing an invalid column name to update_session raises ValueError."""
    s = await _make_session(db)
    with pytest.raises(ValueError, match="Invalid column"):
        await db.update_session(s["id"], nonexistent_column="boom")


async def test_list_sessions_by_status(db: StateDB):
    """list_sessions filters correctly by status."""
    await _make_session(db, status="running")
    await _make_session(db, status="running")
    await _make_session(db, status="completed")
    await _make_session(db, status="failed")

    running = await db.list_sessions(status="running")
    completed = await db.list_sessions(status="completed")
    failed = await db.list_sessions(status="failed")

    assert len(running) == 2
    assert len(completed) == 1
    assert len(failed) == 1
    assert all(s["status"] == "running" for s in running)


async def test_count_sessions(db: StateDB):
    """count_sessions returns correct total and per-status counts."""
    await _make_session(db, status="running")
    await _make_session(db, status="running")
    await _make_session(db, status="completed")

    total = await db.count_sessions()
    assert total == 3

    running = await db.count_sessions(status="running")
    assert running == 2

    completed = await db.count_sessions(status="completed")
    assert completed == 1

    failed = await db.count_sessions(status="failed")
    assert failed == 0


# ── Branches ───────────────────────────────────────────────────────────────────


async def test_create_and_get_branch(db: StateDB):
    """Full branch roundtrip."""
    s = await _make_session(db)
    prog_id = uid()
    await db.create_progression(prog_id)

    branch = {
        "id": uid(),
        "session_id": s["id"],
        "progression_id": prog_id,
        "user": "ocean",
        "name": "main",
        "node_metadata": {"model": "gpt-4.1", "provider": "openai"},
    }
    await db.create_branch(branch)

    retrieved = await db.get_branch(branch["id"])
    assert retrieved is not None
    assert retrieved["id"] == branch["id"]
    assert retrieved["user"] == "ocean"
    assert retrieved["name"] == "main"
    assert retrieved["session_id"] == s["id"]
    # node_metadata deserialised back to dict
    assert isinstance(retrieved["node_metadata"], dict)
    assert retrieved["node_metadata"]["model"] == "gpt-4.1"


async def test_create_branch_idempotent(db: StateDB):
    """INSERT OR IGNORE: second insert with same id is a no-op; original preserved."""
    s = await _make_session(db)
    prog_id = uid()
    await db.create_progression(prog_id)

    branch_id = uid()
    original = {
        "id": branch_id,
        "session_id": s["id"],
        "progression_id": prog_id,
        "name": "original-name",
    }
    await db.create_branch(original)

    # Attempt to overwrite with different name — should be silently ignored
    duplicate = {
        "id": branch_id,
        "session_id": s["id"],
        "progression_id": prog_id,
        "name": "overwritten-name",
    }
    await db.create_branch(duplicate)

    retrieved = await db.get_branch(branch_id)
    assert retrieved["name"] == "original-name"


async def test_list_branches(db: StateDB):
    """list_branches returns all branches for a session ordered by created_at."""
    s = await _make_session(db)

    branch_ids = []
    for i in range(3):
        prog_id = uid()
        await db.create_progression(prog_id)
        b = {
            "id": uid(),
            "session_id": s["id"],
            "progression_id": prog_id,
            "name": f"branch-{i}",
            "created_at": time.time() + i,  # ensure distinct ordering
        }
        await db.create_branch(b)
        branch_ids.append(b["id"])

    branches = await db.list_branches(s["id"])
    assert len(branches) == 3
    assert [b["id"] for b in branches] == branch_ids


async def test_get_branch_messages(db: StateDB):
    """get_branch_messages returns messages in progression order."""
    s = await _make_session(db)
    prog_id = uid()
    await db.create_progression(prog_id)

    # Insert three messages in order
    msgs = [make_message(role="user"), make_message(role="assistant"), make_message(role="user")]
    for m in msgs:
        await db.insert_message(m)
        await db.append_to_progression(prog_id, m["id"])

    branch = {
        "id": uid(),
        "session_id": s["id"],
        "progression_id": prog_id,
    }
    await db.create_branch(branch)

    result = await db.get_branch_messages(branch["id"])
    assert len(result) == 3
    # Order must match progression order
    assert [r["id"] for r in result] == [m["id"] for m in msgs]


# ── Shows ─────────────────────────────────────────────────────────────────────


async def test_create_and_get_show(db: StateDB):
    """Full show roundtrip."""
    show = {
        "id": uid(),
        "topic": "add-feature-x",
        "goal": "Implement X end to end",
        "repo": "owner/repo",
        "base_branch": "main",
        "integration_branch": "integrate/x",
        "status": "active",
        "show_dir": "/tmp/shows/x",
    }
    await db.create_show(show)

    retrieved = await db.get_show(show["id"])
    assert retrieved is not None
    assert retrieved["topic"] == "add-feature-x"
    assert retrieved["goal"] == "Implement X end to end"
    assert retrieved["repo"] == "owner/repo"
    assert retrieved["base_branch"] == "main"
    assert retrieved["integration_branch"] == "integrate/x"
    assert retrieved["status"] == "active"
    assert retrieved["show_dir"] == "/tmp/shows/x"


async def test_get_show_by_topic(db: StateDB):
    """get_show_by_topic finds a show by its unique topic field."""
    topic = f"unique-topic-{uid()}"
    show = {"id": uid(), "topic": topic, "show_dir": "/tmp/x", "status": "active"}
    await db.create_show(show)

    retrieved = await db.get_show_by_topic(topic)
    assert retrieved is not None
    assert retrieved["id"] == show["id"]

    # Non-existent topic returns None
    assert await db.get_show_by_topic("no-such-topic") is None


async def test_list_shows_by_status(db: StateDB):
    """list_shows filters correctly by status."""
    await _make_show(db, status="active")
    await _make_show(db, status="active")
    await _make_show(db, status="completed")

    active = await db.list_shows(status="active")
    completed = await db.list_shows(status="completed")
    all_shows = await db.list_shows()

    assert len(active) == 2
    assert len(completed) == 1
    assert len(all_shows) == 3


async def test_update_show(db: StateDB):
    """update_show changes the given fields."""
    show = await _make_show(db, status="active")

    await db.update_show(show["id"], status="completed")

    retrieved = await db.get_show(show["id"])
    assert retrieved["status"] == "completed"


async def test_update_show_rejects_bad_columns(db: StateDB):
    """Passing an invalid column name to update_show raises ValueError."""
    show = await _make_show(db)
    with pytest.raises(ValueError, match="Invalid column"):
        await db.update_show(show["id"], not_a_column="boom")


# ── Plays ─────────────────────────────────────────────────────────────────────


async def test_create_and_get_play(db: StateDB):
    """Full play roundtrip including depends_on JSON."""
    show = await _make_show(db)

    dep1, dep2 = uid(), uid()
    play = {
        "id": uid(),
        "show_id": show["id"],
        "name": "play-alpha",
        "playbook": "review-flow",
        "effort": "medium",
        "status": "pending",
        "attempt": 1,
        "sort_order": 10,
        "depends_on": [dep1, dep2],
        "worktree": "/tmp/wt/alpha",
        "branch": "show/alpha",
    }
    await db.create_play(play)

    retrieved = await db.get_play(play["id"])
    assert retrieved is not None
    assert retrieved["name"] == "play-alpha"
    assert retrieved["playbook"] == "review-flow"
    assert retrieved["effort"] == "medium"
    assert retrieved["sort_order"] == 10
    assert retrieved["worktree"] == "/tmp/wt/alpha"
    assert retrieved["branch"] == "show/alpha"
    # depends_on deserialized back to list
    assert isinstance(retrieved["depends_on"], list)
    assert retrieved["depends_on"] == [dep1, dep2]


async def test_list_plays_ordered(db: StateDB):
    """list_plays returns plays sorted by sort_order then created_at."""
    show = await _make_show(db)
    t0 = time.time()

    plays = [
        {"id": uid(), "show_id": show["id"], "name": "p3", "sort_order": 30, "created_at": t0},
        {"id": uid(), "show_id": show["id"], "name": "p1", "sort_order": 10, "created_at": t0 + 1},
        {"id": uid(), "show_id": show["id"], "name": "p2", "sort_order": 20, "created_at": t0 + 2},
    ]
    for p in plays:
        await db.create_play(p)

    result = await db.list_plays(show["id"])
    assert [r["name"] for r in result] == ["p1", "p2", "p3"]


async def test_update_play(db: StateDB):
    """update_play changes status and exit_code."""
    show = await _make_show(db)
    play = {"id": uid(), "show_id": show["id"], "name": "update-me"}
    await db.create_play(play)

    end_time = time.time()
    # ADR-0011 vocab: plays use ``running_complete`` (not ``completed``)
    # for the "finished running" terminal — ``completed`` belongs to the
    # sessions vocabulary (ADR-0017), not plays.
    await db.update_play(
        play["id"],
        status="running_complete",
        exit_code=0,
        ended_at=end_time,
    )

    retrieved = await db.get_play(play["id"])
    assert retrieved["status"] == "running_complete"
    assert retrieved["exit_code"] == 0
    assert retrieved["ended_at"] == pytest.approx(end_time, abs=1e-3)


async def test_update_play_rejects_bad_columns(db: StateDB):
    """Passing an invalid column name to update_play raises ValueError."""
    show = await _make_show(db)
    play = {"id": uid(), "show_id": show["id"], "name": "bad-col-test"}
    await db.create_play(play)

    with pytest.raises(ValueError, match="Invalid column"):
        await db.update_play(play["id"], hacker_column="evil")


# ── Definitions ───────────────────────────────────────────────────────────────


async def test_save_and_get_definition(db: StateDB):
    """save_definition returns version 1; get_definition returns latest."""
    version = await db.save_definition(
        kind="agent",
        name="analyst",
        path=".lionagi/agents/analyst.yaml",
        content="role: analyst\nmodel: gpt-4.1",
        message="initial",
    )
    assert version == 1

    defn = await db.get_definition("agent", "analyst")
    assert defn is not None
    assert defn["version"] == 1
    assert defn["kind"] == "agent"
    assert defn["name"] == "analyst"
    assert defn["content"] == "role: analyst\nmodel: gpt-4.1"
    assert defn["message"] == "initial"


async def test_definition_versioning(db: StateDB):
    """save_definition auto-increments; get_definition fetches by exact version."""
    v1 = await db.save_definition(
        kind="playbook",
        name="review-flow",
        path=".lionagi/playbooks/review-flow.yaml",
        content="v1 content",
    )
    v2 = await db.save_definition(
        kind="playbook",
        name="review-flow",
        path=".lionagi/playbooks/review-flow.yaml",
        content="v2 content",
        message="update instructions",
    )

    assert v1 == 1
    assert v2 == 2

    defn_v1 = await db.get_definition("playbook", "review-flow", version=1)
    defn_v2 = await db.get_definition("playbook", "review-flow", version=2)

    assert defn_v1["content"] == "v1 content"
    assert defn_v2["content"] == "v2 content"
    assert defn_v2["message"] == "update instructions"

    # get_definition without version returns latest
    latest = await db.get_definition("playbook", "review-flow")
    assert latest["version"] == 2


async def test_list_definition_versions(db: StateDB):
    """list_definition_versions returns all versions in descending order."""
    for i in range(3):
        await db.save_definition(
            kind="agent",
            name="reviewer",
            path=".lionagi/agents/reviewer.md",
            content=f"version {i + 1}",
        )

    versions = await db.list_definition_versions("agent", "reviewer")
    assert len(versions) == 3
    # Descending version order
    assert [v["version"] for v in versions] == [3, 2, 1]


async def test_save_definition_rejects_non_editable_kind(db: StateDB):
    """ADR-0016: skills + arbitrary kinds are read-only and must be rejected."""
    import pytest

    for bad_kind in ("skill", "plugin", "something_else"):
        with pytest.raises(ValueError, match="Invalid definition kind"):
            await db.save_definition(
                kind=bad_kind,
                name="x",
                path=".lionagi/x",
                content="content",
            )


async def test_get_definition_missing(db: StateDB):
    """get_definition returns None for a (kind, name) that doesn't exist."""
    result = await db.get_definition("agent", "nonexistent-agent")
    assert result is None

    # Also for an explicit version that doesn't exist
    result_versioned = await db.get_definition("agent", "nonexistent-agent", version=99)
    assert result_versioned is None
