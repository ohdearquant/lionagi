# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Selected MCP servers and registered tools retain their intended transports."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from lionagi.protocols.action.manager import ActionManager, load_mcp_tools
from lionagi.service.connections.mcp_wrapper import (
    MCPConnectionPool,
    MCPSecurityConfig,
    create_mcp_tool,
)


@pytest.fixture
def offline_pool(monkeypatch):
    # Replace class globals for this test without modifying an existing pool.
    monkeypatch.setattr(MCPConnectionPool, "_configs", {})
    monkeypatch.setattr(MCPConnectionPool, "_clients", {})
    monkeypatch.setattr(MCPConnectionPool, "_security", None)
    connections = []
    calls = []

    class FakeClient:
        def __init__(self, config):
            self.config = dict(config)

        async def list_tools(self):
            return [
                SimpleNamespace(
                    name="probe",
                    description="offline placeholder",
                    inputSchema={"type": "object", "properties": {}},
                )
            ]

        async def call_tool(self, name, arguments):
            calls.append((self.config, name, dict(arguments)))
            return {"reply": "synthetic"}

    async def fake_get_or_create(cls, config, cache_key, security):
        connections.append((dict(config), cache_key, security))
        return FakeClient(config)

    monkeypatch.setattr(MCPConnectionPool, "_get_or_create_client", classmethod(fake_get_or_create))
    return connections, calls


def _config_file(path, servers):
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return str(path)


@pytest.mark.asyncio
async def test_missing_requested_name_rejected_before_any_tool_registration(tmp_path, offline_pool):
    connections, _ = offline_pool
    old = _config_file(tmp_path / "old.json", {"stale": {"url": "https://stale.invalid/mcp"}})
    selected = _config_file(
        tmp_path / "selected.json", {"current": {"url": "https://current.invalid/mcp"}}
    )
    assert MCPConnectionPool.load_config(old) == ["stale"]
    manager = ActionManager()

    # A valid name comes first, so validating only while iterating would partially register it.
    with pytest.raises(ValueError) as error:
        await manager.load_mcp_config(
            selected,
            server_names=["current", "stale", "absent"],
            mcp_security=MCPSecurityConfig.trusted(),
        )
    assert "stale" in str(error.value)
    assert "absent" in str(error.value)
    assert "https://" not in str(error.value)
    assert not manager.registry
    assert not connections


@pytest.mark.asyncio
@pytest.mark.parametrize("loader", ["manager", "helper"])
async def test_refused_file_does_not_change_pool_configuration(
    tmp_path, monkeypatch, offline_pool, loader
):
    connections, _ = offline_pool
    transport_a = {"url": "https://a.invalid/mcp"}
    transport_b = {"url": "https://b.invalid/mcp"}
    old = _config_file(tmp_path / "old.json", {"x": transport_a})
    selected = _config_file(tmp_path / "selected.json", {"x": transport_b})
    MCPConnectionPool.load_config(old)
    manager = ActionManager()
    if loader == "helper":
        monkeypatch.setattr("lionagi.protocols.action.manager.ActionManager", lambda: manager)
    with pytest.raises(ValueError, match="not declared in the selected config"):
        if loader == "manager":
            await manager.load_mcp_config(selected, server_names=["x", "y"])
        else:
            await load_mcp_tools(selected, server_names=["x", "y"])
    assert MCPConnectionPool._configs["x"] == transport_a
    assert not manager.registry
    assert not connections


@pytest.mark.asyncio
async def test_inline_cache_identity_tracks_transport_content_and_security(monkeypatch):
    monkeypatch.setattr(MCPConnectionPool, "_clients", {})
    monkeypatch.setattr(MCPConnectionPool, "_security", None)
    monkeypatch.setattr(MCPConnectionPool, "_lock", None)
    monkeypatch.setattr(MCPConnectionPool, "_creation_locks", {})
    created = []

    class FakeClient:
        def is_connected(self):
            return True

    async def create_client(cls, config, security=None):
        client = FakeClient()
        created.append((client, dict(config), security))
        return client

    monkeypatch.setattr(MCPConnectionPool, "_create_client", classmethod(create_client))
    first = {"command": "synthetic", "args": ["one"], "env": {"TOKEN": "fixture"}}
    equal = {"command": "synthetic", "args": ["one"], "env": {"TOKEN": "fixture"}}
    different = {"command": "synthetic", "args": ["two"], "env": {"TOKEN": "fixture"}}
    policy = MCPSecurityConfig.trusted()
    client = await MCPConnectionPool._get_bound_client(first, None, policy)
    assert await MCPConnectionPool._get_bound_client(equal, None, policy) is client
    assert len(created) == 1
    assert await MCPConnectionPool.get_client(equal, security=policy) is client
    assert await MCPConnectionPool._get_bound_client(different, None, policy) is not client
    assert len(created) == 2
    assert await MCPConnectionPool._get_bound_client(equal, None, MCPSecurityConfig()) is not client
    assert len(created) == 3
    assert all("fixture" not in key for key in MCPConnectionPool._clients)


