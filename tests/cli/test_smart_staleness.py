# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for smart play/show staleness in `li kill --all-stale`: child-derived staleness detection."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from lionagi.cli.kill import (
    _do_kill_all_stale,
    _play_child_stale,
    _show_children_all_terminal,
)
from lionagi.state.db import StateDB


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


# ── Seed helpers ──────────────────────────────────────────────────────────────


async def _seed_session(db: StateDB, *, status: str = "running") -> str:
    sid = str(uuid.uuid4())
    pid_val = str(uuid.uuid4())
    await db.create_progression(pid_val)
    await db.create_session(
        {
            "id": sid,
            "progression_id": pid_val,
            "status": status,
            "started_at": time.time() - 7200,
        }
    )
    return sid


async def _seed_show(db: StateDB, *, status: str = "active") -> str:
    show_id = str(uuid.uuid4())
    await db.create_show(
        {"id": show_id, "topic": f"t-{show_id[:8]}", "show_dir": "/tmp/s", "status": status}
    )
    return show_id


async def _seed_play(db: StateDB, show_id: str, *, status: str = "running") -> str:
    play_id = str(uuid.uuid4())
    await db.create_play(
        {"id": play_id, "show_id": show_id, "name": f"p-{play_id[:8]}", "status": status}
    )
    return play_id


async def _link_play_session(db: StateDB, play_id: str, session_id: str) -> None:
    await db.db.execute("UPDATE plays SET session_id = ? WHERE id = ?", (session_id, play_id))
    await db.db.commit()


# ── _play_child_stale ─────────────────────────────────────────────────────────


async def test_play_child_stale_no_session_id(temp_db_path: Path):
    """A play with no session_id is not child-stale."""
    async with StateDB() as db:
        show_id = await _seed_show(db)
        play_id = await _seed_play(db, show_id, status="running")
        cur = await db.db.execute("SELECT * FROM plays WHERE id = ?", (play_id,))
        row = db._row_to_dict(await cur.fetchone())
        assert not await _play_child_stale(db, row)


async def test_play_child_stale_with_running_session(temp_db_path: Path):
    """A play whose linked session is still running is not child-stale."""
    async with StateDB() as db:
        show_id = await _seed_show(db)
        play_id = await _seed_play(db, show_id, status="running")
        session_id = await _seed_session(db, status="running")
        await _link_play_session(db, play_id, session_id)
        cur = await db.db.execute("SELECT * FROM plays WHERE id = ?", (play_id,))
        row = db._row_to_dict(await cur.fetchone())
        assert not await _play_child_stale(db, row)


async def test_play_child_stale_with_completed_session(temp_db_path: Path):
    """A play whose linked session completed IS child-stale."""
    async with StateDB() as db:
        show_id = await _seed_show(db)
        play_id = await _seed_play(db, show_id, status="running")
        session_id = await _seed_session(db, status="completed")
        await _link_play_session(db, play_id, session_id)
        cur = await db.db.execute("SELECT * FROM plays WHERE id = ?", (play_id,))
        row = db._row_to_dict(await cur.fetchone())
        assert await _play_child_stale(db, row)


async def test_play_child_stale_with_failed_session(temp_db_path: Path):
    """A play whose linked session failed IS child-stale."""
    async with StateDB() as db:
        show_id = await _seed_show(db)
        play_id = await _seed_play(db, show_id, status="running")
        session_id = await _seed_session(db, status="failed")
        await _link_play_session(db, play_id, session_id)
        cur = await db.db.execute("SELECT * FROM plays WHERE id = ?", (play_id,))
        row = db._row_to_dict(await cur.fetchone())
        assert await _play_child_stale(db, row)


# ── _show_children_all_terminal ───────────────────────────────────────────────


async def test_show_children_all_terminal_no_plays(temp_db_path: Path):
    """A show with no plays is NOT considered child-stale."""
    async with StateDB() as db:
        show_id = await _seed_show(db)
        assert not await _show_children_all_terminal(db, show_id)


async def test_show_children_all_terminal_with_running_play(temp_db_path: Path):
    """A show with an active child play is NOT child-stale."""
    async with StateDB() as db:
        show_id = await _seed_show(db)
        await _seed_play(db, show_id, status="running")
        assert not await _show_children_all_terminal(db, show_id)


async def test_show_children_all_terminal_all_merged(temp_db_path: Path):
    """A show whose plays are all merged IS child-stale."""
    async with StateDB() as db:
        show_id = await _seed_show(db)
        await _seed_play(db, show_id, status="merged")
        await _seed_play(db, show_id, status="merged")
        assert await _show_children_all_terminal(db, show_id)


async def test_show_children_mixed_active_and_terminal(temp_db_path: Path):
    """A show with one active play among terminal ones is NOT child-stale."""
    async with StateDB() as db:
        show_id = await _seed_show(db)
        await _seed_play(db, show_id, status="merged")
        await _seed_play(db, show_id, status="running")
        assert not await _show_children_all_terminal(db, show_id)


# ── Integration: _do_kill_all_stale with child-derived staleness ─────────────


