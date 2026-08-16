# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the shared tri-state process-liveness oracle."""

import asyncio
import json
import os
import subprocess
import time
from unittest.mock import AsyncMock

import psutil
import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")
from lionagi.studio.services.admin import process_liveness  # noqa: E402


def _dead_pid() -> int:
    proc = subprocess.Popen(["/bin/sleep", "0"])  # noqa: S603
    proc.wait()
    return proc.pid


def test_pid_file_dead_pid_is_confirmed_dead(tmp_path):
    (tmp_path / "session.pid").write_text(str(_dead_pid()))
    assert process_liveness({"id": "s1"}, tmp_path, ps_snapshot="") is False


def test_pid_file_live_pid_is_alive(tmp_path):
    (tmp_path / "session.pid").write_text(str(os.getpid()))
    assert process_liveness({"id": "s1"}, tmp_path, ps_snapshot="") is True


def test_node_metadata_pid_with_matching_create_time_is_alive():
    ct = psutil.Process(os.getpid()).create_time()
    session = {
        "id": "s1",
        "node_metadata": {"pid": os.getpid(), "pid_create_time": ct},
    }
    assert process_liveness(session, None, ps_snapshot="") is True


def test_node_metadata_pid_with_mismatched_create_time_is_recycled_dead():
    session = {
        "id": "s1",
        "node_metadata": {"pid": os.getpid(), "pid_create_time": 1.0},
    }
    assert process_liveness(session, None, ps_snapshot="") is False


def test_node_metadata_accepts_json_string():
    session = {
        "id": "s1",
        "node_metadata": json.dumps({"pid": _dead_pid()}),
    }
    assert process_liveness(session, None, ps_snapshot="") is False


def test_no_pid_no_process_match_is_unknown():
    assert process_liveness({"id": "sess-xyz"}, None, ps_snapshot="1 launchd") is None


def test_no_pid_but_session_id_in_snapshot_is_alive():
    snapshot = "1234 li agent --resume sess-xyz"
    assert process_liveness({"id": "sess-xyz"}, None, ps_snapshot=snapshot) is True


@pytest.mark.parametrize("meta", [None, "not-json", {"pid": "garbage"}])
def test_unparseable_metadata_falls_through_to_unknown(meta):
    assert process_liveness({"id": "s1", "node_metadata": meta}, None, ps_snapshot="") is None


def test_node_metadata_pid_with_matching_create_time_but_zombie_status_is_dead(monkeypatch):
    """A zombie pid still resolves to _pid_is_live()==True (it exists in the
    process table, unreaped) but is not a live worker; it must read dead."""
    import lionagi.studio.services.admin as admin_mod

    ct = 42.0
    pid = os.getpid()
    monkeypatch.setattr(admin_mod, "_pid_is_live", lambda _pid: True)

    class _ZombieProcess:
        def __init__(self, _pid):
            pass

        def status(self):
            return psutil.STATUS_ZOMBIE

        def create_time(self):
            return ct

    monkeypatch.setattr(psutil, "Process", _ZombieProcess)

    session = {"id": "s1", "node_metadata": {"pid": pid, "pid_create_time": ct}}
    assert process_liveness(session, None, ps_snapshot="") is False


async def test_identity_complete_runs_page_does_not_capture_process_table(monkeypatch):
    import lionagi.studio.services.admin as admin_mod
    import lionagi.studio.services.run_tags as run_tags_mod
    import lionagi.studio.services.runs as runs_mod

    created = psutil.Process(os.getpid()).create_time()
    sessions = [
        {
            "id": f"session-{i}",
            "status": "running",
            "started_at": time.time(),
            "updated_at": time.time(),
            "node_metadata": {"pid": os.getpid(), "pid_create_time": created},
        }
        for i in range(20)
    ]
    monkeypatch.setattr(runs_mod._sessions_svc, "list_sessions", AsyncMock(return_value=sessions))
    monkeypatch.setattr(run_tags_mod, "tags_for_sessions", AsyncMock(return_value={}))
    snapshot = AsyncMock(return_value="")
    monkeypatch.setattr(admin_mod, "cached_ps_snapshot", snapshot)

    result = await runs_mod.list_runs(limit=20)

    assert len(result) == 20
    snapshot.assert_not_awaited()


