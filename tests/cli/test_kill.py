# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for `li kill`: entity resolution, pid signal flow, cascade kill, stale sweep."""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

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


async def _seed_play(
    db: StateDB,
    show_id: str,
    *,
    status: str = "running",
    session_id: str | None = None,
) -> str:
    play_id = str(uuid.uuid4())
    await db.create_play(
        {
            "id": play_id,
            "show_id": show_id,
            "name": f"play-{play_id[:8]}",
            "status": status,
            "session_id": session_id,
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


def test_identity_accepts_shebang_launched_li(monkeypatch: pytest.MonkeyPatch):
    """Shebang-launched li: argv[0]=python3, argv[1]=.../bin/li — must be accepted."""
    _mock_psutil(
        monkeypatch,
        cmdline=["/opt/.venv/bin/python3", "/opt/.venv/bin/li", "play", "abc123"],
    )
    assert _check_pid_identity(42, "lionagi") is True


def test_identity_rejects_foreign_script_with_li_in_path(monkeypatch: pytest.MonkeyPatch):
    """A non-lionagi script whose path contains 'li' must not be accepted."""
    _mock_psutil(
        monkeypatch,
        cmdline=["/usr/bin/python3", "/usr/local/bin/olia-tool", "run"],
    )
    assert _check_pid_identity(42, "lionagi") is False


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
        assert (await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,)))[
            "status"
        ] == "running", "must not cancel an unverified PID"


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

        row = db._row_to_dict(await db.fetch_one("SELECT * FROM sessions WHERE id = ?", (sid,)))
        result = await _kill_one(db, "session", sid, row, user_reason="")
        assert result["signal"] == "identity_mismatch"

        assert (await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,)))[
            "status"
        ] == "running", "must not cancel a recycled PID"


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

        row = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,))
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

        row = await db.fetch_one(
            "SELECT * FROM status_transitions "
            "WHERE entity_id = ? AND previous_status = 'running' AND status = 'cancelled'",
            (sid,),
        )
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
        row = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,))
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

        row = await db.fetch_one("SELECT status FROM shows WHERE id = ?", (show_id,))
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

        assert (await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,)))[
            "status"
        ] == "cancelled"


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

        assert (await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,)))[
            "status"
        ] == "cancelled"


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

        tr = await db.fetch_one(
            "SELECT reason_code FROM status_transitions "
            "WHERE entity_id = ? AND previous_status = 'running' AND status = 'cancelled'",
            (sid,),
        )
        assert tr["reason_code"] == RunReasons.CANCELLED_FORCE_KILL


# ── _do_kill (end-to-end) ─────────────────────────────────────────────────────


async def test_do_kill_by_full_id(temp_db_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("lionagi.cli.kill._terminate_pid", lambda pid, **kw: "sigterm")

    async with StateDB() as db:
        sid = await _seed_session(db, status="running", pid=12345)

    rc = await _do_kill(sid, user_reason="integration test")
    assert rc == 0

    async with StateDB() as db:
        assert (await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,)))[
            "status"
        ] == "cancelled"


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
        assert (await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,)))[
            "status"
        ] == "cancelled"


async def test_do_kill_all_stale_skips_live_pid(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Running session with a LIVE, identity-matching PID is not touched."""
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: True)
    monkeypatch.setattr("lionagi.cli.kill._check_pid_identity", lambda *a, **kw: True)

    old_start = time.time() - 7200
    async with StateDB() as db:
        sid = await _seed_session(db, status="running", pid=12345, started_at=old_start)

    rc = await _do_kill_all_stale(threshold_seconds=3600, dry_run=False)
    assert rc == 0

    async with StateDB() as db:
        assert (await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,)))[
            "status"
        ] == "running"


async def test_do_kill_all_stale_sweeps_reused_pid(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A live PID that no longer identifies as the tracked process (reused
    after the original died) must still be swept, not treated as live."""
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: True)
    monkeypatch.setattr("lionagi.cli.kill._check_pid_identity", lambda *a, **kw: False)

    old_start = time.time() - 7200
    async with StateDB() as db:
        sid = await _seed_session(db, status="running", pid=12345, started_at=old_start)

    rc = await _do_kill_all_stale(threshold_seconds=3600, dry_run=False)
    assert rc == 0

    async with StateDB() as db:
        assert (await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,)))[
            "status"
        ] == "cancelled"


