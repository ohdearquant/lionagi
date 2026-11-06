# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Setup shared by module and adapter contracts."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from lionagi.ln._proc import raw_create_time
from lionhub.orchestration.runs import PERSISTENCE_DEGRADED_REASON_FIELD

RUN_ID = "20261005T030000-abc123"


@pytest.fixture
def runs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    root = tmp_path / "runs"
    monkeypatch.setattr("lionhub.orchestration.runs.RUNS_ROOT", root)
    return root


@pytest.fixture
def leg(tmp_path: Path):
    """A process `li kill` identifies as a lionagi CLI: a script named `li`, run by its shebang."""
    script = tmp_path / "bin" / "li"
    script.parent.mkdir()
    script.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(300)\n")
    script.chmod(0o755)
    proc = subprocess.Popen([str(script)])  # noqa: S603
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:  # the shebang has to have replaced the image first
        try:
            if os.path.basename(psutil.Process(proc.pid).cmdline()[-1]) == "li":
                break
        except (psutil.NoSuchProcess, psutil.AccessDenied, IndexError):
            pass
        time.sleep(0.05)
    yield proc
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def _markers(pid: int) -> dict:
    return {
        "pid": pid,
        "pid_create_time": raw_create_time(psutil.Process(pid)),
        "pid_host": socket.gethostname(),
        "pid_boot_time": psutil.boot_time(),
        "process_identity_mode": "local",
    }


def _write_run(root: Path, manifest: dict, run_id: str = RUN_ID) -> Path:
    path = root / run_id / "run.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"run_id": run_id, **manifest}))
    return path


def _degraded(pid: int, **extra) -> dict:
    return {
        "status": "running",
        PERSISTENCE_DEGRADED_REASON_FIELD: "OperationalError('database is locked')",
        **_markers(pid),
        **extra,
    }
