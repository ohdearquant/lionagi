# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A configured command never inherits the state-store URL; every `li` child of the scheduler does.

A command is an arbitrary program, and when it runs another interpreter with a different lionagi,
an inherited LIONAGI_STATE_DB_URL points that lionagi at its parent's store. These tests spawn real
children through each path that runs a configured command and read what each one saw.
"""

from __future__ import annotations

import os
import stat

import pytest

from lionagi.ln._proc import STATE_DB_URL_ENV, communicate_in_process_group
from lionagi.state.lifecycle.notify_settings import _make_exec_handler
from lionagi.studio.scheduler.subprocess import spawn_and_wait

ALLOWLIST_ENV = "LIONAGI_SCHEDULER_COMMAND_ALLOWLIST"
STORE_URL = "sqlite:////daemon/store.db"
LOWER = STATE_DB_URL_ENV.lower()
PROBE = "storeprobe"


@pytest.fixture
def probe(tmp_path, monkeypatch):
    """An executable on PATH that writes the store URL it sees (both spellings) and its invocation
    id to argv[1]."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / PROBE
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n%s\\n%s\\n" "${{{STATE_DB_URL_ENV}-<absent>}}" "${{{LOWER}-<absent>}}" '
        '"${LIONAGI_INVOCATION_ID-<absent>}" > "$1"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv(ALLOWLIST_ENV, PROBE)
    monkeypatch.setenv(STATE_DB_URL_ENV, STORE_URL)
    monkeypatch.setenv(LOWER, STORE_URL)
    return script


async def _seen(argv: list[str], out, action_kind: str | None) -> list[str]:
    exit_code, tail = await spawn_and_wait(
        [*argv, str(out)], "inv-store-env", action_kind=action_kind, deadline_seconds=30
    )
    assert exit_code == 0, tail
    return out.read_text().splitlines()


@pytest.mark.asyncio
async def test_a_command_child_does_not_see_the_daemon_store(probe, tmp_path):
    seen = await _seen([PROBE], tmp_path / "command.txt", "command")
    assert seen == ["<absent>", "<absent>", "inv-store-env"]
    # The daemon's own environment is untouched.
    assert os.environ[STATE_DB_URL_ENV] == STORE_URL


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "flow", "fanout", "play", "flow_yaml", "engine", None])
async def test_every_other_child_still_records_into_the_daemon_store(probe, tmp_path, kind):
    seen = await _seen([str(probe)], tmp_path / f"{kind}.txt", kind)
    assert seen == [STORE_URL, STORE_URL, "inv-store-env"]


@pytest.mark.asyncio
async def test_a_hook_or_outbox_command_does_not_see_the_store(probe, tmp_path):
    out = tmp_path / "hook.txt"
    returncode, _, stderr = await communicate_in_process_group(
        [str(probe), str(out)], b"", timeout=30
    )
    assert returncode == 0, stderr
    assert out.read_text().splitlines()[:2] == ["<absent>", "<absent>"]


@pytest.mark.asyncio
async def test_a_notify_command_does_not_see_the_store(probe, tmp_path):
    out = tmp_path / "notify.txt"
    handler = _make_exec_handler([str(probe), str(out)], payload_fn=lambda envelope: {})
    await handler(object())
    assert out.read_text().splitlines()[:2] == ["<absent>", "<absent>"]
