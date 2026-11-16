# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A late process-group witness still lets teardown end the entire group."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

from lionhub.providers import _cli_subprocess as cs

_GRACE = 1.0

_LEADER = """
import signal
import subprocess
import sys

signal.signal(signal.SIGTERM, lambda *_: None)
with subprocess.Popen([sys.executable, "-c", *sys.argv[1:]]) as descendant:
    descendant.wait()
"""

_DESCENDANT = """
import os
import sys
import time
from pathlib import Path

ready, stop = map(Path, sys.argv[1:])
ready_tmp = ready.with_suffix(".tmp")
ready_tmp.write_text(str(os.getpid()))
ready_tmp.replace(ready)
deadline = time.monotonic() + 25
while not stop.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
"""


async def _read_descendant_pid(ready: Path) -> int:
    while not ready.exists():
        await asyncio.sleep(0.02)
    return int(ready.read_text())


def _is_running(process: psutil.Process) -> bool:
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


async def _wait_stopped(process: psutil.Process) -> None:
    while _is_running(process):
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_witness_after_one_grace_still_ends_real_descendant(tmp_path, monkeypatch):
    scratch_home = tmp_path / "home"
    scratch_home.mkdir()
    ready = tmp_path / "descendant.pid"
    stop = tmp_path / "stop"
    env, marker = cs.prepare_process_group_env({"HOME": str(scratch_home), "TMPDIR": str(tmp_path)})
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _LEADER,
        _DESCENDANT,
        str(ready),
        str(stop),
        cwd=tmp_path,
        env=env,
        start_new_session=True,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    descendant = None
    try:
        pid = await asyncio.wait_for(_read_descendant_pid(ready), timeout=5)
        descendant = psutil.Process(pid)
        assert _is_running(descendant)
        assert os.getpgid(pid) == proc.pid
        spawned = cs.observe_spawned(proc.pid, expected_marker=marker)
        assert spawned.create_time is not None

        capture = cs._witnesses_for_spawn

        def slow_capture(child, identity):
            time.sleep(1.25 * _GRACE)
            return capture(child, identity)

        monkeypatch.setattr(cs, "_witnesses_for_spawn", slow_capture)
        result = await cs._bounded_cli_cleanup(proc, spawned, grace=_GRACE)

        assert not _is_running(descendant), "a witnessed descendant survived cleanup"
        assert proc.returncode == 0  # The leader reaped its descendant and exited.
        assert result.outcome is cs._ChildGroupCleanup.CONFIRMED_EMPTY
        assert result.error is None
        assert result.cancellation is None
        lease = SimpleNamespace(slots=[SimpleNamespace(pool="fixture")], release=Mock())
        cs._finish_child_lease(lease, result.outcome, witnesses=result.witnesses)
        lease.release.assert_called_once_with()
    finally:
        # The fixture's own stop file also ends it on an implementation that
        # ends just the leader. No numeric PID/PGID is needed for this fallback.
        stop.touch()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5)
            if descendant is not None:
                await asyncio.wait_for(_wait_stopped(descendant), timeout=5)
