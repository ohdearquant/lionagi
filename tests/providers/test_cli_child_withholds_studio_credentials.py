# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""No CLI child is handed the Studio daemon's bearer or a remote Studio's token.

With either one a child could ask a daemon to launch further runs, here or on another
machine. The spawn removes them whatever environment the caller passed, and the
command-environment scrub removes the remote tokens from tool and MCP server
environments too.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys

import pytest

from lionagi._command_env import scrub_command_env
from lionhub.providers._cli_subprocess import ndjson_from_cli

TOKEN = "example-bearer-value"  # noqa: S105 — a test value, never a real credential
_NAMES = ("LIONAGI_STUDIO_AUTH_TOKEN", "LIONAGI_REMOTE_LEGBOX_TOKEN", "lionagi_remote_other")
_PRINT_ENV = (
    "import json, os, sys; "
    "names = json.loads(sys.argv[1]); "
    "sys.stdout.write(json.dumps({'seen': sorted(n for n in names if n in os.environ), "
    "'control': os.environ.get('LIONAGI_TEST_CONTROL')}) + '\\n')"
)


async def _child_sees(**kwargs) -> dict:
    stream = ndjson_from_cli(
        [sys.executable, "-c", _PRINT_ENV, json.dumps([*_NAMES])], engine="codex", **kwargs
    )
    async with contextlib.aclosing(stream):
        [line] = [obj async for obj in stream]
    return line


@pytest.mark.asyncio
async def test_an_inherited_studio_credential_does_not_reach_the_child(monkeypatch):
    for name in _NAMES:
        monkeypatch.setenv(name, TOKEN)
    monkeypatch.setenv("LIONAGI_TEST_CONTROL", "kept")

    seen = await _child_sees()

    assert seen == {"seen": [], "control": "kept"}


@pytest.mark.asyncio
async def test_a_studio_credential_in_a_passed_environment_does_not_reach_the_child():
    env = {**os.environ, **dict.fromkeys(_NAMES, TOKEN), "LIONAGI_TEST_CONTROL": "kept"}

    seen = await _child_sees(env=env)

    assert seen == {"seen": [], "control": "kept"}


def test_the_command_scrub_removes_remote_tokens():
    parent = {
        "LIONAGI_REMOTE_LEGBOX_TOKEN": TOKEN,
        "LIONAGI_STUDIO_AUTH_TOKEN": TOKEN,
        "PATH": "/bin",
    }
    assert scrub_command_env(parent, declared_names=()) == {"PATH": "/bin"}