async def test_do_kill_all_stale_sweeps_play_with_dead_session(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A play whose linked session is terminal gets cancelled by --all-stale."""
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: False)

    old_time = time.time() - 7200
    async with StateDB() as db:
        show_id = await _seed_show(db)
        play_id = await _seed_play(db, show_id, status="running")
        session_id = await _seed_session(db, status="cancelled")
        await _link_play_session(db, play_id, session_id)
        # Backdate the play so it exceeds threshold
        await db.db.execute("UPDATE plays SET started_at = ? WHERE id = ?", (old_time, play_id))
        await db.db.commit()

    rc = await _do_kill_all_stale(threshold_seconds=3600, dry_run=False)
    assert rc == 0

    async with StateDB() as db:
        cur = await db.db.execute("SELECT status FROM plays WHERE id = ?", (play_id,))
        row = await cur.fetchone()
        assert row["status"] == "blocked"  # _persist_cancel maps play → "blocked"


async def test_do_kill_all_stale_does_not_sweep_play_with_live_session(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A play whose linked session is still running (live PID) is NOT swept by the child-derived pass."""
    import os

    own_pid = os.getpid()
    # _pid_alive returns True for our own pid, False for others.
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: pid == own_pid)

    old_time = time.time() - 7200
    async with StateDB() as db:
        show_id = await _seed_show(db)
        play_id = await _seed_play(db, show_id, status="running")
        # Session gets our own PID so the PID-sweep skips it (alive).
        sid = str(uuid.uuid4())
        pid_val = str(uuid.uuid4())
        await db.create_progression(pid_val)
        await db.create_session(
            {
                "id": sid,
                "progression_id": pid_val,
                "status": "running",
                "started_at": old_time,
                "node_metadata": {"pid": own_pid},
            }
        )
        await _link_play_session(db, play_id, sid)
        await db.db.execute("UPDATE plays SET started_at = ? WHERE id = ?", (old_time, play_id))
        await db.db.commit()

    rc = await _do_kill_all_stale(threshold_seconds=3600, dry_run=False)
    assert rc == 0

    async with StateDB() as db:
        cur = await db.db.execute("SELECT status FROM plays WHERE id = ?", (play_id,))
        assert (await cur.fetchone())["status"] == "running"


async def test_do_kill_all_stale_does_not_sweep_play_without_session(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A play with no session_id is NOT swept even if old."""
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: False)

    old_time = time.time() - 7200
    async with StateDB() as db:
        show_id = await _seed_show(db)
        play_id = await _seed_play(db, show_id, status="running")
        await db.db.execute("UPDATE plays SET started_at = ? WHERE id = ?", (old_time, play_id))
        await db.db.commit()

    rc = await _do_kill_all_stale(threshold_seconds=3600, dry_run=False)
    assert rc == 0

    async with StateDB() as db:
        cur = await db.db.execute("SELECT status FROM plays WHERE id = ?", (play_id,))
        assert (await cur.fetchone())["status"] == "running"


async def test_do_kill_all_stale_sweeps_show_with_all_terminal_plays(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A show whose plays are all terminal gets aborted by --all-stale."""
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: False)

    old_time = time.time() - 7200
    async with StateDB() as db:
        show_id = await _seed_show(db, status="active")
        await _seed_play(db, show_id, status="merged")
        await _seed_play(db, show_id, status="blocked")
        # Backdate the show
        await db.db.execute(
            "UPDATE shows SET updated_at = ?, created_at = ? WHERE id = ?",
            (old_time, old_time, show_id),
        )
        await db.db.commit()

    rc = await _do_kill_all_stale(threshold_seconds=3600, dry_run=False)
    assert rc == 0

    async with StateDB() as db:
        cur = await db.db.execute("SELECT status FROM shows WHERE id = ?", (show_id,))
        assert (await cur.fetchone())["status"] == "aborted"


async def test_do_kill_all_stale_does_not_sweep_show_with_active_play(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A show with at least one active play is NOT swept."""
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: False)

    old_time = time.time() - 7200
    async with StateDB() as db:
        show_id = await _seed_show(db, status="active")
        await _seed_play(db, show_id, status="merged")
        await _seed_play(db, show_id, status="running")  # still active
        await db.db.execute(
            "UPDATE shows SET updated_at = ?, created_at = ? WHERE id = ?",
            (old_time, old_time, show_id),
        )
        await db.db.commit()

    rc = await _do_kill_all_stale(threshold_seconds=3600, dry_run=False)
    assert rc == 0

    async with StateDB() as db:
        cur = await db.db.execute("SELECT status FROM shows WHERE id = ?", (show_id,))
        assert (await cur.fetchone())["status"] == "active"


async def test_do_kill_all_stale_does_not_sweep_show_with_no_plays(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A show with no plays is NOT swept (not yet started)."""
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: False)

    old_time = time.time() - 7200
    async with StateDB() as db:
        show_id = await _seed_show(db, status="active")
        await db.db.execute(
            "UPDATE shows SET updated_at = ?, created_at = ? WHERE id = ?",
            (old_time, old_time, show_id),
        )
        await db.db.commit()

    rc = await _do_kill_all_stale(threshold_seconds=3600, dry_run=False)
    assert rc == 0

    async with StateDB() as db:
        cur = await db.db.execute("SELECT status FROM shows WHERE id = ?", (show_id,))
        assert (await cur.fetchone())["status"] == "active"


async def test_do_kill_all_stale_dry_run_child_derived(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """--dry-run must not write any rows for child-derived stale plays."""
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: False)

    old_time = time.time() - 7200
    async with StateDB() as db:
        show_id = await _seed_show(db)
        play_id = await _seed_play(db, show_id, status="running")
        session_id = await _seed_session(db, status="cancelled")
        await _link_play_session(db, play_id, session_id)
        await db.db.execute("UPDATE plays SET started_at = ? WHERE id = ?", (old_time, play_id))
        await db.db.commit()

    rc = await _do_kill_all_stale(threshold_seconds=3600, dry_run=True)
    assert rc == 0

    async with StateDB() as db:
        cur = await db.db.execute("SELECT status FROM plays WHERE id = ?", (play_id,))
        assert (await cur.fetchone())["status"] == "running"  # unchanged
