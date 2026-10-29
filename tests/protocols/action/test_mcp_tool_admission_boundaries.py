# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Characterizes where an MCP tool is admitted, and that a direct call to the
manager is not checked against Branch authority.

Two admission boundaries are pinned, both outside Branch authority:

- transport admission at ``MCPConnectionPool._create_client``: command and URL
  transports are deny-listed unless a security policy allows them, so a command
  server refuses to register without ``allow_commands=True``;
- descriptor admission at registration (``validate_mcp_tool_admission``): a
  generic-executor tool name, an unbounded ``command``-shaped schema field, and a
  description that advertises command execution alongside a broad free-form input
  each refuse the tool before the registry is touched, and one denied descriptor
  refuses the whole server.

Also pinned: an MCP tool invoked through ``ActionManager.invoke`` runs even
when the owning Branch's governance gate would refuse it; only the ``act``
path consults ``Branch.authorize``.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from lionagi.operations.act.act import _act
from lionagi.protocols.action.manager import ActionManager
from lionagi.protocols.generic.event import EventStatus
from lionagi.service.connections.mcp_wrapper import MCPConnectionPool
from lionagi.session.branch import Branch
from lionagi.session.session import Session
from lionagi.session.signal import GateDenied


@pytest.fixture(autouse=True)
def _isolate_pool(monkeypatch):
    """Keep alias lookup synthetic and make transport construction impossible."""
    monkeypatch.setattr(MCPConnectionPool, "_security", None)
    monkeypatch.setattr(MCPConnectionPool, "_configs", {"srv": {"url": "https://srv.invalid/mcp"}})
    monkeypatch.setattr(MCPConnectionPool, "_clients", {})
    monkeypatch.setattr(MCPConnectionPool, "_creation_locks", {})

    def no_transport(*_args, **_kwargs):
        raise AssertionError("unexpected MCP transport construction")

    fastmcp = ModuleType("fastmcp")
    fastmcp.Client = no_transport
    client_module = ModuleType("fastmcp.client")
    transports = ModuleType("fastmcp.client.transports")
    transports.StdioTransport = no_transport
    monkeypatch.setitem(sys.modules, "fastmcp", fastmcp)
    monkeypatch.setitem(sys.modules, "fastmcp.client", client_module)
    monkeypatch.setitem(sys.modules, "fastmcp.client.transports", transports)


def _descriptor(name: str, schema: dict, description: str | None = None):
    return SimpleNamespace(name=name, description=description, inputSchema=schema)


_BOUNDED = {
    "type": "object",
    "properties": {"url": {"type": "string"}},
    "additionalProperties": False,
}
_EXECUTOR = {
    "type": "object",
    "properties": {"script": {"type": "string"}},
    "additionalProperties": False,
}
_COMMAND_FIELD = {
    "type": "object",
    "properties": {"command": {"type": "string"}},
    "additionalProperties": False,
}
_OPAQUE_PAYLOAD = {
    "type": "object",
    "properties": {"payload": {"type": "string"}},
    "additionalProperties": False,
}
_SERVER = "srv"
_TOOL_NAME = "mcp__srv__request"


async def _register_descriptors(manager, descriptors, server=_SERVER):
    client = AsyncMock()
    client.list_tools.return_value = descriptors

    async def get_client(server_config, security=None, *, _bound_transport=None):
        return client

    with patch.object(MCPConnectionPool, "get_client", side_effect=get_client):
        names = await manager.register_mcp_server({"server": server})
    return names, client


async def _register_request_tool(manager, server=_SERVER):
    """Register one bounded ``request`` MCP tool and return its transport mock."""
    names, client = await _register_descriptors(manager, [_descriptor("request", _BOUNDED)], server)
    assert names == [f"mcp__{server}__request"]
    return client


def _patch_reconnect(client):
    async def get_or_create(config, cache_key, security):
        return client

    return patch.object(MCPConnectionPool, "_get_or_create_client", side_effect=get_or_create)


