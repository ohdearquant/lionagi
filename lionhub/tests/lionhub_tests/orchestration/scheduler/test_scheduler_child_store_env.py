# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Configured commands omit inherited state-store settings; scheduled `li` commands keep them.

A command is an arbitrary program, and when it runs another interpreter with a different lionagi,
an inherited store URL points that lionagi at its parent's store. An inherited migration opt-in
can also affect the default store. These probes read the child environment without opening a store.
"""

from __future__ import annotations

import os
import stat

import anyio
import pytest

from lionagi.ln._proc import STATE_DB_URL_ENV, communicate_in_process_group
from lionagi.state.lifecycle.notify_settings import _make_exec_handler
from lionhub.orchestration.scheduler.subprocess import spawn_and_wait

ALLOWLIST_ENV = "LIONAGI_SCHEDULER_COMMAND_ALLOWLIST"
STORE_URL = "sqlite:////daemon/store.db"
LOWER = STATE_DB_URL_ENV.lower()
AUTO_MIGRATE = "LIONAGI_STATE_AUTO_MIGRATE"
AUTO_MIGRATE_LOWER = AUTO_MIGRATE.lower()
SCRUBBED_STORE_SETTINGS = ["<absent>"] * 4
PROBE = "storeprobe"


@pytest.fixture
def probe(tmp_path, monkeypatch):
    """Write both spellings of the store URL and migration opt-in, then the invocation id."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / PROBE
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "${{{STATE_DB_URL_ENV}-<absent>}}" "${{{LOWER}-<absent>}}" '
        f'"${{{AUTO_MIGRATE}-<absent>}}" "${{{AUTO_MIGRATE_LOWER}-<absent>}}" '
        '"${LIONAGI_INVOCATION_ID-<absent>}" > "$1"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv(ALLOWLIST_ENV, PROBE)
    monkeypatch.setenv(STATE_DB_URL_ENV, STORE_URL)
    monkeypatch.setenv(LOWER, STORE_URL)
    monkeypatch.setenv(AUTO_MIGRATE, "1")
    monkeypatch.setenv(AUTO_MIGRATE_LOWER, "1")
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
    assert seen == [*SCRUBBED_STORE_SETTINGS, "inv-store-env"]
    # The daemon's own environment is untouched.
    assert os.environ[STATE_DB_URL_ENV] == STORE_URL
    assert os.environ[AUTO_MIGRATE] == "1"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "flow", "fanout", "play", "flow_yaml", "engine", None])
async def test_every_other_child_still_records_into_the_daemon_store(probe, tmp_path, kind):
    seen = await _seen([str(probe)], tmp_path / f"{kind}.txt", kind)
    assert seen == [STORE_URL, STORE_URL, "1", "1", "inv-store-env"]


@pytest.mark.asyncio
async def test_a_hook_or_outbox_command_does_not_see_the_store(probe, tmp_path):
    out = tmp_path / "hook.txt"
    returncode, _, stderr = await communicate_in_process_group(
        [str(probe), str(out)], b"", timeout=30
    )
    assert returncode == 0, stderr
    assert out.read_text().splitlines()[:4] == SCRUBBED_STORE_SETTINGS


@pytest.mark.asyncio
async def test_a_notify_command_does_not_see_the_store(probe, tmp_path):
    out = tmp_path / "notify.txt"
    handler = _make_exec_handler([str(probe), str(out)], payload_fn=lambda envelope: {})
    await handler(object())
    assert out.read_text().splitlines()[:4] == SCRUBBED_STORE_SETTINGS


@pytest.mark.asyncio
async def test_an_external_hook_does_not_see_the_store(probe, tmp_path):
    from lionhub.hooks.external import _run_hook_process

    out = tmp_path / "external-hook.txt"
    # A hook reads the envelope on its stdin; this one reads it, then runs the probe.
    hook = tmp_path / "hook"
    hook.write_text(f'#!/bin/sh\ncat > /dev/null\nexec "{probe}" "$1"\n')
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    returncode, _, _ = await _run_hook_process(
        [str(hook), str(out)], None, str(tmp_path), b"{}", 30
    )
    assert returncode == 0
    assert out.read_text().splitlines()[:4] == SCRUBBED_STORE_SETTINGS


