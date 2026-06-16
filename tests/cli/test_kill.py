# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for `li kill`: entity resolution, pid signal flow, cascade kill, stale sweep."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from lionagi.cli.kill import (
    _check_pid_identity,
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


# ── _terminate_pid identity checks ───────────────────────────────────────────


def test_terminate_pid_identity_mismatch_no_signal_sent(
    monkeypatch: pytest.MonkeyPatch,
):
    """If cmdline doesn't match expected_cmd, no signal is sent."""
    import signal as _signal

    kill_calls: list[tuple[int, int]] = []
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: True)
    monkeypatch.setattr("os.kill", lambda pid, sig: kill_calls.append((pid, sig)))

    # Mock psutil with a process whose cmdline does NOT contain "lionagi".
    fake_psutil = MagicMock()
    fake_proc = MagicMock()
    fake_proc.cmdline.return_value = ["/usr/bin/python3", "unrelated_script.py"]
    fake_psutil.Process.return_value = fake_proc
    fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    fake_psutil.AccessDenied = type("AccessDenied", (Exception,), {})
    monkeypatch.setattr("lionagi.cli.kill.psutil", fake_psutil)

    result = _terminate_pid(42, grace_seconds=0.1, expected_cmd="lionagi")
    assert result == "identity_mismatch"
    assert kill_calls == [], "no signal must be sent on cmdline mismatch"


def test_terminate_pid_identity_match_sends_signal(
    monkeypatch: pytest.MonkeyPatch,
):
    """If cmdline contains expected_cmd, kill proceeds normally."""
    kill_calls: list[tuple[int, int]] = []
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: True)
    monkeypatch.setattr("os.kill", lambda pid, sig: kill_calls.append((pid, sig)))

    fake_psutil = MagicMock()
    fake_proc = MagicMock()
    fake_proc.cmdline.return_value = ["/usr/bin/python3", "-m", "lionagi.cli.main"]
    fake_psutil.Process.return_value = fake_proc
    fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    fake_psutil.AccessDenied = type("AccessDenied", (Exception,), {})
    monkeypatch.setattr("lionagi.cli.kill.psutil", fake_psutil)

    result = _terminate_pid(42, grace_seconds=0.01, expected_cmd="lionagi")
    # SIGTERM must have been sent
    assert any(sig == __import__("signal").SIGTERM for _, sig in kill_calls)
    assert result in ("sigterm", "sigkill")


def _mock_psutil(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cmdline: list[str],
    environ: dict[str, str] | None = None,
    create_time: float = 100.0,
) -> list[tuple[int, int]]:
    """Install a fake psutil + capture os.kill calls. Returns the calls list."""
    kill_calls: list[tuple[int, int]] = []
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: True)
    monkeypatch.setattr("os.kill", lambda pid, sig: kill_calls.append((pid, sig)))

    fake_psutil = MagicMock()
    fake_proc = MagicMock()
    fake_proc.cmdline.return_value = cmdline
    fake_proc.environ.return_value = environ or {}
    fake_proc.create_time.return_value = create_time
    fake_psutil.Process.return_value = fake_proc
    fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    fake_psutil.AccessDenied = type("AccessDenied", (Exception,), {})
    monkeypatch.setattr("lionagi.cli.kill.psutil", fake_psutil)
    return kill_calls


def test_identity_rejects_path_substring(monkeypatch: pytest.MonkeyPatch):
    """An unrelated process that only *mentions* lionagi in a path arg is rejected.

    The reported false positive: ``vim /Users/lion/projects/lionagi/README.md``.
    A substring match would signal this recycled PID; an exact-token match must not.
    """
    kill_calls = _mock_psutil(
        monkeypatch,
        cmdline=["/usr/bin/vim", "/Users/lion/projects/lionagi/README.md"],
    )
    result = _terminate_pid(42, grace_seconds=0.1, expected_cmd="lionagi")
    assert result == "identity_mismatch"
    assert kill_calls == [], "must not signal a process that only has lionagi in a path"


def test_identity_accepts_dash_m_module(monkeypatch: pytest.MonkeyPatch):
    """``python -m lionagi.cli.main`` is a genuine invocation and is accepted."""
    _mock_psutil(monkeypatch, cmdline=["/usr/bin/python3", "-m", "lionagi.cli.main"])
    assert _check_pid_identity(42, "lionagi") is True


def test_identity_accepts_li_entrypoint(monkeypatch: pytest.MonkeyPatch):
    """The ``li`` console-script entrypoint is accepted by executable basename."""
    _mock_psutil(monkeypatch, cmdline=["/opt/venv/bin/li", "kill", "abc123"])
    assert _check_pid_identity(42, "lionagi") is True


