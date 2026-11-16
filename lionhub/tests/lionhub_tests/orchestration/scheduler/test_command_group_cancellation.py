# Copyright (c) 2023-2026, LionAGI contributors
# SPDX-License-Identifier: Apache-2.0

"""End-to-end cancellation of a scheduled command's owned process group."""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path

import psutil
import pytest

from lionagi.ln._proc import process_create_time
from lionagi.state import db as db_module
from lionagi.state.reasons import RunReasons
from lionhub.orchestration.scheduler.engine import SchedulerEngine
from lionhub.orchestration.scheduler.scheduler_state import _DBSchedulerStateService
from lionhub.serve.cli.kill import _do_kill
from tests._kill_scope_test_support import stable_boot_time as stable_boot_time

_COMMAND = "fixture-scheduled-command"
_PROGRAM = """\
import os
import signal
import sys
import time
from pathlib import Path

signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = os.fork()
if child == 0:
    time.sleep(20)
    os._exit(0)
ready = Path(sys.argv[1])
ready.with_suffix('.tmp').write_text(f'{os.getpid()} {child}', encoding='ascii')
ready.with_suffix('.tmp').replace(ready)
os.waitpid(child, 0)
"""


async def _running_group(store, occurrence_id: str, ready: Path, fire: asyncio.Task):
    while not fire.done():
        occurrence = await store.get_schedule_run(occurrence_id)
        if (
            occurrence is not None
            and occurrence.get("dispatched_at") is not None
            and ready.is_file()
        ):
            invocation = await store.get_invocation(occurrence["invocation_id"])
            if invocation is not None and (invocation.get("node_metadata") or {}).get("pgid"):
                worker, descendant = (
                    int(value) for value in ready.read_text(encoding="ascii").split()
                )
                return invocation, worker, descendant
        await asyncio.sleep(0.01)
    await fire
    pytest.fail("scheduled command finished before publishing a live group")


async def _await_exit(pid: int, original_birth: float) -> None:
    while True:
        state, birth = process_create_time(pid)
        if state == "gone" or (state == "found" and birth != original_birth):
            return
        await asyncio.sleep(0.01)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups and fork")
async def test_cli_kill_reaps_a_scheduled_command_group(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(db_module, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(
        db_module, "settings", db_module.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", _COMMAND)
    (tmp_path / _COMMAND).symlink_to(sys.executable)
    script = tmp_path / "sleeping_group.py"
    script.write_text(_PROGRAM, encoding="utf-8")
    ready = tmp_path / "group.ready"

    service = _DBSchedulerStateService(persistent=True)
    engine = SchedulerEngine(svc=service)
    released = asyncio.Event()
    terminal = engine._guarded_terminal_status

    async def after_cli(*args, **kwargs):
        await released.wait()
        return await terminal(*args, **kwargs)

    # Hold only the scheduler's terminal writer. Spawn, publication, CLI signalling,
    # process exit and the CLI's own terminal transaction all use their real paths.
    monkeypatch.setattr(engine, "_guarded_terminal_status", after_cli)
    try:
        async with db_module.StateDB(db_path) as db:
            await db.create_schedule(
                {
                    "id": "scheduled-group",
                    "name": "scheduled-group",
                    "trigger_type": "interval",
                    "interval_sec": 60,
                    "action_kind": "command",
                    "action_command": _COMMAND,
                    "action_command_args": [script.name, ready.name],
                    "action_cwd": str(tmp_path),
                }
            )
            occurrence_id = await engine.fire_now("scheduled-group")
            assert occurrence_id is not None
            (fire,) = engine._fire_tasks
            invocation, worker, descendant = await asyncio.wait_for(
                _running_group(db, occurrence_id, ready, fire), timeout=8
            )
            metadata = invocation["node_metadata"]
            assert metadata["pid"] == metadata["pgid"] == worker
            assert metadata["action_kind"] == "command"
            assert metadata["process_identity_mode"] == "local"
            assert metadata["pid_host"] == socket.gethostname()
            assert metadata["pid_boot_time"] == psutil.boot_time()
            assert worker != os.getpgrp() and os.getpgid(descendant) == worker
            worker_state, worker_birth = process_create_time(worker)
            child_state, child_birth = process_create_time(descendant)
            assert worker_state == child_state == "found"
            assert worker_birth == metadata["pid_create_time"] and child_birth is not None

            assert await _do_kill(invocation["id"], grace_seconds=0) == 0
            assert "signal=sigkill" in capsys.readouterr().out
            await asyncio.wait_for(
                asyncio.gather(
                    _await_exit(worker, worker_birth), _await_exit(descendant, child_birth)
                ),
                timeout=5,
            )
            released.set()
            assert await asyncio.wait_for(fire, timeout=5)
            current = await db.get_invocation(invocation["id"])
            occurrence = await db.get_schedule_run(occurrence_id)
            assert (current["status"], occurrence["status"]) == ("cancelled", "cancelled"), (
                current["status_reason_code"],
                occurrence["status_reason_code"],
            )
            assert current["status_reason_code"] == RunReasons.CANCELLED_FORCE_KILL
            assert occurrence["status_reason_code"] == RunReasons.CANCELLED_FORCE_KILL
    finally:
        released.set()
        await asyncio.wait_for(engine.stop(), timeout=10)
