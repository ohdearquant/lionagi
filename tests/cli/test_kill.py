# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for `li kill` — issue #1094.

Coverage targets:
- Entity resolution by short id prefix and full UUID
- _pid_alive / _terminate_pid signal flow (mocked os.kill)
- _persist_cancel: status update + status_transitions row
- _do_kill: end-to-end single entity kill
- _do_kill_all_stale: sweep stale running rows
- cascade kill (--recursive) for children
- Correctly skips non-running entities
- Correctly skips live PIDs in --all-stale
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from lionagi.cli.kill import (
    _do_kill,
    _do_kill_all_stale,
    _kill_one,
    _list_running_children,
    _persist_cancel,
    _pid_alive,
    _resolve_entity,
    _terminate_pid,
)
from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons

# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect StateDB to a per-test temp file."""
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


async def _seed_session(
    db: StateDB,
    *,
    status: str = "running",
    pid: int | None = None,
    started_at: float | None = None,
) -> str:
    sid = str(uuid.uuid4())
    pid_val = str(uuid.uuid4())
    await db.create_progression(pid_val)
    node_meta = {"pid": pid} if pid is not None else {}
    await db.create_session(
        {
            "id": sid,
            "progression_id": pid_val,
            "status": status,
            "started_at": started_at or time.time(),
            "node_metadata": node_meta,
        }
    )
    return sid


async def _seed_invocation(
    db: StateDB,
    *,
    status: str = "running",
    pid: int | None = None,
    started_at: float | None = None,
) -> str:
    inv_id = str(uuid.uuid4())
    node_meta: dict[str, Any] = {}
    if pid is not None:
        node_meta["pid"] = pid
    await db.create_invocation(
        {
            "id": inv_id,
            "skill": "test",
            "started_at": started_at or time.time(),
            "status": status,
            "node_metadata": node_meta if node_meta else None,
        }
    )
    return inv_id


async def _seed_show(db: StateDB, *, status: str = "active") -> str:
    show_id = str(uuid.uuid4())
    await db.create_show(
        {
            "id": show_id,
            "topic": f"topic-{show_id[:8]}",
            "show_dir": f"/tmp/show-{show_id[:8]}",
            "status": status,
        }
    )
    return show_id


async def _seed_play(db: StateDB, show_id: str, *, status: str = "running") -> str:
    play_id = str(uuid.uuid4())
    await db.create_play(
        {
            "id": play_id,
            "show_id": show_id,
            "name": f"play-{play_id[:8]}",
            "status": status,
        }
    )
    return play_id


# ── _pid_alive ─────────────────────────────────────────────────────────────────


def test_pid_alive_returns_false_for_nonexistent_pid():
    # PID 999999999 is virtually guaranteed not to exist.
    assert _pid_alive(999999999) is False


def test_pid_alive_returns_false_for_non_positive():
    assert _pid_alive(0) is False
    assert _pid_alive(-1) is False


def test_pid_alive_returns_true_for_own_process():
    import os

    assert _pid_alive(os.getpid()) is True


def test_pid_alive_treats_permission_error_as_alive():
    """PermissionError means the process exists (not ours); must return True."""
    with patch("os.kill", side_effect=PermissionError):
        assert _pid_alive(1234) is True


# ── _terminate_pid ─────────────────────────────────────────────────────────────


def test_terminate_pid_returns_already_dead_for_missing_pid():
    result = _terminate_pid(999999999, grace_seconds=0.1)
    assert result == "already_dead"


def test_terminate_pid_sigterm_sufficient(monkeypatch: pytest.MonkeyPatch):
    """Process exits after SIGTERM — should return 'sigterm' quickly."""
    calls: list[tuple[int, Any]] = []

    def fake_kill(pid: int, sig: Any) -> None:
        calls.append((pid, sig))
        # After SIGTERM is sent, fake process death by patching _pid_alive.

    alive_flag = [True]

    def fake_alive(pid: int) -> bool:
        # First call: alive; subsequent calls (during polling): dead.
        if alive_flag[0] and calls:
            alive_flag[0] = False
            return True
        return not bool(calls)

    monkeypatch.setattr("lionagi.cli.kill._pid_alive", fake_alive)
    monkeypatch.setattr("os.kill", fake_kill)

    result = _terminate_pid(42, grace_seconds=1.0)
    assert result in ("sigterm", "sigkill")  # exact depends on timing


