# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for `li kill`: entity resolution, pid signal flow, cascade kill, stale sweep."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import psutil
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

# Fixtures


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
    """Seed a play row.

    The default is the shape a live run produces: no session link, because
    nothing on that path binds the column. Pass `session_id` for the shape the
    Studio show importer produces, which resolves the session by name and
    writes it.
    """
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


# _pid_alive


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


# _terminate_pid


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


# _terminate_pid identity checks


def test_terminate_pid_identity_mismatch_no_signal_sent(
    monkeypatch: pytest.MonkeyPatch,
):
    """If cmdline doesn't match expected_cmd, no signal is sent.

    A create-time-bearing fixture is required: without a durable
    ``expected_create_time``/``expected_session_id``, `_check_pid_identity`
    refuses as "unverifiable" before ever calling `_cmdline_is_lionagi`, and
    this test would pass for that unrelated reason regardless of cmdline.
    """
    import signal as _signal

    kill_calls: list[tuple[int, int]] = []
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: True)
    monkeypatch.setattr("os.kill", lambda pid, sig: kill_calls.append((pid, sig)))

    # Mock psutil with a process whose cmdline does NOT contain "lionagi".
    fake_psutil = MagicMock()
    fake_proc = MagicMock()
    fake_proc.cmdline.return_value = ["/usr/bin/python3", "unrelated_script.py"]
    fake_proc.create_time.return_value = 100.0
    fake_psutil.Process.return_value = fake_proc
    fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    fake_psutil.AccessDenied = type("AccessDenied", (Exception,), {})
    # Mirrors psutil: ZombieProcess is a NoSuchProcess subclass, so a double
    # that omits it lets the code under test claim a distinction it never made.
    fake_psutil.ZombieProcess = type("ZombieProcess", (fake_psutil.NoSuchProcess,), {})
    monkeypatch.setattr("lionagi.cli.kill.psutil", fake_psutil)

    result = _terminate_pid(
        42, grace_seconds=0.1, expected_cmd="lionagi", expected_create_time=100.0
    )
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
    fake_proc.create_time.return_value = 100.0
    fake_psutil.Process.return_value = fake_proc
    fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    fake_psutil.AccessDenied = type("AccessDenied", (Exception,), {})
    # Mirrors psutil: ZombieProcess is a NoSuchProcess subclass, so a double
    # that omits it lets the code under test claim a distinction it never made.
    fake_psutil.ZombieProcess = type("ZombieProcess", (fake_psutil.NoSuchProcess,), {})
    monkeypatch.setattr("lionagi.cli.kill.psutil", fake_psutil)

    # A durable create_time marker must be present for a cmdline match to
    # authorize the kill (no session id and no create_time is "unverifiable").
    result = _terminate_pid(
        42, grace_seconds=0.01, expected_cmd="lionagi", expected_create_time=100.0
    )
    # SIGTERM must have been sent
    assert any(sig == __import__("signal").SIGTERM for _, sig in kill_calls)
    assert result in ("sigterm", "sigkill")


# A real, unreaped child
#
# Everything below uses a process this test actually started. A SIGTERMed child
# whose parent has not called wait() is a zombie: it holds its pid, so `kill -0`
# keeps reporting it present, but it has no command line, no environment and no
# way to be killed again. That is a third answer next to "this is our process"
# and "this pid belongs to something else", and the kill path has to record the
# cancellation for it instead of refusing.


def _spawn_marked_child(session_id: str) -> subprocess.Popen:
    """Start a sleeper carrying the session marker `li kill` identifies runs by."""
    return subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(300)"],
        env={**os.environ, "LIONAGI_SESSION_ID": session_id},
    )


def _await(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _is_zombie(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.ZombieProcess:
        return True
    except psutil.NoSuchProcess:
        return False


@pytest.mark.skipif(os.name != "posix", reason="zombies are a POSIX process state")
async def test_kill_cancels_session_whose_child_is_an_unreaped_zombie(
    temp_db_path: Path,
):
    """`li kill` against a SIGTERMed-but-unreaped child must persist the cancel.

    This is the window a caller opens by starting a run and never waiting on
    it: the process is gone, nobody has reaped it, and the row is still
    'running'. Refusing here loses the cancellation for a run that has in fact
    stopped.
    """
    sid = str(uuid.uuid4())
    child = _spawn_marked_child(sid)
    assert child.pid > 1

    try:
        assert _await(
            lambda: _check_pid_identity(child.pid, "lionagi", expected_session_id=sid) == "ours"
        ), "child never came up carrying its session marker"
        create_time = psutil.Process(child.pid).create_time()

        # While it is alive the identity check accepts it. Nothing about the
        # arguments changes below, so whatever answer comes back after the
        # SIGTERM differs only because the process died without being reaped.
        assert (
            _check_pid_identity(
                child.pid,
                "lionagi",
                expected_session_id=sid,
                expected_create_time=create_time,
            )
            == "ours"
        )

        async with StateDB() as db:
            prog = str(uuid.uuid4())
            await db.create_progression(prog)
            await db.create_session(
                {
                    "id": sid,
                    "progression_id": prog,
                    "status": "running",
                    "started_at": time.time(),
                    "node_metadata": {"pid": child.pid, "pid_create_time": create_time},
                }
            )

        os.kill(child.pid, signal.SIGTERM)
        # Deliberately no child.wait()/poll(): an unreaped exit is the state
        # under test, and reaping it here would test a pid that is simply gone.
        assert _await(lambda: _is_zombie(child.pid)), (
            "child did not become a zombie — this environment reaps children "
            "on its own and cannot exercise the window"
        )
        assert _pid_alive(child.pid) is True, "a zombie still answers kill -0"
        assert (
            _check_pid_identity(
                child.pid,
                "lionagi",
                expected_session_id=sid,
                expected_create_time=create_time,
            )
            == "zombie"
        )

        rc = await _do_kill(sid, grace_seconds=0.5)
        assert rc == 0, "a run that has already stopped is not a blocked kill"

        async with StateDB() as db:
            row = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,))
        assert row is not None
        assert row["status"] == "cancelled", (
            "the process is dead and the row must say so; leaving it 'running' "
            "loses the cancellation"
        )
    finally:
        if child.poll() is None and child.pid > 1:
            child.kill()
        child.wait()


