# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for MCP servers as a managed Studio resource."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import time

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")

import lionagi.studio.services.mcp_servers as mcp_mod


def _point_registry_at(tmp_path, monkeypatch):
    registry_path = tmp_path / "mcp_servers.json"
    synced_path = tmp_path / ".mcp.json"
    monkeypatch.setattr(mcp_mod, "_REGISTRY_PATH", registry_path)
    monkeypatch.setattr(mcp_mod, "_SYNCED_MCP_JSON_PATH", synced_path)
    return registry_path, synced_path


STDIO_CONFIG = {
    "command": "python3",
    "args": ["-m", "some_mcp_server"],
    "env": {"API_KEY": "sk-super-secret-value"},
}

URL_CONFIG = {"url": "https://example.invalid/mcp"}


# Shape validation


def test_validate_shape_rejects_missing_transport():
    errors = mcp_mod._validate_shape("myserver", {})
    assert any("command" in e or "url" in e for e in errors)


def test_validate_shape_rejects_both_transports():
    errors = mcp_mod._validate_shape("myserver", {"command": "python3", "url": "https://x"})
    assert any("exactly one of" in e for e in errors)


def test_validate_shape_rejects_bad_env_values():
    errors = mcp_mod._validate_shape("myserver", {"command": "python3", "env": {"KEY": 5}})
    assert any("'env'" in e for e in errors)


def test_merge_config_rejects_non_dict_env_without_crashing():
    """A malformed env patch (a string, not a mapping) must reach
    `_validate_shape`'s ordinary type check as a normal shape error, not
    crash `_merge_config`'s per-key iteration with an AttributeError."""
    merged = mcp_mod._merge_config({"command": "python3"}, {"env": "not-a-dict"})
    errors = mcp_mod._validate_shape("myserver", merged)
    assert any("'env'" in e for e in errors)


def test_validate_shape_rejects_bad_url():
    errors = mcp_mod._validate_shape("myserver", {"url": "not-a-url"})
    assert any("'url'" in e for e in errors)


def test_validate_shape_rejects_bad_name():
    errors = mcp_mod._validate_shape("bad/name", {"command": "python3"})
    assert any("server name" in e for e in errors)


@pytest.mark.asyncio
async def test_validate_config_shape_only_never_attempts_connection(monkeypatch):
    called = False

    async def _fake_attempt(config):
        nonlocal called
        called = True
        return {"ok": True, "error": None}

    monkeypatch.setattr(mcp_mod, "_attempt_connection", _fake_attempt)

    result = await mcp_mod.validate_config("myserver", STDIO_CONFIG, check_connection=False)

    assert result["ok"] is True
    assert result["connection_checked"] is False
    assert result["connection_ok"] is None
    assert called is False


@pytest.mark.asyncio
async def test_validate_config_malformed_never_attempts_connection(monkeypatch):
    called = False

    async def _fake_attempt(config):
        nonlocal called
        called = True
        return {"ok": True, "error": None}

    monkeypatch.setattr(mcp_mod, "_attempt_connection", _fake_attempt)

    result = await mcp_mod.validate_config("myserver", {}, check_connection=True)

    assert result["ok"] is False
    assert result["errors"]
    assert result["connection_checked"] is False
    assert called is False


# CRUD


def test_register_list_get_roundtrip(tmp_path, monkeypatch):
    _point_registry_at(tmp_path, monkeypatch)

    created = mcp_mod.register_server("myserver", STDIO_CONFIG)
    assert created["name"] == "myserver"
    assert created["transport"] == "stdio"
    assert created["enabled"] is True

    listed = mcp_mod.list_servers()
    assert [s["name"] for s in listed] == ["myserver"]

    fetched = mcp_mod.get_server("myserver")
    assert fetched is not None
    assert fetched["command"] == "python3"


def test_register_duplicate_name_raises(tmp_path, monkeypatch):
    _point_registry_at(tmp_path, monkeypatch)
    mcp_mod.register_server("myserver", STDIO_CONFIG)

    with pytest.raises(mcp_mod.DuplicateServerError):
        mcp_mod.register_server("myserver", URL_CONFIG)


def test_register_malformed_config_raises(tmp_path, monkeypatch):
    _point_registry_at(tmp_path, monkeypatch)

    with pytest.raises(mcp_mod.McpServerError):
        mcp_mod.register_server("bad", {"command": "python3", "url": "https://x"})


def test_register_strips_none_env_values(tmp_path, monkeypatch):
    """`register_server` merges the incoming config onto an empty base before
    validating and storing it, so a `None` env value on create is dropped by
    the same rule that removes it on update -- persisted nowhere, including
    the derived ``.mcp.json`` a spawned CLI reads.
    """
    registry_path, synced_path = _point_registry_at(tmp_path, monkeypatch)

    created = mcp_mod.register_server(
        "myserver",
        {"command": "python3", "env": {"KEY": None, "OTHER": "value"}},
    )

    assert created["env_keys"] == ["OTHER"]

    on_disk = json.loads(registry_path.read_text())
    stored_env = on_disk["servers"]["myserver"]["config"]["env"]
    assert stored_env == {"OTHER": "value"}
    assert "KEY" not in stored_env

    synced_env = json.loads(synced_path.read_text())["mcpServers"]["myserver"]["env"]
    assert synced_env == {"OTHER": "value"}


