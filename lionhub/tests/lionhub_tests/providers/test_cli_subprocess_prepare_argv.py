# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A command line rendered at spawn time is rendered on a worker thread. Rendering can block (a
codex launch reads its binary's version with a subprocess and the binary's digest from the file),
and the event loop goes on running every other task meanwhile."""

from __future__ import annotations

import asyncio
import sys
import threading
from contextlib import aclosing

import pytest

from lionagi._errors import CLIInputRefusedError


@pytest.mark.asyncio(loop_scope="function")
async def test_the_spawn_time_command_line_is_rendered_off_the_event_loop(monkeypatch):
    from lionhub.providers import _cli_subprocess as cli

    started = threading.Event()
    freed = threading.Event()
    released = []

    class Lease:
        slots = []

        def release(self):
            released.append(True)

    async def admit(**kwargs):
        return Lease()

    async def fill(env):
        return env, frozenset()

    async def spawn(*args, **kwargs):
        raise AssertionError("a refused request started a subprocess")

    def prepare(_env):
        started.set()
        # Set by a task on the event loop, which runs only while the loop is free.
        if not freed.wait(5):
            raise CLIInputRefusedError("the event loop was held while the command line rendered")
        raise CLIInputRefusedError("rendered while the event loop ran")

    async def free_once_started():
        while not started.is_set():
            await asyncio.sleep(0.01)
        freed.set()

    monkeypatch.setattr(cli, "admit_subagent", admit)
    monkeypatch.setattr(cli, "fill_declared_secrets_and_names", fill)
    monkeypatch.setattr(cli, "refuse_nested_agent", lambda _engine: None)
    monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", spawn)

    helper = asyncio.create_task(free_once_started())
    stream = cli.ndjson_from_cli([sys.executable], env={}, _prepare_argv=prepare)
    with pytest.raises(CLIInputRefusedError, match="rendered while the event loop ran"):
        async with aclosing(stream):
            async for _ in stream:
                pass
    await helper
    # Refused before any spawn was attempted, so the slot is given back once.
    assert released == [True]
