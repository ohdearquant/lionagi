# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Setup shared by module and adapter contracts."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import psutil
import pytest

from lionagi.ln._proc import raw_create_time
from lionagi.state.db import StateDB


def _script_create_time(fake_proc: MagicMock, value: float) -> None:
    """Make a fake process answer the same start time on either read path.

    Production reads the unadjusted start time through ``raw_create_time``,
    which on a platform that adjusts start times reaches the backend object
    (``proc._proc``) rather than the public ``create_time()``; on a bare
    ``MagicMock`` that path returns another mock. Point the backend attribute
    back at the fake itself, so the identity arithmetic under test sees the
    number the test set whichever reader this platform selected — rather than
    patching ``raw_create_time`` out of the module under test, which would
    make every test here bypass the reader production actually uses.
    """
    fake_proc.create_time.return_value = value
    fake_proc._proc = fake_proc


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
    extra_meta: dict[str, Any] | None = None,
) -> str:
    sid = str(uuid.uuid4())
    pid_val = str(uuid.uuid4())
    await db.create_progression(pid_val)
    node_meta = {"pid": pid} if pid is not None else {}
    if extra_meta:
        node_meta.update(extra_meta)
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


def _mock_psutil(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cmdline: list[str],
    environ: dict[str, str] | None = None,
    create_time: float = 100.0,
    runner_pid: int | None = None,
) -> list[tuple[int, int]]:
    """Install a fake psutil + capture os.kill calls. Returns the calls list."""
    kill_calls: list[tuple[int, int]] = []
    patched_pid_alive = lambda pid: True
    monkeypatch.setattr("lionhub.serve.cli.kill._pid_alive", patched_pid_alive)
    monkeypatch.setattr("lionhub.orchestration.kill._pid_alive", patched_pid_alive)
    monkeypatch.setattr("os.kill", lambda pid, sig: kill_calls.append((pid, sig)))

    fake_psutil = MagicMock()
    fake_psutil.LINUX = False
    fake_proc = MagicMock()
    fake_proc.cmdline.return_value = cmdline
    fake_proc.environ.return_value = environ or {}
    _script_create_time(fake_proc, create_time)
    fake_psutil.Process.return_value = fake_proc
    fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    fake_psutil.AccessDenied = type("AccessDenied", (Exception,), {})
    # Mirrors psutil: ZombieProcess is a NoSuchProcess subclass, so a double
    # that omits it lets the code under test claim a distinction it never made.
    fake_psutil.ZombieProcess = type("ZombieProcess", (fake_psutil.NoSuchProcess,), {})
    monkeypatch.setattr("lionhub.orchestration.kill.psutil", fake_psutil)
    if runner_pid is not None:

        def probe(pid: int) -> tuple[str, float | None]:
            assert pid == runner_pid, f"unexpected runner pid: {pid}"
            return "found", raw_create_time(fake_psutil.Process(pid))

        monkeypatch.setattr("lionhub.orchestration.kill._runner_process_create_time", probe)
    return kill_calls