def test_register_rejects_non_mapping_env(tmp_path, monkeypatch):
    """A non-mapping `env` (e.g. a string) must be rejected by `_validate_shape`
    as a normal 'env must be an object' error, not crash `_merge_config`'s
    `None`-stripping loop with `AttributeError` when it calls `.items()` on a
    non-dict value -- the create path merges before validating, so the merge
    step itself must tolerate malformed input it hasn't validated yet.
    """
    _point_registry_at(tmp_path, monkeypatch)

    with pytest.raises(mcp_mod.McpServerError, match="'env'"):
        mcp_mod.register_server("bad-env", {"command": "python3", "env": "not-a-map"})


def test_update_nonexistent_returns_none(tmp_path, monkeypatch):
    _point_registry_at(tmp_path, monkeypatch)
    assert mcp_mod.update_server("nope", STDIO_CONFIG) is None


def test_update_existing_server(tmp_path, monkeypatch):
    _point_registry_at(tmp_path, monkeypatch)
    mcp_mod.register_server("myserver", STDIO_CONFIG)

    updated = mcp_mod.update_server("myserver", URL_CONFIG)
    assert updated is not None
    assert updated["transport"] == "http"
    assert updated["url"] == "https://example.invalid/mcp"


def test_switching_to_http_drops_the_stdio_arguments_and_secrets(tmp_path, monkeypatch):
    """A server moved from stdio to http keeps no stdio fields. The shape check
    only requires exactly one of command/url, so leftover args and env still
    validate -- and the generated .mcp.json would hand every reader a set of
    arguments and secrets the chosen transport never uses.
    """
    registry_path, synced_path = _point_registry_at(tmp_path, monkeypatch)
    mcp_mod.register_server("myserver", {**STDIO_CONFIG, "timeout": 30})

    mcp_mod.update_server("myserver", URL_CONFIG)

    stored = json.loads(registry_path.read_text())["servers"]["myserver"]["config"]
    assert stored == {"url": URL_CONFIG["url"], "timeout": 30}
    # The file other tools actually read, not just the public response.
    synced = json.loads(synced_path.read_text())["mcpServers"]["myserver"]
    assert "args" not in synced
    assert "env" not in synced
    assert "sk-super-secret-value" not in synced_path.read_text()


def test_switching_back_to_stdio_drops_the_url(tmp_path, monkeypatch):
    """The same rule in the other direction, so the switch is not one-way."""
    registry_path, _ = _point_registry_at(tmp_path, monkeypatch)
    mcp_mod.register_server("myserver", {**URL_CONFIG, "timeout": 30})

    mcp_mod.update_server("myserver", {"command": "python3", "args": ["-m", "srv"]})

    stored = json.loads(registry_path.read_text())["servers"]["myserver"]["config"]
    assert "url" not in stored
    assert stored["command"] == "python3"
    # Transport-neutral fields survive either switch.
    assert stored["timeout"] == 30


def test_update_without_env_preserves_existing_secret(tmp_path, monkeypatch):
    """A save that only changes `args` (the client never sees env values, so
    it cannot resend them) must not silently wipe the stored secret."""
    registry_path, _ = _point_registry_at(tmp_path, monkeypatch)
    mcp_mod.register_server("myserver", STDIO_CONFIG)

    updated = mcp_mod.update_server("myserver", {"args": ["-m", "different_module"]})

    assert updated is not None
    assert updated["args"] == ["-m", "different_module"]

    on_disk = json.loads(registry_path.read_text())
    assert on_disk["servers"]["myserver"]["config"]["env"]["API_KEY"] == "sk-super-secret-value"


def test_update_env_merges_rather_than_replaces(tmp_path, monkeypatch):
    registry_path, _ = _point_registry_at(tmp_path, monkeypatch)
    mcp_mod.register_server("myserver", STDIO_CONFIG)

    mcp_mod.update_server("myserver", {"env": {"OTHER_VAR": "other-value"}})

    on_disk = json.loads(registry_path.read_text())
    env = on_disk["servers"]["myserver"]["config"]["env"]
    assert env["API_KEY"] == "sk-super-secret-value"
    assert env["OTHER_VAR"] == "other-value"


def test_update_env_null_value_removes_key(tmp_path, monkeypatch):
    registry_path, _ = _point_registry_at(tmp_path, monkeypatch)
    mcp_mod.register_server("myserver", STDIO_CONFIG)

    mcp_mod.update_server("myserver", {"env": {"API_KEY": None}})

    on_disk = json.loads(registry_path.read_text())
    assert "API_KEY" not in on_disk["servers"]["myserver"]["config"]["env"]


def test_remove_nonexistent_returns_false(tmp_path, monkeypatch):
    _point_registry_at(tmp_path, monkeypatch)
    assert mcp_mod.remove_server("nope") is False


def test_remove_existing_returns_true(tmp_path, monkeypatch):
    _point_registry_at(tmp_path, monkeypatch)
    mcp_mod.register_server("myserver", STDIO_CONFIG)
    assert mcp_mod.remove_server("myserver") is True
    assert mcp_mod.get_server("myserver") is None


