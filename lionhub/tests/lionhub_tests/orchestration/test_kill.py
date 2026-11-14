# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"Kill service signalling, cancellation and process identity."

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import psutil
import pytest

from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons
from lionhub.orchestration.job import resolve_entity as _resolve_entity
from lionhub.orchestration.kill import (
    _BOOT_TIME_TOLERANCE,
    _REFUSED_SIGNAL_REASONS,
    NOT_STOPPED_SIGNALS,
    _cmdline_is_lionagi,
    _persist_cancel,
    _pid_alive,
    _terminate_pid,
    check_pid_identity,
    kill_one,
    list_running_children,
)
from tests._cli_logging_test_support import _restore_cli_logging as _restore_cli_logging
from tests._kill_scope_test_support import (
    _guard_runner_probes_in_kill_tests as _guard_runner_probes_in_kill_tests,
)
from tests._kill_scope_test_support import stable_boot_time as stable_boot_time
from tests._kill_test_support import _await as _await
from tests._kill_test_support import _is_zombie as _is_zombie
from tests._kill_test_support import _mock_psutil as _mock_psutil
from tests._kill_test_support import _script_create_time as _script_create_time
from tests._kill_test_support import _seed_invocation as _seed_invocation
from tests._kill_test_support import _seed_play as _seed_play
from tests._kill_test_support import _seed_session as _seed_session
from tests._kill_test_support import _seed_session_with_id as _seed_session_with_id
from tests._kill_test_support import _seed_show as _seed_show
from tests._kill_test_support import _spawn_marked_child as _spawn_marked_child
from tests._kill_test_support import temp_db_path as temp_db_path

_RECORDS_ITS_MARKERS_UNDER_SLEW = (
    "import json, sys, time\n"
    "import psutil\n"
    "psutil.boot_time = lambda: 1_700_000_000.0\n"
    "from lionagi.ln._proc import current_pid_markers\n"
    "psutil._psplatform.INIT_BOOT_TIME += 5.0\n"
    "sys.stdout.write(json.dumps(current_pid_markers()) + '\\n')\n"
    "sys.stdout.flush()\n"
    "time.sleep(30)\n"
)


def test_runner_probe_guard_rejects_unconfigured_pid():
    from lionhub.orchestration.kill import _runner_process_create_time

    with pytest.raises(AssertionError, match="unconfigured runner process probe: 4242"):
        _runner_process_create_time(4242)


def test_runner_probe_guard_does_not_replace_shared_process_probe(monkeypatch):
    from lionagi.ln import _proc

    fake_process = MagicMock()
    fake_process.status.return_value = psutil.STATUS_RUNNING
    _script_create_time(fake_process, 100.0)
    # This test exercises the shared probe, not psutil reader selection. Pin
    # the reader so a first call cannot inspect the Process double as a class.
    monkeypatch.setattr(_proc, "_read_raw_create_time", lambda proc: proc.create_time())
    monkeypatch.setattr(psutil, "Process", lambda pid: fake_process)

    assert _proc.process_create_time(4242) == ("found", 100.0)


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

    monkeypatch.setattr("lionhub.orchestration.kill._pid_alive", fake_alive)
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
    patched_pid_alive = lambda pid: True
    monkeypatch.setattr("lionhub.orchestration.kill._pid_alive", patched_pid_alive)
    monkeypatch.setattr("os.kill", fake_kill)

    result = _terminate_pid(42, grace_seconds=0.05)  # very short grace
    assert result == "sigkill"
    sigs_sent = [sig for _, sig in kill_calls]
    assert _signal.SIGTERM in sigs_sent
    assert _signal.SIGKILL in sigs_sent


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
    patched_pid_alive = lambda pid: True
    monkeypatch.setattr("lionhub.orchestration.kill._pid_alive", patched_pid_alive)
    monkeypatch.setattr("os.kill", lambda pid, sig: kill_calls.append((pid, sig)))

    # Mock psutil with a process whose cmdline does NOT contain "lionagi".
    fake_psutil = MagicMock()
    fake_psutil.LINUX = False
    fake_proc = MagicMock()
    fake_proc.cmdline.return_value = ["/usr/bin/python3", "unrelated_script.py"]
    _script_create_time(fake_proc, 100.0)
    fake_psutil.Process.return_value = fake_proc
    fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    fake_psutil.AccessDenied = type("AccessDenied", (Exception,), {})
    # Mirrors psutil: ZombieProcess is a NoSuchProcess subclass, so a double
    # that omits it lets the code under test claim a distinction it never made.
    fake_psutil.ZombieProcess = type("ZombieProcess", (fake_psutil.NoSuchProcess,), {})
    monkeypatch.setattr("lionhub.orchestration.kill.psutil", fake_psutil)

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
    patched_pid_alive = lambda pid: True
    monkeypatch.setattr("lionhub.orchestration.kill._pid_alive", patched_pid_alive)
    monkeypatch.setattr("os.kill", lambda pid, sig: kill_calls.append((pid, sig)))

    fake_psutil = MagicMock()
    fake_proc = MagicMock()
    fake_proc.cmdline.return_value = ["/usr/bin/python3", "-m", "lionhub.serve.cli.main"]
    _script_create_time(fake_proc, 100.0)
    fake_psutil.Process.return_value = fake_proc
    fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    fake_psutil.AccessDenied = type("AccessDenied", (Exception,), {})
    # Mirrors psutil: ZombieProcess is a NoSuchProcess subclass, so a double
    # that omits it lets the code under test claim a distinction it never made.
    fake_psutil.ZombieProcess = type("ZombieProcess", (fake_psutil.NoSuchProcess,), {})
    monkeypatch.setattr("lionhub.orchestration.kill.psutil", fake_psutil)

    # A durable create_time marker must be present for a cmdline match to
    # authorize the kill (no session id and no create_time is "unverifiable").
    result = _terminate_pid(
        42, grace_seconds=0.01, expected_cmd="lionagi", expected_create_time=100.0
    )
    # SIGTERM must have been sent
    assert any(sig == __import__("signal").SIGTERM for _, sig in kill_calls)
    assert result in ("sigterm", "sigkill")


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


