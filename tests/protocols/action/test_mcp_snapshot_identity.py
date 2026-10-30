# Copyright (c) 2023-2026, LionAGI contributors
# SPDX-License-Identifier: Apache-2.0

"""Snapshot isolation and client identity across MCP entry points."""

from __future__ import annotations

import json

import pytest

from lionagi.protocols.action.manager import ActionManager, load_mcp_tools
from lionagi.service.connections.mcp_wrapper import MCPConnectionPool, MCPSecurityConfig


@pytest.fixture
def pool(monkeypatch):
    monkeypatch.setattr(MCPConnectionPool, "_configs", {"existing": {"args": ["original"]}})
    monkeypatch.setattr(MCPConnectionPool, "_clients", {})
    monkeypatch.setattr(MCPConnectionPool, "_creation_locks", {})
    monkeypatch.setattr(MCPConnectionPool, "_lock", None)
    monkeypatch.setattr(MCPConnectionPool, "_security", None)
    return MCPConnectionPool


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / ".mcp.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "alpha": {"command": "runner", "args": ["one"]},
                    "beta": {"command": "runner", "args": ["two"]},
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def test_snapshot_is_independent_and_does_not_update_registry(pool, config_file):
    snapshot = pool._load_config_snapshot(str(config_file), ["alpha"])

    assert snapshot == {
        "alpha": {"command": "runner", "args": ["one"]},
        "beta": {"command": "runner", "args": ["two"]},
    }
    assert pool._configs == {"existing": {"args": ["original"]}}
    snapshot["alpha"]["args"].append("changed")
    assert pool._load_config_snapshot(str(config_file))["alpha"]["args"] == ["one"]
    assert pool._configs == {"existing": {"args": ["original"]}}


def test_public_load_publishes_independent_registry_copy(pool, config_file):
    assert pool.load_config(str(config_file)) == ["alpha", "beta"]
    assert pool._configs == {
        "existing": {"args": ["original"]},
        "alpha": {"command": "runner", "args": ["one"]},
        "beta": {"command": "runner", "args": ["two"]},
    }


def test_default_reference_lookup_updates_registry(pool, config_file, monkeypatch):
    monkeypatch.chdir(config_file.parent)
    assert pool._resolve_config({"server": "alpha"}) == {"command": "runner", "args": ["one"]}
    assert pool._configs["alpha"] == {"command": "runner", "args": ["one"]}
    assert pool._configs["beta"] == {"command": "runner", "args": ["two"]}


@pytest.mark.parametrize("entry_point", ["manager", "helper"])
async def test_config_consumers_publish_the_snapshot_without_aliasing(
    pool, config_file, monkeypatch, entry_point
):
    bound = []

    async def register(self, _config, **kwargs):
        bound.append(kwargs["_bound_transport"])
        return []

    monkeypatch.setattr(ActionManager, "register_mcp_server", register)
    if entry_point == "manager":
        assert await ActionManager().load_mcp_config(str(config_file), ["alpha"]) == {"alpha": []}
    else:
        assert await load_mcp_tools(str(config_file), ["alpha"]) == []

    assert pool._configs["alpha"] == {"command": "runner", "args": ["one"]}
    assert pool._configs["beta"] == {"command": "runner", "args": ["two"]}
    assert bound == [{"command": "runner", "args": ["one"]}]
    bound[0]["args"].append("after-registration")
    assert pool._configs["alpha"]["args"] == ["one"]


@pytest.mark.parametrize("entry_point", ["manager", "helper"])
async def test_explicit_empty_selection_registers_nothing_but_publishes_file(
    pool, config_file, monkeypatch, entry_point
):
    async def unexpected_registration(*_args, **_kwargs):
        pytest.fail("an explicit empty selection must not register servers")

    monkeypatch.setattr(ActionManager, "register_mcp_server", unexpected_registration)
    if entry_point == "manager":
        assert await ActionManager().load_mcp_config(str(config_file), []) == {}
    else:
        assert await load_mcp_tools(str(config_file), []) == []
    assert set(pool._configs) == {"existing", "alpha", "beta"}


@pytest.mark.parametrize("entry_point", ["manager", "helper"])
async def test_missing_name_is_refused_once_without_registering(
    pool, config_file, monkeypatch, entry_point
):
    calls = []
    original = pool._load_config_snapshot.__func__

    def snapshot(cls, path, names=None):
        calls.append((path, names))
        return original(cls, path, names)

    async def unexpected_registration(*_args, **_kwargs):
        pytest.fail("registration occurred before missing-name validation")

    monkeypatch.setattr(pool, "_load_config_snapshot", classmethod(snapshot))
    monkeypatch.setattr(ActionManager, "register_mcp_server", unexpected_registration)
    expected = "MCP servers not declared in the selected config: ['missing']"
    with pytest.raises(ValueError) as error:
        if entry_point == "manager":
            await ActionManager().load_mcp_config(str(config_file), ["alpha", "missing"])
        else:
            await load_mcp_tools(str(config_file), ["alpha", "missing"])
    assert str(error.value) == expected
    assert calls == [(str(config_file), ["alpha", "missing"])]
    assert pool._configs == {"existing": {"args": ["original"]}}


@pytest.mark.parametrize("server_name", [None, "alpha"], ids=["inline", "server"])
async def test_resolved_identity_equals_bound_client_cache_key(pool, monkeypatch, server_name):
    transport = {"command": "runner", "args": ["one"]}
    policy = MCPSecurityConfig.trusted()
    if server_name is None:
        identity_input = transport
    else:
        pool._configs[server_name] = transport
        identity_input = {"server": server_name}

    class Client:
        def is_connected(self):
            return True

    created = []

    async def create(cls, config, security=None):
        created.append((config, security))
        return Client()

    monkeypatch.setattr(pool, "_create_client", classmethod(create))
    original_key = pool._cache_key

    def marked_key(*args, **kwargs):
        return original_key(*args, **kwargs) + ":marker"

    # Both paths must follow a future change to the shared key builder.
    monkeypatch.setattr(pool, "_cache_key", staticmethod(marked_key))
    resolved, policy_key, identity_key = pool._resolve_identity(identity_input, policy)
    client = await pool._get_bound_client(transport, server_name, policy)

    assert resolved == transport
    security_fp = pool._security_fingerprint(policy)
    if server_name is None:
        expected = f"inline:{policy_key}:sec:{security_fp}:marker"
    else:
        expected = f"server:{server_name}:{policy_key}:sec:{security_fp}:marker"
    assert identity_key == expected
    assert list(pool._clients) == [identity_key]
    assert pool._clients[identity_key] is client
    assert await pool.get_client(identity_input, security=policy) is client
    assert len(created) == 1


def test_server_reference_membership_is_checked_once_per_resolution_layer(pool):
    class Reference(dict):
        def __init__(self):
            super().__init__({"server": "alpha"})
            self.contains_calls = 0

        def __contains__(self, key):
            self.contains_calls += 1
            return super().__contains__(key)

    pool._configs["alpha"] = {"command": "runner"}
    reference = Reference()
    resolved, _, _ = pool._resolve_identity(reference, MCPSecurityConfig.trusted())
    assert resolved == {"command": "runner"}
    # One check in identity resolution and one in the nested config resolver.
    assert reference.contains_calls == 2
