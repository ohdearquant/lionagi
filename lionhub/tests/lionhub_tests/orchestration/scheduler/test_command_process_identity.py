# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail.com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import signal
from collections.abc import Awaitable, Callable
from types import SimpleNamespace

import psutil
import pytest

from lionagi.ln import _proc
from lionhub.orchestration.scheduler import subprocess as scheduler_process
from lionhub.orchestration.scheduler.engine import SchedulerEngine
from lionhub.state.db import StateDB
from lionhub.state.reasons import RunReasons
from tests._scheduler_claims import fire_with_claim

_RUN_ID = "scheduled-command-run"
_OPERATOR_SUMMARY = "Stopped by operator."


class _CommandChild:
    pid = 4024242
    started = 123.5
    stdout = None
    stderr = None

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.group_present = True
        self.identity_available = True
        self.before_publish: Callable[[], Awaitable[None]] | None = None
        self.after_publish: Callable[[], Awaitable[None]] | None = None
        self.invocation_id = ""
        self.marker = ""
        self.group_signals: list[signal.Signals] = []
        self.child_signals: list[signal.Signals] = []
        self.waits = 0

    async def spawn(self, *argv, env, start_new_session, **_kwargs):
        assert argv == ("probe-cmd",)
        assert start_new_session is True
        self.invocation_id = env["LIONAGI_INVOCATION_ID"]
        self.marker = env[_proc._PROCESS_GROUP_MARKER]
        assert self.marker
        if self.before_publish is not None:
            await self.before_publish()
        return self

    def observe_member(self, pid, pgid, *, marker_var):
        assert pid == pgid == self.pid
        assert marker_var == _proc._PROCESS_GROUP_MARKER
        if self.identity_available:
            return "found", (pid, self.started, self.marker, True)
        return "gone", None

    def capture_witnesses(self, proc, *, expected_leader_created, expected_marker):
        assert proc is self
        assert expected_leader_created == self.started
        assert expected_marker == self.marker
        return ((self.pid, self.started),)

    def signal_group(self, pgid, witnesses, sig):
        assert pgid == self.pid
        if not witnesses:
            return "unverifiable"
        assert witnesses == ((self.pid, self.started),)
        self.group_signals.append(sig)
        self.group_present = False
        return "sent"

    def group_has_members(self, pgid):
        assert pgid == self.pid
        return self.group_present

    def terminate(self) -> None:
        if self.returncode is not None:
            raise ProcessLookupError
        self.child_signals.append(signal.SIGTERM)
        self.returncode = -signal.SIGTERM

    def kill(self) -> None:
        self.child_signals.append(signal.SIGKILL)
        self.returncode = -signal.SIGKILL

    async def wait(self) -> int:
        self.waits += 1
        if self.after_publish is not None:
            await self.after_publish()
        if self.returncode is None:
            self.returncode = 0
        self.group_present = False
        return self.returncode


@pytest.fixture
def command_child(monkeypatch, tmp_path):
    child = _CommandChild()
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "probe-cmd")
    monkeypatch.setattr("lionhub.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(scheduler_process.asyncio, "create_subprocess_exec", child.spawn)
    monkeypatch.setattr(_proc, "pinned_member", child.observe_member)
    monkeypatch.setattr(scheduler_process, "capture_group_witnesses", child.capture_witnesses)
    monkeypatch.setattr(scheduler_process, "_group_has_members", child.group_has_members)
    monkeypatch.setattr(scheduler_process, "live_group_members", lambda *a, **k: ([], False))
    monkeypatch.setattr(_proc, "_group_has_members", child.group_has_members)
    monkeypatch.setattr(_proc, "_signal_group_outcome", child.signal_group)
    monkeypatch.setattr(scheduler_process.socket, "gethostname", lambda: "test-host")
    monkeypatch.setattr(psutil, "boot_time", lambda: 17.0)

    def unexpected_signal(*_args):
        pytest.fail("A command fixture must use the synthetic signal port.")

    monkeypatch.setattr(os, "kill", unexpected_signal)
    monkeypatch.setattr(os, "killpg", unexpected_signal, raising=False)
    return child


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX session and group checks")
@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("live-owned", "fresh"),
        ("different-parent", None),
        ("different-session", None),
        ("caller-group", None),
        ("reaped-marked", "witnessed"),
        ("reaped-unverified", None),
    ],
)
def test_unpublished_command_group_signals_only_its_own_session(monkeypatch, scenario, expected):
    child = _CommandChild()
    if scenario.startswith("reaped"):
        child.returncode = 0
    monkeypatch.setattr(os, "getpgrp", lambda: child.pid if scenario == "caller-group" else 100)
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(os, "getsid", lambda pid: 100 if scenario == "different-session" else pid)
    monkeypatch.setattr(
        psutil,
        "Process",
        lambda pid: SimpleNamespace(
            ppid=lambda: 1 if scenario == "different-parent" else os.getpid()
        ),
    )
    marker = "own-spawn" if scenario == "reaped-marked" else "another-spawn"
    member = (child.pid + 1, child.started, marker, True)
    monkeypatch.setattr(scheduler_process, "live_group_members", lambda *a, **k: ([member], True))
    sent = []

    def kill_fresh(pgid):
        assert pgid == child.pid
        sent.append("fresh")
        return True

    def kill_witnessed(pgid, witnesses, sig):
        assert pgid == child.pid and sig == signal.SIGKILL
        if not witnesses:
            return False
        assert witnesses == (member[:2],)
        sent.append("witnessed")
        return True

    monkeypatch.setattr(scheduler_process, "kill_group_now", kill_fresh)
    monkeypatch.setattr(scheduler_process, "signal_group_if_witnessed", kill_witnessed)

    assert scheduler_process._kill_unpublished_command_group(child, "own-spawn") is bool(expected)
    assert sent == ([expected] if expected else [])