class TestMcpTransportAdmission:
    async def test_command_transport_is_denied_without_a_policy(self):
        MCPConnectionPool._security = None
        with pytest.raises(PermissionError, match="allow_commands=False"):
            await MCPConnectionPool._create_client({"command": "python", "args": ["s.py"]})

    async def test_url_transport_is_denied_without_a_policy(self):
        MCPConnectionPool._security = None
        with pytest.raises(PermissionError, match="allow_urls=False"):
            await MCPConnectionPool._create_client({"url": "https://example.com/mcp"})

    async def test_register_mcp_server_command_admits_only_with_the_transport_policy(self):
        from lionagi.service.connections.mcp_wrapper import MCPSecurityConfig

        MCPConnectionPool._security = None
        manager = ActionManager()
        with pytest.raises(PermissionError, match="allow_commands=False"):
            await manager.register_mcp_server({"command": "python", "args": ["s.py"]})

        MCPConnectionPool._security = MCPSecurityConfig(allow_commands=True)
        client = AsyncMock()
        client.list_tools.return_value = [_descriptor("request", _BOUNDED)]

        async def get_client(server_config, security=None, *, _bound_transport=None):
            return client

        with patch.object(MCPConnectionPool, "get_client", side_effect=get_client):
            names = await manager.register_mcp_server({"server": _SERVER})

        assert names == [_TOOL_NAME]


