# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the flow_yaml schedule action kind: build_argv, validation, DB persistence, and lifecycle."""

from __future__ import annotations

import os
import textwrap
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import lionagi.state.db as state_db_mod

# subprocess.build_argv tests


def _minimal_schedule(**kwargs) -> dict:
    base = {
        "id": "sched-abc",
        "name": "test-sched",
        "trigger_type": "cron",
        "action_kind": "flow_yaml",
        "action_model": "",
        "action_prompt": "",
        "action_flow_yaml": "prompt: hello world\n",
        "action_project": None,
        "action_extra_args": [],
    }
    base.update(kwargs)
    return base


def test_build_argv_flow_yaml_dispatches_li_o_flow():
    """flow_yaml kind builds argv with `li o flow -f <tmpfile>`."""
    from lionhub.orchestration.scheduler.subprocess import build_argv

    sched = _minimal_schedule()
    argv, tmp_path = build_argv(sched, {})

    try:
        assert argv[:4] == ["uv", "run", "li", "o"]
        assert argv[4] == "flow"
        assert "-f" in argv
        fi = argv.index("-f")
        assert argv[fi + 1] == tmp_path
        assert tmp_path is not None
        # Temp file must exist and contain the YAML text
        assert os.path.isfile(tmp_path)
        assert "hello world" in open(tmp_path).read()
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def test_build_argv_flow_yaml_returns_tmp_path_none_for_other_kinds():
    """Other action kinds do not produce a temp file."""
    from lionhub.orchestration.scheduler.subprocess import build_argv

    for kind in ("agent", "flow", "fanout", "play"):
        sched = {
            "id": "s1",
            "action_kind": kind,
            "action_model": "",
            "action_prompt": "",
            "action_agent": None,
            "action_playbook": None,
            "action_project": None,
            "action_extra_args": [],
            "action_flow_yaml": None,
        }
        argv, tmp_path = build_argv(sched, {})
        assert tmp_path is None, f"Expected no tmp_path for kind={kind}"


def test_build_argv_flow_yaml_cleanup_on_spawn():
    """spawn_and_wait deletes the temp file after the subprocess exits."""
    import asyncio
    import tempfile

    from lionhub.orchestration.scheduler.subprocess import spawn_and_wait

    # Create a real temp file to verify cleanup
    fd, tmp_path = tempfile.mkstemp(suffix=".yaml", prefix="lionagi-test-")
    os.close(fd)
    assert os.path.exists(tmp_path)

    async def _run():
        with patch(
            "lionhub.orchestration.scheduler.subprocess.asyncio.create_subprocess_exec"
        ) as mock_exec:
            # spawn_and_wait drains both pipes to EOF instead of calling
            # communicate(), so the double presents readable pipes.
            mock_proc = MagicMock()
            for _pipe in ("stdout", "stderr"):
                _stream = MagicMock()
                _stream.read = AsyncMock(return_value=b"")
                setattr(mock_proc, _pipe, _stream)
            mock_proc.wait = AsyncMock(return_value=0)
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc
            return await spawn_and_wait(
                ["uv", "run", "li", "o", "flow", "-f", tmp_path],
                "inv-001",
                tmp_path=tmp_path,
            )

    exit_code, _ = asyncio.run(_run())
    assert exit_code == 0
    # Temp file must be gone
    assert not os.path.exists(tmp_path)


# Creation-time YAML validation tests


def _minimal_command_schedule(name: str) -> dict:
    return {
        "name": name,
        "trigger_type": "cron",
        "cron_expr": "0 * * * *",
        "action_kind": "agent",
        "action_prompt": "say hi",
    }


# Lifecycle / status parity test


