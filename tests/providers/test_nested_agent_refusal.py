# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A CLI agent lionagi started cannot start another through lionagi.

The refusal reads this process's inherited depth and, failing that, the environment
each ancestor started with, and the spawn runs it before any admission, so a nested
agent is refused at once. The spawn hands every child depth 1 unless this process
launched a top-level profile. The conftest runs every test outside any agent; tests here set the
depth and ancestry they need, and the ancestry test reads real processes.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from lionagi._provider_errors import (
    NestedAgentRefusedError,
    SubagentAdmissionRefusedError,
)
from lionagi.service import _subagent_admission as admission
from lionagi.service._subagent_admission import (
    AGENT_DEPTH_ENV,
    _ancestor_agent_depth,
    _parse_agent_depth,
    child_agent_depth,
    refuse_nested_agent,
    set_child_agent_depth,
    subagent_admission_status,
)
from lionhub.providers._cli_subprocess import ndjson_from_cli

# Bound at import, before the conftest replaces it for each test.
_REAL_ANCESTOR_AGENT_DEPTH = _ancestor_agent_depth
_TREE = Path(admission.__file__).resolve().parents[2]

_SLEEPER = (
    "import sys, time; "
    'sys.stdout.write(\'{"type": "hello"}\\n\'); sys.stdout.flush(); '
    "time.sleep(30)"
)
_PRINT_DEPTH = (
    "import json, os, sys; "
    "sys.stdout.write(json.dumps({'depth': os.environ.get('LIONAGI_AGENT_DEPTH')}) + '\\n')"
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 0), ("", 0), ("not-a-number", 0), ("-3", 0), ("0", 0), ("3", 3)],
)
def test_a_depth_that_is_not_a_positive_whole_number_reads_as_zero(raw, expected):
    assert _parse_agent_depth(raw) == expected


def test_an_inherited_depth_is_refused(monkeypatch):
    monkeypatch.setattr(admission, "_INHERITED_AGENT_DEPTH", 1)
    with pytest.raises(NestedAgentRefusedError) as raised:
        refuse_nested_agent("codex")
    message = str(raised.value)
    assert "this process has LIONAGI_AGENT_DEPTH=1" in message
    assert "cannot start a codex agent through lionagi" in message
    assert raised.value.retryable is False
    assert not isinstance(raised.value, SubagentAdmissionRefusedError)


def test_an_ancestor_that_started_with_a_depth_is_refused(monkeypatch):
    monkeypatch.setattr(admission, "_ancestor_agent_depth", lambda: (2, 4242))
    with pytest.raises(NestedAgentRefusedError, match="pid 4242 has LIONAGI_AGENT_DEPTH=2"):
        refuse_nested_agent()


def test_outside_any_agent_nothing_is_refused_even_after_this_process_stamps():
    refuse_nested_agent("claude")
    # `li agent` stamps its own environment before it starts its child; the refusal
    # reads the depth this process started with, not that stamp.
    set_child_agent_depth(1)
    assert os.environ[AGENT_DEPTH_ENV] == "1"
    refuse_nested_agent("claude")