async def test_do_kill_all_stale_skips_recent(temp_db_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Session started recently (under threshold) must not be swept."""
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: False)

    recent_start = time.time() - 60  # only 1 min ago
    async with StateDB() as db:
        sid = await _seed_session(db, status="running", pid=None, started_at=recent_start)

    rc = await _do_kill_all_stale(threshold_seconds=3600, dry_run=False)
    assert rc == 0

    async with StateDB() as db:
        assert (await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,)))[
            "status"
        ] == "running"


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
        assert (await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,)))[
            "status"
        ] == "running"


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
        row = await db.fetch_one(
            "SELECT reason_code FROM status_transitions "
            "WHERE entity_id = ? AND previous_status = 'running' AND status = 'cancelled'",
            (sid,),
        )
        assert row is not None
        assert row["reason_code"] == RunReasons.CANCELLED_STALE_AUTO


# ── cascade kill ───────────────────────────────────────────────────────────────


async def test_list_running_children_show_behavior_is_unchanged(temp_db_path: Path):
    """The existing show branch continues to return only running direct plays."""
    async with StateDB() as db:
        show_id = await _seed_show(db)
        running_play_id = await _seed_play(db, show_id, status="running")
        await _seed_play(db, show_id, status="blocked")

        children = await _list_running_children(db, "show", show_id)

    assert [(kind, row["id"]) for _, kind, row in children] == [("play", running_play_id)]


async def test_walk_running_children_stops_at_safety_cap(
    monkeypatch: pytest.MonkeyPatch,
):
    """The transitive walk terminates and warns if malformed links exceed its cap."""
    import lionagi.cli.kill as kill_mod

    async def fake_children(db: Any, entity_type: str, entity_id: str):
        next_id = str(int(entity_id) + 1)
        return [("sessions", "session", {"id": next_id})]

    warnings: list[str] = []
    monkeypatch.setattr(kill_mod, "_MAX_RECURSIVE_CHILDREN", 2)
    monkeypatch.setattr(kill_mod, "_list_running_children", fake_children)
    monkeypatch.setattr(kill_mod, "warn", warnings.append)

    children = await kill_mod._walk_running_children(object(), "play", "0")

    assert [row["id"] for _, _, row in children] == ["2", "1"]
    assert warnings == [
        "recursive kill stopped after 2 children; remaining descendants were not reaped"
    ]


async def test_walk_running_children_cycle_terminates_without_cap_warning(
    monkeypatch: pytest.MonkeyPatch,
):
    """A finite session/invocation cycle is deduplicated before the cap check."""
    import lionagi.cli.kill as kill_mod

    session = ("sessions", "session", {"id": "session-a"})
    invocation = ("invocations", "invocation", {"id": "invocation-b"})
    graph = {
        ("play", "play-root"): [session],
        ("session", "session-a"): [invocation],
        ("invocation", "invocation-b"): [session],
    }
    calls: list[tuple[str, str]] = []

    async def fake_children(db: Any, entity_type: str, entity_id: str):
        calls.append((entity_type, entity_id))
        return graph.get((entity_type, entity_id), [])

    warnings: list[str] = []
    monkeypatch.setattr(kill_mod, "_MAX_RECURSIVE_CHILDREN", 2)
    monkeypatch.setattr(kill_mod, "_list_running_children", fake_children)
    monkeypatch.setattr(kill_mod, "warn", warnings.append)

    children = await kill_mod._walk_running_children(object(), "play", "play-root")

    assert [row["id"] for _, _, row in children] == ["invocation-b", "session-a"]
    assert calls == [
        ("play", "play-root"),
        ("session", "session-a"),
        ("invocation", "invocation-b"),
    ]
    assert warnings == []


async def test_do_kill_play_reaps_worker_chain_without_recursive_flag(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A bare play kill reaps its linked session and invocation before the play."""
    import lionagi.cli.kill as kill_mod

    signalled_pids: list[int] = []
    persisted_entities: list[tuple[str, str]] = []
    original_persist_cancel = kill_mod._persist_cancel

    def fake_terminate(pid: int, **kwargs: Any) -> str:
        assert pid > 1
        signalled_pids.append(pid)
        return "sigterm"

    async def record_persist_cancel(
        db: Any, entity_type: str, entity_id: str, **kwargs: Any
    ) -> None:
        persisted_entities.append((entity_type, entity_id))
        await original_persist_cancel(db, entity_type, entity_id, **kwargs)

    monkeypatch.setattr(kill_mod, "_terminate_pid", fake_terminate)
    monkeypatch.setattr(kill_mod, "_persist_cancel", record_persist_cancel)

    async with StateDB() as db:
        invocation_id = await _seed_invocation(db, status="running", pid=42002)
        session_id = await _seed_session(db, status="running", pid=42001)
        await db.update_session(session_id, invocation_id=invocation_id)
        show_id = await _seed_show(db)
        play_id = await _seed_play(db, show_id, session_id=session_id)

    rc = await _do_kill(play_id)

    assert rc == 0
    assert signalled_pids == [42002, 42001]
    assert persisted_entities == [
        ("invocation", invocation_id),
        ("session", session_id),
        ("play", play_id),
    ]
    async with StateDB() as db:
        assert (await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (session_id,)))[
            "status"
        ] == "cancelled"
        assert (
            await db.fetch_one("SELECT status FROM invocations WHERE id = ?", (invocation_id,))
        )["status"] == "cancelled"
        assert (await db.fetch_one("SELECT status FROM plays WHERE id = ?", (play_id,)))[
            "status"
        ] == "blocked"


