# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the shared tri-state process-liveness oracle."""

import json
import os
import socket
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
    # Patched on the socket module itself rather than through the admin module:
    # the host question is answered by recorded_pid_is_foreign in cli._util, and
    # both modules read the same module object.
    monkeypatch.setattr(socket, "gethostname", lambda: "current-host")
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


def test_an_unreadable_identity_mode_is_unknown_not_local():
    """A mode marker of the wrong type must not be read as no marker at all.

    Everything else on this row is a genuine live local process, so the two
    readings give opposite answers: absent-marker takes the local path and
    reports the run alive, while present-but-unreadable reports unknown. The
    first is a positive liveness claim about a run whose stop-and-liveness
    protocol this code does not implement.
    """
    markers = {
        "pid": os.getpid(),
        "pid_create_time": psutil.Process(os.getpid()).create_time(),
        "pid_host": socket.gethostname(),
        "pid_boot_time": psutil.boot_time(),
    }

    # Control: without a mode marker at all, this exact row is observed alive.
    assert process_liveness({"id": "s", "node_metadata": dict(markers)}, None) is True

    for unreadable in (123, {"kind": "remote"}, ["external"]):
        session = {
            "id": "s",
            "node_metadata": {**markers, "process_identity_mode": unreadable},
        }
        assert process_liveness(session, None) is None


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

    monkeypatch.setattr(socket, "gethostname", lambda: "this-host")
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

    monkeypatch.setattr(socket, "gethostname", lambda: "this-host")
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


def test_a_boot_time_that_cannot_be_read_does_not_make_a_live_process_unknown(monkeypatch):
    """A failed boot-time read leaves one check unevaluated; it is not an answer.

    Reporting unknown here is worse than never having had the check: the
    lifecycle reapers read any non-True liveness as death once the row goes
    stale, so on a machine where this read keeps failing every live session
    would eventually be reaped. The pid checks still run, which is the same
    best-effort stance the status and create-time reads already take.
    """
    monkeypatch.setattr(socket, "gethostname", lambda: "this-host")

    recorded_boot = psutil.boot_time()

    def _unreadable_boot_time():
        raise OSError("boot time unavailable")

    monkeypatch.setattr(psutil, "boot_time", _unreadable_boot_time)

    session = {
        "id": "live-session",
        "node_metadata": {
            "pid": os.getpid(),
            "pid_create_time": psutil.Process(os.getpid()).create_time(),
            "pid_host": "this-host",
            "pid_boot_time": recorded_boot,
        },
    }

    assert process_liveness(session, None, ps_snapshot="") is True


def test_a_failed_boot_time_read_still_reports_a_dead_pid_as_dead(monkeypatch):
    """The control: falling through to the pid checks means they still decide.

    Without this, returning True unconditionally on a read failure would pass
    the test above while reporting every dead session as alive.
    """
    monkeypatch.setattr(socket, "gethostname", lambda: "this-host")

    recorded_boot = psutil.boot_time()
    dead = _dead_pid()

    def _unreadable_boot_time():
        raise OSError("boot time unavailable")

    monkeypatch.setattr(psutil, "boot_time", _unreadable_boot_time)

    session = {
        "id": "dead-session",
        "node_metadata": {
            "pid": dead,
            "pid_host": "this-host",
            "pid_boot_time": recorded_boot,
        },
    }

    assert process_liveness(session, None, ps_snapshot="") is False


# ---------------------------------------------------------------------------
# The host question, asked once
#
# The oracle used to answer it with its own inline comparison while the
# reapers' foreign-row guard answered it with recorded_pid_is_foreign. Two
# implementations of one predicate disagreed about the values neither was
# written for, and the disagreement was only visible from a caller that
# consulted both.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host_value, is_foreign",
    [
        pytest.param({}, False, id="pid_host-absent-is-a-legacy-row-not-a-foreign-one"),
        pytest.param({"pid_host": ""}, False, id="empty-pid_host-records-no-host"),
        pytest.param({"pid_host": 1234}, False, id="non-string-pid_host-records-no-host"),
        pytest.param({"pid_host": None}, False, id="null-pid_host-records-no-host"),
        pytest.param({"pid_host": "definitely-another-machine"}, True, id="a-real-other-host"),
    ],
)
def test_the_guard_and_the_oracle_read_every_shape_of_pid_host_the_same_way(host_value, is_foreign):
    """The property is agreement, not either answer taken alone.

    Asserting only the oracle would let the guard drift back, and asserting
    only the guard would miss the case that actually bit: an empty pid_host
    read as "no host recorded" by the guard and as "a host that is not mine"
    by the oracle. A row sat in that gap with a live process, unprotected by
    the guard and answered unknown by the oracle, and the reapers turn a
    non-True answer into death once the row goes stale.
    """
    from lionagi.cli._util import recorded_pid_is_foreign

    meta = {
        "pid": os.getpid(),
        "pid_create_time": psutil.Process(os.getpid()).create_time(),
        **host_value,
    }

    assert recorded_pid_is_foreign(meta) is is_foreign

    liveness = process_liveness({"id": "s", "node_metadata": meta}, None, ps_snapshot="")
    # Foreign => unknown. Not foreign => this genuinely live process is seen.
    assert (liveness is None) is is_foreign
    if not is_foreign:
        assert liveness is True


