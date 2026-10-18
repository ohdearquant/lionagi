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


@pytest.mark.asyncio
async def test_an_external_hook_does_not_see_the_store(probe, tmp_path):
    from lionagi.hooks.external import _run_hook_process

    out = tmp_path / "external-hook.txt"
    returncode, _, _ = await _run_hook_process(
        [str(probe), str(out)], None, str(tmp_path), b"{}", 30
    )
    assert returncode == 0
    assert out.read_text().splitlines()[:2] == ["<absent>", "<absent>"]


def test_an_mcp_terminal_notice_does_not_see_the_store(probe, tmp_path):
    from lionagi.mcp._notify_hook import _deliver

    out = tmp_path / "terminal-notice.txt"
    _deliver([str(probe), str(out)], {}, None, program=PROBE, cwd=str(tmp_path), timeout=30)
    assert out.read_text().splitlines()[:2] == ["<absent>", "<absent>"]


@pytest.mark.asyncio
async def test_a_secrets_lookup_command_does_not_see_the_store(probe, tmp_path):
    from lionagi.providers._secret_resolution import (
        ResolvedSecretLookup,
        _run_lookup,
        lookup_one_name,
    )

    first, second = tmp_path / "lookup.txt", tmp_path / "lookup-one.txt"
    await _run_lookup(ResolvedSecretLookup(argv=(str(probe), str(first)), names=("KEY",)), "KEY")
    await lookup_one_name(
        ResolvedSecretLookup(argv=(str(probe), str(second)), names=("KEY",)),
        "KEY",
        env=dict(os.environ),
        max_bytes=64,
    )
    for out in (first, second):
        assert out.read_text().splitlines()[:2] == ["<absent>", "<absent>"]


def test_a_command_an_agent_runs_does_not_see_the_store(probe, tmp_path):
    from lionagi.tools._subprocess import _subprocess_sync

    out = tmp_path / "agent-command.txt"
    result = _subprocess_sync([str(probe), str(out)], False, 30, str(tmp_path))
    assert result["returncode"] == 0, result
    assert out.read_text().splitlines()[:2] == ["<absent>", "<absent>"]


@pytest.mark.asyncio
async def test_a_configured_stdio_mcp_server_does_not_see_the_store(probe, tmp_path):
    pytest.importorskip("fastmcp")
    from lionagi.service.connections.mcp_wrapper import MCPConnectionPool, MCPSecurityConfig
    from lionagi.studio.services.mcp_servers import _attempt_connection

    checked, pooled = tmp_path / "mcp-check.txt", tmp_path / "mcp-pool.txt"
    # The probe is no MCP server: each connection fails once the probe has written what it saw.
    result = await _attempt_connection({"command": str(probe), "args": [str(checked)]})
    assert result["ok"] is False
    with pytest.raises(Exception):
        await MCPConnectionPool._create_client(
            {"command": str(probe), "args": [str(pooled)]},
            security=MCPSecurityConfig.trusted(),
        )
    for out in (checked, pooled):
        assert out.read_text().splitlines()[:2] == ["<absent>", "<absent>"]


@pytest.mark.asyncio
async def test_an_environment_a_caller_names_is_passed_as_given(probe, tmp_path):
    """Only what a command would inherit is scrubbed: a store URL its caller names is the
    caller's choice."""
    from lionagi.tools._subprocess import _subprocess_sync

    chosen = "sqlite:////chosen/store.db"
    agent_out, notify_out = tmp_path / "agent-explicit.txt", tmp_path / "notify-explicit.txt"
    result = _subprocess_sync(
        [str(probe), str(agent_out)], False, 30, str(tmp_path), env={STATE_DB_URL_ENV: chosen}
    )
    assert result["returncode"] == 0, result
    assert agent_out.read_text().splitlines()[:2] == [chosen, "<absent>"]

    handler = _make_exec_handler(
        [str(probe), str(notify_out)],
        payload_fn=lambda envelope: {},
        env_fn=lambda envelope: {STATE_DB_URL_ENV: chosen},
    )
    await handler(object())
    # The inherited lowercase spelling is still gone; the named one arrives.
    assert notify_out.read_text().splitlines()[:2] == [chosen, "<absent>"]