class TestMcpDescriptorAdmission:
    async def test_a_generic_executor_descriptor_is_refused_before_any_registration(self):
        manager = ActionManager()
        with pytest.raises(PermissionError, match="generic executor surface"):
            await _register_descriptors(manager, [_descriptor("bash", _EXECUTOR, description=None)])
        assert manager.registry == {}

    async def test_an_unbounded_command_schema_field_is_refused_on_a_benign_name(self):
        """The schema-only rule: a benign tool name is still refused when its
        schema exposes a caller-chosen ``command`` string."""
        manager = ActionManager()
        with pytest.raises(PermissionError, match="unbounded-command-input"):
            await _register_descriptors(manager, [_descriptor("run_tests", _COMMAND_FIELD)])
        assert manager.registry == {}

    async def test_a_description_advertising_command_execution_is_refused_on_a_benign_name(self):
        """The description-plus-broad-input rule: a benign name and an opaque
        string payload are still refused when the description advertises command
        execution."""
        manager = ActionManager()
        with pytest.raises(PermissionError, match="executor-description-with-broad-input"):
            await _register_descriptors(
                manager, [_descriptor("maintenance", _OPAQUE_PAYLOAD, description="run a command")]
            )
        assert manager.registry == {}

    async def test_one_denied_descriptor_refuses_the_whole_server(self):
        manager = ActionManager()
        with pytest.raises(PermissionError, match="bash"):
            await _register_descriptors(
                manager,
                [
                    _descriptor("request", _BOUNDED),
                    _descriptor("bash", _EXECUTOR, description=None),
                ],
            )
        assert manager.registry == {}

    async def test_an_ordinary_descriptor_is_admitted(self):
        manager = ActionManager()
        names, _ = await _register_descriptors(manager, [_descriptor("request", _BOUNDED)])
        assert names == [_TOOL_NAME]
        assert set(manager.registry) == {_TOOL_NAME}

    async def test_the_name_only_shortcut_still_admits_by_name(self):
        manager = ActionManager()
        names = await manager.register_mcp_server({"server": _SERVER}, tool_names=["request"])
        assert names == [_TOOL_NAME]

    async def test_the_name_only_shortcut_resolves_the_default_file(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(MCPConnectionPool, "_configs", {})
        (tmp_path / ".mcp.json").write_text(
            '{"mcpServers":{"srv":{"url":"https://srv.invalid/mcp"}}}', encoding="utf-8"
        )
        manager = ActionManager()

        assert await manager.register_mcp_server({"server": _SERVER}, tool_names=["request"]) == [
            _TOOL_NAME
        ]

    async def test_the_name_only_shortcut_refuses_an_unresolved_name(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(MCPConnectionPool, "_configs", {})
        (tmp_path / ".mcp.json").write_text('{"mcpServers":{}}', encoding="utf-8")
        manager = ActionManager()

        with pytest.raises(ValueError, match="Unknown MCP server: srv"):
            await manager.register_mcp_server({"server": _SERVER}, tool_names=["request"])
        assert manager.registry == {}

    async def test_the_name_only_shortcut_still_refuses_a_generic_executor_name(self):
        manager = ActionManager()
        with pytest.raises(PermissionError, match="generic executor surface"):
            await manager.register_mcp_server({"server": _SERVER}, tool_names=["bash"])
        assert manager.registry == {}

    def test_dict_registration_admits_by_name_without_a_descriptor(self):
        manager = ActionManager()
        manager.register_tool({"request": {"command": "python", "args": ["-m", "x"]}})
        assert "request" in manager.registry

    def test_dict_registration_refuses_a_generic_executor_name(self):
        manager = ActionManager()
        with pytest.raises(PermissionError, match="generic executor surface"):
            manager.register_tool({"bash": {"command": "python", "args": ["-m", "x"]}})
        assert manager.registry == {}


class TestMcpInvocationSkipsBranchAuthority:
    async def test_a_denied_gate_does_not_stop_a_direct_manager_invoke(self, plugin_home):
        branch = Branch()
        client = await _register_request_tool(branch.acts)
        client.call_tool.return_value = "mcp result"

        session = Session(default_branch=branch)
        session.gate(lambda invocation: False)

        assert await branch.authorize(SimpleNamespace(function=_TOOL_NAME)) is False

        with _patch_reconnect(client):
            function_calling = await branch.acts.invoke(
                {"function": _TOOL_NAME, "arguments": {"url": "https://x"}}
            )

        assert function_calling.status == EventStatus.COMPLETED
        assert function_calling.execution.response == "mcp result"
        client.call_tool.assert_awaited_once_with("request", {"url": "https://x"})

    async def test_the_gate_is_only_recorded_when_authorize_is_itself_called(self, plugin_home):
        branch = Branch()
        client = await _register_request_tool(branch.acts)
        client.call_tool.return_value = "mcp result"
        session = Session(default_branch=branch)
        session.gate(lambda invocation: False)

        with _patch_reconnect(client):
            function_calling = await branch.acts.invoke(
                {"function": _TOOL_NAME, "arguments": {"url": "https://x"}}
            )

        assert function_calling.execution.response == "mcp result"
        assert session.observer.by_type(GateDenied) == []

        assert await branch.authorize(SimpleNamespace(function=_TOOL_NAME)) is False
        assert len(session.observer.by_type(GateDenied)) == 1

    async def test_the_act_path_does_deny_an_mcp_tool_the_gate_refuses(self, plugin_home):
        """The contrast that makes the boundary visible: the ``act`` operation
        consults ``Branch.authorize`` before the manager, so the very same MCP
        tool is blocked there and never reaches the transport."""
        branch = Branch()
        client = await _register_request_tool(branch.acts)
        client.call_tool.return_value = "mcp result"
        session = Session(default_branch=branch)
        session.gate(lambda invocation: False)

        with _patch_reconnect(client):
            result = await _act(branch, {"function": _TOOL_NAME, "arguments": {"url": "https://x"}})

        assert result.output == {"error": "denied by governance gate", "function": _TOOL_NAME}
        assert client.call_tool.await_count == 0
        assert len(session.observer.by_type(GateDenied)) == 1

    async def test_the_act_path_runs_the_same_mcp_tool_when_the_gate_allows(self, plugin_home):
        """Positive control for the deny case above: with a gate that allows,
        the same ``act`` call reaches the transport exactly once, so the deny
        case pins the gate stopping a call that would otherwise happen."""
        branch = Branch()
        client = await _register_request_tool(branch.acts)
        client.call_tool.return_value = "mcp result"
        session = Session(default_branch=branch)
        session.gate(lambda invocation: True)

        with _patch_reconnect(client):
            result = await _act(branch, {"function": _TOOL_NAME, "arguments": {"url": "https://x"}})

        assert result.output == "mcp result"
        client.call_tool.assert_awaited_once_with("request", {"url": "https://x"})
        assert session.observer.by_type(GateDenied) == []
