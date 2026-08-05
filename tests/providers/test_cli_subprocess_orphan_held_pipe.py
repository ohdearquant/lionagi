# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""stdout EOF needs every holder of the pipe's write end to close it, not just
the child. A CLI child that spawns long-lived helpers leaks the write end into
them, so a child that exits cleanly can leave the stream read waiting forever —
a finished leg, artifacts on disk, reported as running for the rest of its
caller's budget. The stream must end within a bounded grace of the child's
exit, and the orphan must not survive teardown.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

import lionagi.providers._cli_subprocess as cli_subprocess
from lionagi.providers._cli_subprocess import ndjson_from_cli

# Emits one event carrying its orphan's pid, leaks stdout into a 30s grandchild,
# exits 0. Without a post-exit bound the stream hangs ~30s; with it, it ends
# within the grace.
_ORPHAN_HOLDER = (
    "import json, subprocess, sys\n"
    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
    "print(json.dumps({'type': 'x', 'orphan_pid': p.pid}), flush=True)\n"
    "sys.exit(0)\n"
)

# Streams slowly while ALIVE: the bound applies after exit, never to a living
# child that is just quiet between events.
_SLOW_BUT_ALIVE = (
    "import time\nprint('{\"n\": 1}', flush=True)\ntime.sleep(2)\nprint('{\"n\": 2}', flush=True)\n"
)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.asyncio
async def test_stream_ends_within_grace_when_orphan_holds_stdout(monkeypatch):
    monkeypatch.setattr(cli_subprocess, "_POST_EXIT_DRAIN_GRACE", 0.5)

    start = time.monotonic()
    events = []
    async for obj in ndjson_from_cli([sys.executable, "-c", _ORPHAN_HOLDER]):
        events.append(obj)
    elapsed = time.monotonic() - start

    assert [e["type"] for e in events] == ["x"], events
    # Far under the orphan's 30s sleep — the bound, not the orphan, ended the stream.
    assert elapsed < 10, f"stream took {elapsed:.1f}s; the orphan held it open"

    # Teardown ends the child's process group, which is what closes the
    # orphan's copy of the pipe — the orphan must not outlive the stream.
    deadline = time.monotonic() + 5
    orphan_pid = events[0]["orphan_pid"]
    while time.monotonic() < deadline and _pid_alive(orphan_pid):
        await asyncio.sleep(0.1)
    assert not _pid_alive(orphan_pid), f"orphan {orphan_pid} survived teardown"


# The child exits immediately; the inherited stdout keeps FLOWING from the
# orphan for longer than one grace period, then goes quiet. Data still
# arriving is a live stream regardless of the child's exit — only silence for
# a whole grace ends it. A deadline shared across reads (grace measured from
# the exit, not from each read) truncates this mid-flow.
_ORPHAN_KEEPS_WRITING = (
    "import subprocess, sys\n"
    "code = (\n"
    "    'import json, time\\n'\n"
    "    'for i in range(5):\\n'\n"
    "    '    print(json.dumps({\"i\": i}), flush=True)\\n'\n"
    "    '    time.sleep(0.25)\\n'\n"
    "    'time.sleep(30)\\n'\n"
    ")\n"
    "subprocess.Popen([sys.executable, '-c', code], stdout=sys.stdout)\n"
    "sys.exit(0)\n"
)


@pytest.mark.asyncio
async def test_data_still_flowing_after_exit_is_delivered_in_full(monkeypatch):
    monkeypatch.setattr(cli_subprocess, "_POST_EXIT_DRAIN_GRACE", 0.5)

    start = time.monotonic()
    events = []
    async for obj in ndjson_from_cli([sys.executable, "-c", _ORPHAN_KEEPS_WRITING]):
        events.append(obj)
    elapsed = time.monotonic() - start

    assert [e["i"] for e in events] == list(range(5)), (
        f"delivered {len(events)}/5 post-exit events — the drain truncated a flowing stream"
    )
    # After the writer goes quiet, one grace ends the stream — not the 30s sleep.
    assert elapsed < 10, f"stream took {elapsed:.1f}s after the writer went quiet"


@pytest.mark.asyncio
async def test_living_child_is_never_time_boxed(monkeypatch):
    """A quiet-but-alive child streams past the grace untouched: the bound
    keys on exit, not on silence."""
    monkeypatch.setattr(cli_subprocess, "_POST_EXIT_DRAIN_GRACE", 0.5)

    events = []
    async for obj in ndjson_from_cli([sys.executable, "-c", _SLOW_BUT_ALIVE]):
        events.append(obj)

    assert [e["n"] for e in events] == [1, 2], events


@pytest.mark.asyncio
async def test_clean_exit_without_orphans_is_unchanged():
    events = []
    async for obj in ndjson_from_cli([sys.executable, "-c", "print('{\"ok\": true}', flush=True)"]):
        events.append(obj)
    assert events == [{"ok": True}]