def test_identity_rejects_path_substring(monkeypatch: pytest.MonkeyPatch):
    """An unrelated process that only *mentions* lionagi in a path arg is rejected.

    The reported false positive: ``vim /workspace/lionagi/README.md``.
    A substring match would signal this recycled PID; an exact-token match must not.

    ``expected_create_time`` is required and matched to the fake process's
    create_time: without it, `_check_pid_identity` refuses as "unverifiable"
    before `_cmdline_is_lionagi` ever runs, and this test would pass for that
    unrelated reason regardless of the cmdline predicate.
    """
    kill_calls = _mock_psutil(
        monkeypatch,
        cmdline=["/usr/bin/vim", "/workspace/lionagi/README.md"],
    )
    result = _terminate_pid(
        42, grace_seconds=0.1, expected_cmd="lionagi", expected_create_time=100.0
    )
    assert result == "identity_mismatch"
    assert kill_calls == [], "must not signal a process that only has lionagi in a path"


def test_identity_accepts_dash_m_module(monkeypatch: pytest.MonkeyPatch):
    """``python -m lionhub.serve.cli.main`` is a genuine invocation and is accepted."""
    _mock_psutil(monkeypatch, cmdline=["/usr/bin/python3", "-m", "lionhub.serve.cli.main"])
    assert check_pid_identity(42, "lionagi", expected_create_time=100.0) == "ours"


def test_identity_accepts_li_entrypoint(monkeypatch: pytest.MonkeyPatch):
    """The ``li`` console-script entrypoint is accepted by executable basename."""
    _mock_psutil(monkeypatch, cmdline=["/opt/venv/bin/li", "kill", "abc123"])
    assert check_pid_identity(42, "lionagi", expected_create_time=100.0) == "ours"


def test_identity_accepts_shebang_launched_li(monkeypatch: pytest.MonkeyPatch):
    """Shebang-launched li: argv[0]=python3, argv[1]=.../bin/li — must be accepted."""
    _mock_psutil(
        monkeypatch,
        cmdline=["/opt/.venv/bin/python3", "/opt/.venv/bin/li", "play", "abc123"],
    )
    assert check_pid_identity(42, "lionagi", expected_create_time=100.0) == "ours"


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
    assert check_pid_identity(42, "lionagi", expected_create_time=100.0) == "ours"


def test_identity_rejects_foreign_script_with_li_in_path(monkeypatch: pytest.MonkeyPatch):
    """A non-lionagi script whose path contains 'li' must not be accepted."""
    _mock_psutil(
        monkeypatch,
        cmdline=["/usr/bin/python3", "/usr/local/bin/olia-tool", "run"],
    )
    assert check_pid_identity(42, "lionagi", expected_create_time=100.0) == "not_ours"