@pytest.mark.skipif(os.name != "posix", reason="a zombie is a POSIX process state")
def test_terminate_pid_reports_an_unreaped_process_dead_with_no_identity_to_check():
    """The same window, reached where there is nothing to identify the pid by.

    `_terminate_pid` is also called with no expected command — killing by pid,
    and every liveness poll inside the grace loop. On that path the identity
    classifier never runs, so its verdict cannot be what saves this: with only
    `kill -0` to go on, an unreaped process looks alive forever and the caller
    would SIGTERM a corpse and then sit out the whole grace window waiting for
    a flag that can never flip.
    """
    sid = str(uuid.uuid4())
    child = _spawn_marked_child(sid)
    assert child.pid > 1

    try:
        assert _await(lambda: _pid_alive(child.pid)), "child never started"
        os.kill(child.pid, signal.SIGTERM)
        assert _await(lambda: _is_zombie(child.pid)), (
            "child did not become a zombie — this environment reaps children "
            "on its own and cannot exercise the window"
        )
        assert _pid_alive(child.pid) is True, "a zombie still answers kill -0"

        started = time.monotonic()
        assert _terminate_pid(child.pid, grace_seconds=5.0) == "already_dead"
        assert time.monotonic() - started < 1.0, "it waited out a grace window for a dead process"
    finally:
        if child.poll() is None and child.pid > 1:
            child.kill()
        child.wait()


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
    # Mirrors psutil: ZombieProcess is a NoSuchProcess subclass, so a double
    # that omits it lets the code under test claim a distinction it never made.
    fake_psutil.ZombieProcess = type("ZombieProcess", (fake_psutil.NoSuchProcess,), {})
    monkeypatch.setattr("lionagi.cli.kill.psutil", fake_psutil)
    return kill_calls


def test_identity_rejects_path_substring(monkeypatch: pytest.MonkeyPatch):
    """An unrelated process that only *mentions* lionagi in a path arg is rejected.

    The reported false positive: ``vim /Users/lion/projects/lionagi/README.md``.
    A substring match would signal this recycled PID; an exact-token match must not.

    ``expected_create_time`` is required and matched to the fake process's
    create_time: without it, `_check_pid_identity` refuses as "unverifiable"
    before `_cmdline_is_lionagi` ever runs, and this test would pass for that
    unrelated reason regardless of the cmdline predicate.
    """
    kill_calls = _mock_psutil(
        monkeypatch,
        cmdline=["/usr/bin/vim", "/Users/lion/projects/lionagi/README.md"],
    )
    result = _terminate_pid(
        42, grace_seconds=0.1, expected_cmd="lionagi", expected_create_time=100.0
    )
    assert result == "identity_mismatch"
    assert kill_calls == [], "must not signal a process that only has lionagi in a path"


def test_identity_accepts_dash_m_module(monkeypatch: pytest.MonkeyPatch):
    """``python -m lionagi.cli.main`` is a genuine invocation and is accepted."""
    _mock_psutil(monkeypatch, cmdline=["/usr/bin/python3", "-m", "lionagi.cli.main"])
    assert _check_pid_identity(42, "lionagi", expected_create_time=100.0) == "ours"


def test_identity_accepts_li_entrypoint(monkeypatch: pytest.MonkeyPatch):
    """The ``li`` console-script entrypoint is accepted by executable basename."""
    _mock_psutil(monkeypatch, cmdline=["/opt/venv/bin/li", "kill", "abc123"])
    assert _check_pid_identity(42, "lionagi", expected_create_time=100.0) == "ours"


def test_identity_accepts_shebang_launched_li(monkeypatch: pytest.MonkeyPatch):
    """Shebang-launched li: argv[0]=python3, argv[1]=.../bin/li — must be accepted."""
    _mock_psutil(
        monkeypatch,
        cmdline=["/opt/.venv/bin/python3", "/opt/.venv/bin/li", "play", "abc123"],
    )
    assert _check_pid_identity(42, "lionagi", expected_create_time=100.0) == "ours"


def test_identity_accepts_macos_framework_python_launcher(monkeypatch: pytest.MonkeyPatch):
    _mock_psutil(
        monkeypatch,
        cmdline=[
            "/opt/homebrew/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/"
            "Contents/MacOS/Python",
            "/opt/.venv/bin/li",
            "agent",
        ],
    )
    assert _check_pid_identity(42, "lionagi", expected_create_time=100.0) == "ours"


def test_identity_rejects_foreign_script_with_li_in_path(monkeypatch: pytest.MonkeyPatch):
    """A non-lionagi script whose path contains 'li' must not be accepted."""
    _mock_psutil(
        monkeypatch,
        cmdline=["/usr/bin/python3", "/usr/local/bin/olia-tool", "run"],
    )
    assert _check_pid_identity(42, "lionagi", expected_create_time=100.0) == "not_ours"


