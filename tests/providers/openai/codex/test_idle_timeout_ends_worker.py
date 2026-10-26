# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A codex worker that goes quiet after partial output is ended when the run gives up on it.

The idle watchdog raises ``WorkerLivenessError`` once the worker has said something and then
nothing for ``idle_timeout`` seconds. The run must not report that failure while the worker's
process group is still running: a codex child left behind keeps its connection open and keeps
spending. These tests drive the real codex provider through ``run()`` with a stand-in CLI that
emits two events and then sleeps, and require the stand-in and its own child to be gone.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys

import psutil
import pytest

from lionagi.operations.run.run import RunParam, run
from lionagi.providers._provider_errors import WorkerLivenessError
from lionagi.service.imodel import iModel
from lionagi.session.branch import Branch


def _process_is_live(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


async def _wait_until_gone(pid: int, *, timeout: float = 10.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while _process_is_live(pid) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    return not _process_is_live(pid)


def _stand_in(pidfile, *, ignore_term: bool) -> str:
    ignore = "signal.signal(signal.SIGTERM, signal.SIG_IGN); " if ignore_term else ""
    return "\n".join(
        (
            f"#!{sys.executable}",
            "import json, os, signal, subprocess, sys, time",
            ignore,
            "desc = subprocess.Popen([sys.executable, '-c', "
            f"'import signal, time; {ignore}time.sleep(120)'])",
            f"with open({str(pidfile)!r}, 'w') as f:",
            "    json.dump({'leader': os.getpid(), 'descendant': desc.pid}, f)",
            "sys.stdin.read()",
            "print(json.dumps({'type': 'thread.started', 'thread_id': 't-idle'}), flush=True)",
            "print(json.dumps({'type': 'item.completed', "
            "'item': {'type': 'agent_message', 'text': 'partial'}}), flush=True)",
            "while True:",
            "    time.sleep(120)",
            "",
        )
    )


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX process groups")
@pytest.mark.slow_timing
@pytest.mark.parametrize("ignore_term", [False, True], ids=["default-term", "term-ignored"])
async def test_idle_timeout_after_partial_output_ends_the_worker_group(
    tmp_path, monkeypatch, ignore_term
) -> None:
    from lionagi.providers.openai import codex as codex_mod

    pidfile = tmp_path / "pids.json"
    fake_cli = tmp_path / "fake-codex"
    fake_cli.write_text(_stand_in(pidfile, ignore_term=ignore_term))
    fake_cli.chmod(0o755)
    monkeypatch.setattr(codex_mod, "CODEX_CLI", str(fake_cli))

    branch = Branch(chat_model=iModel(provider="codex", model="gpt-5.5"))
    pids: dict[str, int] = {}
    try:
        with pytest.raises(WorkerLivenessError) as exc_info:
            await asyncio.wait_for(
                _drain(
                    run(
                        branch,
                        "go",
                        RunParam(imodel_kw={"liveness_timeout": 30, "idle_timeout": 1.0}),
                    )
                ),
                timeout=60,
            )
        assert exc_info.value.reason == "worker.stream_idle"
        pids = json.loads(pidfile.read_text())
        assert await _wait_until_gone(pids["leader"]), "the codex stand-in outlived the run"
        assert await _wait_until_gone(pids["descendant"]), "the stand-in's child outlived the run"
    finally:
        if not pids and pidfile.exists():
            with contextlib.suppress(ValueError):
                pids = json.loads(pidfile.read_text())
        for pid in pids.values():
            if _process_is_live(pid):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)


async def _drain(gen) -> list:
    return [item async for item in gen]