def _refusal_under_a_parent(parent_depth: str | None) -> tuple[int, str, str, int]:
    """Run the refusal in a python whose parent started with ``parent_depth`` and starts
    it without one, as codex starts an MCP server with only the variables its config
    names. The parent is a python rather than a shell: on macOS the kernel hands out no
    environment for a platform binary such as /bin/sh, so a shell there reads as no depth.

    Returns the child's exit code, its output, its stderr and the parent's pid."""
    child = (
        "import sys\n"
        "from lionagi.service._subagent_admission import refuse_nested_agent\n"
        "from lionagi._provider_errors import NestedAgentRefusedError\n"
        "try:\n"
        "    refuse_nested_agent('codex')\n"
        "except NestedAgentRefusedError as exc:\n"
        "    print(exc)\n"
        "    sys.exit(3)\n"
    )
    parent = (
        "import os, subprocess, sys\n"
        "env = {k: v for k, v in os.environ.items() if k != 'LIONAGI_AGENT_DEPTH'}\n"
        "done = subprocess.run([sys.executable, '-c', sys.argv[1]], env=env)\n"
        "sys.exit(done.returncode)\n"
    )
    env = {key: value for key, value in os.environ.items() if key != AGENT_DEPTH_ENV}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(_TREE), env.get("PYTHONPATH")]))
    if parent_depth is not None:
        env[AGENT_DEPTH_ENV] = parent_depth
    proc = subprocess.Popen(
        [sys.executable, "-c", parent, child],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out, err = proc.communicate(timeout=120)
    return proc.returncode, out, err, proc.pid


def test_the_refusal_finds_a_depth_an_agent_withheld_from_its_child():
    code, out, err, parent_pid = _refusal_under_a_parent("7")
    assert code == 3, err
    assert f"pid {parent_pid} has LIONAGI_AGENT_DEPTH=7" in out


def test_the_same_chain_outside_any_agent_is_not_refused():
    own_start = _parse_agent_depth(psutil.Process().environ().get(AGENT_DEPTH_ENV))
    if own_start or _REAL_ANCESTOR_AGENT_DEPTH()[0]:
        pytest.skip("this suite runs inside an agent lionagi started, so the chain is too")
    code, out, err, _ = _refusal_under_a_parent(None)
    assert code == 0, (out, err)


@pytest.fixture()
def one_slot(tmp_path, monkeypatch):
    from lionagi import _paths

    monkeypatch.setattr(_paths, "LIONAGI_HOME", tmp_path / "home")
    monkeypatch.setattr(admission, "_POLL_SECONDS", 0.05)
    monkeypatch.setenv("LIONAGI_MAX_SUBAGENTS", "1")
    monkeypatch.setenv("LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS", "30")
    return tmp_path


@pytest.mark.asyncio
async def test_a_nested_spawn_is_refused_at_once_without_waiting_for_a_slot(one_slot, monkeypatch):
    """The pool is full and the wait is 30 s, so a spawn admission reached would wait and
    then be refused for the slot; the nested one is refused before admission, at once, and
    nothing is started."""
    marker = one_slot / "nested-started"
    first = ndjson_from_cli([sys.executable, "-c", _SLEEPER], engine="codex")
    async with contextlib.aclosing(first):
        assert (await first.__anext__()) == {"type": "hello"}

        monkeypatch.setattr(admission, "_ancestor_agent_depth", lambda: (1, 4242))
        started: list[tuple] = []
        real_exec = asyncio.create_subprocess_exec

        async def _spy(*args, **kwargs):
            started.append(args)
            return await real_exec(*args, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _spy)
        nested = ndjson_from_cli(
            [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"], engine="claude"
        )
        began = time.monotonic()
        with pytest.raises(NestedAgentRefusedError, match="a claude agent"):
            async with contextlib.aclosing(nested):
                await nested.__anext__()
        assert time.monotonic() - began < 5
        assert started == []
        assert not marker.exists()
        assert subagent_admission_status()["pools"]["all"]["held"] == 1


async def _child_depth(**kwargs) -> str | None:
    stream = ndjson_from_cli([sys.executable, "-c", _PRINT_DEPTH], engine="codex", **kwargs)
    async with contextlib.aclosing(stream):
        [line] = [obj async for obj in stream]
    return line["depth"]


@pytest.mark.asyncio
async def test_a_child_is_handed_depth_one_over_the_zero_a_shell_carries(monkeypatch):
    """An interactive shell can carry depth 0; an agent a program in it starts through
    the Python API is still an agent lionagi started, and must refuse to start another."""
    monkeypatch.setenv(AGENT_DEPTH_ENV, "0")
    assert await _child_depth() == "1"


@pytest.mark.asyncio
async def test_a_child_is_handed_depth_one_over_a_depth_its_caller_passed():
    env = {**os.environ, AGENT_DEPTH_ENV: "0"}
    assert await _child_depth(env=env) == "1"


@pytest.mark.asyncio
async def test_a_seat_launch_hands_its_child_depth_zero():
    set_child_agent_depth(0)
    assert child_agent_depth() == 0
    assert await _child_depth() == "0"