async def test_do_kill_active_show_succeeds(temp_db_path: Path):
    """`li kill <show-id>` on a fresh, unmocked active show maps to 'aborted'."""
    async with StateDB() as db:
        show_id = await _seed_show(db)  # default status="active" -- no mocking

    rc = await _do_kill(show_id)
    assert rc == 0

    async with StateDB() as db:
        assert (await db.fetch_one("SELECT status FROM shows WHERE id = ?", (show_id,)))[
            "status"
        ] == "aborted"


@pytest.mark.parametrize("terminal_status", ["completed", "aborted", "imported"])
async def test_do_kill_show_terminal_statuses_refuse(temp_db_path: Path, terminal_status: str):
    """A show already in a terminal (non-'active') status is rejected, rc=1."""
    async with StateDB() as db:
        show_id = await _seed_show(db, status=terminal_status)

    rc = await _do_kill(show_id)
    assert rc == 1

    async with StateDB() as db:
        assert (await db.fetch_one("SELECT status FROM shows WHERE id = ?", (show_id,)))[
            "status"
        ] == terminal_status


async def test_do_kill_recursive_show_does_not_reap_play_workers(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """--recursive is a documented no-op boundary for shows (ADR-0104): the
    show row goes terminal, but its plays/workers are left untouched."""
    from lionagi.cli._logging import configure_cli_logging

    configure_cli_logging(verbose=False)
    signalled_pids: list[int] = []

    def fake_terminate(pid: int, **kwargs: Any) -> str:
        signalled_pids.append(pid)
        return "sigterm"

    async with StateDB() as db:
        invocation_id = await _seed_invocation(db, status="running", pid=43002)
        session_id = await _seed_session(db, status="running", pid=43001)
        await db.update_session(session_id, invocation_id=invocation_id)
        show_id = await _seed_show(db)  # default status="active" -- no mocking
        play_id = await _seed_play(db, show_id, session_id=session_id)

    monkeypatch.setattr("lionagi.cli.kill._terminate_pid", fake_terminate)

    capsys.readouterr()
    assert await _do_kill(show_id, recursive=True) == 0
    assert signalled_pids == []
    assert "does not reap a show's plays or their workers" in capsys.readouterr().err

    async with StateDB() as db:
        assert (await db.fetch_one("SELECT status FROM shows WHERE id = ?", (show_id,)))[
            "status"
        ] == "aborted"
        assert (await db.fetch_one("SELECT status FROM plays WHERE id = ?", (play_id,)))[
            "status"
        ] == "running"
        assert (await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (session_id,)))[
            "status"
        ] == "running"
        assert (
            await db.fetch_one("SELECT status FROM invocations WHERE id = ?", (invocation_id,))
        )["status"] == "running"


async def test_do_kill_emits_settings_terminal_notification(temp_db_path: Path, tmp_path: Path):
    """The kill transition still reaches the settings notify handler exactly once."""
    async with StateDB() as db:
        session_id = await _seed_session(db, status="running")

    output_path = tmp_path / "kill-notify.jsonl"
    project_dir = tmp_path / "project"
    settings_dir = project_dir / ".lionagi"
    settings_dir.mkdir(parents=True)
    capture_script = (
        "import pathlib, sys; pathlib.Path(sys.argv[1]).open('a').write(sys.stdin.read() + '\\n')"
    )
    (settings_dir / "settings.yaml").write_text(
        yaml.safe_dump(
            {
                "notify": {
                    "on_terminal": {
                        "enabled": True,
                        "adapter": {
                            "kind": "exec",
                            "argv": [sys.executable, "-c", capture_script, str(output_path)],
                        },
                        "filter": {"ids": [session_id]},
                    }
                }
            }
        )
    )

    from lionagi.state.lifecycle.callbacks import DEFAULT_TERMINAL_CALLBACKS
    from lionagi.state.lifecycle.notify_settings import register_settings_terminal_callback

    callback_name = "notify.settings.on_terminal"
    DEFAULT_TERMINAL_CALLBACKS.unregister(callback_name)
    assert register_settings_terminal_callback(project_dir=str(project_dir)) is True
    try:
        assert await _do_kill(session_id) == 0
    finally:
        DEFAULT_TERMINAL_CALLBACKS.unregister(callback_name)

    payloads = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert len(payloads) == 1
    assert payloads[0]["entity"] == {"kind": "session", "id": session_id}
    assert payloads[0]["terminal_status"] == "cancelled"
    assert payloads[0]["reason_code"] == RunReasons.CANCELLED_MANUAL_KILL


