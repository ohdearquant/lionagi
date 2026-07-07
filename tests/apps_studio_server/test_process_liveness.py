# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the shared tri-state process-liveness oracle."""

import json
import os
import subprocess

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
