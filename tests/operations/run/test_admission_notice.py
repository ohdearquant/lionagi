# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The notice a run listens for while a CLI agent waits for a machine-wide slot.

The admission that gives the notice is the hub's (``lionhub.providers._subagent_admission``);
the notice path a run listens on is core, so these tests import nothing from the hub and run
where no hub is installed. Without the hub nothing starts a CLI agent, so a run hears no notice
and times its first output as it always has.
"""

from __future__ import annotations

import ast
import contextlib
import json
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import lionagi.operations.run.run as run_module
from lionagi.operations.run.run import _stream_with_liveness
from lionagi.service import _admission_notice

ROOT = Path(__file__).resolve().parents[3]


class _Chunks:
    def __init__(self, chunks):
        self._chunks = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self):
        return None


def test_the_notice_module_imports_nothing_from_the_hub():
    tree = ast.parse((ROOT / "lionagi" / "service" / "_admission_notice.py").read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported == {"__future__", "collections.abc", "contextlib", "contextvars"}


def test_a_notice_reaches_only_the_listener_set_in_its_context():
    heard: list[bool] = []
    # No listener: the notice goes nowhere and raises nothing.
    _admission_notice._tell_admission(False)
    with _admission_notice._on_subagent_admission(heard.append):
        _admission_notice._tell_admission(False)
        _admission_notice._tell_admission(True)
    _admission_notice._tell_admission(True)
    assert heard == [False, True]


async def test_a_run_that_starts_no_cli_agent_hears_no_notice_and_completes(monkeypatch):
    listeners: list = []
    heard: list[bool] = []
    listen = run_module._on_subagent_admission

    @contextlib.contextmanager
    def recording(callback):
        listeners.append(callback)

        def relay(admitted: bool) -> None:
            heard.append(admitted)
            callback(admitted)

        with listen(relay):
            yield

    monkeypatch.setattr(run_module, "_on_subagent_admission", recording)
    monkeypatch.setattr(
        run_module, "_stream_with_deadline", lambda model, api_call, deadline: _Chunks(["a", "b"])
    )
    model = types.SimpleNamespace(
        create_event=AsyncMock(return_value=types.SimpleNamespace(id="call-1"))
    )

    chunks = [
        chunk
        async for chunk in _stream_with_liveness(
            model, {}, stream_deadline=None, liveness_timeout=5, api_call_holder=[]
        )
    ]

    assert chunks == ["a", "b"]
    # The run listened while it waited for the first output, and nothing spoke.
    assert len(listeners) == 1
    assert heard == []


# Runs the same stream in a fresh interpreter in which no ``lionhub`` module can be imported,
# whether or not one is installed beside the core.
_WITHOUT_THE_HUB = r"""
import asyncio
import contextlib
import importlib.abc
import json
import sys
import types
from unittest.mock import AsyncMock


class _NoHub(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "lionhub" or name.startswith("lionhub."):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None


sys.meta_path.insert(0, _NoHub())

import lionagi.operations.run.run as run_module

listeners, heard = [], []
listen = run_module._on_subagent_admission


@contextlib.contextmanager
def recording(callback):
    listeners.append(callback)

    def relay(admitted):
        heard.append(admitted)
        callback(admitted)

    with listen(relay):
        yield


class Chunks:
    def __init__(self):
        self._chunks = iter(["a", "b"])

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self):
        return None


run_module._on_subagent_admission = recording
run_module._stream_with_deadline = lambda model, api_call, deadline: Chunks()
model = types.SimpleNamespace(create_event=AsyncMock(return_value=types.SimpleNamespace(id="c")))


async def main():
    return [
        chunk
        async for chunk in run_module._stream_with_liveness(
            model, {}, stream_deadline=None, liveness_timeout=5, api_call_holder=[]
        )
    ]


chunks = asyncio.run(main())
hub = sorted(name for name in sys.modules if name.split(".")[0] == "lionhub")
print(json.dumps({"chunks": chunks, "listeners": len(listeners), "heard": heard, "hub": hub}))
"""


def test_a_run_without_the_hub_installed_hears_no_notice_and_does_not_fail():
    done = subprocess.run(
        [sys.executable, "-c", _WITHOUT_THE_HUB],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert result == {"chunks": ["a", "b"], "listeners": 1, "heard": [], "hub": []}