def test_identity_session_marker_match(monkeypatch: pytest.MonkeyPatch):
    """A matching LIONAGI_SESSION_ID env marker is a definitive match."""
    _mock_psutil(
        monkeypatch,
        cmdline=["/usr/bin/python3", "-m", "lionagi.cli"],
        environ={"LIONAGI_SESSION_ID": "run-123"},
    )
    assert _check_pid_identity(42, "lionagi", expected_session_id="run-123") == "ours"


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
    assert _check_pid_identity(42, "lionagi", expected_session_id="run-123") == "not_ours"


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
    assert _check_pid_identity(42, "lionagi", expected_session_id="run-123") == "unverifiable"
    # create_time matches AND cmdline is lionagi → positively identified.
    assert (
        _check_pid_identity(
            42, "lionagi", expected_session_id="run-123", expected_create_time=500.0
        )
        == "ours"
    )
    # create_time differs → recycled PID → skip.
    assert (
        _check_pid_identity(42, "lionagi", expected_session_id="run-123", expected_create_time=1.0)
        == "not_ours"
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
        == "not_ours"
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
    assert _check_pid_identity(42, "lionagi", expected_create_time=999.0) == "not_ours"
    # 0.5s apart → different process → reject (was accepted under the old 2s gate).
    assert _check_pid_identity(42, "lionagi", expected_create_time=100.5) == "not_ours"
    # within tick-rounding tolerance → accepted.
    assert _check_pid_identity(42, "lionagi", expected_create_time=100.05) == "ours"


def test_identity_no_durable_marker_refuses_cmdline_only_authorization(
    monkeypatch: pytest.MonkeyPatch,
):
    """No session id AND no create_time recorded: cmdline shape alone must not authorize.

    A record with neither identity field carries nothing that distinguishes the
    live process at this pid from any other process that merely looks like a
    lionagi invocation. Falling back to `_cmdline_is_lionagi` here would let a
    same-looking, unrelated process be signalled.
    """
    _mock_psutil(monkeypatch, cmdline=["/usr/bin/python3", "-m", "lionagi.cli"])
    assert _check_pid_identity(42, "lionagi") == "unverifiable"
    assert (
        _check_pid_identity(42, "lionagi", expected_session_id=None, expected_create_time=None)
        == "unverifiable"
    )


async def test_do_kill_invocation_without_pid_create_time_does_not_signal(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An invocation whose metadata carries a pid but no pid_create_time refuses the kill.

    Reproduces the reported hazard: `li invoke start --metadata '{"pid": N}'`
    stores a pid with no creation-time marker, and invocations never carry a
    session id either, so a live process that merely looks like a lionagi
    invocation must not receive SIGTERM.
    """
    kill_calls = _mock_psutil(
        monkeypatch,
        cmdline=["/usr/bin/python3", "-m", "lionagi.cli"],
    )

    async with StateDB() as db:
        inv_id = await _seed_invocation(db, status="running", pid=4242)

    rc = await _do_kill(inv_id)
    assert rc == 1, "an unverifiable identity must block the kill"
    assert kill_calls == [], "must not signal a process with no durable identity to confirm"

    async with StateDB() as db:
        row = await db.fetch_one("SELECT status FROM invocations WHERE id = ?", (inv_id,))
        assert row["status"] == "running", "must not cancel a row whose kill was refused"


# current_pid_markers (launch-time recording)


def test_current_pid_markers_records_own_pid():
    """Markers describe the calling process; create_time present when psutil is."""
    import os

    from lionagi.cli.kill import current_pid_markers

    markers = current_pid_markers()
    assert markers["pid"] == os.getpid()
    # dev env has psutil; create_time must be a real float matching this process.
    import psutil

    assert markers["pid_create_time"] == pytest.approx(psutil.Process(os.getpid()).create_time())


def test_recorded_markers_read_as_foreign_from_another_machine(
    monkeypatch: pytest.MonkeyPatch,
):
    """A row this code writes must be placeable by the machine that reads it.

    The assertion is made from the position of a second machine sharing the
    state store, because that is the only position from which the marker does
    any work. Asking on the machine that wrote the row proves nothing either
    way: a row carrying no host reads as "origin unknown", which is the same
    permissive branch as "origin is you", so a same-machine check passes
    whether or not the host was ever recorded.
    """
    import socket

    from lionagi.cli._util import recorded_pid_is_foreign
    from lionagi.cli.kill import current_pid_markers

    markers = current_pid_markers()
    assert recorded_pid_is_foreign(markers) is False, "our own row is not foreign to us"

    real_host = socket.gethostname()
    monkeypatch.setattr(socket, "gethostname", lambda: f"not-{real_host}")
    assert recorded_pid_is_foreign(markers) is True, (
        "a row recorded here must read as foreign from another machine, or that "
        "machine signals our pid and reads our silence as our death"
    )


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


# _resolve_entity


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


# _persist_cancel


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


async def test_persist_cancel_populates_duration_ms(temp_db_path: Path):
    async with StateDB() as db:
        started_at = time.time() - 5.0
        sid = await _seed_session(db, status="running", started_at=started_at)

        await _persist_cancel(
            db,
            "session",
            sid,
            reason_code=RunReasons.CANCELLED_MANUAL_KILL,
            reason_summary="test cancel",
            evidence={"signal": "sigterm", "pid": 42},
        )

        row = await db.get_session(sid)
        assert row["ended_at"] is not None
        assert row["duration_ms"] == pytest.approx((row["ended_at"] - started_at) * 1000)


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


# _kill_one


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


# _do_kill (end-to-end)


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


# _do_kill_all_stale


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
    monkeypatch.setattr("lionagi.cli.kill._check_pid_identity", lambda *a, **kw: "ours")

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
    monkeypatch.setattr("lionagi.cli.kill._check_pid_identity", lambda *a, **kw: "not_ours")

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


async def test_do_kill_all_stale_recycled_pid_swept_with_correlation(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A recycled pid occupied by an unrelated lionagi-shaped process must be
    swept once the row's own pid_create_time is correlated against it, even
    though the cmdline alone still looks like a genuine lionagi invocation."""
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: True)

    fake_psutil = MagicMock()
    fake_proc = MagicMock()
    fake_proc.cmdline.return_value = ["/usr/bin/python3", "-m", "lionagi.cli"]
    fake_proc.environ.return_value = {}
    fake_proc.create_time.return_value = 999.0  # does NOT match recorded 100.0
    fake_psutil.Process.return_value = fake_proc
    fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    fake_psutil.AccessDenied = type("AccessDenied", (Exception,), {})
    # Mirrors psutil: ZombieProcess is a NoSuchProcess subclass, so a double
    # that omits it lets the code under test claim a distinction it never made.
    fake_psutil.ZombieProcess = type("ZombieProcess", (fake_psutil.NoSuchProcess,), {})
    monkeypatch.setattr("lionagi.cli.kill.psutil", fake_psutil)

    old_start = time.time() - 7200
    async with StateDB() as db:
        sid = str(uuid.uuid4())
        prog = str(uuid.uuid4())
        await db.create_progression(prog)
        await db.create_session(
            {
                "id": sid,
                "progression_id": prog,
                "status": "running",
                "started_at": old_start,
                "node_metadata": {"pid": 12345, "pid_create_time": 100.0},
            }
        )

    rc = await _do_kill_all_stale(threshold_seconds=3600, dry_run=False)
    assert rc == 0

    async with StateDB() as db:
        assert (await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,)))[
            "status"
        ] == "cancelled", "recycled pid with mismatched create_time must be swept"


