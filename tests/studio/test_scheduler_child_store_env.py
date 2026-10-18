# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A command-kind child never inherits the daemon's store URL; every other kind does.

A command is an arbitrary program, and when it runs another interpreter with a different
lionagi, an inherited LIONAGI_STATE_DB_URL points that lionagi at the daemon's store. These
tests spawn real children and read what each one saw.
"""

from __future__ import annotations

import os
import stat

import pytest

from lionagi.studio.scheduler.subprocess import STORE_URL_ENV, spawn_and_wait

ALLOWLIST_ENV = "LIONAGI_SCHEDULER_COMMAND_ALLOWLIST"
STORE_URL = "sqlite:////daemon/store.db"
PROBE = "storeprobe"


@pytest.fixture
def probe(tmp_path, monkeypatch):
    """An executable on PATH that writes the store URL it sees, and its invocation id, to argv[1]."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / PROBE
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n%s\\n" "${{{STORE_URL_ENV}-<absent>}}" "${{LIONAGI_INVOCATION_ID-<absent>}}" > "$1"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv(ALLOWLIST_ENV, PROBE)
    monkeypatch.setenv(STORE_URL_ENV, STORE_URL)
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
    assert seen == ["<absent>", "inv-store-env"]
    # The daemon's own environment is untouched.
    assert os.environ[STORE_URL_ENV] == STORE_URL


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "flow", "fanout", "play", "flow_yaml", "engine", None])
async def test_every_other_child_still_records_into_the_daemon_store(probe, tmp_path, kind):
    seen = await _seen([str(probe)], tmp_path / f"{kind}.txt", kind)
    assert seen == [STORE_URL, "inv-store-env"]