@pytest.mark.asyncio
async def test_inline_transport_is_copied_before_waiting_for_client(monkeypatch):
    monkeypatch.setattr(MCPConnectionPool, "_clients", {})
    monkeypatch.setattr(MCPConnectionPool, "_security", None)
    monkeypatch.setattr(MCPConnectionPool, "_lock", None)
    monkeypatch.setattr(MCPConnectionPool, "_creation_locks", {})
    entered, resume = asyncio.Event(), asyncio.Event()
    original_cached = MCPConnectionPool._cached_client
    created = []

    class FakeClient:
        def is_connected(self):
            return True

    async def cached(cls, key):
        if not entered.is_set():
            entered.set()
            await resume.wait()
        return await original_cached(key)

    async def create_client(cls, config, security=None):
        client = FakeClient()
        created.append((client, dict(config)))
        return client

    monkeypatch.setattr(MCPConnectionPool, "_cached_client", classmethod(cached))
    monkeypatch.setattr(MCPConnectionPool, "_create_client", classmethod(create_client))
    config = {"command": "synthetic", "args": ["one"], "env": {"MODE": "one"}}
    original = {"command": "synthetic", "args": ["one"], "env": {"MODE": "one"}}
    pending = asyncio.create_task(MCPConnectionPool.get_client(config))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        config["args"][0] = "two"
        config["env"]["MODE"] = "two"
    finally:
        resume.set()
    client = await pending
    assert created[0][1] == original
    assert await MCPConnectionPool.get_client(original) is client
    assert await MCPConnectionPool.get_client(config) is not client


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (None, {"current": ["mcp__current__probe"], "other": ["mcp__other__probe"]}),
        ([], {}),
        (["current"], {"current": ["mcp__current__probe"]}),
    ],
    ids=["omitted", "empty", "declared"],
)
@pytest.mark.asyncio
async def test_selected_names_only_are_registered(tmp_path, offline_pool, requested, expected):
    connections, _ = offline_pool
    old = _config_file(tmp_path / "old.json", {"stale": {"url": "https://stale.invalid/mcp"}})
    selected = _config_file(
        tmp_path / "selected.json",
        {
            "current": {"url": "https://current.invalid/mcp"},
            "other": {"url": "https://other.invalid/mcp"},
        },
    )
    assert MCPConnectionPool.load_config(old) == ["stale"]
    manager = ActionManager()
    assert (
        await manager.load_mcp_config(
            selected, server_names=requested, mcp_security=MCPSecurityConfig.trusted()
        )
        == expected
    )
    assert set(manager.registry) == {tool for tools in expected.values() for tool in tools}
    assert len(connections) == len(expected)
    assert {config["url"] for config, _, _ in connections} == {
        f"https://{name}.invalid/mcp" for name in expected
    }