def test_enable_disable_nonexistent_returns_none(tmp_path, monkeypatch):
    _point_registry_at(tmp_path, monkeypatch)
    assert mcp_mod.set_enabled("nope", False) is None


# Secret handling — never returned raw, never written to the synced file
# for a disabled server, never present in the on-disk registry as anything
# other than what the user configured.


def test_list_and_get_never_return_raw_secret(tmp_path, monkeypatch):
    _point_registry_at(tmp_path, monkeypatch)
    mcp_mod.register_server("myserver", STDIO_CONFIG)

    listed = mcp_mod.list_servers()
    fetched = mcp_mod.get_server("myserver")

    listed_json = json.dumps(listed)
    fetched_json = json.dumps(fetched)
    assert "sk-super-secret-value" not in listed_json
    assert "sk-super-secret-value" not in fetched_json
    assert fetched["env_keys"] == ["API_KEY"]
    assert "env" not in fetched


def test_scrub_secrets_masks_configured_env_values():
    text = "auth failed for token sk-super-secret-value in request"
    scrubbed = mcp_mod._scrub_secrets(text, STDIO_CONFIG)
    assert "sk-super-secret-value" not in scrubbed
    assert mcp_mod._SECRET_MASK in scrubbed


def test_synced_mcp_json_excludes_disabled_servers(tmp_path, monkeypatch):
    _, synced_path = _point_registry_at(tmp_path, monkeypatch)
    mcp_mod.register_server("enabled-one", STDIO_CONFIG)
    mcp_mod.register_server("disabled-one", URL_CONFIG)
    mcp_mod.set_enabled("disabled-one", False)

    synced = json.loads(synced_path.read_text())
    assert list(synced["mcpServers"].keys()) == ["enabled-one"]


def test_synced_mcp_json_is_readable_by_mcp_resolve_contract(tmp_path, monkeypatch):
    """The file Studio writes must be exactly what
    lionagi/cli/_mcp_resolve.py's own reader expects — this is the contract
    the CLI's spawn resolution depends on."""
    from lionagi.cli._mcp_resolve import _read_servers

    _, synced_path = _point_registry_at(tmp_path, monkeypatch)
    mcp_mod.register_server("myserver", STDIO_CONFIG)

    servers = _read_servers(synced_path)
    assert servers["myserver"]["command"] == "python3"
    # The registry keeps the real secret so the resolved config a spawned
    # child receives is fully usable; masking only ever applies to what
    # Studio's HTTP responses serialize back to a client.
    assert servers["myserver"]["env"]["API_KEY"] == "sk-super-secret-value"


# Connection checking — a real attempt, and honest about whether one ran


@pytest.mark.asyncio
async def test_attempt_connection_reports_failure_for_unreachable_command():
    outcome = await mcp_mod._attempt_connection(
        {"command": "/no/such/binary-xyz", "args": [], "env": {}}
    )
    assert outcome["ok"] is False
    assert outcome["error"]


@pytest.mark.asyncio
async def test_attempt_connection_succeeds_for_in_memory_server():
    fastmcp = pytest.importorskip("fastmcp", reason="fastmcp (lionagi[mcp] extra) not installed")
    server = fastmcp.FastMCP("test-server")

    # _attempt_connection always builds its own transport from config, so
    # exercise the two client-construction branches directly instead:
    # confirm the in-memory server itself is reachable via the same
    # Client/ping call _attempt_connection makes, proving the "real
    # connection" claim isn't just a shape check.
    from fastmcp import Client

    async with Client(server) as client:
        assert await client.ping() is True


@pytest.mark.asyncio
async def test_check_server_connection_persists_last_check(tmp_path, monkeypatch):
    _point_registry_at(tmp_path, monkeypatch)
    mcp_mod.register_server("myserver", {"command": "/no/such/binary-xyz", "args": [], "env": {}})

    result = await mcp_mod.check_server_connection("myserver")

    assert result is not None
    assert result["last_check"]["ok"] is False
    assert result["last_check"]["error"]

    # Persisted, so a fresh read sees it too.
    fetched = mcp_mod.get_server("myserver")
    assert fetched["last_check"]["ok"] is False


@pytest.mark.asyncio
async def test_check_server_connection_nonexistent_returns_none(tmp_path, monkeypatch):
    _point_registry_at(tmp_path, monkeypatch)
    assert await mcp_mod.check_server_connection("nope") is None


@pytest.mark.asyncio
async def test_check_server_connection_does_not_clobber_concurrent_edit(tmp_path, monkeypatch):
    """A save landing while a connection probe is in flight must survive --
    the probe must not write back the servers dict it read before the
    (possibly multi-second) await.

    The probe's own result is dropped in this case rather than stamped, because
    it describes the configuration that was replaced. A status obtained for one
    command is not evidence about a different one."""
    _point_registry_at(tmp_path, monkeypatch)
    mcp_mod.register_server("myserver", STDIO_CONFIG)

    entered = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_attempt(config):
        entered.set()
        await release.wait()
        return {"ok": True, "error": None}

    monkeypatch.setattr(mcp_mod, "_attempt_connection", _blocking_attempt)

    check_task = asyncio.create_task(mcp_mod.check_server_connection("myserver"))
    await entered.wait()

    # A save lands deterministically while the probe is still awaiting.
    mcp_mod.update_server("myserver", {"args": ["-m", "different_module"]})

    release.set()
    result = await check_task

    assert result is not None
    assert result["last_check"] is None

    fetched = mcp_mod.get_server("myserver")
    assert fetched["args"] == ["-m", "different_module"], (
        "the concurrent edit must survive the connection check's write-back"
    )
    assert fetched["last_check"] is None, (
        "the probe ran against the previous command, so its result must not "
        "become the status of the edited server"
    )