def test_identity_session_marker_match(monkeypatch: pytest.MonkeyPatch):
    """A matching LIONAGI_SESSION_ID env marker is a definitive match."""
    _mock_psutil(
        monkeypatch,
        cmdline=["/usr/bin/python3", "-m", "lionagi.cli"],
        environ={"LIONAGI_SESSION_ID": "run-123"},
    )
    assert _check_pid_identity(42, "lionagi", expected_session_id="run-123") is True


def test_identity_session_marker_mismatch_rejected(monkeypatch: pytest.MonkeyPatch):
    """A *different* session marker means another lionagi run holds this PID — reject.

    Even though the cmdline looks like lionagi, the recycled PID belongs to a
    different run, so the kill must be skipped (CWE-362).
    """
    _mock_psutil(
        monkeypatch,
        cmdline=["/usr/bin/python3", "-m", "lionagi.cli"],
        environ={"LIONAGI_SESSION_ID": "other-run"},
    )
    assert _check_pid_identity(42, "lionagi", expected_session_id="run-123") is False


def test_identity_absent_marker_requires_create_time_match(monkeypatch: pytest.MonkeyPatch):
    """Session expected + no env marker: needs create_time AND lionagi cmdline.

    A lionagi-looking cmdline cannot distinguish THIS run from a different
    concurrent run that recycled the PID, and a create_time match alone could be
    a recycled PID that started inside the tolerance. Without the env marker,
    both must hold; otherwise skip the kill.
    """
    _mock_psutil(
        monkeypatch,
        cmdline=["/usr/bin/python3", "-m", "lionagi.cli"],
        environ={},
        create_time=500.0,
    )
    # No create_time recorded → cannot prove this run → skip.
    assert _check_pid_identity(42, "lionagi", expected_session_id="run-123") is False
    # create_time matches AND cmdline is lionagi → positively identified.
    assert (
        _check_pid_identity(
            42, "lionagi", expected_session_id="run-123", expected_create_time=500.0
        )
        is True
    )
    # create_time differs → recycled PID → skip.
    assert (
        _check_pid_identity(42, "lionagi", expected_session_id="run-123", expected_create_time=1.0)
        is False
    )


def test_identity_absent_marker_rejects_nonlionagi_cmdline(monkeypatch: pytest.MonkeyPatch):
    """No marker + matching create_time but a non-lionagi cmdline → reject.

    Guards the recycled-PID case where an unrelated process started within the
    create_time tolerance: create_time alone must not authorize the kill.
    """
    _mock_psutil(
        monkeypatch,
        cmdline=["/usr/bin/vim", "/Users/lion/projects/lionagi/README.md"],
        environ={},
        create_time=500.0,
    )
    assert (
        _check_pid_identity(
            42, "lionagi", expected_session_id="run-123", expected_create_time=500.0
        )
        is False
    )