@pytest.mark.asyncio
async def test_registered_tool_reconnect_remains_bound_after_alias_replacement(
    tmp_path, offline_pool
):
    connections, calls = offline_pool
    config_a = {"url": "https://a.invalid/mcp"}
    config_b = {"url": "https://b.invalid/mcp"}
    path_a = _config_file(tmp_path / "a.json", {"srv": config_a})
    path_b = _config_file(tmp_path / "b.json", {"srv": config_b})
    policy = MCPSecurityConfig.trusted()
    manager = ActionManager()
    assert await manager.load_mcp_config(path_a, mcp_security=policy) == {
        "srv": ["mcp__srv__probe"]
    }
    tool = manager.registry["mcp__srv__probe"]
    assert tool.mcp_capability is not None
    assert tool.mcp_capability.security is policy
    assert connections[-1][0] == config_a

    assert MCPConnectionPool.load_config(path_b) == ["srv"]
    assert MCPConnectionPool._configs["srv"] == config_b
    invocation = await manager.invoke(
        {"function": "mcp__srv__probe", "arguments": {"value": "synthetic"}}
    )
    assert invocation.response == {"reply": "synthetic"}
    assert connections[-1][0] == config_a
    assert connections[-1][2] is policy
    assert calls == [(config_a, "probe", {"value": "synthetic"})]

    # Reconstructed tools without a recovery capability still fail before transport.
    before = len(connections)
    uncredentialed = create_mcp_tool({"server": "srv"}, "probe")
    with pytest.raises(PermissionError, match="recovery requires the authorization capability"):
        await uncredentialed(value="synthetic")
    assert len(connections) == before


@pytest.mark.asyncio
async def test_multi_server_selection_survives_rebinding_during_registration(
    tmp_path, monkeypatch, offline_pool
):
    connections, calls = offline_pool
    config_alpha = {"url": "https://alpha.invalid/mcp"}
    config_beta = {"url": "https://beta.invalid/mcp"}
    replacement_beta = {"url": "https://replacement.invalid/mcp"}
    selected = _config_file(
        tmp_path / "selected.json", {"alpha": config_alpha, "beta": config_beta}
    )
    replacement = _config_file(tmp_path / "replacement.json", {"beta": replacement_beta})
    original_register = ActionManager.register_mcp_server
    completed = []

    async def register_with_interleaved_reload(self, *args, **kwargs):
        result = await original_register(self, *args, **kwargs)
        completed.append(result)
        if len(completed) == 1:
            assert MCPConnectionPool.load_config(replacement) == ["beta"]
            assert MCPConnectionPool._configs["beta"] == replacement_beta
        return result

    monkeypatch.setattr(ActionManager, "register_mcp_server", register_with_interleaved_reload)
    manager = ActionManager()
    result = await manager.load_mcp_config(selected, mcp_security=MCPSecurityConfig.trusted())
    assert result == {"alpha": ["mcp__alpha__probe"], "beta": ["mcp__beta__probe"]}
    assert [config for config, _, _ in connections] == [config_alpha, config_beta]

    for name in ("alpha", "beta"):
        response = await manager.invoke(
            {"function": f"mcp__{name}__probe", "arguments": {"value": name}}
        )
        assert response.response == {"reply": "synthetic"}
    assert calls == [
        (config_alpha, "probe", {"value": "alpha"}),
        (config_beta, "probe", {"value": "beta"}),
    ]


@pytest.mark.asyncio
async def test_public_and_bound_paths_share_client_and_isolate_transports(tmp_path, monkeypatch):
    # Exercise the real pool and its creation locks, replacing only transport creation.
    monkeypatch.setattr(MCPConnectionPool, "_configs", {})
    monkeypatch.setattr(MCPConnectionPool, "_clients", {})
    monkeypatch.setattr(MCPConnectionPool, "_security", None)
    monkeypatch.setattr(MCPConnectionPool, "_lock", None)
    monkeypatch.setattr(MCPConnectionPool, "_creation_locks", {})
    created, calls = [], []

    class FakeClient:
        def __init__(self, config):
            self.config = dict(config)
            self.closed = False

        def is_connected(self):
            return not self.closed

        async def list_tools(self):
            return [
                SimpleNamespace(
                    name="probe",
                    description="offline placeholder",
                    inputSchema={"type": "object", "properties": {}},
                )
            ]

        async def call_tool(self, name, arguments):
            calls.append((self, name, dict(arguments)))
            return {"reply": "synthetic"}

        async def __aexit__(self, *_args):
            self.closed = True

    async def create_client(cls, config, security=None):
        client = FakeClient(config)
        created.append(client)
        return client

    monkeypatch.setattr(MCPConnectionPool, "_create_client", classmethod(create_client))
    config_a = {"url": "https://a.invalid/mcp"}
    config_b = {"url": "https://b.invalid/mcp"}
    path_a = _config_file(tmp_path / "a.json", {"srv": config_a})
    path_b = _config_file(tmp_path / "b.json", {"srv": config_b})
    policy = MCPSecurityConfig.trusted()
    try:
        assert MCPConnectionPool.load_config(path_a) == ["srv"]
        client_a = await MCPConnectionPool.get_client({"server": "srv"}, security=policy)
        manager = ActionManager()
        assert await manager.load_mcp_config(path_a, mcp_security=policy) == {
            "srv": ["mcp__srv__probe"]
        }
        assert created == [client_a]
        await manager.invoke({"function": "mcp__srv__probe", "arguments": {"value": "before"}})
        assert calls[-1] == (client_a, "probe", {"value": "before"})

        assert MCPConnectionPool.load_config(path_b) == ["srv"]
        client_b = await MCPConnectionPool.get_client({"server": "srv"}, security=policy)
        assert client_b is not client_a
        assert client_b.config == config_b
        await manager.invoke({"function": "mcp__srv__probe", "arguments": {"value": "after"}})
        assert calls[-1][0].config == config_a
        assert calls[-1][0] is not client_b
    finally:
        await MCPConnectionPool.cleanup()