def test_identity_session_marker_match(monkeypatch: pytest.MonkeyPatch):
    """A matching LIONAGI_SESSION_ID env marker is a definitive match."""
    _mock_psutil(
        monkeypatch,
        cmdline=["/usr/bin/python3", "-m", "lionhub.serve.cli"],
        environ={"LIONAGI_SESSION_ID": "run-123"},
    )
    assert check_pid_identity(42, "lionagi", expected_session_id="run-123") == "ours"


def test_identity_session_marker_mismatch_rejected(monkeypatch: pytest.MonkeyPatch):
    """A *different* session marker means another lionagi run holds this PID — reject.

    Even though the cmdline looks like lionagi, the recycled PID belongs to a
    different run, so the kill must be skipped (CWE-362).
    """
    _mock_psutil(
        monkeypatch,
        cmdline=["/usr/bin/python3", "-m", "lionhub.serve.cli"],
        environ={"LIONAGI_SESSION_ID": "other-run"},
    )
    assert check_pid_identity(42, "lionagi", expected_session_id="run-123") == "not_ours"


def test_identity_absent_marker_requires_create_time_match(monkeypatch: pytest.MonkeyPatch):
    """Session expected + no env marker: needs create_time AND lionagi cmdline.

    A lionagi-looking cmdline cannot distinguish THIS run from a different
    concurrent run that recycled the PID, and a create_time match alone could be
    a recycled PID that started inside the tolerance. Without the env marker,
    both must hold; otherwise skip the kill.
    """
    _mock_psutil(
        monkeypatch,
        cmdline=["/usr/bin/python3", "-m", "lionhub.serve.cli"],
        environ={},
        create_time=500.0,
    )
    # No create_time recorded → cannot prove this run → skip.
    assert check_pid_identity(42, "lionagi", expected_session_id="run-123") == "unverifiable"
    # create_time matches AND cmdline is lionagi → positively identified.
    assert (
        check_pid_identity(42, "lionagi", expected_session_id="run-123", expected_create_time=500.0)
        == "ours"
    )
    # create_time differs → recycled PID → skip.
    assert (
        check_pid_identity(42, "lionagi", expected_session_id="run-123", expected_create_time=1.0)
        == "not_ours"
    )


def test_identity_absent_marker_rejects_nonlionagi_cmdline(monkeypatch: pytest.MonkeyPatch):
    """No marker + matching create_time but a non-lionagi cmdline → reject.

    Guards the recycled-PID case where an unrelated process started within the
    create_time tolerance: create_time alone must not authorize the kill.
    """
    _mock_psutil(
        monkeypatch,
        cmdline=["/usr/bin/vim", "/workspace/lionagi/README.md"],
        environ={},
        create_time=500.0,
    )
    assert (
        check_pid_identity(42, "lionagi", expected_session_id="run-123", expected_create_time=500.0)
        == "not_ours"
    )


def test_identity_create_time_mismatch_rejected(monkeypatch: pytest.MonkeyPatch):
    """create_time is a tight fingerprint: only a sub-tolerance match is accepted.

    Same host/kernel → create_time is reproducible to sub-tick precision, so the
    tolerance is ~10ms. A 0.5s difference is a *different* process and must be
    rejected; only a near-exact match (within tick rounding) is accepted.
    """
    _mock_psutil(
        monkeypatch,
        cmdline=["/usr/bin/python3", "-m", "lionhub.serve.cli"],
        create_time=100.0,
    )
    assert check_pid_identity(42, "lionagi", expected_create_time=999.0) == "not_ours"
    # 0.5s apart → different process → reject (was accepted under the old 2s gate).
    assert check_pid_identity(42, "lionagi", expected_create_time=100.5) == "not_ours"
    # within tick-rounding tolerance → accepted.
    assert check_pid_identity(42, "lionagi", expected_create_time=100.05) == "ours"


