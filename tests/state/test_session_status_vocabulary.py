# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0025 session status vocabulary tests — seven-value vocabulary (adds
completed_empty for the completion-trust gate), FSM transitions, admin
transitions, and legacy CHECK-constraint rebuild path."""

from __future__ import annotations

import uuid
from pathlib import Path

import aiosqlite
import pytest

from lionagi.state.db import (
    ADMIN_TRANSITION_TARGETS,
    SESSION_TERMINAL_STATUSES,
    VALID_SESSION_STATUSES,
    StateDB,
    can_transition,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def db():
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


def _uid() -> str:
    return str(uuid.uuid4())


async def _make_session(db: StateDB, *, status: str | None = None) -> dict:
    prog_id = _uid()
    await db.create_progression(prog_id)
    session = {
        "id": _uid(),
        "progression_id": prog_id,
        "status": status,
    }
    await db.create_session(session)
    return session


# ── Vocabulary ────────────────────────────────────────────────────────────────


def test_vocabulary_has_seven_values():
    assert VALID_SESSION_STATUSES == frozenset(
        {
            "running",
            "completed",
            "completed_empty",
            "failed",
            "timed_out",
            "aborted",
            "cancelled",
        }
    )


def test_terminal_set_excludes_running():
    assert "running" not in SESSION_TERMINAL_STATUSES
    assert SESSION_TERMINAL_STATUSES == VALID_SESSION_STATUSES - {"running"}


def test_admin_targets_exclude_completed_and_timed_out():
    # Admins shouldn't backfill "the model timed out itself" — that is a
    # system determination, not an operator decision.
    assert "completed" not in ADMIN_TRANSITION_TARGETS
    assert "timed_out" not in ADMIN_TRANSITION_TARGETS
    assert ADMIN_TRANSITION_TARGETS == frozenset({"failed", "aborted", "cancelled"})


# ── can_transition ────────────────────────────────────────────────────────────


def test_can_transition_only_from_running():
    for target in SESSION_TERMINAL_STATUSES:
        assert can_transition("running", target)


def test_can_transition_rejects_terminal_origin():
    for current in SESSION_TERMINAL_STATUSES:
        for target in SESSION_TERMINAL_STATUSES:
            assert not can_transition(current, target), (
                f"transition {current!r} → {target!r} should be rejected"
            )


def test_can_transition_rejects_unknown_target():
    assert not can_transition("running", "in_progress")
    assert not can_transition("running", "stale")  # stale = health, not status


def test_can_transition_rejects_none_origin():
    assert not can_transition(None, "completed")


# ── DB-level validation ───────────────────────────────────────────────────────


async def test_create_session_accepts_all_seven_statuses(db: StateDB):
    for status in VALID_SESSION_STATUSES:
        s = await _make_session(db, status=status)
        retrieved = await db.get_session(s["id"])
        assert retrieved["status"] == status


async def test_update_session_accepts_timed_out(db: StateDB):
    s = await _make_session(db, status="running")
    await db.update_session(s["id"], status="timed_out")
    assert (await db.get_session(s["id"]))["status"] == "timed_out"


async def test_update_session_accepts_cancelled(db: StateDB):
    s = await _make_session(db, status="running")
    await db.update_session(s["id"], status="cancelled")
    assert (await db.get_session(s["id"]))["status"] == "cancelled"


async def test_update_session_rejects_unknown_status(db: StateDB):
    s = await _make_session(db, status="running")
    with pytest.raises(ValueError, match="ADR-0025 vocabulary"):
        await db.update_session(s["id"], status="stale")


# ── Legacy CHECK constraint rebuild ───────────────────────────────────────────


async def test_drop_legacy_check_rebuilds_table(tmp_path: Path):
    """An existing DB with the ADR-0017 4-value CHECK is migrated on open."""
    path = tmp_path / "legacy.db"

    # Hand-build the legacy schema: ADR-0017 4-value CHECK on sessions.status.
    async with aiosqlite.connect(str(path)) as old:
        await old.execute(
            """
            CREATE TABLE progressions (
              id          TEXT    PRIMARY KEY,
              created_at  REAL    NOT NULL,
              collection  TEXT    NOT NULL DEFAULT '[]'
            )
            """
        )
        await old.execute(
            """
            CREATE TABLE sessions (
              id              TEXT    PRIMARY KEY,
              created_at      REAL    NOT NULL,
              node_metadata   JSON,
              name            TEXT,
              user            TEXT,
              progression_id  TEXT    NOT NULL REFERENCES progressions(id),
              first_msg_id    TEXT,
              last_msg_id     TEXT,
              updated_at      REAL,
              status          TEXT CHECK(
                                status IS NULL
                                OR status IN ('running', 'completed', 'failed', 'aborted')
                              )
            )
            """
        )
        # Seed one row to verify INSERT SELECT preserves data.
        prog_id = _uid()
        sess_id = _uid()
        await old.execute(
            "INSERT INTO progressions (id, created_at) VALUES (?, ?)",
            (prog_id, 1.0),
        )
        await old.execute(
            "INSERT INTO sessions (id, created_at, progression_id, updated_at, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (sess_id, 1.0, prog_id, 1.0, "running"),
        )
        await old.commit()

    # Open via StateDB — migration should run, CHECK should disappear.
    state = StateDB(path)
    await state.open()
    try:
        # Row preserved.
        retrieved = await state.get_session(sess_id)
        assert retrieved is not None
        assert retrieved["status"] == "running"

        # The new vocabulary values now accepted — would have raised
        # IntegrityError before the rebuild.
        await state.update_session(sess_id, status="timed_out")
        assert (await state.get_session(sess_id))["status"] == "timed_out"
    finally:
        await state.close()


async def test_drop_legacy_check_is_idempotent(tmp_path: Path):
    """Re-opening a migrated DB is a no-op (no further table rebuild)."""
    path = tmp_path / "twice.db"

    state1 = StateDB(path)
    await state1.open()
    s = await _make_session(state1, status="running")
    await state1.update_session(s["id"], status="cancelled")
    await state1.close()

    # Second open — _drop_legacy_session_status_check should find no
    # marker and return immediately.
    state2 = StateDB(path)
    await state2.open()
    try:
        assert (await state2.get_session(s["id"]))["status"] == "cancelled"
    finally:
        await state2.close()


# ── CLI exit-code map ─────────────────────────────────────────────────────────


def test_cli_exit_code_map_matches_adr0025():
    from lionagi.cli._util import EXIT_CODE_BY_STATUS as _EXIT_CODE_BY_TERMINAL_STATUS

    # ADR-0025 spec table: 0 / 1 / 124 / 130 / 143. completed_empty (the
    # completion-trust gate) shares exit code 1 with failed — both are
    # non-zero so scripts/CI/schedule chaining treat them as a failure.
    assert _EXIT_CODE_BY_TERMINAL_STATUS == {
        "completed": 0,
        "completed_empty": 1,
        "failed": 1,
        "timed_out": 124,
        "aborted": 130,
        "cancelled": 143,
    }