async def _fire_command(tmp_path):
    async with StateDB() as db:
        await db.create_schedule(
            {
                "id": "command-schedule",
                "name": "command-schedule",
                "trigger_type": "interval",
                "interval_sec": 3600,
                "action_kind": "command",
                "action_command": "probe-cmd",
                "action_cwd": str(tmp_path),
            }
        )
        schedule = await db.get_schedule("command-schedule")

    assert await fire_with_claim(SchedulerEngine(), schedule, _RUN_ID, trigger_context={})
    async with StateDB() as db:
        run = await db.get_schedule_run(_RUN_ID)
        invocation = await db.get_invocation(run["invocation_id"])
        assert run["dispatched_at"] is not None
        assert await db.list_undispatched_schedule_runs() == []
    return run, invocation


async def _operator_cancel(db, invocation_id, *, expected_updated_at=None):
    return await db.update_status(
        "invocation",
        invocation_id,
        new_status="cancelled",
        reason_code=RunReasons.CANCELLED_MANUAL_KILL,
        reason_summary=_OPERATOR_SUMMARY,
        evidence_refs=[],
        source="admin",
        actor="li_kill",
        expected_statuses={"running"},
        expected_updated_at=expected_updated_at,
    )


async def test_command_publication_wins_over_stale_no_pid_snapshot(command_child, tmp_path):
    version_without_pid = None

    async def before_publish():
        nonlocal version_without_pid
        async with StateDB() as db:
            await db.merge_invocation_node_metadata(
                command_child.invocation_id, {"metrics": {"attempt": 1}}
            )
            snapshot = await db.get_invocation(command_child.invocation_id)
            assert snapshot["node_metadata"] == {
                "action_kind": "command",
                "metrics": {"attempt": 1},
            }
            version_without_pid = snapshot["updated_at"]

    async def after_publish():
        async with StateDB() as db:
            run = await db.get_schedule_run(_RUN_ID)
            assert run["dispatched_at"] is not None
            invocation = await db.get_invocation(command_child.invocation_id)
            assert invocation["updated_at"] != version_without_pid
            assert invocation["node_metadata"] == {
                "action_kind": "command",
                "metrics": {"attempt": 1},
                "pid": command_child.pid,
                "pgid": command_child.pid,
                "pid_create_time": command_child.started,
                "pid_host": "test-host",
                "pid_boot_time": 17.0,
                "process_identity_mode": "local",
            }
            assert not await _operator_cancel(
                db, command_child.invocation_id, expected_updated_at=version_without_pid
            )

    command_child.before_publish = before_publish
    command_child.after_publish = after_publish
    run, invocation = await _fire_command(tmp_path)
    assert run["status"] == invocation["status"] == "completed"
    assert command_child.waits == 1
    assert command_child.group_signals == command_child.child_signals == []