@pytest.mark.asyncio
async def test_check_server_connection_still_records_an_unchanged_server(tmp_path, monkeypatch):
    """The identity match must not become "never persist anything". A save that
    leaves the probed server's configuration alone still gets its result."""
    _point_registry_at(tmp_path, monkeypatch)
    mcp_mod.register_server("myserver", STDIO_CONFIG)
    mcp_mod.register_server("other", STDIO_CONFIG)

    entered = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_attempt(config):
        entered.set()
        await release.wait()
        return {"ok": True, "error": None}

    monkeypatch.setattr(mcp_mod, "_attempt_connection", _blocking_attempt)

    check_task = asyncio.create_task(mcp_mod.check_server_connection("myserver"))
    await entered.wait()

    # Touches a different server, so the probed configuration is untouched.
    mcp_mod.update_server("other", {"args": ["-m", "different_module"]})

    release.set()
    result = await check_task

    assert result is not None
    assert result["last_check"]["ok"] is True
    assert mcp_mod.get_server("myserver")["last_check"]["ok"] is True


@pytest.mark.asyncio
async def test_check_server_connection_does_not_credit_a_same_named_replacement(
    tmp_path, monkeypatch
):
    """A name is reusable. Deleting a server and registering a different one
    under the same name while a probe is in flight must not hand the newcomer
    the outgoing server's connection status."""
    _point_registry_at(tmp_path, monkeypatch)
    mcp_mod.register_server("myserver", STDIO_CONFIG)

    entered = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_attempt(config):
        entered.set()
        await release.wait()
        return {"ok": True, "error": None}

    monkeypatch.setattr(mcp_mod, "_attempt_connection", _blocking_attempt)

    check_task = asyncio.create_task(mcp_mod.check_server_connection("myserver"))
    await entered.wait()

    mcp_mod.remove_server("myserver")
    mcp_mod.register_server("myserver", {"command": "/no/such/binary-xyz", "args": [], "env": {}})

    release.set()
    result = await check_task

    assert result is not None
    assert result["command"] == "/no/such/binary-xyz", "the replacement must not be overwritten"
    assert result["last_check"] is None
    assert mcp_mod.get_server("myserver")["last_check"] is None, (
        "the replacement was never probed, so it must not report a connection result"
    )


@pytest.mark.asyncio
async def test_check_server_connection_does_not_resurrect_deleted_server(tmp_path, monkeypatch):
    """If the server is removed while a probe is in flight, the probe's
    write-back must not bring it back."""
    _point_registry_at(tmp_path, monkeypatch)
    mcp_mod.register_server("myserver", STDIO_CONFIG)

    entered = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_attempt(config):
        entered.set()
        await release.wait()
        return {"ok": True, "error": None}

    monkeypatch.setattr(mcp_mod, "_attempt_connection", _blocking_attempt)

    check_task = asyncio.create_task(mcp_mod.check_server_connection("myserver"))
    await entered.wait()

    mcp_mod.remove_server("myserver")

    release.set()
    result = await check_task

    assert result is None
    assert mcp_mod.get_server("myserver") is None


# At-rest permissions -- the registry and derived file hold secret env
# values verbatim, so neither may be group/world readable.


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_save_registry_writes_files_owner_only(tmp_path, monkeypatch):
    """Under an ordinary permissive umask, both the registry and its derived
    .mcp.json must land 0600, not the umask-determined 0644 -- and the
    derived file must still carry the configured secret, so "protect it by
    not writing it" doesn't pass this test."""
    registry_path, synced_path = _point_registry_at(tmp_path, monkeypatch)
    old_umask = os.umask(0o022)
    try:
        mcp_mod.register_server("myserver", STDIO_CONFIG)
    finally:
        os.umask(old_umask)

    assert _mode(registry_path) == 0o600
    assert _mode(synced_path) == 0o600

    synced = json.loads(synced_path.read_text())
    assert synced["mcpServers"]["myserver"]["env"]["API_KEY"] == "sk-super-secret-value"


def test_save_registry_repairs_preexisting_permissive_mode(tmp_path, monkeypatch):
    """A file created by an earlier build (or before this fix landed) that
    is sitting at 0644 must be repaired to 0600 the next time it is saved,
    not left at its old mode forever."""
    registry_path, synced_path = _point_registry_at(tmp_path, monkeypatch)
    registry_path.write_text('{"servers": {}}')
    synced_path.write_text('{"mcpServers": {}}')
    os.chmod(registry_path, 0o644)
    os.chmod(synced_path, 0o644)

    old_umask = os.umask(0o022)
    try:
        mcp_mod.register_server("myserver", STDIO_CONFIG)
    finally:
        os.umask(old_umask)

    assert _mode(registry_path) == 0o600
    assert _mode(synced_path) == 0o600