async def test_do_kill_play_emits_blocked_terminal_envelope(temp_db_path: Path):
    """A play kill emits its blocked envelope through the lifecycle callback seam."""
    from lionagi.state.lifecycle.callbacks import (
        DEFAULT_TERMINAL_CALLBACKS,
        RunTerminalEnvelope,
    )

    async with StateDB() as db:
        session_id = await _seed_session(db, status="running")
        show_id = await _seed_show(db)
        play_id = await _seed_play(db, show_id, session_id=session_id)

    received: list[RunTerminalEnvelope] = []

    async def collect(envelope: RunTerminalEnvelope) -> None:
        received.append(envelope)

    callback_name = "test.kill.play-terminal"
    DEFAULT_TERMINAL_CALLBACKS.register(
        callback_name,
        collect,
        kinds=["play"],
        ids=[play_id],
    )
    try:
        assert await _do_kill(play_id) == 0
    finally:
        DEFAULT_TERMINAL_CALLBACKS.unregister(callback_name)

    assert len(received) == 1
    envelope = received[0]
    assert envelope.entity.kind == "play"
    assert envelope.entity.id == play_id
    assert envelope.previous_status == "running"
    assert envelope.terminal_status == "blocked"
    assert envelope.reason_code == RunReasons.CANCELLED_MANUAL_KILL


async def test_do_kill_play_without_session_warns_and_continues(
    temp_db_path: Path, capsys: pytest.CaptureFixture[str]
):
    """A NULL play session link is status-only but never silently misleading."""
    from lionagi.cli._logging import configure_cli_logging

    configure_cli_logging(verbose=False)
    async with StateDB() as db:
        show_id = await _seed_show(db)
        play_id = await _seed_play(db, show_id, session_id=None)

    capsys.readouterr()
    rc = await _do_kill(play_id)

    assert rc == 0
    assert f"play {play_id[:12]} has no running worker session to reap" in capsys.readouterr().err
    async with StateDB() as db:
        assert (await db.fetch_one("SELECT status FROM plays WHERE id = ?", (play_id,)))[
            "status"
        ] == "blocked"


async def test_do_kill_play_with_dangling_session_warns_and_continues(
    temp_db_path: Path, capsys: pytest.CaptureFixture[str]
):
    """A non-NULL play link with no session row warns and still blocks the play."""
    import sqlite3

    from lionagi.cli._logging import configure_cli_logging

    configure_cli_logging(verbose=False)
    dangling_session_id = str(uuid.uuid4())
    async with StateDB() as db:
        show_id = await _seed_show(db)
        play_id = await _seed_play(db, show_id)

    with sqlite3.connect(temp_db_path) as conn:
        conn.execute(
            "UPDATE plays SET session_id = ? WHERE id = ?",
            (dangling_session_id, play_id),
        )

    capsys.readouterr()
    assert await _do_kill(play_id) == 0

    assert f"play {play_id[:12]} has no running worker session to reap" in capsys.readouterr().err
    async with StateDB() as db:
        assert (await db.fetch_one("SELECT status FROM plays WHERE id = ?", (play_id,)))[
            "status"
        ] == "blocked"


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
        assert (await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,)))[
            "status"
        ] == "cancelled"


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
        await db.execute(
            "UPDATE shows SET updated_at = ?, created_at = ? WHERE id = ?",
            (old_time, old_time, show_id),
        )
        # Seed a child play so we also verify plays are not swept.
        play_id = await _seed_play(db, show_id, status="running")
        await db.execute(
            "UPDATE plays SET started_at = ? WHERE id = ?",
            (old_time, play_id),
        )

    rc = await _do_kill_all_stale(threshold_seconds=3600, dry_run=False)
    assert rc == 0

    async with StateDB() as db:
        row = await db.fetch_one("SELECT status FROM shows WHERE id = ?", (show_id,))
        assert row is not None
        # Show must remain active — the sweep must not have touched it.
        assert row["status"] == "active"

        row2 = await db.fetch_one("SELECT status FROM plays WHERE id = ?", (play_id,))
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
        await db.execute(
            "UPDATE plays SET started_at = ? WHERE id = ?",
            (old_start, play_id),
        )

    rc = await _do_kill_all_stale(threshold_seconds=3600, dry_run=False)
    assert rc == 0

    async with StateDB() as db:
        row = await db.fetch_one("SELECT status FROM plays WHERE id = ?", (play_id,))
        assert row is not None
        # Play must remain running — the sweep must not have touched it.
        assert row["status"] == "running"