@pytest.mark.parametrize(
    "cancel_at", ["before-publication", "after-publication", "before-terminal-write"]
)
async def test_cli_first_projects_its_reason_to_the_linked_command_run(
    command_child, monkeypatch, tmp_path, cancel_at
):
    async def cancel():
        async with StateDB() as db:
            assert await _operator_cancel(db, command_child.invocation_id)

    if cancel_at == "before-publication":
        command_child.before_publish = cancel
    elif cancel_at == "after-publication":

        async def cancel_during_exit():
            await cancel()
            command_child.returncode = -signal.SIGTERM

        command_child.after_publish = cancel_during_exit
    else:
        write_status = SchedulerEngine._guarded_terminal_status

        async def exit_after_signal():
            command_child.returncode = -signal.SIGTERM

        async def cancel_before_scheduler_write(self, entity_type, entity_id, **fields):
            if entity_type == "invocation":
                assert entity_id == command_child.invocation_id
                await cancel()
            return await write_status(self, entity_type, entity_id, **fields)

        command_child.after_publish = exit_after_signal
        monkeypatch.setattr(
            SchedulerEngine, "_guarded_terminal_status", cancel_before_scheduler_write
        )
    run, invocation = await _fire_command(tmp_path)

    assert run["status"] == invocation["status"] == "cancelled"
    assert (
        run["status_reason_code"]
        == invocation["status_reason_code"]
        == (RunReasons.CANCELLED_MANUAL_KILL)
    )
    assert run["status_reason_summary"] == invocation["status_reason_summary"] == _OPERATOR_SUMMARY
    assert run["error_detail"] is None
    assert not command_child.group_present
    if cancel_at == "before-publication":
        assert invocation["node_metadata"] == {"action_kind": "command"}
        assert command_child.returncode == -signal.SIGTERM
        assert command_child.group_signals == [signal.SIGTERM]
        assert command_child.child_signals == [signal.SIGTERM]
        assert command_child.waits == 0
    else:
        assert invocation["node_metadata"]["pid"] == command_child.pid
        assert command_child.waits == 1
        assert command_child.group_signals == command_child.child_signals == []


async def test_scheduler_first_keeps_the_existing_terminal_reason(command_child, tmp_path):
    run, invocation = await _fire_command(tmp_path)
    assert run["status"] == invocation["status"] == "completed"
    async with StateDB() as db:
        assert not await _operator_cancel(db, invocation["id"])
        assert await db.get_invocation(invocation["id"]) == invocation
        assert await db.get_schedule_run(run["id"]) == run
    assert run["status_reason_code"] == invocation["status_reason_code"] == RunReasons.COMPLETED_OK


async def test_failed_command_publication_stops_the_owned_group(
    command_child, monkeypatch, tmp_path
):
    dispatch_times = []

    async def store_failure(db, invocation_id, metadata):
        assert invocation_id == command_child.invocation_id
        assert metadata["pid"] == command_child.pid
        run = await db.get_schedule_run(_RUN_ID)
        dispatch_times.append(run["dispatched_at"])
        raise RuntimeError("synthetic backend request detail")

    monkeypatch.setattr(StateDB, "merge_invocation_node_metadata_if_running", store_failure)
    run, invocation = await _fire_command(tmp_path)

    assert dispatch_times == [run["dispatched_at"]]
    assert run["status"] == invocation["status"] == "failed"
    assert (
        run["status_reason_code"] == invocation["status_reason_code"] == RunReasons.FAILED_EXCEPTION
    )
    assert run["error_detail"].endswith("Could not record the scheduled command process.")
    assert "synthetic backend request detail" not in run["error_detail"]
    assert invocation["node_metadata"]["action_kind"] == "command"
    assert {"pid", "pgid", "pid_create_time"}.isdisjoint(invocation["node_metadata"])
    assert command_child.group_signals == [signal.SIGTERM]
    assert command_child.child_signals == [signal.SIGTERM]
    assert command_child.returncode == -signal.SIGTERM
    assert not command_child.group_present
    assert command_child.waits == 0


@pytest.mark.parametrize(
    ("reaped", "group_present"),
    [(True, False), (False, True), (True, True)],
)
async def test_command_without_identity_requires_a_reaped_child_and_empty_group(
    command_child, tmp_path, reaped, group_present
):
    command_child.identity_available = False
    command_child.group_present = group_present
    if reaped:
        command_child.returncode = 0

    run, invocation = await _fire_command(tmp_path)
    assert invocation["node_metadata"]["action_kind"] == "command"
    assert {"pid", "pgid", "pid_create_time"}.isdisjoint(invocation["node_metadata"])
    assert command_child.group_signals == []
    if reaped and not group_present:
        assert run["status"] == invocation["status"] == "completed"
        assert command_child.waits == 1
        assert command_child.child_signals == []
    else:
        assert run["status"] == invocation["status"] == "failed"
        assert run["error_detail"].endswith("Could not identify the scheduled command process.")
        assert command_child.child_signals == ([] if reaped else [signal.SIGTERM])
        assert command_child.returncode == (0 if reaped else -signal.SIGTERM)
        assert command_child.group_present == group_present
        assert command_child.waits == 0