async def test_do_kill_all_stale_not_ours_evidence_reports_pid_was_alive(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The cancellation evidence for a recycled-pid sweep must say the pid WAS
    alive, distinguishing "gone" from "alive but not ours" -- the two call for
    different operator follow-up, and only one of them means the process is
    actually gone.
    """
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: True)

    fake_psutil = MagicMock()
    fake_proc = MagicMock()
    fake_proc.cmdline.return_value = ["/usr/bin/python3", "-m", "lionagi.cli"]
    fake_proc.environ.return_value = {}
    fake_proc.create_time.return_value = 999.0  # does NOT match recorded 100.0
    fake_psutil.Process.return_value = fake_proc
    fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    fake_psutil.AccessDenied = type("AccessDenied", (Exception,), {})
    fake_psutil.ZombieProcess = type("ZombieProcess", (fake_psutil.NoSuchProcess,), {})
    monkeypatch.setattr("lionagi.cli.kill.psutil", fake_psutil)

    old_start = time.time() - 7200
    async with StateDB() as db:
        sid = str(uuid.uuid4())
        prog = str(uuid.uuid4())
        await db.create_progression(prog)
        await db.create_session(
            {
                "id": sid,
                "progression_id": prog,
                "status": "running",
                "started_at": old_start,
                "node_metadata": {"pid": 12345, "pid_create_time": 100.0},
            }
        )

    rc = await _do_kill_all_stale(threshold_seconds=3600, dry_run=False)
    assert rc == 0

    async with StateDB() as db:
        s = await db.get_session(sid)
    assert s["status"] == "cancelled"
    evidence_raw = s["status_evidence_refs"]
    evidence = json.loads(evidence_raw) if isinstance(evidence_raw, str) else evidence_raw
    assert isinstance(evidence, list) and evidence
    entry = evidence[0]
    assert entry["pid_alive"] is True, (
        "the pid WAS alive -- only its recorded identity didn't match"
    )
    assert entry["identity_verdict"] == "not_ours"


async def test_do_kill_all_stale_matching_correlation_skips_live(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When the row's pid_create_time genuinely matches the live process,
    the sweep must still skip it as live (not a regression on the happy path)."""
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: True)

    fake_psutil = MagicMock()
    fake_proc = MagicMock()
    fake_proc.cmdline.return_value = ["/usr/bin/python3", "-m", "lionagi.cli"]
    fake_proc.environ.return_value = {}
    fake_proc.create_time.return_value = 100.0  # matches recorded value
    fake_psutil.Process.return_value = fake_proc
    fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    fake_psutil.AccessDenied = type("AccessDenied", (Exception,), {})
    # Mirrors psutil: ZombieProcess is a NoSuchProcess subclass, so a double
    # that omits it lets the code under test claim a distinction it never made.
    fake_psutil.ZombieProcess = type("ZombieProcess", (fake_psutil.NoSuchProcess,), {})
    monkeypatch.setattr("lionagi.cli.kill.psutil", fake_psutil)

    old_start = time.time() - 7200
    async with StateDB() as db:
        sid = str(uuid.uuid4())
        prog = str(uuid.uuid4())
        await db.create_progression(prog)
        await db.create_session(
            {
                "id": sid,
                "progression_id": prog,
                "status": "running",
                "started_at": old_start,
                "node_metadata": {"pid": 12345, "pid_create_time": 100.0},
            }
        )

    rc = await _do_kill_all_stale(threshold_seconds=3600, dry_run=False)
    assert rc == 0

    async with StateDB() as db:
        assert (await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,)))[
            "status"
        ] == "running", "a genuinely live, correlated process must not be swept"


async def test_do_kill_all_stale_access_denied_not_cancelled(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A live pid we cannot inspect (psutil.AccessDenied) must be treated as
    still alive by the sweep, not cancelled out from under a running worker.

    Discrimination: an identity check that collapses AccessDenied into "not
    ours" gets the row cancelled while the process keeps running. The verdict
    reports "unverifiable" instead and the sweep skips it.
    """
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: True)

    fake_access_denied = type("AccessDenied", (Exception,), {})
    fake_psutil = MagicMock()
    fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    fake_psutil.AccessDenied = fake_access_denied
    fake_psutil.ZombieProcess = type("ZombieProcess", (fake_psutil.NoSuchProcess,), {})
    fake_psutil.Process.side_effect = fake_access_denied("no access")
    monkeypatch.setattr("lionagi.cli.kill.psutil", fake_psutil)

    old_start = time.time() - 7200
    async with StateDB() as db:
        sid = await _seed_session(db, status="running", pid=54321, started_at=old_start)

    rc = await _do_kill_all_stale(threshold_seconds=3600, dry_run=False)
    assert rc == 0

    async with StateDB() as db:
        assert (await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,)))[
            "status"
        ] == "running", "AccessDenied must not be treated as a dead/recycled pid"


async def test_do_kill_all_stale_unverifiable_pid_persists_first_observed_marker(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A row skipped as unverifiable must retain evidence of that across sweeps.

    Before this fix the fail-safe "treat as live" decision was only a local
    counter for the one sweep: the record kept no `unverifiable_since` or
    count, so a permanently-uninspectable pid was silently skipped on every
    run forever with nothing durable to show for it. The first sweep must
    record when this was first observed; a second sweep must keep that
    timestamp fixed while advancing the count, not restart the clock.
    """
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: True)

    fake_access_denied = type("AccessDenied", (Exception,), {})
    fake_psutil = MagicMock()
    fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    fake_psutil.AccessDenied = fake_access_denied
    fake_psutil.ZombieProcess = type("ZombieProcess", (fake_psutil.NoSuchProcess,), {})
    fake_psutil.Process.side_effect = fake_access_denied("no access")
    monkeypatch.setattr("lionagi.cli.kill.psutil", fake_psutil)

    old_start = time.time() - 7200
    async with StateDB() as db:
        sid = await _seed_session(db, status="running", pid=54321, started_at=old_start)

    assert await _do_kill_all_stale(threshold_seconds=3600, dry_run=False) == 0

    async with StateDB() as db:
        s = await db.get_session(sid)
    assert s["status"] == "running"
    meta = s["node_metadata"]
    meta = json.loads(meta) if isinstance(meta, str) else meta
    first_seen = meta["unverifiable_since"]
    assert isinstance(first_seen, float)
    assert meta["unverifiable_count"] == 1

    assert await _do_kill_all_stale(threshold_seconds=3600, dry_run=False) == 0

    async with StateDB() as db:
        s = await db.get_session(sid)
    meta = s["node_metadata"]
    meta = json.loads(meta) if isinstance(meta, str) else meta
    assert meta["unverifiable_since"] == first_seen, "first-observed marker must not reset"
    assert meta["unverifiable_count"] == 2