def test_an_mcp_terminal_notice_does_not_see_the_store(probe, tmp_path):
    from lionhub.serve.mcp._notify_hook import _deliver

    out = tmp_path / "terminal-notice.txt"
    _deliver([str(probe), str(out)], {}, None, program=PROBE, cwd=str(tmp_path), timeout=30)
    assert out.read_text().splitlines()[:4] == SCRUBBED_STORE_SETTINGS


@pytest.mark.asyncio
async def test_a_secrets_lookup_command_does_not_see_the_store(probe, tmp_path):
    from lionagi.service.accounts._secret_resolution import (
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
        assert out.read_text().splitlines()[:4] == SCRUBBED_STORE_SETTINGS


def test_a_command_an_agent_runs_does_not_see_the_store(probe, tmp_path):
    from lionhub.tools._subprocess import _subprocess_sync

    out = tmp_path / "agent-command.txt"
    result = _subprocess_sync([str(probe), str(out)], False, 30, str(tmp_path))
    assert result["returncode"] == 0, result
    assert out.read_text().splitlines()[:4] == SCRUBBED_STORE_SETTINGS


async def _pool_connect_fails(config: dict) -> None:
    """Start *config* through the pooled connect, which fails: the probe is no MCP server.

    The pooled connect has no deadline of its own, and a server that exits during the handshake
    can leave it waiting for an answer that never comes (seen on Linux), so the wait is bounded
    here. Either way the probe has already written what it saw.
    """
    from lionagi.service.connections.mcp_wrapper import MCPConnectionPool, MCPSecurityConfig

    with pytest.raises(Exception):
        with anyio.fail_after(10):
            await MCPConnectionPool._create_client(config, security=MCPSecurityConfig.trusted())


@pytest.mark.asyncio
async def test_a_configured_stdio_mcp_server_does_not_see_the_store(probe, tmp_path):
    """Studio's connection check starts the same server; it is checked with the application's tests."""
    pytest.importorskip("fastmcp")

    pooled = tmp_path / "mcp-pool.txt"
    # The probe is no MCP server: the connection fails once the probe has written what it saw.
    await _pool_connect_fails({"command": str(probe), "args": [str(pooled)]})
    assert pooled.read_text().splitlines()[:4] == SCRUBBED_STORE_SETTINGS


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [STATE_DB_URL_ENV, LOWER])
async def test_a_store_url_a_configured_mcp_server_names_is_passed_as_given(probe, tmp_path, name):
    """A server's own ``env`` entry is configuration, not inheritance: the URL it names arrives,
    and the other spelling, inherited, is still gone."""
    pytest.importorskip("fastmcp")

    chosen = "sqlite:////chosen/store.db"
    expected = SCRUBBED_STORE_SETTINGS.copy()
    expected[0 if name == STATE_DB_URL_ENV else 1] = chosen
    pooled = tmp_path / "mcp-pool-env.txt"
    await _pool_connect_fails({"command": str(probe), "args": [str(pooled)], "env": {name: chosen}})
    assert pooled.read_text().splitlines()[:4] == expected


@pytest.mark.asyncio
async def test_an_environment_a_caller_names_is_passed_as_given(probe, tmp_path):
    """Honor the caller's explicit store URL and migration setting."""
    from lionhub.tools._subprocess import _subprocess_sync

    chosen = "sqlite:////chosen/store.db"
    explicit_env = {STATE_DB_URL_ENV: chosen, AUTO_MIGRATE: "0"}
    agent_out, notify_out = tmp_path / "agent-explicit.txt", tmp_path / "notify-explicit.txt"
    result = _subprocess_sync(
        [str(probe), str(agent_out)], False, 30, str(tmp_path), env=explicit_env
    )
    assert result["returncode"] == 0, result
    assert agent_out.read_text().splitlines()[:4] == [chosen, "<absent>", "0", "<absent>"]

    handler = _make_exec_handler(
        [str(probe), str(notify_out)],
        payload_fn=lambda envelope: {},
        env_fn=lambda envelope: explicit_env,
    )
    await handler(object())
    # The inherited lowercase spelling is still gone; the named one arrives.
    assert notify_out.read_text().splitlines()[:4] == [chosen, "<absent>", "0", "<absent>"]