async def test_do_kill_identity_mismatch_reports_failure(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """identity_mismatch must NOT report success: no 'killed' line, exit code 1.

    The session stays running and `li kill` returns non-zero so callers/scripts
    see the kill was blocked rather than silently 'successful'.
    """
    async with StateDB() as db:
        sid = str(uuid.uuid4())
        prog = str(uuid.uuid4())
        await db.create_progression(prog)
        await db.create_session(
            {
                "id": sid,
                "progression_id": prog,
                "status": "running",
                "started_at": time.time(),
                "node_metadata": {"pid": 4242, "pid_create_time": 100.0},
            }
        )

    # Live pid but a different create_time → recycled → identity_mismatch.
    _mock_psutil(
        monkeypatch,
        cmdline=["/usr/bin/python3", "-m", "lionagi.cli"],
        create_time=999.0,
    )

    rc = await _do_kill(sid)
    assert rc == 1, "blocked kill must return non-zero"

    async with StateDB() as db:
        cur = await db.db.execute("SELECT status FROM sessions WHERE id = ?", (sid,))
        assert (await cur.fetchone())["status"] == "running", "must not cancel an unverified PID"


def test_identity_create_time_mismatch_rejected(monkeypatch: pytest.MonkeyPatch):
    """create_time is a tight fingerprint: only a sub-tolerance match is accepted.

    Same host/kernel → create_time is reproducible to sub-tick precision, so the
    tolerance is ~10ms. A 0.5s difference is a *different* process and must be
    rejected; only a near-exact match (within tick rounding) is accepted.
    """
    _mock_psutil(
        monkeypatch,
        cmdline=["/usr/bin/python3", "-m", "lionagi.cli"],
        create_time=100.0,
    )
    assert _check_pid_identity(42, "lionagi", expected_create_time=999.0) is False
    # 0.5s apart → different process → reject (was accepted under the old 2s gate).
    assert _check_pid_identity(42, "lionagi", expected_create_time=100.5) is False
    # within tick-rounding tolerance → accepted.
    assert _check_pid_identity(42, "lionagi", expected_create_time=100.05) is True


# ── current_pid_markers (launch-time recording) ───────────────────────────────


def test_current_pid_markers_records_own_pid():
    """Markers describe the calling process; create_time present when psutil is."""
    import os

    from lionagi.cli.kill import current_pid_markers

    markers = current_pid_markers()
    assert markers["pid"] == os.getpid()
    # dev env has psutil; create_time must be a real float matching this process.
    import psutil

    assert markers["pid_create_time"] == pytest.approx(psutil.Process(os.getpid()).create_time())


async def test_kill_one_skips_recycled_pid_via_create_time(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A recorded create_time that no longer matches → skip, no false cancel.

    Seeds a session whose node_metadata carries a pid plus a stale
    pid_create_time, with a live pid whose psutil create_time differs. The kill
    must report identity_mismatch and leave the row 'running' (CWE-362).
    """
    async with StateDB() as db:
        sid = str(uuid.uuid4())
        prog = str(uuid.uuid4())
        await db.create_progression(prog)
        await db.create_session(
            {
                "id": sid,
                "progression_id": prog,
                "status": "running",
                "started_at": time.time(),
                "node_metadata": {"pid": 4242, "pid_create_time": 100.0},
            }
        )

        # Live pid, but psutil reports a *different* create_time (recycled).
        _mock_psutil(
            monkeypatch,
            cmdline=["/usr/bin/python3", "-m", "lionagi.cli"],
            create_time=999.0,
        )

        row = db._row_to_dict(
            await (await db.db.execute("SELECT * FROM sessions WHERE id = ?", (sid,))).fetchone()
        )
        result = await _kill_one(db, "session", sid, row, user_reason="")
        assert result["signal"] == "identity_mismatch"

        cur = await db.db.execute("SELECT status FROM sessions WHERE id = ?", (sid,))
        assert (await cur.fetchone())["status"] == "running", "must not cancel a recycled PID"


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
        assert row["source"] == "admin"  # CLI kill is an admin action (ADR-0028)
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


# ── plays and shows excluded from sweep ──────────────────────────────────────


async def test_do_kill_all_stale_does_NOT_touch_show_at_all(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Shows are skipped entirely in the all-stale sweep.

    Shows have no direct PID; treating pid=None as 'stale' would abort
    long-running shows whose child plays/sessions are still alive.
    Both the show and any co-seeded child play must survive unchanged.
    """
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: False)

    old_time = time.time() - 7200  # 2 hours ago
    async with StateDB() as db:
        show_id = await _seed_show(db, status="active")
        # Backdate so the show looks stale by age threshold.
        await db.db.execute(
            "UPDATE shows SET updated_at = ?, created_at = ? WHERE id = ?",
            (old_time, old_time, show_id),
        )
        # Seed a child play so we also verify plays are not swept.
        play_id = await _seed_play(db, show_id, status="running")
        await db.db.execute(
            "UPDATE plays SET started_at = ? WHERE id = ?",
            (old_time, play_id),
        )
        await db.db.commit()

    rc = await _do_kill_all_stale(threshold_seconds=3600, dry_run=False)
    assert rc == 0

    async with StateDB() as db:
        cur = await db.db.execute("SELECT status FROM shows WHERE id = ?", (show_id,))
        row = await cur.fetchone()
        assert row is not None
        # Show must remain active — the sweep must not have touched it.
        assert row["status"] == "active"

        cur2 = await db.db.execute("SELECT status FROM plays WHERE id = ?", (play_id,))
        row2 = await cur2.fetchone()
        assert row2 is not None
        # Play must also remain running — the sweep must not have touched it.
        assert row2["status"] == "running"


async def test_do_kill_all_stale_does_NOT_touch_play_at_all(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Plays are skipped entirely in the all-stale sweep.

    Plays are orchestrators with no direct PID; their child sessions carry
    the actual OS process. Sweeping by PID-absence would silently abort
    legitimate long-running plays. The play's status must remain 'running'
    after the sweep regardless of age or PID presence.
    """
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: False)

    old_start = time.time() - 7200  # 2 hours ago
    async with StateDB() as db:
        show_id = await _seed_show(db, status="active")
        play_id = await _seed_play(db, show_id, status="running")
        # Backdate so the play is well outside the stale threshold.
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
        # Play must remain running — the sweep must not have touched it.
        assert row["status"] == "running"