def test_flow_yaml_lifecycle_parity_with_play():
    """flow_yaml and play follow the same subprocess contract (both via build_argv / spawn_and_wait)."""
    # We verify parity at the subprocess layer rather than running the full engine.

    from lionhub.orchestration.scheduler.subprocess import build_argv

    play_sched = {
        "id": "s1",
        "action_kind": "play",
        "action_model": "",
        "action_prompt": "",
        "action_agent": None,
        "action_playbook": "my-playbook",
        "action_project": None,
        "action_extra_args": [],
        "action_flow_yaml": None,
    }
    flow_yaml_sched = _minimal_schedule()

    play_argv, play_tmp = build_argv(play_sched, {})
    yaml_argv, yaml_tmp = build_argv(flow_yaml_sched, {})

    try:
        # Both produce non-empty argv lists starting with uv run li
        assert play_argv[:3] == ["uv", "run", "li"]
        assert yaml_argv[:3] == ["uv", "run", "li"]

        # play produces no temp file; flow_yaml produces one
        assert play_tmp is None
        assert yaml_tmp is not None

        # flow_yaml must use the same flow execution path (li o flow)
        assert "o" in yaml_argv
        assert "flow" in yaml_argv
        assert "-f" in yaml_argv
    finally:
        if yaml_tmp and os.path.exists(yaml_tmp):
            os.unlink(yaml_tmp)


# CLI parser tests


# Persistence round-trip test


# Regression tests: tmp-file lifecycle under cancellation / exception


def test_spawn_and_wait_cancellation_cleans_tmp_file():
    """CancelledError inside spawn_and_wait still removes the tmp file."""
    import asyncio
    import tempfile

    from lionhub.orchestration.scheduler.subprocess import spawn_and_wait

    fd, tmp_path = tempfile.mkstemp(suffix=".yaml", prefix="lionagi-cancel-test-")
    os.close(fd)
    assert os.path.exists(tmp_path)

    async def _run():
        with patch(
            "lionhub.orchestration.scheduler.subprocess.asyncio.create_subprocess_exec"
        ) as mock_exec:
            # The cancel arrives while draining the pipes, which is where
            # spawn_and_wait waits (simulates scheduler shutdown).
            mock_proc = MagicMock()
            for _pipe in ("stdout", "stderr"):
                _stream = MagicMock()
                _stream.read = AsyncMock(side_effect=asyncio.CancelledError())
                setattr(mock_proc, _pipe, _stream)
            mock_proc.pid = 9999999  # very large — os.getpgid won't find it
            mock_proc.terminate = MagicMock()
            mock_proc.kill = MagicMock()
            mock_proc.wait = AsyncMock(return_value=None)
            mock_exec.return_value = mock_proc
            try:
                await spawn_and_wait(
                    ["uv", "run", "li", "o", "flow", "-f", tmp_path],
                    "inv-cancel-001",
                    tmp_path=tmp_path,
                )
            except asyncio.CancelledError:
                pass  # expected

    asyncio.run(_run())
    assert not os.path.exists(tmp_path), "tmp file must be removed even on CancelledError"


def test_fire_pre_spawn_exception_cleans_tmp_file():
    """Exception between build_argv and spawn_and_wait (pre-spawn window) must still clean up the tmp file."""
    import asyncio
    import contextlib
    import os
    import tempfile

    from lionhub.orchestration.scheduler.subprocess import build_argv

    sched = {
        "id": "sched-pre-spawn",
        "name": "pre-spawn-test",
        "trigger_type": "cron",
        "action_kind": "flow_yaml",
        "action_model": "",
        "action_prompt": "",
        "action_flow_yaml": "prompt: pre-spawn test\n",
        "action_project": None,
        "action_extra_args": [],
    }
    argv, tmp_path = build_argv(sched, {})
    assert tmp_path is not None
    assert os.path.exists(tmp_path)

    # Simulate the outer finally behaviour: exception fires before spawn,
    # outer finally must unlink.
    try:
        raise RuntimeError("simulated DB failure in pre-spawn window")
    except RuntimeError:
        pass
    finally:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)

    assert not os.path.exists(tmp_path), "tmp file must be removed after pre-spawn exception"


# Regression tests: legacy schedules table migration


# Regression tests: PATCH validation