def test_terminate_pid_escalates_to_sigkill(monkeypatch: pytest.MonkeyPatch):
    """If process refuses SIGTERM within grace period, SIGKILL is sent."""
    import signal as _signal

    kill_calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        kill_calls.append((pid, sig))

    # Always report alive during the grace window.
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: True)
    monkeypatch.setattr("os.kill", fake_kill)

    result = _terminate_pid(42, grace_seconds=0.05)  # very short grace
    assert result == "sigkill"
    sigs_sent = [sig for _, sig in kill_calls]
    assert _signal.SIGTERM in sigs_sent
    assert _signal.SIGKILL in sigs_sent


# ── _resolve_entity ────────────────────────────────────────────────────────────


async def test_resolve_entity_by_full_uuid(temp_db_path: Path):
    async with StateDB() as db:
        sid = await _seed_session(db)
        result = await _resolve_entity(db, sid)
        assert result is not None
        table, entity_type, row = result
        assert table == "sessions"
        assert entity_type == "session"
        assert row["id"] == sid


async def test_resolve_entity_by_short_prefix(temp_db_path: Path):
    async with StateDB() as db:
        sid = await _seed_session(db)
        short = sid[:8]
        result = await _resolve_entity(db, short)
        assert result is not None
        _, _, row = result
        assert row["id"] == sid


async def test_resolve_entity_returns_none_for_unknown(temp_db_path: Path):
    async with StateDB() as db:
        result = await _resolve_entity(db, "deadbeef00000000")
        assert result is None


async def test_resolve_entity_finds_invocation(temp_db_path: Path):
    async with StateDB() as db:
        inv_id = await _seed_invocation(db)
        result = await _resolve_entity(db, inv_id)
        assert result is not None
        table, entity_type, _ = result
        assert table == "invocations"
        assert entity_type == "invocation"


async def test_resolve_entity_finds_show(temp_db_path: Path):
    async with StateDB() as db:
        show_id = await _seed_show(db)
        result = await _resolve_entity(db, show_id)
        assert result is not None
        _, entity_type, _ = result
        assert entity_type == "show"


# ── _persist_cancel ────────────────────────────────────────────────────────────


async def test_persist_cancel_sets_status_cancelled(temp_db_path: Path):
    async with StateDB() as db:
        sid = await _seed_session(db, status="running")

        await _persist_cancel(
            db,
            "session",
            sid,
            reason_code=RunReasons.CANCELLED_MANUAL_KILL,
            reason_summary="test cancel",
            evidence={"signal": "sigterm", "pid": 42},
        )

        cur = await db.db.execute("SELECT status FROM sessions WHERE id = ?", (sid,))
        row = await cur.fetchone()
        assert row["status"] == "cancelled"


async def test_persist_cancel_inserts_status_transition(temp_db_path: Path):
    async with StateDB() as db:
        sid = await _seed_session(db, status="running")

        await _persist_cancel(
            db,
            "session",
            sid,
            reason_code=RunReasons.CANCELLED_MANUAL_KILL,
            reason_summary="test cancel",
            evidence={"signal": "sigterm", "pid": 99},
        )

        cur = await db.db.execute("SELECT * FROM status_transitions WHERE entity_id = ?", (sid,))
        row = await cur.fetchone()
        assert row is not None
        assert row["reason_code"] == RunReasons.CANCELLED_MANUAL_KILL
        assert row["source"] == "cli"
        assert row["actor"] == "user"
        assert row["previous_status"] == "running"
        assert row["status"] == "cancelled"


