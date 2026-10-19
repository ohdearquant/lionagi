# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Every CLI agent takes its machine-wide slot in ``ndjson_from_cli``, before the
child exists, and holds it until the child's group has ended.

Real subprocesses: the slot is a lock another process would contend for, and the
recorded child is the kernel's pid.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

import pytest

from lionagi.providers import _subagent_admission as admission
from lionagi.providers._cli_subprocess import ndjson_from_cli
from lionagi.providers._subagent_admission import (
    SubagentAdmissionRefusedError,
    subagent_admission_status,
)

# Emits one object, then blocks until killed.
_SLEEPER = (
    "import sys, time; "
    'sys.stdout.write(\'{"type": "hello"}\\n\'); sys.stdout.flush(); '
    "time.sleep(30)"
)


@pytest.fixture()
def capped(tmp_path, monkeypatch):
    from lionagi import _paths

    monkeypatch.setattr(_paths, "LIONAGI_HOME", tmp_path / "home")
    monkeypatch.setattr(admission, "_POLL_SECONDS", 0.05)
    monkeypatch.setenv("LIONAGI_MAX_SUBAGENTS", "1")
    monkeypatch.setenv("LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS", "0")
    return tmp_path


def _held():
    return subagent_admission_status()["pools"].get("all", {"held": 0, "holders": []})


@pytest.mark.asyncio
async def test_a_spawn_at_the_cap_is_refused_before_its_child_starts(capped):
    marker = capped / "second-started"
    first = ndjson_from_cli([sys.executable, "-c", _SLEEPER], engine="codex")
    async with contextlib.aclosing(first):
        assert (await first.__anext__()) == {"type": "hello"}
        held = _held()
        assert held["held"] == 1
        [holder] = held["holders"]
        assert isinstance(holder["child_pid"], int) and holder["engine"] == "codex"

        second = ndjson_from_cli(
            [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"], engine="claude"
        )
        with pytest.raises(SubagentAdmissionRefusedError, match="cap of 1"):
            async with contextlib.aclosing(second):
                await second.__anext__()
        assert not marker.exists()
    # The first stream's teardown ended its child and gave the slot back.
    assert _held()["held"] == 0


@pytest.mark.asyncio
async def test_a_child_that_finishes_gives_its_slot_to_the_next_spawn(capped):
    quick = 'import sys; sys.stdout.write(\'{"type": "done"}\\n\')'
    for _ in range(2):
        stream = ndjson_from_cli([sys.executable, "-c", quick], engine="codex")
        async with contextlib.aclosing(stream):
            assert [obj async for obj in stream] == [{"type": "done"}]
    assert _held()["held"] == 0


@pytest.mark.asyncio
async def test_a_spawn_that_fails_gives_its_slot_back(capped):
    missing = str(Path(capped) / "no-such-binary")
    stream = ndjson_from_cli([missing], engine="codex")
    with pytest.raises(OSError):
        async with contextlib.aclosing(stream):
            await stream.__anext__()
    assert _held()["held"] == 0