async def test_explicit_nonlocal_run_is_unverifiable_without_legacy_snapshot(monkeypatch):
    import lionagi.studio.services.admin as admin_mod
    import lionagi.studio.services.run_tags as run_tags_mod
    import lionagi.studio.services.runs as runs_mod

    sessions = [
        {
            "id": "imported-session",
            "status": "running",
            "started_at": time.time(),
            "updated_at": time.time(),
            "node_metadata": {"process_identity_mode": "external"},
        }
    ]
    monkeypatch.setattr(runs_mod._sessions_svc, "list_sessions", AsyncMock(return_value=sessions))
    monkeypatch.setattr(run_tags_mod, "tags_for_sessions", AsyncMock(return_value={}))
    snapshot = AsyncMock(return_value="")
    monkeypatch.setattr(admin_mod, "cached_ps_snapshot", snapshot)

    result = await runs_mod.list_runs(limit=1)

    assert len(result) == 1
    snapshot.assert_not_awaited()


def test_process_identity_from_another_host_is_unknown(monkeypatch):
    import lionagi.studio.services.admin as admin_mod

    monkeypatch.setattr(admin_mod.socket, "gethostname", lambda: "current-host")
    session = {
        "id": "remote-session",
        "node_metadata": {
            "pid": os.getpid(),
            "pid_create_time": psutil.Process(os.getpid()).create_time(),
            "pid_host": "another-host",
            "pid_boot_time": psutil.boot_time(),
        },
    }

    assert process_liveness(session, None, ps_snapshot="") is None


def test_a_boot_time_that_drifted_within_tolerance_is_not_a_reboot(monkeypatch):
    """Clock jitter must not read as a reboot on the liveness path either.

    Boot time is re-derived from the current clock on every read, so an NTP
    step or a suspend/resume moves it on a machine that never rebooted. Process
    create time is not like that: the kernel fixed it once, so the tighter
    create-time tolerance is generous there and far too tight here. Reading
    drift as a reboot reports a healthy local session as dead, and the
    lifecycle reapers act on that answer.
    """
    import lionagi.studio.services.admin as admin_mod
    from lionagi.cli._util import BOOT_TIME_TOLERANCE

    monkeypatch.setattr(admin_mod.socket, "gethostname", lambda: "this-host")
    drift = BOOT_TIME_TOLERANCE / 2
    assert drift > 0, "a zero tolerance would make this test assert nothing"

    session = {
        "id": "drifted-session",
        "node_metadata": {
            "pid": os.getpid(),
            "pid_create_time": psutil.Process(os.getpid()).create_time(),
            "pid_host": "this-host",
            "pid_boot_time": psutil.boot_time() - drift,
        },
    }

    assert process_liveness(session, None, ps_snapshot="") is True


def test_a_boot_time_from_before_the_last_reboot_is_dead(monkeypatch):
    """The control for the tolerance: a real reboot still reads as dead.

    Without it, widening the tolerance to something absurd would pass the test
    above and the check would have stopped detecting reissued pids.
    """
    import lionagi.studio.services.admin as admin_mod

    monkeypatch.setattr(admin_mod.socket, "gethostname", lambda: "this-host")
    session = {
        "id": "pre-reboot-session",
        "node_metadata": {
            "pid": os.getpid(),
            "pid_create_time": psutil.Process(os.getpid()).create_time(),
            "pid_host": "this-host",
            "pid_boot_time": psutil.boot_time() - 86400.0,
        },
    }

    assert process_liveness(session, None, ps_snapshot="") is False