@pytest.mark.asyncio
async def test_name_only_registration_binds_preloaded_transport(tmp_path, offline_pool):
    connections, calls = offline_pool
    config_a = {"url": "https://a.invalid/mcp"}
    config_b = {"url": "https://b.invalid/mcp"}
    path_a = _config_file(tmp_path / "a.json", {"srv": config_a})
    path_b = _config_file(tmp_path / "b.json", {"srv": config_b})
    assert MCPConnectionPool.load_config(path_a) == ["srv"]
    manager = ActionManager()
    assert await manager.register_mcp_server(
        {"server": "srv"}, tool_names=["probe"], security=MCPSecurityConfig.trusted()
    ) == ["mcp__srv__probe"]
    assert not connections
    assert MCPConnectionPool.load_config(path_b) == ["srv"]
    response = await manager.invoke(
        {"function": "mcp__srv__probe", "arguments": {"value": "synthetic"}}
    )
    assert response.response == {"reply": "synthetic"}
    assert calls == [(config_a, "probe", {"value": "synthetic"})]


@pytest.mark.asyncio
async def test_name_only_registration_without_config_fails_closed(
    tmp_path, monkeypatch, offline_pool
):
    connections, _ = offline_pool
    monkeypatch.chdir(tmp_path)
    manager = ActionManager()
    with pytest.raises(FileNotFoundError, match=r"MCP config file not found: \.mcp\.json"):
        await manager.register_mcp_server({"server": "missing"}, tool_names=["probe"])
    assert not manager.registry
    assert not connections


@pytest.mark.asyncio
async def test_reloads_and_inflight_creation_preserve_and_clean_all_clients(tmp_path, monkeypatch):
    monkeypatch.setattr(MCPConnectionPool, "_configs", {})
    monkeypatch.setattr(MCPConnectionPool, "_clients", {})
    monkeypatch.setattr(MCPConnectionPool, "_security", None)
    monkeypatch.setattr(MCPConnectionPool, "_lock", None)
    monkeypatch.setattr(MCPConnectionPool, "_creation_locks", {})
    started, release = asyncio.Event(), asyncio.Event()
    created, calls = [], []
    config_a = {"url": "https://a.invalid/mcp"}
    config_b = {"url": "https://b.invalid/mcp"}

    class FakeClient:
        def __init__(self, config, security):
            self.config = dict(config)
            self.security = security
            self.close_count = 0

        def is_connected(self):
            return self.close_count == 0

        async def list_tools(self):
            return [
                SimpleNamespace(
                    name="probe",
                    description="offline placeholder",
                    inputSchema={"type": "object", "properties": {}},
                )
            ]

        async def call_tool(self, name, arguments):
            calls.append((self.config, name, dict(arguments)))
            return {"reply": "synthetic"}

        async def __aexit__(self, *_args):
            self.close_count += 1

    async def create_client(cls, config, security=None):
        client = FakeClient(config, security)
        created.append(client)
        if config == config_a and not started.is_set():
            started.set()
            await release.wait()
        return client

    monkeypatch.setattr(MCPConnectionPool, "_create_client", classmethod(create_client))
    path_a = _config_file(tmp_path / "a.json", {"srv": config_a})
    path_b = _config_file(tmp_path / "b.json", {"srv": config_b})
    trusted = MCPSecurityConfig.trusted()
    restricted = MCPSecurityConfig(
        allow_urls=True, url_allowlist=frozenset({"a.invalid", "b.invalid"})
    )
    manager = ActionManager()
    pending = asyncio.create_task(manager.load_mcp_config(path_a, mcp_security=trusted))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        assert MCPConnectionPool.load_config(path_b) == ["srv"]
        client_b = await MCPConnectionPool.get_client({"server": "srv"}, security=trusted)
        client_b_restricted = await MCPConnectionPool.get_client(
            {"server": "srv"}, security=restricted
        )
        assert client_b is not client_b_restricted
        assert client_b.config == client_b_restricted.config == config_b
        release.set()
        assert await asyncio.wait_for(pending, timeout=2) == {"srv": ["mcp__srv__probe"]}

        await manager.invoke({"function": "mcp__srv__probe", "arguments": {"value": "first"}})
        assert calls[-1] == (config_a, "probe", {"value": "first"})
        assert MCPConnectionPool.load_config(path_a) == ["srv"]
        assert MCPConnectionPool.load_config(path_b) == ["srv"]
        await manager.invoke({"function": "mcp__srv__probe", "arguments": {"value": "second"}})
        assert calls[-1] == (config_a, "probe", {"value": "second"})
        assert any(client.config == config_a for client in created)
        assert any(client.config == config_b for client in created)
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
            try:
                await pending
            except asyncio.CancelledError:
                pass
        await MCPConnectionPool.cleanup()
    assert all(client.close_count == 1 for client in created)