def test_identity_no_durable_marker_refuses_cmdline_only_authorization(
    monkeypatch: pytest.MonkeyPatch,
):
    """No session id AND no create_time recorded: cmdline shape alone must not authorize.

    A record with neither identity field carries nothing that distinguishes the
    live process at this pid from any other process that merely looks like a
    lionagi invocation. Falling back to `_cmdline_is_lionagi` here would let a
    same-looking, unrelated process be signalled.
    """
    _mock_psutil(monkeypatch, cmdline=["/usr/bin/python3", "-m", "lionhub.serve.cli"])
    assert check_pid_identity(42, "lionagi") == "unverifiable"
    assert (
        check_pid_identity(42, "lionagi", expected_session_id=None, expected_create_time=None)
        == "unverifiable"
    )


def test_a_launch_record_still_identifies_its_process_across_boot_time_slew(monkeypatch):
    """The start time a kill checks was recorded by a different process.

    psutil adjusts ``Process.create_time()`` on macOS by a boot-time value it
    freezes at its own import, so two processes read the same live pid through
    different adjustments once the boot clock has slewed between their imports.
    The child records its markers under a value moved by five seconds and this
    process checks them under one moved by ten: the record must still identify
    the child, which holds only when both sides read the unadjusted start time.
    """
    psplatform = psutil._psplatform
    if not hasattr(psplatform, "INIT_BOOT_TIME"):
        pytest.skip("this platform's psutil backend has no boot-time adjustment")

    session_id = f"slew-{uuid.uuid4().hex}"
    child = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", _RECORDS_ITS_MARKERS_UNDER_SLEW],
        env={**os.environ, "LIONAGI_SESSION_ID": session_id},
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        markers = json.loads(child.stdout.readline())
        assert markers["pid"] == child.pid

        monkeypatch.setattr(psplatform, "INIT_BOOT_TIME", psplatform.INIT_BOOT_TIME + 10.0)
        verdict = check_pid_identity(
            child.pid,
            "lionagi",
            expected_session_id=session_id,
            expected_create_time=markers["pid_create_time"],
        )
        assert verdict == "ours"
    finally:
        child.kill()
        child.wait(timeout=5)
        child.stdout.close()


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
            cmdline=["/usr/bin/python3", "-m", "lionhub.serve.cli"],
            create_time=999.0,
        )

        row = db._row_to_dict(await db.fetch_one("SELECT * FROM sessions WHERE id = ?", (sid,)))
        result = await kill_one(db, "session", sid, row, user_reason="")
        assert result["signal"] == "identity_mismatch"

        assert (await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,)))[
            "status"
        ] == "running", "must not cancel a recycled PID"


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


async def test_kill_one_no_pid(temp_db_path: Path):
    """Entity without a PID: no OS signal, but DB updated."""
    async with StateDB() as db:
        sid = await _seed_session(db, status="running", pid=None)
        resolved = await _resolve_entity(db, sid)
        assert resolved is not None
        _, _, row = resolved

        result = await kill_one(db, "session", sid, row, user_reason="test")
        assert result["signal"] == "no_pid"

        assert (await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,)))[
            "status"
        ] == "cancelled"


async def test_kill_one_refuses_an_in_process_run(temp_db_path: Path):
    """A run hosted inside a long-lived server has no process of its own to signal, so recording a cancellation here would falsely report a stop that did not happen."""
    async with StateDB() as db:
        sid = await _seed_session(
            db,
            status="running",
            extra_meta={
                "process_identity_mode": "in_process",
                "host_pid": os.getpid(),
            },
        )
        resolved = await _resolve_entity(db, sid)
        assert resolved is not None
        _, _, row = resolved

        result = await kill_one(db, "session", sid, row, user_reason="test")

        # Checked before the signal, deliberately: without it the row would read "cancelled"
        # while the workflow keeps executing.
        after = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,))
        assert after["status"] == "running"

        # Nothing written to history either, so no reader downstream can
        # conclude a cancellation happened.
        tr = await db.fetch_one(
            "SELECT COUNT(*) AS n FROM status_transitions "
            "WHERE entity_id = ? AND status = 'cancelled'",
            (sid,),
        )
        assert tr["n"] == 0

        assert result["signal"] == "in_process"
        assert result["pid"] is None


async def test_kill_one_refuses_a_mode_marker_that_is_not_a_string(temp_db_path: Path):
    """A marker of the wrong type is an unknown mode, not a missing one — reading it as missing would let a foreign row get a cancellation written for it."""
    async with StateDB() as db:
        sid = await _seed_session(db, status="running", extra_meta={"process_identity_mode": 123})
        resolved = await _resolve_entity(db, sid)
        assert resolved is not None
        _, _, row = resolved

        result = await kill_one(db, "session", sid, row, user_reason="test")

        assert result["signal"] == "foreign_mode"
        after = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,))
        assert after["status"] == "running"