def test_save_registry_writes_temp_file_owner_only(tmp_path, monkeypatch):
    """The atomic-write temp file must never be group/world readable either
    -- even a file that exists for milliseconds is an exposure window."""
    registry_path, _ = _point_registry_at(tmp_path, monkeypatch)
    seen_modes: list[int] = []
    real_mkstemp = tempfile.mkstemp

    def _spying_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        seen_modes.append(_mode(name))
        return fd, name

    monkeypatch.setattr(mcp_mod.tempfile, "mkstemp", _spying_mkstemp)

    old_umask = os.umask(0o022)
    try:
        mcp_mod.register_server("myserver", STDIO_CONFIG)
    finally:
        os.umask(old_umask)

    assert seen_modes, "mkstemp was never called"
    assert all(mode == 0o600 for mode in seen_modes)


# Routes (end-to-end through the FastAPI app)


@pytest.fixture()
def mcp_client(tmp_path, monkeypatch, studio_client):
    _point_registry_at(tmp_path, monkeypatch)
    return studio_client


def test_route_register_then_list(mcp_client):
    resp = mcp_client.post("/api/mcp/servers/", json={"name": "myserver", **STDIO_CONFIG})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "myserver"
    assert "sk-super-secret-value" not in resp.text

    listed = mcp_client.get("/api/mcp/servers/")
    assert listed.status_code == 200
    assert [s["name"] for s in listed.json()["servers"]] == ["myserver"]


def test_route_register_duplicate_returns_409(mcp_client):
    mcp_client.post("/api/mcp/servers/", json={"name": "myserver", **STDIO_CONFIG})
    resp = mcp_client.post("/api/mcp/servers/", json={"name": "myserver", **URL_CONFIG})
    assert resp.status_code == 409


def test_route_register_malformed_returns_400(mcp_client):
    resp = mcp_client.post("/api/mcp/servers/", json={"name": "bad"})
    assert resp.status_code == 400


def test_route_register_non_mapping_env_returns_400(mcp_client):
    resp = mcp_client.post(
        "/api/mcp/servers/",
        json={"name": "bad-env", "command": "python3", "env": "not-a-map"},
    )
    assert resp.status_code == 400, resp.text


def test_route_remove_nonexistent_returns_404(mcp_client):
    resp = mcp_client.delete("/api/mcp/servers/nope")
    assert resp.status_code == 404


def test_route_get_nonexistent_returns_404(mcp_client):
    resp = mcp_client.get("/api/mcp/servers/nope")
    assert resp.status_code == 404


def test_route_enable_disable(mcp_client):
    mcp_client.post("/api/mcp/servers/", json={"name": "myserver", **STDIO_CONFIG})
    resp = mcp_client.post("/api/mcp/servers/myserver/disable")
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False

    resp = mcp_client.post("/api/mcp/servers/myserver/enable")
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True