async def test_do_kill_all_stale_unverifiable_markers_survive_flow_metadata_write(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Ordinary flow progress must not erase the unverifiable-pid evidence a
    sweep just recorded.

    flow.py's session metadata writers (the early-graph snapshot, segment
    writes, control-log writes) go through a shared merge-preserving helper
    so they layer their own fields onto whatever is already in
    `node_metadata` instead of replacing the column outright. This proves
    the merge survives a full sweep → flow-write → sweep cycle: the marker
    set by the first sweep must still be there, unmodified, for the second
    sweep to build on.
    """
    from lionagi.cli.orchestrate.flow import _persist_node_metadata_patch

    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: True)

    fake_access_denied = type("AccessDenied", (Exception,), {})
    fake_psutil = MagicMock()
    fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    fake_psutil.AccessDenied = fake_access_denied
    fake_psutil.ZombieProcess = type("ZombieProcess", (fake_psutil.NoSuchProcess,), {})
    fake_psutil.Process.side_effect = fake_access_denied("no access")
    monkeypatch.setattr("lionagi.cli.kill.psutil", fake_psutil)

    old_start = time.time() - 7200
    async with StateDB() as db:
        sid = await _seed_session(db, status="running", pid=54321, started_at=old_start)

    assert await _do_kill_all_stale(threshold_seconds=3600, dry_run=False) == 0

    async with StateDB() as db:
        s = await db.get_session(sid)
    meta = s["node_metadata"]
    meta = json.loads(meta) if isinstance(meta, str) else meta
    first_seen = meta["unverifiable_since"]
    assert meta["unverifiable_count"] == 1

    # Ordinary flow progress: a segment/control-log style metadata write,
    # the same write shape flow.py's live-persist callbacks use.
    async with StateDB() as db:
        await _persist_node_metadata_patch(
            db, sid, {"segments": [{"branch_name": "worker-1", "status": "completed"}]}
        )

    assert await _do_kill_all_stale(threshold_seconds=3600, dry_run=False) == 0

    async with StateDB() as db:
        s = await db.get_session(sid)
    meta = s["node_metadata"]
    meta = json.loads(meta) if isinstance(meta, str) else meta
    assert meta["unverifiable_since"] == first_seen, (
        "a flow metadata write must not reset the first-observed marker"
    )
    assert meta["unverifiable_count"] == 2, (
        "the sweep count must still increment after an intervening flow metadata write"
    )
    assert meta["segments"] == [{"branch_name": "worker-1", "status": "completed"}], (
        "the flow metadata write itself must still land"
    )


async def test_do_kill_all_stale_sweep_write_does_not_clobber_interleaved_flow_write(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A flow metadata write landing INSIDE the sweep's own read-to-write
    window must survive the sweep's write, not just a flow write that lands
    between two separate sweep calls.

    test_do_kill_all_stale_unverifiable_markers_survive_flow_metadata_write
    (above) only proves the marker survives a full sweep -> explicit flow
    write -> full sweep round trip; each sweep call there completes its own
    read and write before the flow write is ever issued, so it cannot catch
    a whole-column writer racing a concurrent write. This test forces the
    flow write to fire from inside the sweep's SELECT (the read the sweep's
    marker-write patch is computed from), before the sweep's own write for
    that row runs, reproducing the exact interleaving a whole-column
    `update_session(node_metadata=json.dumps(new_meta))` write loses:
    seed the unverifiable markers, let the sweep read the row, land a flow
    segment write, then let the sweep write its markers back.
    """
    from lionagi.cli.orchestrate.flow import _persist_node_metadata_patch

    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: True)

    fake_access_denied = type("AccessDenied", (Exception,), {})
    fake_psutil = MagicMock()
    fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    fake_psutil.AccessDenied = fake_access_denied
    fake_psutil.ZombieProcess = type("ZombieProcess", (fake_psutil.NoSuchProcess,), {})
    fake_psutil.Process.side_effect = fake_access_denied("no access")
    monkeypatch.setattr("lionagi.cli.kill.psutil", fake_psutil)

    old_start = time.time() - 7200
    async with StateDB() as db:
        sid = await _seed_session(db, status="running", pid=54321, started_at=old_start)
        # Seed the markers the way a prior sweep would have, so this sweep's
        # read sees a `meta` dict that already carries them (matching round
        # 6's reproduction, which seeded unverifiable_since/count before the
        # interleaving).
        await db.merge_session_node_metadata(
            sid, {"unverifiable_since": 111.0, "unverifiable_count": 1}
        )

    real_fetch_all = StateDB.fetch_all
    flow_write_done = asyncio.Event()

    async def gated_fetch_all(self, query, params):
        result = await real_fetch_all(self, query, params)
        if "FROM sessions WHERE status" in query and not flow_write_done.is_set():
            # This is the sweep's read of live session rows -- the read the
            # per-row `meta`/marker computation below is taken from. Land a
            # concurrent flow write right now, before the sweep issues its
            # own write for this row.
            async with StateDB() as flow_db:
                await _persist_node_metadata_patch(
                    flow_db, sid, {"segments": [{"branch_name": "worker-1"}]}
                )
            flow_write_done.set()
        return result

    monkeypatch.setattr(StateDB, "fetch_all", gated_fetch_all)
    try:
        assert await _do_kill_all_stale(threshold_seconds=3600, dry_run=False) == 0
    finally:
        monkeypatch.setattr(StateDB, "fetch_all", real_fetch_all)

    assert flow_write_done.is_set(), "the gated fetch_all never fired the interleaved flow write"

    async with StateDB() as db:
        s = await db.get_session(sid)
    meta = s["node_metadata"]
    meta = json.loads(meta) if isinstance(meta, str) else meta
    assert meta.get("unverifiable_since") == 111.0
    assert meta.get("unverifiable_count") == 2, (
        "the sweep's own marker write must still land after the interleaved flow write"
    )
    assert meta.get("segments") == [{"branch_name": "worker-1"}], (
        "a flow write landing inside the sweep's read-to-write window must survive "
        "the sweep's own metadata write, not be clobbered by a whole-column snapshot"
    )


