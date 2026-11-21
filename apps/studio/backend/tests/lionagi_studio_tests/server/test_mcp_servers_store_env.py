# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Studio's MCP connection check starts a configured server without the inherited store settings.

The probe stands in for the server: it writes the store URL and migration opt-in it sees, in both
spellings, then exits, so the check fails once the probe has written what it saw.
"""

from __future__ import annotations

import os
import stat

import pytest

from lionagi.ln._proc import STATE_DB_URL_ENV

STORE_URL = "sqlite:////daemon/store.db"
LOWER = STATE_DB_URL_ENV.lower()
AUTO_MIGRATE = "LIONAGI_STATE_AUTO_MIGRATE"
AUTO_MIGRATE_LOWER = AUTO_MIGRATE.lower()
SCRUBBED_STORE_SETTINGS = ["<absent>"] * 4


@pytest.fixture
def probe(tmp_path, monkeypatch):
    script = tmp_path / "storeprobe"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "${{{STATE_DB_URL_ENV}-<absent>}}" "${{{LOWER}-<absent>}}" '
        f'"${{{AUTO_MIGRATE}-<absent>}}" "${{{AUTO_MIGRATE_LOWER}-<absent>}}" > "$1"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv(STATE_DB_URL_ENV, STORE_URL)
    monkeypatch.setenv(LOWER, STORE_URL)
    monkeypatch.setenv(AUTO_MIGRATE, "1")
    monkeypatch.setenv(AUTO_MIGRATE_LOWER, "1")
    return script


@pytest.mark.asyncio
async def test_a_configured_stdio_mcp_server_does_not_see_the_store(probe, tmp_path):
    pytest.importorskip("fastmcp")
    from lionagi_studio.services.mcp_servers import _attempt_connection

    checked = tmp_path / "mcp-check.txt"
    result = await _attempt_connection({"command": str(probe), "args": [str(checked)]})
    assert result["ok"] is False
    assert checked.read_text().splitlines()[:4] == SCRUBBED_STORE_SETTINGS
    # The daemon's own environment is untouched.
    assert os.environ[STATE_DB_URL_ENV] == STORE_URL


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [STATE_DB_URL_ENV, LOWER])
async def test_a_store_url_a_configured_mcp_server_names_is_passed_as_given(probe, tmp_path, name):
    """A server's own ``env`` entry is configuration, not inheritance: the URL it names arrives,
    and the other spelling, inherited, is still gone."""
    pytest.importorskip("fastmcp")
    from lionagi_studio.services.mcp_servers import _attempt_connection

    chosen = "sqlite:////chosen/store.db"
    expected = SCRUBBED_STORE_SETTINGS.copy()
    expected[0 if name == STATE_DB_URL_ENV else 1] = chosen
    checked = tmp_path / "mcp-check-env.txt"
    result = await _attempt_connection(
        {"command": str(probe), "args": [str(checked)], "env": {name: chosen}}
    )
    assert result["ok"] is False
    assert checked.read_text().splitlines()[:4] == expected