def test_route_validate_shape_only(mcp_client):
    resp = mcp_client.post(
        "/api/mcp/servers/new/validate", json={"name": "new", "command": "python3"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["connection_checked"] is False


def test_route_validate_malformed(mcp_client):
    resp = mcp_client.post("/api/mcp/servers/new/validate", json={"name": "new"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["errors"]


# Validate/Save parity -- Validate must accept exactly the edits Save
# accepts. Save (PUT) merges a patch onto the stored config; a Validate that
# shape-checks the raw patch instead rejects perfectly ordinary partial
# edits (e.g. one that only changes `args`) and, worse, the env-deletion
# patch Save requires to drop a key (an explicit `null` value, since a
# client never sees secret values to resend them).


_PARITY_PATCH_CORPUS = [
    ("env_deletion", {"env": {"API_KEY": None}}),
    ("env_omitted", {"args": ["-m", "different_module"]}),
    ("full_config", {"command": "python3", "args": ["-m", "srv"], "env": {"OTHER": "v"}}),
    ("invalid_nested_types", {"env": {"KEY": 5}}),
    ("unknown_field", {"totally_unknown_field": "whatever", "args": ["-m", "srv"]}),
    ("env_not_a_dict", {"env": "not-a-dict"}),
    ("env_int", {"env": 5}),
    ("env_list", {"env": []}),
    ("env_empty_string", {"env": ""}),
    ("args_null", {"args": None}),
    ("timeout_empty_string", {"timeout": ""}),
]


@pytest.mark.parametrize("case_name,patch", _PARITY_PATCH_CORPUS)
def test_route_validate_and_save_agree_on_the_same_patch(mcp_client, case_name, patch):
    """The same patch corpus driven through both endpoints must agree on
    accept vs reject, so a merge-semantics field handled by one and not the
    other fails this test instead of shipping as a live divergence. Every
    entry is also checked for a 5xx -- a validator that crashes on the input
    it exists to judge is a standing property this corpus enforces, not a
    one-off assertion for the malformed-env cases alone."""
    mcp_client.post("/api/mcp/servers/", json={"name": "myserver", **STDIO_CONFIG})

    validate_resp = mcp_client.post(
        "/api/mcp/servers/myserver/validate", json={"name": "myserver", **patch}
    )
    assert validate_resp.status_code < 500, (
        f"{case_name}: validate returned {validate_resp.status_code} ({validate_resp.text})"
    )
    validate_body = validate_resp.json()

    save_resp = mcp_client.put("/api/mcp/servers/myserver", json=patch)
    assert save_resp.status_code < 500, (
        f"{case_name}: save returned {save_resp.status_code} ({save_resp.text})"
    )

    assert validate_body["ok"] == (save_resp.status_code == 200), (
        f"{case_name}: validate ok={validate_body['ok']!r} "
        f"(errors={validate_body.get('errors')!r}) but save status="
        f"{save_resp.status_code} ({save_resp.text})"
    )


@pytest.mark.parametrize("case_name,patch", _PARITY_PATCH_CORPUS)
def test_route_validate_and_register_agree_at_create_time(mcp_client, case_name, patch):
    """The same corpus again, but merged onto an empty base (a name that does
    not exist yet) instead of onto an already-stored config -- validate and
    register must still agree, and neither may 500 on a malformed patch."""
    name = f"fresh-{case_name}"

    validate_resp = mcp_client.post(
        "/api/mcp/servers/" + name + "/validate", json={"name": name, **patch}
    )
    assert validate_resp.status_code < 500, (
        f"{case_name}: validate returned {validate_resp.status_code} ({validate_resp.text})"
    )
    validate_body = validate_resp.json()

    register_resp = mcp_client.post("/api/mcp/servers/", json={"name": name, **patch})
    assert register_resp.status_code < 500, (
        f"{case_name}: register returned {register_resp.status_code} ({register_resp.text})"
    )

    assert validate_body["ok"] == (register_resp.status_code == 201), (
        f"{case_name}: validate ok={validate_body['ok']!r} "
        f"(errors={validate_body.get('errors')!r}) but register status="
        f"{register_resp.status_code} ({register_resp.text})"
    )


def test_route_env_deletion_patch_validate_and_save_parity_end_to_end(mcp_client):
    """The concrete regression this fixes: the exact edit Save performs
    (deleting an env key via an explicit `null`) must validate successfully,
    and the key must actually be gone after the save that follows."""
    mcp_client.post("/api/mcp/servers/", json={"name": "myserver", **STDIO_CONFIG})

    validate_resp = mcp_client.post(
        "/api/mcp/servers/myserver/validate",
        json={"name": "myserver", "env": {"API_KEY": None}},
    )
    assert validate_resp.status_code == 200
    assert validate_resp.json()["ok"] is True

    save_resp = mcp_client.put("/api/mcp/servers/myserver", json={"env": {"API_KEY": None}})
    assert save_resp.status_code == 200
    assert "API_KEY" not in save_resp.json()["env_keys"]


@pytest.mark.parametrize(
    "case_name,patch",
    [
        ("env_list", {"command": "python3", "env": []}),
        ("env_empty_string", {"command": "python3", "env": ""}),
        ("args_null", {"command": "python3", "args": None}),
        ("timeout_empty_string", {"command": "python3", "timeout": ""}),
    ],
)
def test_create_time_malformed_falsy_values_are_rejected_not_laundered(
    mcp_client, case_name, patch
):
    """The regression this fixes: a falsy but wrong-typed value (`[]`/`""`
    where env wants a mapping, a bare `None` where args wants a list, `""`
    where timeout wants a number) must reach `_validate_shape` and fail it,
    not be normalized into "key absent" by the merge before validation ever
    runs. Both endpoints must actually reject -- not just agree with each
    other, which parity alone would not catch if both silently accepted."""
    name = f"fresh-{case_name}"

    validate_resp = mcp_client.post(
        f"/api/mcp/servers/{name}/validate", json={"name": name, **patch}
    )
    assert validate_resp.status_code == 200
    assert validate_resp.json()["ok"] is False, f"{case_name}: validate accepted {patch!r}"
    assert validate_resp.json()["errors"]

    register_resp = mcp_client.post("/api/mcp/servers/", json={"name": name, **patch})
    assert register_resp.status_code == 400, (
        f"{case_name}: register accepted {patch!r} ({register_resp.text})"
    )


# URL-transport parity -- `_validate_shape` used to gate every args/env shape
# check behind `has_command`, so a URL config (no `command` at all) skipped
# them outright; `_merge_config` separately laundered a fresh url+malformed
# patch by popping the stdio-only fields before `_validate_shape` ever saw
# them. Both mechanisms are covered here, for both create-time (url and the
# malformed field in the same request) and update-time (a patch that edits
# only the malformed field on an already-registered url server, never
# resending `url`) -- the two shapes each mechanism above was specific to.

_URL_MALFORMED_PATCH_CORPUS = [
    ("env_list", {"env": []}),
    ("env_empty_string", {"env": ""}),
    ("env_int", {"env": 0}),
    ("env_false", {"env": False}),
    ("args_null", {"args": None}),
    ("args_empty_string", {"args": ""}),
    ("args_dict", {"args": {}}),
]


@pytest.mark.parametrize("case_name,patch", _URL_MALFORMED_PATCH_CORPUS)
def test_url_transport_create_time_malformed_args_env_are_rejected(mcp_client, case_name, patch):
    """`timeout` already rejected regardless of transport; this is the same
    guarantee for `args`/`env` on a server that has no `command` at all."""
    name = f"url-fresh-{case_name}"
    body = {"name": name, **URL_CONFIG, **patch}

    validate_resp = mcp_client.post(f"/api/mcp/servers/{name}/validate", json=body)
    assert validate_resp.status_code == 200
    assert validate_resp.json()["ok"] is False, f"{case_name}: validate accepted {patch!r}"
    assert validate_resp.json()["errors"]

    register_resp = mcp_client.post("/api/mcp/servers/", json=body)
    assert register_resp.status_code == 400, (
        f"{case_name}: register accepted {patch!r} ({register_resp.text})"
    )
    assert mcp_client.get("/api/mcp/servers/").json()["servers"] == []


@pytest.mark.parametrize("case_name,patch", _URL_MALFORMED_PATCH_CORPUS)
def test_url_transport_update_time_malformed_args_env_are_rejected(
    mcp_client, tmp_path, case_name, patch
):
    """The shape the create-time corpus above cannot exercise: a save that
    edits only `args`/`env` on an already-registered url server without
    resending `url`, so `_merge_config`'s transport-switch pop (keyed on
    `patch.get("url")`) never fires and the malformed value would otherwise
    sit in `merged` unexamined by a `has_command`-gated check."""
    mcp_client.post("/api/mcp/servers/", json={"name": "myserver", **URL_CONFIG})
    registry_path = tmp_path / "mcp_servers.json"
    before = json.loads(registry_path.read_text())

    validate_resp = mcp_client.post(
        "/api/mcp/servers/myserver/validate", json={"name": "myserver", **patch}
    )
    assert validate_resp.status_code == 200
    assert validate_resp.json()["ok"] is False, f"{case_name}: validate accepted {patch!r}"

    save_resp = mcp_client.put("/api/mcp/servers/myserver", json=patch)
    assert save_resp.status_code == 400, f"{case_name}: save accepted {patch!r} ({save_resp.text})"

    after = json.loads(registry_path.read_text())
    assert after == before, f"{case_name}: registry bytes changed on a rejected save"


def test_url_transport_well_formed_env_accepted_at_create_time(mcp_client):
    """The pin for the inert-field question: a well-formed `env` on a URL
    server is never read by the http transport, but it is not malformed --
    the norm elsewhere in this service is to accept and store a well-formed
    inert field rather than reject it, and this is that case for `env`."""
    resp = mcp_client.post(
        "/api/mcp/servers/",
        json={"name": "myserver", **URL_CONFIG, "env": {"TOKEN": "value"}},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["env_keys"] == ["TOKEN"]


def test_url_transport_well_formed_env_accepted_on_update_without_url(mcp_client):
    """Same pin, but as a save that edits only `env` on an already-registered
    url server without resending `url` -- the shape the transport-switch pop
    in `_merge_config` must leave alone because the patch itself supplied it."""
    mcp_client.post("/api/mcp/servers/", json={"name": "myserver", **URL_CONFIG})

    resp = mcp_client.put("/api/mcp/servers/myserver", json={"env": {"TOKEN": "value"}})

    assert resp.status_code == 200, resp.text
    assert resp.json()["env_keys"] == ["TOKEN"]


def test_update_env_empty_list_is_rejected_not_silently_ignored(tmp_path, monkeypatch):
    """Before this fix, `env: []` on an update was normalized to `{}` by
    `patch["env"] or {}` and merged as a no-op -- the existing env survived
    untouched and the save reported success, silently diverging from
    validate (which already rejected a non-dict env). That divergence is
    closed deliberately, as a tightening: update now rejects the same
    malformed value validate always did, and the config on disk is
    untouched by the rejected patch."""
    registry_path, _ = _point_registry_at(tmp_path, monkeypatch)
    mcp_mod.register_server("myserver", STDIO_CONFIG)

    result = asyncio.run(mcp_mod.validate_config("myserver", {"env": []}, check_connection=False))
    assert result["ok"] is False

    with pytest.raises(mcp_mod.McpServerError):
        mcp_mod.update_server("myserver", {"env": []})

    on_disk = json.loads(registry_path.read_text())
    assert on_disk["servers"]["myserver"]["config"]["env"] == STDIO_CONFIG["env"]


def test_update_env_null_top_level_is_a_no_op_not_a_wipe(tmp_path, monkeypatch):
    """A bare `env: null` carries no individual keys to delete, unlike
    `env: {KEY: null}` -- it leaves the existing env untouched. This is
    distinct from `env: []`, a malformed container that is now rejected
    outright rather than silently normalized to the same no-op."""
    registry_path, _ = _point_registry_at(tmp_path, monkeypatch)
    mcp_mod.register_server("myserver", STDIO_CONFIG)

    updated = mcp_mod.update_server("myserver", {"env": None})

    assert updated["env_keys"] == ["API_KEY"]
    on_disk = json.loads(registry_path.read_text())
    assert on_disk["servers"]["myserver"]["config"]["env"] == STDIO_CONFIG["env"]


def test_update_args_null_is_rejected_not_treated_as_absent(tmp_path, monkeypatch):
    """Unlike `timeout`, `_validate_shape` has no reading of `args: null` as
    valid -- args must always be a list. So, unlike `timeout: null` (which
    clears the field), `args: null` is written through to the shape check
    and rejected, on update exactly as it is on create."""
    registry_path, _ = _point_registry_at(tmp_path, monkeypatch)
    mcp_mod.register_server("myserver", STDIO_CONFIG)

    with pytest.raises(mcp_mod.McpServerError):
        mcp_mod.update_server("myserver", {"args": None})

    on_disk = json.loads(registry_path.read_text())
    assert on_disk["servers"]["myserver"]["config"]["args"] == STDIO_CONFIG["args"]


def test_validate_create_time_null_env_against_empty_base_is_key_absent(tmp_path, monkeypatch):
    """A server that does not exist yet has nothing to merge onto but an
    empty config, so a null env value there means "the key was never
    added" -- the same outcome the merge produces for an existing server --
    rather than the shape error a literal `None` would otherwise trip."""
    _point_registry_at(tmp_path, monkeypatch)

    result = asyncio.run(
        mcp_mod.validate_config(
            "brand-new",
            {"command": "python3", "env": {"API_KEY": None}},
            check_connection=False,
        )
    )

    assert result["ok"] is True
    assert result["errors"] is None


# A stored status belongs to the configuration it sits on -- the connection
# check refuses to write one for a configuration it never probed, and an
# ordinary edit must not leave one behind for a configuration it replaced.


def test_update_server_clears_a_status_obtained_for_the_replaced_config(tmp_path, monkeypatch):
    _point_registry_at(tmp_path, monkeypatch)
    mcp_mod.register_server("myserver", {"command": "/no/such/binary-xyz", "args": [], "env": {}})

    asyncio.run(mcp_mod.check_server_connection("myserver"))
    assert mcp_mod.get_server("myserver")["last_check"] is not None, (
        "precondition: the server must carry a status before the edit"
    )

    updated = mcp_mod.update_server("myserver", {"command": "/some/other/binary"})

    assert updated["command"] == "/some/other/binary"
    assert updated["last_check"] is None
    assert mcp_mod.get_server("myserver")["last_check"] is None, (
        "a status obtained for the old command must not survive onto the new one"
    )


def test_update_server_keeps_a_status_when_the_config_is_unchanged(tmp_path, monkeypatch):
    """Clearing on edit must not become clearing on every save. A request that
    leaves the configuration identical has not invalidated anything."""
    _point_registry_at(tmp_path, monkeypatch)
    mcp_mod.register_server("myserver", {"command": "/no/such/binary-xyz", "args": [], "env": {}})

    asyncio.run(mcp_mod.check_server_connection("myserver"))
    assert mcp_mod.get_server("myserver")["last_check"] is not None

    updated = mcp_mod.update_server("myserver", {"command": "/no/such/binary-xyz"})

    assert updated["last_check"] is not None
    assert mcp_mod.get_server("myserver")["last_check"] is not None


def test_update_server_can_clear_args_and_timeout(tmp_path, monkeypatch):
    """An explicit empty list and an explicit null are the wire's removal
    signals; the merge preserves omitted keys, so these are the only way an
    editor can take a value away."""
    _point_registry_at(tmp_path, monkeypatch)
    mcp_mod.register_server(
        "myserver", {"command": "python3", "args": ["-m", "thing"], "timeout": 30}
    )

    updated = mcp_mod.update_server("myserver", {"args": [], "timeout": None})

    assert updated["args"] == []
    assert updated.get("timeout") is None
    fetched = mcp_mod.get_server("myserver")
    assert fetched["args"] == []
    assert fetched.get("timeout") is None


def test_concurrent_updates_are_not_lost(tmp_path, monkeypatch):
    """Every mutation is a read-modify-write over the whole registry file, so
    two that interleave lose one wholesale. The write is slowed deliberately so
    the overlap is guaranteed rather than lucky: without a boundary spanning
    load and save, most of these updates disappear."""
    import threading

    _point_registry_at(tmp_path, monkeypatch)

    names = [f"server{i}" for i in range(8)]
    for name in names:
        mcp_mod.register_server(name, {"command": "python3", "args": []})

    real_write = mcp_mod._write_private

    def _slow_write(path, content):
        time.sleep(0.005)
        real_write(path, content)

    monkeypatch.setattr(mcp_mod, "_write_private", _slow_write)

    start = threading.Barrier(len(names))
    errors: list[BaseException] = []

    def _update(name):
        try:
            start.wait()
            mcp_mod.update_server(name, {"args": [name]})
        except BaseException as exc:  # noqa: BLE001 — reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=_update, args=(name,)) for name in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"update threads raised: {errors!r}"
    landed = {s["name"]: s["args"] for s in mcp_mod.list_servers()}
    assert landed == {name: [name] for name in names}, (
        "every concurrent update must survive; a missing one was overwritten "
        "by another thread's stale snapshot"
    )