async def test_persist_node_metadata_patch_concurrent_writers_both_land(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Two concurrent flow-metadata writers merging onto the SAME session row
    must both survive -- the read and the write have to happen as one atomic
    database operation, not a get_session() followed by a separate
    update_session(), or the second writer's read (taken before the first
    writer's write lands) silently clobbers the first writer's patch.

    This is not a hypothetical: the segment writer and the control-log writer
    (flow.py's _persist_segments / _persist_control_log) both merge onto the
    same session row, from independent callbacks that DAG execution can fire
    close together, and a stale-sweep can write between either of them.

    get_session() is gated so both callers finish their read before either
    is allowed to proceed -- the worst-case interleaving -- rather than
    relying on incidental asyncio scheduling to happen to hit it. Under the
    fix neither caller calls get_session() at all (the merge is one atomic
    UPDATE), so the gate never fires and sits inert; this instrumentation
    only forces the window open for a read-then-write implementation.
    """
    from lionagi.cli.orchestrate.flow import _persist_node_metadata_patch

    async with StateDB() as db:
        sid = await _seed_session(db, status="running", pid=54321)
        await db.merge_session_node_metadata(sid, {"seed": 1})

    real_get_session = StateDB.get_session
    barrier = asyncio.Event()
    entered = 0

    async def gated_get_session(self, session_id):
        nonlocal entered
        result = await real_get_session(self, session_id)
        entered += 1
        if entered >= 2:
            barrier.set()
        await barrier.wait()
        return result

    monkeypatch.setattr(StateDB, "get_session", gated_get_session)

    async def _write(patch):
        async with StateDB() as db:
            await _persist_node_metadata_patch(db, sid, patch)

    await asyncio.gather(_write({"left": 1}), _write({"right": 2}))

    # Un-gate before the verification read: it must not itself wait on a
    # barrier only the (absent, under the fix) get_session() callers could
    # have opened past count 1.
    monkeypatch.setattr(StateDB, "get_session", real_get_session)

    async with StateDB() as db:
        s = await db.get_session(sid)
    meta = s["node_metadata"]
    meta = json.loads(meta) if isinstance(meta, str) else meta
    assert meta.get("seed") == 1, "pre-existing metadata must survive both concurrent writes"
    assert meta.get("left") == 1, "the first writer's patch must not be lost to the race"
    assert meta.get("right") == 2, "the second writer's patch must not be lost to the race"


async def test_do_kill_all_stale_process_vanishing_mid_check_does_not_abort_sweep(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A process dying between the liveness check and the psutil detail reads
    (NoSuchProcess from environ/create_time/cmdline) must classify the row as
    stale and keep the sweep going — not escape and abort the whole sweep with
    later rows unprocessed."""
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: True)

    fake_no_such = type("NoSuchProcess", (Exception,), {})
    fake_psutil = MagicMock()
    fake_psutil.NoSuchProcess = fake_no_such
    fake_psutil.AccessDenied = type("AccessDenied", (Exception,), {})
    fake_psutil.ZombieProcess = type("ZombieProcess", (fake_no_such,), {})
    fake_proc = MagicMock()
    fake_proc.environ.side_effect = fake_no_such("gone")
    fake_proc.cmdline.side_effect = fake_no_such("gone")
    fake_proc.create_time.side_effect = fake_no_such("gone")
    fake_psutil.Process.return_value = fake_proc
    monkeypatch.setattr("lionagi.cli.kill.psutil", fake_psutil)

    old_start = time.time() - 7200
    async with StateDB() as db:
        first = await _seed_session(db, status="running", pid=11111, started_at=old_start)
        second = await _seed_session(db, status="running", pid=22222, started_at=old_start)

    rc = await _do_kill_all_stale(threshold_seconds=3600, dry_run=False)
    assert rc == 0

    async with StateDB() as db:
        for sid in (first, second):
            assert (await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,)))[
                "status"
            ] == "cancelled", "vanished processes are stale; BOTH rows must be swept"


# cascade kill


async def test_list_running_children_show_behavior_is_unchanged(temp_db_path: Path):
    """The existing show branch continues to return only running direct plays."""
    async with StateDB() as db:
        show_id = await _seed_show(db)
        running_play_id = await _seed_play(db, show_id, status="running")
        await _seed_play(db, show_id, status="blocked")

        children = await _list_running_children(db, "show", show_id)

    assert [(kind, row["id"]) for _, kind, row in children] == [("play", running_play_id)]


async def test_do_kill_play_leaves_workers_running_and_exits_non_zero(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """A play kill cannot reach the play's workers, and reports that as a failure.

    Both rows here are shaped the way the running system shapes them: the play
    row carries no session link, and the worker session carries no play
    reference, so neither end of the pair can name the other. The kill marks
    the play row terminal, leaves the worker session running, and exits
    non-zero so a caller cannot read the run as stopped.
    """
    import lionagi.cli.kill as kill_mod
    from lionagi.cli._logging import configure_cli_logging

    configure_cli_logging(verbose=False)

    signalled_pids: list[int] = []

    def fake_terminate(pid: int, **kwargs: Any) -> str:
        signalled_pids.append(pid)
        return "sigterm"

    monkeypatch.setattr(kill_mod, "_terminate_pid", fake_terminate)

    async with StateDB() as db:
        invocation_id = await _seed_invocation(db, status="running", pid=42002)
        worker_session_id = await _seed_session(db, status="running", pid=42001)
        await db.update_session(worker_session_id, invocation_id=invocation_id)
        show_id = await _seed_show(db)
        play_id = await _seed_play(db, show_id)

    capsys.readouterr()
    rc = await _do_kill(play_id)
    captured = capsys.readouterr()

    assert rc == 1
    assert "no worker process was stopped" in captured.err.replace("\n", " ")
    # No worker was signalled: the play row has no path to either of them.
    assert signalled_pids == []
    async with StateDB() as db:
        assert (
            await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (worker_session_id,))
        )["status"] == "running"
        assert (
            await db.fetch_one("SELECT status FROM invocations WHERE id = ?", (invocation_id,))
        )["status"] == "running"
        play_status = (await db.fetch_one("SELECT status FROM plays WHERE id = ?", (play_id,)))[
            "status"
        ]
    assert play_status == "blocked"
    # The message has to name the status that was actually written. Asserting the
    # literal word here would pass just as well against a message that names a
    # status the kill never wrote, which is what it used to do.
    assert f"is marked {play_status}" in captured.err.replace("\n", " ")


async def test_list_running_children_play_returns_worker_chain_deepest_first(
    temp_db_path: Path,
):
    """A play with a recorded session resolves that session and its invocation.

    The invocation comes first: a child is signalled before the parent that
    owns it, so terminating the session never leaves its invocation orphaned.
    """
    async with StateDB() as db:
        invocation_id = await _seed_invocation(db, status="running", pid=43002)
        worker_session_id = await _seed_session(db, status="running", pid=43001)
        await db.update_session(worker_session_id, invocation_id=invocation_id)
        show_id = await _seed_show(db)
        play_id = await _seed_play(db, show_id, session_id=worker_session_id)

        children = await _list_running_children(db, "play", play_id)

    assert [(kind, row["id"]) for _, kind, row in children] == [
        ("invocation", invocation_id),
        ("session", worker_session_id),
    ]


