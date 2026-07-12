"""Tests for lionagi.service.connections.mcp.wrapper module."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest

from lionagi.service.connections.mcp_wrapper import (
    MCPConnectionPool,
    MCPSecurityConfig,
    create_mcp_tool,
    validate_mcp_tool_admission,
)


class TestMCPConnectionPoolContextManager:
    @pytest.mark.asyncio
    async def test_aenter_returns_self(self):
        pool = MCPConnectionPool()
        result = await pool.__aenter__()
        assert result is pool

    @pytest.mark.asyncio
    async def test_aexit_calls_cleanup(self):
        pool = MCPConnectionPool()

        with patch.object(MCPConnectionPool, "cleanup", new_callable=AsyncMock) as mock_cleanup:
            await pool.__aexit__(None, None, None)
            mock_cleanup.assert_called_once()


class TestMCPConnectionPoolLoadConfig:
    def test_load_config_file_not_found(self):
        with pytest.raises(FileNotFoundError, match="MCP config file not found"):
            MCPConnectionPool.load_config("nonexistent.json")

    def test_load_config_invalid_json(self):
        invalid_json = "{invalid json"

        with patch("builtins.open", mock_open(read_data=invalid_json)):
            with patch.object(Path, "exists", return_value=True):
                with pytest.raises(json.JSONDecodeError, match="Invalid JSON"):
                    MCPConnectionPool.load_config(".mcp.json")

    def test_load_config_not_dict(self):
        json_data = "[]"  # Array instead of object

        with patch("builtins.open", mock_open(read_data=json_data)):
            with patch.object(Path, "exists", return_value=True):
                with pytest.raises(ValueError, match="MCP config must be a JSON object"):
                    MCPConnectionPool.load_config(".mcp.json")

    def test_load_config_mcpservers_not_dict(self):
        json_data = json.dumps({"mcpServers": []})

        with patch("builtins.open", mock_open(read_data=json_data)):
            with patch.object(Path, "exists", return_value=True):
                with pytest.raises(ValueError, match="mcpServers must be a dictionary"):
                    MCPConnectionPool.load_config(".mcp.json")

    def test_load_config_success(self):
        MCPConnectionPool._configs = {}  # Reset configs

        config_data = {"mcpServers": {"test_server": {"command": "python", "args": ["server.py"]}}}
        json_data = json.dumps(config_data)

        with patch("builtins.open", mock_open(read_data=json_data)):
            with patch.object(Path, "exists", return_value=True):
                MCPConnectionPool.load_config(".mcp.json")

        assert "test_server" in MCPConnectionPool._configs
        assert MCPConnectionPool._configs["test_server"]["command"] == "python"


class TestMCPConnectionPoolGetClient:
    @pytest.fixture(autouse=True)
    def reset_pool(self):
        MCPConnectionPool._clients = {}
        MCPConnectionPool._configs = {}
        yield
        MCPConnectionPool._clients = {}
        MCPConnectionPool._configs = {}

    @pytest.mark.asyncio
    async def test_get_client_with_server_reference_not_found(self):
        server_config = {"server": "unknown_server"}

        with patch.object(MCPConnectionPool, "load_config") as mock_load:
            mock_load.return_value = None  # Config still empty after load

            with pytest.raises(ValueError, match="Unknown MCP server"):
                await MCPConnectionPool.get_client(server_config)

    @pytest.mark.asyncio
    async def test_get_client_with_server_reference_success(self):
        MCPConnectionPool._configs = {"test_server": {"command": "python", "args": ["server.py"]}}

        server_config = {"server": "test_server"}
        mock_client = AsyncMock()
        mock_client.is_connected.return_value = False  # Force new client creation

        with patch.object(
            MCPConnectionPool, "_create_client", return_value=mock_client
        ) as mock_create:
            client = await MCPConnectionPool.get_client(server_config)
            assert client is mock_client
            mock_create.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_client_with_inline_config(self):
        inline_config = {"command": "python", "args": ["server.py"]}

        mock_client = AsyncMock()

        with patch.object(
            MCPConnectionPool, "_create_client", return_value=mock_client
        ) as mock_create:
            client = await MCPConnectionPool.get_client(inline_config)
            assert client is mock_client
            # get_client threads the per-call security policy down to creation.
            mock_create.assert_called_once_with(inline_config, security=None)

    @pytest.mark.asyncio
    async def test_get_client_reuses_connected_client(self):
        inline_config = {"command": "python", "args": ["server.py"]}

        # Pre-populate pool with connected client
        mock_client = MagicMock()
        mock_client.is_connected.return_value = True
        cache_key = f"inline:{inline_config.get('command')}:{id(inline_config)}"
        MCPConnectionPool._clients[cache_key] = mock_client

        with patch.object(MCPConnectionPool, "_create_client") as mock_create:
            client = await MCPConnectionPool.get_client(inline_config)
            # Should not create new client
            mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_client_removes_stale_client(self):
        inline_config = {"command": "python", "args": ["server.py"]}

        # Pre-populate pool with disconnected client
        stale_client = MagicMock()
        stale_client.is_connected.return_value = False
        cache_key = f"inline:{inline_config.get('command')}:{id(inline_config)}"
        MCPConnectionPool._clients[cache_key] = stale_client

        new_client = AsyncMock()

        with patch.object(MCPConnectionPool, "_create_client", return_value=new_client):
            client = await MCPConnectionPool.get_client(inline_config)
            assert client is new_client
            # Stale client should be removed
            assert (
                cache_key not in MCPConnectionPool._clients
                or MCPConnectionPool._clients[cache_key] is new_client
            )


class TestMCPConnectionPoolCreateClient:
    @pytest.fixture(autouse=True)
    def reset_pool_security(self):
        original = MCPConnectionPool._security
        yield
        MCPConnectionPool._security = original

    @pytest.mark.asyncio
    async def test_create_client_invalid_config_type(self):
        with pytest.raises(ValueError, match="Config must be a dictionary"):
            await MCPConnectionPool._create_client("not a dict")

    @pytest.mark.asyncio
    async def test_create_client_missing_url_and_command(self):
        config = {"some_other_key": "value"}

        with pytest.raises(ValueError, match="Config must have either 'url' or 'command'"):
            await MCPConnectionPool._create_client(config)

    @pytest.mark.asyncio
    async def test_create_client_command_denied_by_default(self):
        """Command transports are denied when no security config is set (fail-closed)."""
        MCPConnectionPool._security = None
        config = {"command": "python", "args": ["server.py"]}
        with pytest.raises(PermissionError, match="allow_commands=False"):
            await MCPConnectionPool._create_client(config)

    @pytest.mark.asyncio
    async def test_create_client_url_denied_by_default(self):
        """URL transports are denied when no security config is set (fail-closed)."""
        MCPConnectionPool._security = None
        config = {"url": "https://api.example.com/mcp"}
        with pytest.raises(PermissionError, match="allow_urls=False"):
            await MCPConnectionPool._create_client(config)

    @pytest.mark.asyncio
    async def test_create_client_fastmcp_not_installed(self):
        # Need allow_urls to get past the security gate to the import check
        MCPConnectionPool._security = MCPSecurityConfig(allow_urls=True)
        config = {"url": "https://api.example.com/mcp"}

        with patch("builtins.__import__", side_effect=ImportError):
            with pytest.raises((ImportError, Exception)):
                await MCPConnectionPool._create_client(config)

    @pytest.mark.asyncio
    async def test_create_client_with_url(self):
        MCPConnectionPool._security = MCPSecurityConfig(allow_urls=True)
        config = {"url": "https://api.example.com/mcp"}

        mock_client = AsyncMock()

        with patch("fastmcp.Client", return_value=mock_client) as mock_fastmcp:
            client = await MCPConnectionPool._create_client(config)

            mock_fastmcp.assert_called_once_with(config["url"])
            mock_client.__aenter__.assert_called_once()
            assert client is mock_client

    @pytest.mark.asyncio
    async def test_create_client_with_command(self):
        MCPConnectionPool._security = MCPSecurityConfig(allow_commands=True)
        config = {
            "command": "python",
            "args": ["server.py"],
            "env": {"CUSTOM_VAR": "value"},
        }

        mock_client = AsyncMock()
        mock_transport = MagicMock()

        with patch("fastmcp.Client", return_value=mock_client):
            with patch(
                "fastmcp.client.transports.StdioTransport",
                return_value=mock_transport,
            ) as mock_stdio:
                client = await MCPConnectionPool._create_client(config)

                mock_stdio.assert_called_once()
                call_kwargs = mock_stdio.call_args.kwargs
                assert call_kwargs["command"] == "python"
                assert call_kwargs["args"] == ["server.py"]
                assert "CUSTOM_VAR" in call_kwargs["env"]
                assert call_kwargs["env"]["CUSTOM_VAR"] == "value"

                mock_client.__aenter__.assert_called_once()
                assert client is mock_client

    @pytest.mark.asyncio
    async def test_create_client_command_invalid_args(self):
        MCPConnectionPool._security = MCPSecurityConfig(allow_commands=True)
        config = {"command": "python", "args": "not_a_list"}  # Invalid

        with patch("fastmcp.Client"):
            with pytest.raises(ValueError, match="Config 'args' must be a list"):
                await MCPConnectionPool._create_client(config)

    @pytest.mark.asyncio
    async def test_create_client_command_debug_mode(self):
        MCPConnectionPool._security = MCPSecurityConfig(allow_commands=True)
        config = {"command": "python", "args": [], "debug": True}

        mock_client = AsyncMock()
        mock_transport = MagicMock()

        with patch("fastmcp.Client", return_value=mock_client):
            with patch(
                "fastmcp.client.transports.StdioTransport",
                return_value=mock_transport,
            ) as mock_stdio:
                await MCPConnectionPool._create_client(config)

                call_kwargs = mock_stdio.call_args.kwargs
                env = call_kwargs["env"]
                # In debug mode we must NOT force LOG_LEVEL=ERROR
                assert env.get("LOG_LEVEL") != "ERROR"


class TestMCPConnectionPoolCleanup:
    @pytest.fixture(autouse=True)
    def reset_pool(self):
        MCPConnectionPool._clients = {}
        yield
        MCPConnectionPool._clients = {}

    @pytest.mark.asyncio
    async def test_cleanup_empty_pool(self):
        await MCPConnectionPool.cleanup()
        assert len(MCPConnectionPool._clients) == 0

    @pytest.mark.asyncio
    async def test_cleanup_multiple_clients(self):
        mock_client1 = AsyncMock()
        mock_client2 = AsyncMock()

        MCPConnectionPool._clients = {
            "client1": mock_client1,
            "client2": mock_client2,
        }

        await MCPConnectionPool.cleanup()

        mock_client1.__aexit__.assert_called_once()
        mock_client2.__aexit__.assert_called_once()
        assert len(MCPConnectionPool._clients) == 0

    @pytest.mark.asyncio
    async def test_cleanup_continues_on_error(self):
        mock_client1 = AsyncMock()
        mock_client1.__aexit__.side_effect = Exception("Cleanup error")
        mock_client2 = AsyncMock()

        MCPConnectionPool._clients = {
            "client1": mock_client1,
            "client2": mock_client2,
        }

        # Should not raise, just log
        await MCPConnectionPool.cleanup()

        mock_client1.__aexit__.assert_called_once()
        mock_client2.__aexit__.assert_called_once()
        assert len(MCPConnectionPool._clients) == 0


class TestCreateMCPTool:
    @pytest.mark.asyncio
    async def test_create_mcp_tool_basic(self):
        mcp_config = {"url": "http://localhost:8080"}
        tool_name = "test_tool"

        tool = create_mcp_tool(mcp_config, tool_name)

        assert callable(tool)
        assert tool.__name__ == tool_name
        assert "MCP tool" in tool.__doc__

    @pytest.mark.asyncio
    async def test_mcp_tool_execution(self):
        mcp_config = {"url": "http://localhost:8080"}
        tool_name = "test_tool"

        mock_client = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = [MagicMock(text="result text")]
        mock_client.call_tool.return_value = mock_result

        with patch.object(MCPConnectionPool, "get_client", return_value=mock_client):
            tool = create_mcp_tool(mcp_config, tool_name)
            result = await tool(arg1="value1")

            mock_client.call_tool.assert_called_once_with(tool_name, {"arg1": "value1"})
            assert result == "result text"

    @pytest.mark.asyncio
    async def test_mcp_tool_with_original_name_metadata(self):
        mcp_config = {
            "url": "http://localhost:8080",
            "_original_tool_name": "actual_name",
        }
        tool_name = "prefixed_actual_name"

        mock_client = AsyncMock()
        mock_result = "result"
        mock_client.call_tool.return_value = mock_result

        with patch.object(MCPConnectionPool, "get_client", return_value=mock_client):
            tool = create_mcp_tool(mcp_config, tool_name)
            await tool()

            # Should call with original name, not prefixed
            mock_client.call_tool.assert_called_once_with("actual_name", {})

    @pytest.mark.asyncio
    async def test_mcp_tool_result_with_dict_content(self):
        mcp_config = {"url": "http://localhost:8080"}
        tool_name = "test_tool"

        mock_client = AsyncMock()
        mock_result = [{"type": "text", "text": "dict result"}]
        mock_client.call_tool.return_value = mock_result

        with patch.object(MCPConnectionPool, "get_client", return_value=mock_client):
            tool = create_mcp_tool(mcp_config, tool_name)
            result = await tool()

            assert result == "dict result"

    @pytest.mark.asyncio
    async def test_mcp_tool_result_passthrough(self):
        mcp_config = {"url": "http://localhost:8080"}
        tool_name = "test_tool"

        mock_client = AsyncMock()
        mock_result = {"custom": "data"}
        mock_client.call_tool.return_value = mock_result

        with patch.object(MCPConnectionPool, "get_client", return_value=mock_client):
            tool = create_mcp_tool(mcp_config, tool_name)
            result = await tool()

            assert result == {"custom": "data"}


class TestValidateMcpToolAdmission:
    """Pure classifier cases: representative tool name / schema / description
    combinations that must be denied or admitted by the generic-executor
    admission rule."""

    DENY_CASES = [
        pytest.param(
            "run_tests",
            {"type": "object", "properties": {"command": {"type": "string"}}},
            None,
            "unbounded-command-input",
            id="command-only-field-benign-name",
        ),
        pytest.param(
            "maintenance",
            {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
            },
            None,
            "unbounded-command-input",
            id="command-field-with-auxiliary-modifiers",
        ),
        pytest.param(
            "spawn_process",
            {
                "type": "object",
                "properties": {
                    "program": {"type": "string"},
                    "argv": {"type": "array", "items": {"type": "string"}},
                },
            },
            None,
            "unbounded-process-input",
            id="program-plus-argv",
        ),
        pytest.param(
            "bash",
            {"type": "object", "properties": {"script": {"type": "string"}}},
            None,
            "unbounded-script-payload",
            id="strong-name-with-script-payload",
        ),
        pytest.param(
            "maintenance",
            {"type": "object", "properties": {"input": {"type": "string"}}},
            "executes arbitrary shell commands",
            "unbounded-script-payload",
            id="payload-field-corroborated-by-description",
        ),
        pytest.param(
            "maintenance",
            {"type": "object", "properties": {"payload": {"type": "string"}}},
            "run a command",
            "executor-description-with-broad-input",
            id="broad-field-corroborated-by-description",
        ),
        pytest.param(
            "run_command",
            None,
            None,
            "executor-identity-with-insufficient-schema",
            id="strong-name-metadata-free",
        ),
        pytest.param(
            "exec",
            None,
            None,
            "executor-identity-with-insufficient-schema",
            id="strong-name-explicit-registration-no-descriptor",
        ),
        pytest.param(
            "run-command",
            None,
            None,
            "executor-identity-with-insufficient-schema",
            id="hyphen-normalizes-to-strong-name",
        ),
        pytest.param(
            "runCommand",
            None,
            None,
            "executor-identity-with-insufficient-schema",
            id="camel-case-normalizes-to-strong-name",
        ),
        pytest.param(
            "maintenance",
            {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "label": {"type": "string"},
                },
            },
            "Runs a maintenance task",
            "unbounded-command-input",
            id="unrelated-extra-field-does-not-neutralize-command-key",
        ),
        pytest.param(
            "exec",
            {"type": "object", "properties": {"payload": {"type": "string"}}},
            None,
            "executor-identity-with-insufficient-schema",
            id="strong-name-with-uncategorized-free-form-property",
        ),
        pytest.param(
            "run_command",
            {"type": "object", "properties": {"command": True}},
            None,
            "unbounded-command-input",
            id="boolean-true-schema-accepts-any-value",
        ),
        pytest.param(
            "run_command",
            {"type": "object", "properties": {"command": {"type": ["string", "null"]}}},
            None,
            "unbounded-command-input",
            id="nullable-type-union-still-accepts-string",
        ),
        pytest.param(
            "maintenance",
            {"type": "object", "properties": {"payload": {"type": "string"}}},
            "Runs OS commands supplied by the caller",
            "executor-description-with-broad-input",
            id="plural-inflection-of-description-phrase",
        ),
        pytest.param(
            "maintenance",
            {
                "type": ["object", "null"],
                "properties": {"command": {"type": "string"}},
            },
            None,
            "unbounded-command-input",
            id="nullable-object-top-level-type-array-still-inspected",
        ),
        # --- Descriptor-indirection evasion shapes (traversal coverage) ---
        pytest.param(
            "maintenance",
            {
                "type": "object",
                "properties": {
                    "options": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                    }
                },
            },
            "runs shell commands",
            "unbounded-command-input",
            id="nested-object-property-command-channel",
        ),
        pytest.param(
            "maintenance",
            {
                "type": "object",
                "anyOf": [
                    {"properties": {"target": {"type": "string", "enum": ["a", "b"]}}},
                    {"properties": {"command": {"type": "string"}}},
                ],
            },
            "runs shell commands",
            "unbounded-command-input",
            id="anyof-branch-command-channel",
        ),
        pytest.param(
            "maintenance",
            {
                "type": "object",
                "properties": {"config": {"$ref": "#/$defs/CommandConfig"}},
                "$defs": {
                    "CommandConfig": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                    }
                },
            },
            "runs shell commands",
            "unbounded-command-input",
            id="local-ref-resolves-to-command-channel",
        ),
        pytest.param(
            "maintenance",
            {"type": "object", "additionalProperties": {"type": "string"}},
            "runs shell commands",
            "executor-description-with-broad-input",
            id="freeform-additionalproperties-channel-with-executor-description",
        ),
        pytest.param(
            "maintenance",
            {
                "type": "object",
                "patternProperties": {"^command$": {"type": "string"}},
            },
            "runs shell commands",
            "unbounded-command-input",
            id="patternproperties-command-channel",
        ),
        pytest.param(
            "exec",
            {"type": "object", "additionalProperties": {"type": "string"}},
            None,
            "executor-identity-with-insufficient-schema",
            id="strong-name-freeform-additionalproperties-channel",
        ),
        pytest.param(
            "maintenance",
            {
                "type": "object",
                "properties": {"config": {"$ref": "https://example.com/schemas/x.json"}},
            },
            "runs shell commands",
            "executor-description-with-broad-input",
            id="external-ref-fails-closed-for-executor-description",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {"config": {"$ref": "https://example.com/schemas/x.json"}},
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="external-ref-fails-closed-for-strong-name",
        ),
        # --- Round-2 evasions: conditional/array/object-map command channels ---
        pytest.param(
            "maintenance",
            {
                "type": "object",
                "if": {"properties": {"mode": {"const": "advanced"}}},
                "then": {"properties": {"command": {"type": "string"}}},
            },
            "runs shell commands",
            "unbounded-command-input",
            id="if-then-branch-command-channel",
        ),
        pytest.param(
            "maintenance",
            {
                "type": "object",
                "properties": {
                    "cmds": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"command": {"type": "string"}},
                        },
                    }
                },
            },
            "runs shell commands",
            "unbounded-command-input",
            id="array-items-object-command-channel",
        ),
        pytest.param(
            "maintenance",
            {
                "type": "object",
                "properties": {
                    "cmds": {
                        "type": "array",
                        "prefixItems": [
                            {
                                "type": "object",
                                "properties": {"command": {"type": "string"}},
                            }
                        ],
                    }
                },
            },
            "runs shell commands",
            "unbounded-command-input",
            id="prefix-items-object-command-channel",
        ),
        pytest.param(
            "maintenance",
            {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                },
            },
            "runs shell commands",
            "unbounded-command-input",
            id="object-valued-additionalproperties-map-command-channel",
        ),
        # --- Round-2: identifier-suffix exemption must not launder exec targets ---
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["run"]},
                    "executable_path": {"type": "string"},
                },
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="executable-path-not-exempted-under-strong-name",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["run"]},
                    "script_path": {"type": "string"},
                },
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="script-path-not-exempted-under-strong-name",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["run"]},
                    "command_path": {"type": "string"},
                },
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="command-path-not-exempted-under-strong-name",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["run"]},
                    "binary_path": {"type": "string"},
                },
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="binary-path-not-exempted-under-strong-name",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["run"]},
                    "program_path": {"type": "string"},
                },
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="program-path-not-exempted-under-strong-name",
        ),
        # --- Round-2: malformed patternProperties must fail closed, not open ---
        pytest.param(
            "maintenance",
            {"type": "object", "patternProperties": ["not-a-mapping"]},
            "runs shell commands",
            "executor-description-with-broad-input",
            id="non-mapping-pattern-properties-fails-closed",
        ),
        pytest.param(
            "maintenance",
            {"type": "object", "patternProperties": {"(": {"type": "string"}}},
            "runs shell commands",
            "executor-description-with-broad-input",
            id="invalid-pattern-regex-fails-closed",
        ),
        # --- Round-2: unresolvable node-work budget denies fast for
        # executor-signaling descriptors with harmless-looking wide fan-out. ---
        pytest.param(
            "maintenance",
            {
                "type": "object",
                "anyOf": [
                    {"properties": {f"field_{i}": {"type": "string", "enum": ["a", "b"]}}}
                    for i in range(10_000)
                ],
            },
            "runs shell commands",
            "executor-description-with-broad-input",
            id="wide-anyof-fanout-exceeds-node-budget",
        ),
        # --- Composition on the keyed property itself must not strip the
        # key association: these wrap a plain free-form string leaf in one
        # applicator layer (anyOf / if-then / allOf), which constrains the
        # SAME instance the key names. ---
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {"input": {"anyOf": [{"type": "string"}]}},
            },
            None,
            "unbounded-script-payload",
            id="anyof-wrapped-free-form-property-still-attributed-to-key",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "payload": {
                        "if": {"minLength": 1},
                        "then": {"type": "string"},
                    }
                },
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="conditional-wrapped-free-form-property-still-attributed-to-key",
        ),
        pytest.param(
            "spawn_process",
            {
                "type": "object",
                "properties": {
                    "command": {"allOf": [{"type": "string"}]},
                },
            },
            None,
            "unbounded-command-input",
            id="allof-wrapped-command-key-is-still-a-command-channel",
        ),
        # --- prefixItems tuple validation is an argv-shaped channel exactly
        # like items-of-strings. ---
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "argv": {
                        "type": "array",
                        "prefixItems": [{"type": "string"}],
                    }
                },
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="prefixitems-array-of-strings-is-a-free-form-leaf",
        ),
        # --- The unknown-keyword whitelist applies to leaf-shaped property
        # schemas too, not only to schema nodes reached via _walk_schema. ---
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "options": {
                        "type": "object",
                        "unevaluatedProperties": {"type": "string"},
                    }
                },
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="unknown-subschema-keyword-on-property-is-unresolvable",
        ),
        # --- $dynamicRef / $recursiveRef are schema-bearing REFERENCE
        # keywords whose value is a plain string, not a Mapping -- they must
        # be recognized by keyword identity (not the generic value-type
        # test) and treated as unresolvable, so a command channel reachable
        # only behind one is not silently admitted. ---
        pytest.param(
            "exec",
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {"options": {"$dynamicRef": "#command_object"}},
                "$defs": {
                    "command_object": {
                        "$dynamicAnchor": "command_object",
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                    }
                },
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="dynamic-ref-string-valued-reference-is-unresolvable",
        ),
        pytest.param(
            "maintenance",
            {
                "type": "object",
                "properties": {"options": {"$recursiveRef": "#"}},
            },
            "runs shell commands",
            "executor-description-with-broad-input",
            id="recursive-ref-string-valued-reference-is-unresolvable",
        ),
        # --- Array-leaf free-form detection must be a recursive,
        # key-preserving predicate over `items`/`prefixItems`: `true`, `{}`
        # (an empty/unconstrained item schema), item-level `anyOf` reaching
        # a string, and a `prefixItems` member of `true` are all just as
        # free-form as `items: {"type": "string"}`. ---
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"const": "status"},
                    "args": {"type": "array", "items": True},
                },
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="array-items-boolean-true-is-free-form-argv-channel",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"const": "status"},
                    "args": {"type": "array", "items": {}},
                },
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="array-items-empty-schema-is-free-form-argv-channel",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"const": "status"},
                    "args": {
                        "type": "array",
                        "items": {"anyOf": [{"type": "string"}]},
                    },
                },
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="array-items-anyof-reaches-string-is-free-form-argv-channel",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"const": "status"},
                    "argv": {"type": "array", "prefixItems": [True]},
                },
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="prefixitems-boolean-true-member-is-free-form-argv-channel",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"const": "status"},
                    "args": {
                        "type": "array",
                        "items": {"$ref": "#/$defs/StringArg"},
                    },
                },
                "$defs": {"StringArg": {"type": "string"}},
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="array-items-local-ref-reaches-string-is-free-form-argv-channel",
        ),
        # --- Nested-array items: an array item is not "non-string, therefore
        # bounded" on its own -- the walker must recurse into ITS OWN
        # items/prefixItems, or a caller can smuggle a free-form argv
        # channel one array level deeper (`args: [["sh", "-c", ...]]`). ---
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"const": "status"},
                    "args": {
                        "type": "array",
                        "items": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="nested-array-items-reaches-free-form-string-is-argv-channel",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"const": "status"},
                    "args": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "prefixItems": [{"type": "string"}],
                        },
                    },
                },
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="nested-prefixitems-reaches-free-form-string-is-argv-channel",
        ),
        # --- Union-composition sufficiency: `_schema_is_insufficient` must judge `anyOf`/`oneOf`
        # as UNIONS -- every branch must independently prove bounded, or a
        # caller can register under the least-bounded alternative. ---
        pytest.param(
            "exec",
            {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {"operation": {"const": "status"}},
                        "required": ["operation"],
                        "additionalProperties": False,
                    },
                    {"type": "object"},
                ]
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="anyof-with-fully-open-object-alternative-is-insufficient",
        ),
        pytest.param(
            "exec",
            {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {"operation": {"const": "status"}},
                        "required": ["operation"],
                        "additionalProperties": False,
                    },
                    {},
                ]
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="anyof-with-empty-schema-alternative-is-insufficient",
        ),
        # --- Vendor-annotation exemption must be VALUE-based,
        # not just key-based -- an `x-*` extension whose value embeds real
        # schema vocabulary is a hidden channel, not inert metadata. ---
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {"operation": {"const": "status"}},
                "required": ["operation"],
                "additionalProperties": False,
                "x-input-schema": {"properties": {"command": {"type": "string"}}},
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="schema-bearing-vendor-extension-is-not-exempted",
        ),
    ]

    ADMIT_CASES = [
        pytest.param(
            "run_tests",
            {
                "type": "object",
                "properties": {
                    "suite": {"type": "string", "enum": ["unit", "integration"]},
                    "test_path": {"type": "string"},
                    "markers": {"type": "array", "items": {"type": "string"}},
                    "coverage": {"type": "boolean"},
                },
            },
            None,
            id="structured-run-tests-is-not-a-shell-executor",
        ),
        pytest.param(
            "search",
            {"type": "object", "properties": {"query": {"type": "string"}}},
            None,
            id="ordinary-single-string-search",
        ),
        pytest.param(
            "execute_query",
            {
                "type": "object",
                "properties": {
                    "database_id": {"type": "string"},
                    "query": {"type": "string"},
                },
            },
            None,
            id="execute-query-name-is-not-strong-name",
        ),
        pytest.param(
            "command_status",
            {"type": "object", "properties": {"job_id": {"type": "string"}}},
            None,
            id="command-status-is-observation-not-executor",
        ),
        pytest.param(
            "shellfish_lookup",
            {"type": "object", "properties": {"query": {"type": "string"}}},
            None,
            id="substring-shell-does-not-match",
        ),
        pytest.param(
            "format_command",
            {
                "type": "object",
                "properties": {"parts": {"type": "array", "items": {"type": "string"}}},
            },
            "formats a shell command",
            id="formatter-phrase-is-not-a-description-signal",
        ),
        pytest.param(
            "build_target",
            {
                "type": "object",
                "properties": {"command": {"type": "string", "enum": ["build", "clean", "test"]}},
            },
            None,
            id="enum-bounded-command-field",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {"operation": {"type": "string", "enum": ["status", "restart"]}},
            },
            None,
            id="strong-name-with-rich-bounded-schema-overrides-heuristic",
        ),
        pytest.param(
            "mocked_tool",
            None,
            None,
            id="metadata-free-non-strong-name-compatibility",
        ),
        pytest.param(
            "search",
            {
                "type": ["object", "null"],
                "properties": {"query": {"type": "string"}},
            },
            None,
            id="nullable-object-type-array-with-harmless-property-still-admitted",
        ),
        # --- False-positive fix: strong name + fixed operation + dynamic
        # identifier/path/request-id fields is not executor-shaped. ---
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["status", "restart"]},
                    "service_id": {"type": "string"},
                },
            },
            None,
            id="strong-name-fixed-operation-with-dynamic-service-id",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["status", "restart"]},
                    "resource_path": {"type": "string"},
                },
            },
            None,
            id="strong-name-fixed-operation-with-dynamic-resource-path",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["status", "restart"]},
                    "request_id": {"type": "string"},
                },
            },
            None,
            id="strong-name-fixed-operation-with-dynamic-request-id",
        ),
        # --- Traversal coverage: harmless nested/composed schemas remain
        # admitted (no command-like free-form channel reachable). ---
        pytest.param(
            "maintenance",
            {
                "type": "object",
                "anyOf": [
                    {"properties": {"mode": {"type": "string", "enum": ["fast", "slow"]}}},
                    {"properties": {"level": {"type": "string", "enum": ["low", "high"]}}},
                ],
            },
            None,
            id="anyof-of-two-bounded-shapes",
        ),
        pytest.param(
            "maintenance",
            {
                "type": "object",
                "properties": {
                    "options": {
                        "type": "object",
                        "properties": {
                            "verbosity": {"type": "string", "enum": ["low", "high"]},
                            "label": {"type": "string"},
                        },
                    }
                },
            },
            None,
            id="nested-config-object-without-command-like-fields",
        ),
        # --- Round-2: benign identifier-suffix fields remain admitted. ---
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["status", "restart"]},
                    "tenant_uuid": {"type": "string"},
                },
            },
            None,
            id="strong-name-fixed-operation-with-dynamic-tenant-uuid",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["status", "restart"]},
                    "callback_url": {"type": "string"},
                },
            },
            None,
            id="strong-name-fixed-operation-with-dynamic-callback-url",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["status", "restart"]},
                    "page_slug": {"type": "string"},
                },
            },
            None,
            id="strong-name-fixed-operation-with-dynamic-page-slug",
        ),
        # --- Round-1 regression: array-of-strings free-form leaf channel
        # (argv) must still be caught even though the walker now also
        # recurses into `items` for hidden object-shaped command channels. ---
        pytest.param(
            "run_tests",
            {
                "type": "object",
                "properties": {
                    "suite": {"type": "string", "enum": ["unit", "integration"]},
                    "markers": {"type": "array", "items": {"type": "string"}},
                },
            },
            None,
            id="array-of-strings-leaf-remains-benign-when-not-corroborated",
        ),
        # --- False-positive fix: a bounded, closed schema expressed via a
        # top-level local `$ref` is not "schema-less" -- the sufficiency
        # gate must resolve it (as the walker itself already does) instead
        # of demanding the caller-provided root carry `properties` directly.
        pytest.param(
            "exec",
            {
                "$ref": "#/$defs/BoundedOperation",
                "$defs": {
                    "BoundedOperation": {
                        "type": "object",
                        "properties": {"operation": {"const": "status"}},
                        "required": ["operation"],
                        "additionalProperties": False,
                    }
                },
            },
            None,
            id="strong-name-root-ref-to-bounded-closed-schema",
        ),
        # --- False-positive fix: a vendor-extension/annotation keyword
        # (`x-ui`) on an otherwise bounded schema is metadata, not an
        # applicator, and must not trip the unknown-subschema-bearing check.
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {"operation": {"type": "string", "enum": ["status", "restart"]}},
                "x-ui": {"widget": "select", "order": 1},
            },
            None,
            id="strong-name-bounded-schema-with-vendor-extension-annotation",
        ),
        pytest.param(
            "spawn_process",
            {
                "$ref": "#/$defs/BoundedOperation",
                "$defs": {
                    "BoundedOperation": {
                        "type": "object",
                        "properties": {"operation": {"const": "status"}},
                        "required": ["operation"],
                        "additionalProperties": False,
                    }
                },
            },
            None,
            id="strong-name-spawn-process-root-ref-to-bounded-closed-schema",
        ),
        # --- Anti-over-block: a nested array bounded by enum/const
        # items at every level must remain admitted -- only a nested array
        # that itself REACHES a free-form string is denied. ---
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"const": "status"},
                    "args": {
                        "type": "array",
                        "items": {"type": "array", "items": {"enum": ["a", "b"]}},
                    },
                },
            },
            None,
            id="nested-array-bounded-by-enum-items-remains-admitted",
        ),
        # --- Anti-over-block: `anyOf` where EVERY alternative is
        # independently closed-bounded must still admit -- the union check
        # only denies when at least one alternative is unbounded. ---
        pytest.param(
            "exec",
            {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {"operation": {"const": "status"}},
                        "required": ["operation"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {"operation": {"const": "restart"}},
                        "required": ["operation"],
                        "additionalProperties": False,
                    },
                ]
            },
            None,
            id="anyof-with-every-alternative-closed-bounded-remains-admitted",
        ),
    ]

    @pytest.mark.parametrize("tool_name, input_schema, description, reason", DENY_CASES)
    def test_denies_generic_executor_shape(self, tool_name, input_schema, description, reason):
        with pytest.raises(PermissionError) as exc_info:
            validate_mcp_tool_admission(tool_name, input_schema, description)

        message = str(exc_info.value)
        assert tool_name in message
        assert reason in message
        assert "opt-out" in message

    @pytest.mark.parametrize("tool_name, input_schema, description", ADMIT_CASES)
    def test_admits_ordinary_or_bounded_tool(self, tool_name, input_schema, description):
        assert validate_mcp_tool_admission(tool_name, input_schema, description) is None

    def test_nested_channel_denial_does_not_echo_schema_or_description_content(self):
        """Traversal reaches deep into nested/composed schemas to find the
        command channel, but the denial message must still carry only the
        tool name and stable reason code -- never schema values, the
        offending key path, or the description text."""
        sentinel_command_value_marker = "sentinel-should-never-appear-CmdSecretXYZ"
        with pytest.raises(PermissionError) as exc_info:
            validate_mcp_tool_admission(
                "maintenance",
                {
                    "type": "object",
                    "properties": {
                        "options": {
                            "type": "object",
                            "properties": {
                                "command": {
                                    "type": "string",
                                    "default": sentinel_command_value_marker,
                                }
                            },
                        }
                    },
                },
                f"runs shell commands with {sentinel_command_value_marker}",
            )

        message = str(exc_info.value)
        assert sentinel_command_value_marker not in message
        assert "maintenance" in message
        assert "unbounded-command-input" in message

    def test_wide_anyof_fanout_stays_bounded_and_denies(self):
        """A 10,000-branch `anyOf` fan-out (all harmless enum-bounded
        properties) must not be fully walked node-by-node: the node-work
        budget trips, and for an executor-signaling tool that unresolvable
        result denies fail-closed -- in well under a second, not the
        multi-second cost of walking every branch."""
        import time

        huge_schema = {
            "type": "object",
            "anyOf": [
                {"properties": {f"field_{i}": {"type": "string", "enum": ["a", "b"]}}}
                for i in range(10_000)
            ],
        }

        start = time.perf_counter()
        with pytest.raises(PermissionError) as exc_info:
            validate_mcp_tool_admission("maintenance", huge_schema, "runs shell commands")
        elapsed = time.perf_counter() - start

        assert "executor-description-with-broad-input" in str(exc_info.value)
        assert elapsed < 2.0

    def test_wide_anyof_fanout_admits_when_not_executor_signaling(self):
        """The same wide fan-out schema, with no strong name or executor
        description, is still just insufficient evidence -- not a denial."""
        huge_schema = {
            "type": "object",
            "anyOf": [
                {"properties": {f"field_{i}": {"type": "string", "enum": ["a", "b"]}}}
                for i in range(10_000)
            ],
        }

        assert validate_mcp_tool_admission("search_config", huge_schema, None) is None