async def test_kill_one_still_cancels_a_local_run_without_a_pid(temp_db_path: Path):
    """The in-process guard keys on identity mode, not a missing pid — a pid-less local run must keep working, or the guard would silently widen to every pid-less row."""
    async with StateDB() as db:
        sid = await _seed_session(
            db, status="running", extra_meta={"process_identity_mode": "local"}
        )
        resolved = await _resolve_entity(db, sid)
        assert resolved is not None
        _, _, row = resolved

        result = await kill_one(db, "session", sid, row, user_reason="test")
        assert result["signal"] == "no_pid"

        after = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,))
        assert after["status"] == "cancelled"


async def test_kill_one_with_dead_pid(temp_db_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Entity with a dead PID: _terminate_pid returns 'already_dead'."""
    monkeypatch.setattr(
        "lionhub.orchestration.kill._terminate_pid", lambda pid, **kw: "already_dead"
    )

    async with StateDB() as db:
        sid = await _seed_session(db, status="running", pid=999999999)
        # Use _resolve_entity to get the row with JSON columns decoded.
        resolved = await _resolve_entity(db, sid)
        assert resolved is not None
        _, _, row = resolved

        result = await kill_one(db, "session", sid, row, user_reason="")
        assert result["signal"] == "already_dead"

        assert (await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,)))[
            "status"
        ] == "cancelled"


async def test_kill_one_force_kill_uses_force_kill_reason(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When SIGKILL is needed, CANCELLED_FORCE_KILL reason code is written."""
    monkeypatch.setattr("lionhub.orchestration.kill._terminate_pid", lambda pid, **kw: "sigkill")

    async with StateDB() as db:
        sid = await _seed_session(db, status="running", pid=12345)
        resolved = await _resolve_entity(db, sid)
        assert resolved is not None
        _, _, row = resolved

        await kill_one(db, "session", sid, row, user_reason="")

        tr = await db.fetch_one(
            "SELECT reason_code FROM status_transitions "
            "WHERE entity_id = ? AND previous_status = 'running' AND status = 'cancelled'",
            (sid,),
        )
        assert tr["reason_code"] == RunReasons.CANCELLED_FORCE_KILL


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


async def test_resolve_entity_rejects_ambiguous_prefix(temp_db_path: Path):
    from lionhub.orchestration.job import AmbiguousIdError

    async with StateDB() as db:
        first = await _seed_session_with_id(db, "abcde000-0000-0000-0000-000000000001")
        second = await _seed_session_with_id(db, "abcde111-0000-0000-0000-000000000002")

        with pytest.raises(AmbiguousIdError) as excinfo:
            await _resolve_entity(db, "abcde")

    assert set(excinfo.value.candidates) == {first, second}
    assert "abcde" in str(excinfo.value)


async def test_list_running_children_show_behavior_is_unchanged(temp_db_path: Path):
    """The existing show branch continues to return only running direct plays."""
    async with StateDB() as db:
        show_id = await _seed_show(db)
        running_play_id = await _seed_play(db, show_id, status="running")
        await _seed_play(db, show_id, status="blocked")

        children = await list_running_children(db, "show", show_id)

    assert [(kind, row["id"]) for _, kind, row in children] == [("play", running_play_id)]


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

        children = await list_running_children(db, "play", play_id)

    assert [(kind, row["id"]) for _, kind, row in children] == [
        ("invocation", invocation_id),
        ("session", worker_session_id),
    ]


class TestTerminatePidIdentityRevalidation:
    """SIGKILL is not survivable and gives the target no chance to identify
    itself, so escalation re-checks that the pid still belongs to the process
    we meant to kill."""

    def _patch(self, monkeypatch, *, alive, identity_calls):
        import lionhub.orchestration.kill as kill_service
        from lionhub.serve.cli import kill as kill_mod

        patched_pid_alive = lambda pid: alive(pid)
        monkeypatch.setattr(kill_mod, "_pid_alive", patched_pid_alive)
        monkeypatch.setattr(kill_service, "_pid_alive", patched_pid_alive)
        monkeypatch.setattr(kill_mod.time, "sleep", lambda s: None)

        def _identity(pid, expected_cmd, **kw):
            identity_calls.append(pid)
            # first call (pre-SIGTERM) matches, later calls do not: the pid was
            # recycled while we waited out the grace window
            return "ours" if len(identity_calls) == 1 else "not_ours"

        monkeypatch.setattr(kill_mod, "check_pid_identity", _identity)
        monkeypatch.setattr(kill_service, "check_pid_identity", _identity)

    def test_recycled_pid_is_not_sigkilled(self, monkeypatch):
        import lionhub.orchestration.kill as kill_service
        from lionhub.serve.cli import kill as kill_mod

        sent = []
        monkeypatch.setattr(kill_service.os, "kill", lambda pid, sig: sent.append(sig))
        identity_calls: list[int] = []
        self._patch(monkeypatch, alive=lambda pid: True, identity_calls=identity_calls)

        result = kill_service._terminate_pid(4242, expected_cmd="li agent", grace_seconds=0.01)

        assert result == "identity_mismatch"
        assert len(identity_calls) == 2, "identity must be re-checked before escalating"
        assert signal.SIGKILL not in sent, "escalated onto a recycled pid"
        assert sent == [signal.SIGTERM]

    def test_same_process_still_escalates(self, monkeypatch):
        """The re-check must not block escalation against the real target."""
        import lionhub.orchestration.kill as kill_service
        from lionhub.serve.cli import kill as kill_mod

        sent = []
        monkeypatch.setattr(kill_service.os, "kill", lambda pid, sig: sent.append(sig))
        patched_pid_alive = lambda pid: True
        monkeypatch.setattr(kill_mod, "_pid_alive", patched_pid_alive)
        monkeypatch.setattr(kill_service, "_pid_alive", patched_pid_alive)
        monkeypatch.setattr(kill_mod.time, "sleep", lambda s: None)
        patched_check_pid_identity = lambda *a, **kw: "ours"
        monkeypatch.setattr(kill_mod, "check_pid_identity", patched_check_pid_identity)
        monkeypatch.setattr(kill_service, "check_pid_identity", patched_check_pid_identity)

        result = kill_service._terminate_pid(4242, expected_cmd="li agent", grace_seconds=0.01)

        assert result == "sigkill"
        assert sent == [signal.SIGTERM, signal.SIGKILL]


async def test_kill_refuses_a_pid_recorded_on_another_host(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A row from another machine must not have its pid signalled here — the number exists in this host's pid space too and belongs to something unrelated."""
    signalled: list[int] = []

    def _fake_terminate(pid, **kw):
        signalled.append(pid)
        return "sigterm"

    monkeypatch.setattr("lionhub.orchestration.kill._terminate_pid", _fake_terminate)
    monkeypatch.setattr("lionhub.orchestration.job.socket.gethostname", lambda: "this-host")

    async with StateDB() as db:
        sid = await _seed_session(
            db,
            status="running",
            pid=os.getpid(),
            extra_meta={"pid_host": "some-other-host"},
        )
        resolved = await _resolve_entity(db, sid)
        assert resolved is not None
        _, _, row = resolved

        result = await kill_one(db, "session", sid, row, user_reason="")

    assert signalled == [], "no signal may be sent to a pid recorded on another host"
    assert result["signal"] == "host_mismatch"
    assert result["signal"] in NOT_STOPPED_SIGNALS

    async with StateDB() as db:
        after = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,))
    assert after["status"] == "running", (
        "refusing to signal must also refuse to write a cancellation: the row "
        "would otherwise claim a stop that never happened"
    )