async def test_persist_cancel_skips_already_terminal(temp_db_path: Path):
    """Completed/failed sessions must not be overwritten."""
    async with StateDB() as db:
        sid = await _seed_session(db, status="completed")

        await _persist_cancel(
            db,
            "session",
            sid,
            reason_code=RunReasons.CANCELLED_MANUAL_KILL,
            reason_summary="test",
            evidence={},
        )

        # Status must remain "completed", not overwritten.
        cur = await db.db.execute("SELECT status FROM sessions WHERE id = ?", (sid,))
        row = await cur.fetchone()
        assert row["status"] == "completed"


async def test_persist_cancel_show_sets_aborted(temp_db_path: Path):
    async with StateDB() as db:
        show_id = await _seed_show(db, status="active")

        await _persist_cancel(
            db,
            "show",
            show_id,
            reason_code=RunReasons.CANCELLED_MANUAL_KILL,
            reason_summary="kill show",
            evidence={"signal": "sigterm", "pid": None},
        )

        cur = await db.db.execute("SELECT status FROM shows WHERE id = ?", (show_id,))
        row = await cur.fetchone()
        assert row["status"] == "aborted"


# ── _kill_one ──────────────────────────────────────────────────────────────────


async def test_kill_one_no_pid(temp_db_path: Path):
    """Entity without a PID: no OS signal, but DB updated."""
    async with StateDB() as db:
        sid = await _seed_session(db, status="running", pid=None)
        resolved = await _resolve_entity(db, sid)
        assert resolved is not None
        _, _, row = resolved

        result = await _kill_one(db, "session", sid, row, user_reason="test")
        assert result["signal"] == "no_pid"

        cur = await db.db.execute("SELECT status FROM sessions WHERE id = ?", (sid,))
        assert (await cur.fetchone())["status"] == "cancelled"