@pytest.mark.asyncio
async def test_bound_transport_secrets_do_not_serialize_with_tool(tmp_path, offline_pool):
    del offline_pool
    secret_url = "synthetic-url-secret"
    secret_env = "synthetic-env-secret"
    config = {
        "url": f"https://example.invalid/mcp?token={secret_url}",
        "env": {"SYNTHETIC_TOKEN": secret_env},
    }
    selected = _config_file(tmp_path / "secrets.json", {"srv": config})
    manager = ActionManager()
    await manager.load_mcp_config(selected, mcp_security=MCPSecurityConfig.trusted())
    tool = manager.registry["mcp__srv__probe"]
    for serialized in (str(tool.to_dict()), tool.to_json(), str(tool.model_dump())):
        assert secret_url not in serialized
        assert secret_env not in serialized
        assert "mcp_capability" not in serialized


@pytest.mark.asyncio
async def test_load_mcp_tools_without_config_accepts_old_registration_override(monkeypatch):
    calls = []
    synthetic_tool = object()
    options = {"probe": object}
    policy = MCPSecurityConfig.trusted()

    class ExistingManagerOverride:
        def __init__(self):
            self.registry = {}

        async def register_mcp_server(
            self, server_config, tool_names=None, request_options=None, update=False, security=None
        ):
            calls.append((server_config, tool_names, request_options, update, security))
            self.registry["probe"] = synthetic_tool
            return ["probe"]

    monkeypatch.setattr("lionagi.protocols.action.manager.ActionManager", ExistingManagerOverride)
    result = await load_mcp_tools(
        server_names=["srv"],
        request_options_map={"srv": options},
        update=True,
        mcp_security=policy,
    )
    assert result == [synthetic_tool]
    assert calls == [({"server": "srv"}, None, options, True, policy)]


@pytest.mark.asyncio
async def test_load_mcp_tools_with_config_keeps_selected_transport_after_rebinding(
    tmp_path, offline_pool
):
    _, calls = offline_pool
    config_a = {"url": "https://a.invalid/mcp"}
    config_b = {"url": "https://b.invalid/mcp"}
    path_a = _config_file(tmp_path / "a.json", {"srv": config_a})
    path_b = _config_file(tmp_path / "b.json", {"srv": config_b})
    tools = await load_mcp_tools(config_path=path_a, mcp_security=MCPSecurityConfig.trusted())
    assert len(tools) == 1
    assert MCPConnectionPool.load_config(path_b) == ["srv"]
    assert await tools[0].func_callable(value="synthetic") == {"reply": "synthetic"}
    assert calls == [(config_a, "probe", {"value": "synthetic"})]