async def test_kill_refuses_a_pid_recorded_before_this_machine_rebooted(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Same host, earlier boot: the pid numbers were reissued from scratch."""
    monkeypatch.setattr(psutil, "LINUX", False)
    signalled: list[int] = []

    def _fake_terminate(pid, **kw):
        signalled.append(pid)
        return "sigterm"

    monkeypatch.setattr("lionhub.orchestration.kill._terminate_pid", _fake_terminate)

    async with StateDB() as db:
        sid = await _seed_session(
            db,
            status="running",
            pid=os.getpid(),
            extra_meta={
                "pid_host": socket.gethostname(),
                "pid_boot_time": psutil.boot_time() - 86400.0,
            },
        )
        resolved = await _resolve_entity(db, sid)
        assert resolved is not None
        _, _, row = resolved

        result = await kill_one(db, "session", sid, row, user_reason="")

    assert signalled == []
    assert result["signal"] == "boot_mismatch"

    async with StateDB() as db:
        after = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,))
    assert after["status"] == "running"


async def test_a_boot_time_that_drifted_within_tolerance_is_not_a_reboot(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Clock jitter must not read as a reboot — without tolerance, a machine that never rebooted would quietly stop killing its own processes."""
    signalled: list[int] = []

    def _fake_terminate(pid, **kw):
        signalled.append(pid)
        return "sigterm"

    monkeypatch.setattr("lionhub.orchestration.kill._terminate_pid", _fake_terminate)

    drift = _BOOT_TIME_TOLERANCE / 2
    assert drift > 0, "a zero tolerance would make this test assert nothing"

    async with StateDB() as db:
        sid = await _seed_session(
            db,
            status="running",
            pid=os.getpid(),
            extra_meta={
                "pid_host": socket.gethostname(),
                "pid_boot_time": psutil.boot_time() - drift,
            },
        )
        resolved = await _resolve_entity(db, sid)
        assert resolved is not None
        _, _, row = resolved

        result = await kill_one(db, "session", sid, row, user_reason="")

    assert signalled == [os.getpid()]
    assert result["signal"] == "sigterm"


async def test_kill_still_signals_when_host_and_boot_agree(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The control for the two refusals above — without it, an unconditional refusal would pass both and hide that `li kill` had stopped killing anything."""
    signalled: list[int] = []

    def _fake_terminate(pid, **kw):
        signalled.append(pid)
        return "sigterm"

    monkeypatch.setattr("lionhub.orchestration.kill._terminate_pid", _fake_terminate)

    async with StateDB() as db:
        sid = await _seed_session(
            db,
            status="running",
            pid=os.getpid(),
            extra_meta={
                "pid_host": socket.gethostname(),
                "pid_boot_time": psutil.boot_time(),
            },
        )
        resolved = await _resolve_entity(db, sid)
        assert resolved is not None
        _, _, row = resolved

        result = await kill_one(db, "session", sid, row, user_reason="")

    assert signalled == [os.getpid()]
    assert result["signal"] == "sigterm"

    async with StateDB() as db:
        after = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,))
    assert after["status"] == "cancelled"


async def test_kill_still_signals_a_row_that_recorded_no_host_at_all(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Rows written before these markers existed stay killable — not knowing where a pid came from is not evidence it's foreign, and failing closed would make them permanently uncancellable."""
    signalled: list[int] = []

    def _fake_terminate(pid, **kw):
        signalled.append(pid)
        return "sigterm"

    monkeypatch.setattr("lionhub.orchestration.kill._terminate_pid", _fake_terminate)
    monkeypatch.setattr("lionhub.orchestration.job.socket.gethostname", lambda: "this-host")

    async with StateDB() as db:
        sid = await _seed_session(db, status="running", pid=4242)
        resolved = await _resolve_entity(db, sid)
        assert resolved is not None
        _, _, row = resolved

        result = await kill_one(db, "session", sid, row, user_reason="")

    assert signalled == [4242]
    assert result["signal"] == "sigterm"


async def test_kill_refuses_a_runtime_this_cli_does_not_manage(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An identity mode this code does not know has its stop protocol elsewhere — even with host, boot, and pid all agreeing, the pid still may not be signalled."""
    signalled: list[int] = []

    def _fake_terminate(pid, **kw):
        signalled.append(pid)
        return "sigterm"

    monkeypatch.setattr("lionhub.orchestration.kill._terminate_pid", _fake_terminate)
    monkeypatch.setattr("lionhub.orchestration.job.socket.gethostname", lambda: "this-host")

    async with StateDB() as db:
        sid = await _seed_session(
            db,
            status="running",
            pid=os.getpid(),
            extra_meta={
                "pid_host": "this-host",
                "process_identity_mode": "external",
            },
        )
        resolved = await _resolve_entity(db, sid)
        assert resolved is not None
        _, _, row = resolved

        result = await kill_one(db, "session", sid, row, user_reason="")

    assert signalled == [], "a run this CLI does not manage must not be signalled"
    assert result["signal"] == "foreign_mode"
    assert result["signal"] in NOT_STOPPED_SIGNALS

    async with StateDB() as db:
        after = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,))
    assert after["status"] == "running", (
        "refusing to signal must also refuse to write a cancellation"
    )


async def test_kill_refuses_an_unmanaged_runtime_that_recorded_no_pid(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The row with no pid is the dangerous one, not the safe one — with the identity check inside the pid branch, a pid-less row fell through to a false cancellation."""
    monkeypatch.setattr("lionhub.orchestration.job.socket.gethostname", lambda: "this-host")

    async with StateDB() as db:
        sid = await _seed_session(
            db,
            status="running",
            extra_meta={"process_identity_mode": "external"},
        )
        resolved = await _resolve_entity(db, sid)
        assert resolved is not None
        _, _, row = resolved

        result = await kill_one(db, "session", sid, row, user_reason="")

    async with StateDB() as db:
        after = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,))
        tr = await db.fetch_one(
            "SELECT COUNT(*) AS n FROM status_transitions "
            "WHERE entity_id = ? AND status = 'cancelled'",
            (sid,),
        )

    assert after["status"] == "running"
    assert tr["n"] == 0, "no cancellation may reach history either"
    assert result["signal"] == "foreign_mode"


async def test_kill_refuses_a_foreign_host_row_that_recorded_no_pid(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The same hole on the host marker: no pid meant no host check."""
    monkeypatch.setattr("lionhub.orchestration.job.socket.gethostname", lambda: "this-host")

    async with StateDB() as db:
        sid = await _seed_session(
            db,
            status="running",
            extra_meta={"pid_host": "some-other-host"},
        )
        resolved = await _resolve_entity(db, sid)
        assert resolved is not None
        _, _, row = resolved

        result = await kill_one(db, "session", sid, row, user_reason="")

    async with StateDB() as db:
        after = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,))

    assert after["status"] == "running"
    assert result["signal"] == "host_mismatch"


async def test_kill_refuses_a_row_whose_identity_mode_is_an_explicit_null(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An explicit null is a marker that was written, not one that is missing — reading the value instead of the key's presence collapses the two and lets a null-marked row get a false cancellation."""
    monkeypatch.setattr("lionhub.orchestration.job.socket.gethostname", lambda: "this-host")

    async with StateDB() as db:
        sid = await _seed_session(
            db,
            status="running",
            extra_meta={"process_identity_mode": None},
        )
        resolved = await _resolve_entity(db, sid)
        assert resolved is not None
        _, _, row = resolved

        result = await kill_one(db, "session", sid, row, user_reason="")

    async with StateDB() as db:
        after = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (sid,))
        tr = await db.fetch_one(
            "SELECT COUNT(*) AS n FROM status_transitions "
            "WHERE entity_id = ? AND status = 'cancelled'",
            (sid,),
        )

    assert result["signal"] == "foreign_mode"
    assert after["status"] == "running"
    assert tr["n"] == 0, "no cancellation may reach history either"


def test_every_refusal_reason_is_a_not_stopped_signal():
    """A refusal writes no cancellation, so callers must never read it as a stop."""
    assert NOT_STOPPED_SIGNALS == frozenset(_REFUSED_SIGNAL_REASONS)
    assert NOT_STOPPED_SIGNALS == {
        "identity_mismatch",
        "identity_unverifiable",
        "identity_unverifiable_after_sigterm",
        "in_process",
        "host_mismatch",
        "boot_mismatch",
        "boot_unverifiable",
        "foreign_mode",
        "permission_denied",
        "no_recorded_process",
        "no_recorded_identity",
        "imported_no_process",
    }


@pytest.mark.parametrize(
    ("cmdline", "expected"),
    [
        (["/usr/bin/python3", "-m", "lionhub.serve.cli"], True),
        (["/usr/bin/python3", "-m", "lionhub.serve.cli.main", "o", "flow"], True),
        # The CLI under the name it had before it moved to lionhub, kept through 1.x.
        (["/usr/bin/python3", "-m", "lionagi.cli"], True),
        (["/usr/bin/python3", "-m", "lionagi.cli.main", "o", "flow"], True),
        (["/usr/bin/python3", "-m", "lionhub.serve.clix"], False),
        (["/usr/bin/python3", "-m", "lionhub.serve.mcp"], False),
        (["/usr/bin/python3", "-m", "lionhub"], False),
        (["/usr/bin/python3", "lionhub.serve.cli"], False),
    ],
)
def test_cmdline_recognises_the_cli_under_both_module_names(cmdline, expected):
    assert _cmdline_is_lionagi(cmdline, "lionagi") is expected