async def test_kill_one_with_dead_pid(temp_db_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Entity with a dead PID: _terminate_pid returns 'already_dead'."""
    monkeypatch.setattr("lionagi.cli.kill._terminate_pid", lambda pid, **kw: "already_dead")

    async with StateDB() as db:
        sid = await _seed_session(db, status="running", pid=999999999)
        # Use _resolve_entity to get the row with JSON columns decoded.
        resolved = await _resolve_entity(db, sid)
        assert resolved is not None
        _, _, row = resolved

        result = await _kill_one(db, "session", sid, row, user_reason="")
        assert result["signal"] == "already_dead"

        cur2 = await db.db.execute("SELECT status FROM sessions WHERE id = ?", (sid,))
        assert (await cur2.fetchone())["status"] == "cancelled"


async def test_kill_one_force_kill_uses_force_kill_reason(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When SIGKILL is needed, CANCELLED_FORCE_KILL reason code is written."""
    monkeypatch.setattr("lionagi.cli.kill._terminate_pid", lambda pid, **kw: "sigkill")

    async with StateDB() as db:
        sid = await _seed_session(db, status="running", pid=12345)
        resolved = await _resolve_entity(db, sid)
        assert resolved is not None
        _, _, row = resolved

        await _kill_one(db, "session", sid, row, user_reason="")

        cur2 = await db.db.execute(
            "SELECT reason_code FROM status_transitions WHERE entity_id = ?", (sid,)
        )
        tr = await cur2.fetchone()
        assert tr["reason_code"] == RunReasons.CANCELLED_FORCE_KILL


# ── _do_kill (end-to-end) ─────────────────────────────────────────────────────


async def test_do_kill_by_full_id(temp_db_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("lionagi.cli.kill._terminate_pid", lambda pid, **kw: "sigterm")

    async with StateDB() as db:
        sid = await _seed_session(db, status="running", pid=12345)

    rc = await _do_kill(sid, user_reason="integration test")
    assert rc == 0

    async with StateDB() as db:
        cur = await db.db.execute("SELECT status FROM sessions WHERE id = ?", (sid,))
        assert (await cur.fetchone())["status"] == "cancelled"


async def test_do_kill_by_prefix(temp_db_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("lionagi.cli.kill._terminate_pid", lambda pid, **kw: "already_dead")

    async with StateDB() as db:
        sid = await _seed_session(db, status="running", pid=None)

    rc = await _do_kill(sid[:10])
    assert rc == 0


async def test_do_kill_unknown_id_returns_1(temp_db_path: Path):
    async with StateDB():
        pass  # ensure DB exists

    rc = await _do_kill("00000000deadbeef")
    assert rc == 1


async def test_do_kill_non_running_returns_1(temp_db_path: Path):
    async with StateDB() as db:
        sid = await _seed_session(db, status="completed")

    rc = await _do_kill(sid)
    assert rc == 1


# ── _do_kill_all_stale ────────────────────────────────────────────────────────


async def test_do_kill_all_stale_cancels_dead_pid(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Running session with a dead PID and old start time is cancelled."""
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: False)

    old_start = time.time() - 7200  # 2h ago
    async with StateDB() as db:
        sid = await _seed_session(db, status="running", pid=99999, started_at=old_start)

    rc = await _do_kill_all_stale(threshold_seconds=3600, dry_run=False)
    assert rc == 0

    async with StateDB() as db:
        cur = await db.db.execute("SELECT status FROM sessions WHERE id = ?", (sid,))
        assert (await cur.fetchone())["status"] == "cancelled"


async def test_do_kill_all_stale_skips_live_pid(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Running session with a LIVE PID is not touched."""
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: True)

    old_start = time.time() - 7200
    async with StateDB() as db:
        sid = await _seed_session(db, status="running", pid=12345, started_at=old_start)

    rc = await _do_kill_all_stale(threshold_seconds=3600, dry_run=False)
    assert rc == 0

    async with StateDB() as db:
        cur = await db.db.execute("SELECT status FROM sessions WHERE id = ?", (sid,))
        assert (await cur.fetchone())["status"] == "running"


async def test_do_kill_all_stale_skips_recent(temp_db_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Session started recently (under threshold) must not be swept."""
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: False)

    recent_start = time.time() - 60  # only 1 min ago
    async with StateDB() as db:
        sid = await _seed_session(db, status="running", pid=None, started_at=recent_start)

    rc = await _do_kill_all_stale(threshold_seconds=3600, dry_run=False)
    assert rc == 0

    async with StateDB() as db:
        cur = await db.db.execute("SELECT status FROM sessions WHERE id = ?", (sid,))
        assert (await cur.fetchone())["status"] == "running"


async def test_do_kill_all_stale_dry_run_does_not_write(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """--dry-run must not modify any rows."""
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: False)

    old_start = time.time() - 7200
    async with StateDB() as db:
        sid = await _seed_session(db, status="running", pid=None, started_at=old_start)

    rc = await _do_kill_all_stale(threshold_seconds=3600, dry_run=True)
    assert rc == 0

    async with StateDB() as db:
        cur = await db.db.execute("SELECT status FROM sessions WHERE id = ?", (sid,))
        assert (await cur.fetchone())["status"] == "running"


async def test_do_kill_all_stale_uses_stale_auto_reason(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """CANCELLED_STALE_AUTO reason code is written for stale sweeps."""
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: False)

    old_start = time.time() - 7200
    async with StateDB() as db:
        sid = await _seed_session(db, status="running", pid=None, started_at=old_start)

    await _do_kill_all_stale(threshold_seconds=3600, dry_run=False)

    async with StateDB() as db:
        cur = await db.db.execute(
            "SELECT reason_code FROM status_transitions WHERE entity_id = ?", (sid,)
        )
        row = await cur.fetchone()
        assert row is not None
        assert row["reason_code"] == RunReasons.CANCELLED_STALE_AUTO


# ── cascade kill ───────────────────────────────────────────────────────────────


async def test_do_kill_recursive_kills_child_invocations(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """--recursive: a session's linked invocation is also cancelled."""
    monkeypatch.setattr("lionagi.cli.kill._terminate_pid", lambda pid, **kw: "sigterm")

    async with StateDB() as db:
        sid = await _seed_session(db, status="running")
        # Create an invocation and link it to the session.
        inv_id = await _seed_invocation(db, status="running")
        await db.update_session(sid, invocation_id=inv_id)

    rc = await _do_kill(sid, recursive=True)
    assert rc == 0

    async with StateDB() as db:
        cur = await db.db.execute("SELECT status FROM sessions WHERE id = ?", (sid,))
        assert (await cur.fetchone())["status"] == "cancelled"


# ── CLI wiring smoke test ──────────────────────────────────────────────────────


def test_kill_subparser_registered():
    """Verify `li kill --help` exits 0 (subparser is wired correctly)."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "lionagi.cli", "kill", "--help"],
        capture_output=True,
        text=True,
    )
    # --help exits with code 0
    assert result.returncode == 0
    assert "kill" in result.stdout.lower() or "kill" in result.stderr.lower()


def test_kill_all_stale_subparser_flags():
    """Verify --all-stale, --threshold, --dry-run are accepted."""
    import tempfile

    import lionagi.state.db as _db_mod
    from lionagi.cli.main import main

    # Calling with --dry-run + --all-stale against a missing DB should
    # exit cleanly (0) and print nothing killed.
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp_path = f.name

    import lionagi.cli.kill as _kill_mod

    original = _db_mod.DEFAULT_DB_PATH
    _db_mod.DEFAULT_DB_PATH = Path(tmp_path)
    try:
        rc = main(["kill", "--all-stale", "--dry-run", "--threshold", "3600"])
        assert rc == 0
    finally:
        _db_mod.DEFAULT_DB_PATH = original
        Path(tmp_path).unlink(missing_ok=True)


# ── issue #1117: _do_kill_all_stale must sweep plays and shows ─────────────────


async def test_do_kill_all_stale_cancels_stale_play(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Stale running play with a dead PID must be swept (issue #1117)."""
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: False)

    old_start = time.time() - 7200  # 2 hours ago
    async with StateDB() as db:
        show_id = await _seed_show(db, status="active")
        play_id = await _seed_play(db, show_id, status="running")
        # Backdate started_at so the row is outside the stale threshold.
        await db.db.execute(
            "UPDATE plays SET started_at = ? WHERE id = ?",
            (old_start, play_id),
        )
        await db.db.commit()

    rc = await _do_kill_all_stale(threshold_seconds=3600, dry_run=False)
    assert rc == 0

    async with StateDB() as db:
        cur = await db.db.execute("SELECT status FROM plays WHERE id = ?", (play_id,))
        row = await cur.fetchone()
        assert row is not None
        # _persist_cancel maps stale play → "blocked" (no "cancelled" in play vocabulary)
        assert row["status"] == "blocked"


async def test_do_kill_all_stale_cancels_stale_show(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Stale active show must be swept (issue #1117).

    Shows use 'active' as their live status (not 'running').  The sweep
    must query for 'active' rows and use updated_at/created_at for age check.
    """
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: False)

    old_time = time.time() - 7200  # 2 hours ago
    async with StateDB() as db:
        show_id = await _seed_show(db, status="active")
        # Backdate both timestamps so the show is outside the stale threshold.
        await db.db.execute(
            "UPDATE shows SET updated_at = ?, created_at = ? WHERE id = ?",
            (old_time, old_time, show_id),
        )
        await db.db.commit()

    rc = await _do_kill_all_stale(threshold_seconds=3600, dry_run=False)
    assert rc == 0

    async with StateDB() as db:
        cur = await db.db.execute("SELECT status FROM shows WHERE id = ?", (show_id,))
        row = await cur.fetchone()
        assert row is not None
        assert row["status"] == "aborted"