async def test_do_kill_play_with_recorded_session_reaps_the_worker_chain(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """A play whose row records its worker session has that chain terminated.

    This is the shape the Studio show importer writes: `plays.session_id` is
    bound to the session it matched by name. Both worker pids are signalled,
    both rows go terminal, and the kill exits 0 — it did stop the work.
    """
    import lionagi.cli.kill as kill_mod
    from lionagi.cli._logging import configure_cli_logging

    configure_cli_logging(verbose=False)

    signalled_pids: list[int] = []

    def fake_terminate(pid: int, **kwargs: Any) -> str:
        signalled_pids.append(pid)
        return "sigterm"

    monkeypatch.setattr(kill_mod, "_terminate_pid", fake_terminate)

    async with StateDB() as db:
        invocation_id = await _seed_invocation(db, status="running", pid=44002)
        worker_session_id = await _seed_session(db, status="running", pid=44001)
        await db.update_session(worker_session_id, invocation_id=invocation_id)
        show_id = await _seed_show(db)
        play_id = await _seed_play(db, show_id, session_id=worker_session_id)

    capsys.readouterr()
    rc = await _do_kill(play_id)
    captured = capsys.readouterr()

    assert rc == 0
    assert signalled_pids == [44002, 44001]
    assert "no worker process was stopped" not in captured.err.replace("\n", " ")
    async with StateDB() as db:
        assert (
            await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (worker_session_id,))
        )["status"] == "cancelled"
        assert (
            await db.fetch_one("SELECT status FROM invocations WHERE id = ?", (invocation_id,))
        )["status"] == "cancelled"
        assert (await db.fetch_one("SELECT status FROM plays WHERE id = ?", (play_id,)))[
            "status"
        ] == "blocked"