def test_an_unknown_identity_mode_is_refused_before_the_host_is_consulted():
    """The order these two checks run in is what makes a malformed host safe.

    The host guard reads an unreadable ``pid_host`` as no host recorded, which
    is only defensible because a row written by something this code does not
    understand is already refused a step earlier, by its identity mode. That is
    an ordering dependency and nothing else asserts it: swap the two and a row
    announcing a foreign protocol would be judged on a local pid, with the host
    field the only thing left to catch it and unable to.

    Asserted through the caller, and against a host guard that says *not*
    foreign for the same row, so it cannot pass by the host answer changing.
    """
    from lionagi.cli._util import recorded_pid_is_foreign
    from lionagi.studio.services.admin import process_identity_is_foreign

    meta = {
        "pid": os.getpid(),
        "pid_create_time": psutil.Process(os.getpid()).create_time(),
        "process_identity_mode": "a-protocol-this-code-does-not-know",
        "pid_host": 1234,
    }

    assert recorded_pid_is_foreign(meta) is False, (
        "premise: the host guard does not catch this row, so the refusal below "
        "can only come from the identity-mode check running first"
    )
    assert process_identity_is_foreign({"id": "s", "node_metadata": meta}) is True


def test_a_pid_that_collides_with_a_live_local_one_is_rejected_by_its_creation_time():
    """The check the host marker is a fast path in front of, not a substitute for.

    A row from elsewhere whose pid happens to name a live process here is the
    failure the host marker exists to make cheap to spot. It is not the only
    thing standing in the way: the recorded creation time rejects the
    coincidence on its own, and it keeps doing so when the host field says
    nothing usable. If this stops holding, an unreadable host stops being safe
    to treat as absent.
    """
    live_pid = os.getpid()
    real_create_time = psutil.Process(live_pid).create_time()

    for host in ({}, {"pid_host": 1234}, {"pid_host": ""}):
        meta = {"pid": live_pid, "pid_create_time": real_create_time - 9999, **host}
        liveness = process_liveness({"id": "s", "node_metadata": meta}, None, ps_snapshot="")
        assert liveness is False, f"pid collision admitted as live for pid_host={host}"


def test_an_unparseable_pid_on_a_foreign_row_cannot_pick_up_a_local_one(tmp_path):
    """The artifact fallback resolves a pid against *this* machine.

    The host check used to be gated on having parsed a pid, so a row whose pid
    was unreadable skipped it entirely and then reached the fallback below,
    which reads the local process table. Another machine's row could acquire a
    local pid that way and be reported alive by a host that cannot see it.
    """
    (tmp_path / "session.pid").write_text(str(os.getpid()))  # a live LOCAL pid

    session = {
        "id": "remote-session",
        "node_metadata": {"pid": "not-a-number", "pid_host": "definitely-another-machine"},
    }

    assert process_liveness(session, tmp_path, ps_snapshot="") is None


def test_the_same_unparseable_pid_on_a_local_row_still_uses_the_artifact_fallback(tmp_path):
    """The control, and the reason the fix is a reorder rather than a removal.

    Without this, deleting the fallback outright would satisfy the test above
    while breaking every local row that relies on the pid file.
    """
    (tmp_path / "session.pid").write_text(str(os.getpid()))

    session = {
        "id": "local-session",
        "node_metadata": {"pid": "not-a-number", "pid_host": socket.gethostname()},
    }

    assert process_liveness(session, tmp_path, ps_snapshot="") is True