async def test_do_kill_play_reaps_worker_chain_without_recursive_flag(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The play's worker chain is not gated behind `--recursive`.

    A play row carries no PID of its own, so resolving the sessions it started
    is the whole kill rather than an opt-in extra.
    """
    import lionagi.cli.kill as kill_mod

    signalled_pids: list[int] = []
    monkeypatch.setattr(
        kill_mod,
        "_terminate_pid",
        lambda pid, **kwargs: (signalled_pids.append(pid), "sigterm")[1],
    )

    async with StateDB() as db:
        worker_session_id = await _seed_session(db, status="running", pid=45001)
        show_id = await _seed_show(db)
        play_id = await _seed_play(db, show_id, session_id=worker_session_id)

    assert await _do_kill(play_id, recursive=False) == 0
    assert signalled_pids == [45001]


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
        play_id = await _seed_play(db, show_id)

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
        play_id = await _seed_play(db, show_id)

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
        # Non-zero: the play row went terminal, but its workers were not reached.
        assert await _do_kill(play_id) == 1
    finally:
        DEFAULT_TERMINAL_CALLBACKS.unregister(callback_name)

    assert len(received) == 1
    envelope = received[0]
    assert envelope.entity.kind == "play"
    assert envelope.entity.id == play_id
    assert envelope.previous_status == "running"
    assert envelope.terminal_status == "blocked"
    assert envelope.reason_code == RunReasons.CANCELLED_MANUAL_KILL


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
        # The name of this test promises the invocation goes too, and until
        # now nothing checked it: the traversal could have been dropped
        # entirely and this stayed green.
        assert (await db.fetch_one("SELECT status FROM invocations WHERE id = ?", (inv_id,)))[
            "status"
        ] == "cancelled"


async def test_do_kill_recursive_invocation_cancels_child_sessions(temp_db_path: Path):
    """The downward edge: an invocation's sessions are cancelled with it.

    `sessions.invocation_id` is read in both directions — up from a session to
    the invocation running it, and down from an invocation to the sessions it
    owns. This pins the downward half, which the upward tests do not cover.
    """
    async with StateDB() as db:
        invocation_id = await _seed_invocation(db, status="running")
        child_ids = [
            await _seed_session(db, status="running"),
            await _seed_session(db, status="running"),
        ]
        for child_id in child_ids:
            await db.update_session(child_id, invocation_id=invocation_id)

    assert await _do_kill(invocation_id, recursive=True) == 0

    async with StateDB() as db:
        invocation = await db.fetch_one(
            "SELECT status FROM invocations WHERE id = ?", (invocation_id,)
        )
        children = await db.fetch_all(
            "SELECT id, status FROM sessions WHERE invocation_id = ?", (invocation_id,)
        )

    assert invocation is not None and invocation["status"] == "cancelled"
    assert {row["id"]: row["status"] for row in children} == {
        child_id: "cancelled" for child_id in child_ids
    }


# CLI wiring smoke test


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


def _kill_help_text() -> str:
    """`li kill --help` stdout, whitespace-normalised so argparse's line
    wrapping does not decide whether a sentence is present."""
    result = subprocess.run(
        [sys.executable, "-m", "lionagi.cli", "kill", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    return " ".join(result.stdout.split())


def test_help_states_what_the_sweep_does_to_a_play():
    """The sweep's two conditions have to survive into the text people read.

    A play does not go `cancelled` — that word belongs to sessions and
    invocations — and the sweep does not act on every play: one whose row
    records no worker session is left running, because its age says nothing
    about whether its workers are gone. Help text that promises either of those
    describes a command that does not exist, and the MCP schema below is
    generated from the same strings, so a caller reading the projection gets
    whatever this says.
    """
    help_text = _kill_help_text()

    assert "is marked 'blocked'" in help_text
    assert "records no worker session is left running" in help_text
    assert "and ALL of its plays are terminal" in help_text
    # The sweep is conditional; nothing may claim it acts on every play.
    assert "cancels play and show rows once their workers are gone" not in help_text


def test_mcp_projection_carries_the_same_sweep_contract():
    """The projected schema is the same text by another route.

    An MCP caller never sees `--help`; it sees this schema, and it cannot check
    the claim against the database. Pinning the contract on both surfaces is
    what stops a correction to one of them from leaving the other promising the
    old behaviour.
    """
    golden = json.loads(
        (
            Path(__file__).resolve().parents[1] / "mcp" / "golden_projections" / "kill.json"
        ).read_text()
    )
    schema = golden["schema"]
    all_stale = schema["properties"]["all_stale"]["description"]

    assert "is marked 'blocked'" in all_stale
    assert "records no worker session is left running" in all_stale
    assert "A show is marked 'aborted' only once" in all_stale
    assert "cancels play and show rows once their workers are gone" not in schema["description"]


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


# plays and shows excluded from sweep


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


async def test_do_kill_all_stale_reports_plays_it_could_not_assess(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """A play with no recorded worker session is counted and named once.

    The sweep has no way to tell whether such a play is abandoned, so it leaves
    it alone. Saying so once per sweep, with a count in the closing line, keeps
    the operator from reading silence as "nothing was stale". A per-row message
    would read as an observation about that row rather than the structural fact
    it is.
    """
    from lionagi.cli._logging import configure_cli_logging

    configure_cli_logging(verbose=False)
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: False)

    old_start = time.time() - 7200
    async with StateDB() as db:
        show_id = await _seed_show(db, status="active")
        play_id = await _seed_play(db, show_id, status="running")
        await db.execute("UPDATE plays SET started_at = ? WHERE id = ?", (old_start, play_id))

    capsys.readouterr()
    assert await _do_kill_all_stale(threshold_seconds=3600, dry_run=False) == 0
    captured = capsys.readouterr()

    assert "skipped_unlinked_plays=1" in captured.out
    operator_output = captured.err.replace("\n", " ")
    assert "1 running play row(s) were not swept" in operator_output
    assert "record no link to the sessions they started" in operator_output

    async with StateDB() as db:
        assert (await db.fetch_one("SELECT status FROM plays WHERE id = ?", (play_id,)))[
            "status"
        ] == "running"


async def test_do_kill_all_stale_sweeps_play_whose_worker_session_is_terminal(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """A play the sweep *can* assess is swept, and is marked blocked.

    This is the case the CLI reference describes: an old play whose recorded
    worker session has already gone terminal. The persisted status is `blocked`
    — the word a play takes — not the `cancelled` a session takes.
    """
    from lionagi.cli._logging import configure_cli_logging

    configure_cli_logging(verbose=False)
    monkeypatch.setattr("lionagi.cli.kill._pid_alive", lambda pid: False)

    old_start = time.time() - 7200
    async with StateDB() as db:
        dead_session_id = await _seed_session(db, status="cancelled")
        show_id = await _seed_show(db, status="active")
        play_id = await _seed_play(db, show_id, status="running", session_id=dead_session_id)
        await db.execute("UPDATE plays SET started_at = ? WHERE id = ?", (old_start, play_id))

    capsys.readouterr()
    assert await _do_kill_all_stale(threshold_seconds=3600, dry_run=False) == 0
    captured = capsys.readouterr()

    assert "skipped_unlinked_plays=0" in captured.out
    async with StateDB() as db:
        assert (await db.fetch_one("SELECT status FROM plays WHERE id = ?", (play_id,)))[
            "status"
        ] == "blocked"


class TestTerminatePidIdentityRevalidation:
    """SIGKILL is not survivable and gives the target no chance to identify
    itself, so escalation re-checks that the pid still belongs to the process
    we meant to kill."""

    def _patch(self, monkeypatch, *, alive, identity_calls):
        from lionagi.cli import kill as kill_mod

        monkeypatch.setattr(kill_mod, "_pid_alive", lambda pid: alive(pid))
        monkeypatch.setattr(kill_mod.time, "sleep", lambda s: None)

        def _identity(pid, expected_cmd, **kw):
            identity_calls.append(pid)
            # first call (pre-SIGTERM) matches, later calls do not: the pid was
            # recycled while we waited out the grace window
            return "ours" if len(identity_calls) == 1 else "not_ours"

        monkeypatch.setattr(kill_mod, "_check_pid_identity", _identity)

    def test_recycled_pid_is_not_sigkilled(self, monkeypatch):
        from lionagi.cli import kill as kill_mod

        sent = []
        monkeypatch.setattr(kill_mod.os, "kill", lambda pid, sig: sent.append(sig))
        identity_calls: list[int] = []
        self._patch(monkeypatch, alive=lambda pid: True, identity_calls=identity_calls)

        result = kill_mod._terminate_pid(4242, expected_cmd="li agent", grace_seconds=0.01)

        assert result == "identity_mismatch"
        assert len(identity_calls) == 2, "identity must be re-checked before escalating"
        assert signal.SIGKILL not in sent, "escalated onto a recycled pid"
        assert sent == [signal.SIGTERM]

    def test_same_process_still_escalates(self, monkeypatch):
        """The re-check must not block escalation against the real target."""
        from lionagi.cli import kill as kill_mod

        sent = []
        monkeypatch.setattr(kill_mod.os, "kill", lambda pid, sig: sent.append(sig))
        monkeypatch.setattr(kill_mod, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(kill_mod.time, "sleep", lambda s: None)
        monkeypatch.setattr(kill_mod, "_check_pid_identity", lambda *a, **kw: "ours")

        result = kill_mod._terminate_pid(4242, expected_cmd="li agent", grace_seconds=0.01)

        assert result == "sigkill"
        assert sent == [signal.SIGTERM, signal.SIGKILL]


# Ambiguous short-id prefixes
#
# A prefix that matches two rows must never resolve to "whichever came first":
# `li kill` would then signal a process the caller never named.


async def _seed_session_with_id(db: StateDB, sid: str, *, pid: int | None = None) -> str:
    prog_id = str(uuid.uuid4())
    await db.create_progression(prog_id)
    await db.create_session(
        {
            "id": sid,
            "progression_id": prog_id,
            "status": "running",
            "started_at": time.time(),
            "node_metadata": {"pid": pid} if pid is not None else {},
        }
    )
    return sid


async def test_resolve_entity_rejects_ambiguous_prefix(temp_db_path: Path):
    from lionagi.cli._util import AmbiguousIdError

    async with StateDB() as db:
        first = await _seed_session_with_id(db, "abcde000-0000-0000-0000-000000000001")
        second = await _seed_session_with_id(db, "abcde111-0000-0000-0000-000000000002")

        with pytest.raises(AmbiguousIdError) as excinfo:
            await _resolve_entity(db, "abcde")

    assert set(excinfo.value.candidates) == {first, second}
    assert "abcde" in str(excinfo.value)


async def test_do_kill_ambiguous_prefix_signals_nothing_and_fails(
    temp_db_path: Path, caplog: pytest.LogCaptureFixture
):
    async with StateDB() as db:
        first = await _seed_session_with_id(db, "abcde000-0000-0000-0000-000000000001", pid=4242)
        second = await _seed_session_with_id(db, "abcde111-0000-0000-0000-000000000002", pid=4243)

    with patch("lionagi.cli.kill._kill_one") as kill_one:
        with caplog.at_level("ERROR"):
            rc = await _do_kill("abcde")

    assert rc == 1, "an ambiguous prefix must not be a success exit code"
    kill_one.assert_not_called()
    assert "ambiguous" in caplog.text
    assert first in caplog.text and second in caplog.text
