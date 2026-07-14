"""Tests for lionagi.service.connections.mcp.wrapper module."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest
from jsonschema import Draft202012Validator

# Private sufficiency-proof internals -- imported directly so the
# registry-generated regression matrix below can assert the STRUCTURAL
# coverage claim in isolation, independent of the (separately tested)
# key-name walker layer. `_schema_is_insufficient` is the same function
# `validate_mcp_tool_admission` calls; testing it directly avoids a
# confound where the walker's own command-key detection would raise a
# DENY for a reason unrelated to the sufficiency proof's own coverage.
from lionagi.service.connections.mcp_wrapper import (
    _BOUNDING_KEYWORDS,
    _DENIED_APPLICATOR_KEYWORDS,
    _INERT_ANNOTATION_KEYWORDS,
    _MODELED_APPLICATOR_KEYWORDS,
    MCPConnectionPool,
    MCPSecurityConfig,
    _classify_keyword,
    _schema_is_insufficient,
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
        # Conditional/array/object-map command channels must fail closed.
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
        # Identifier-suffix exemption must not launder exec targets.
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
        # Malformed patternProperties must fail closed, not open.
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
        # Unresolvable node-work budget denies fast for
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
        # --- A missing `items` keyword defaults to `true` in
        # Draft 2020-12 -- an entirely unconstrained "rest of the array" --
        # and every `prefixItems` member must itself be checked, not just
        # whatever `items` says. An inner `{"type": "array"}` item with no
        # `items`/`prefixItems` of its own is free-form one level deeper,
        # not "non-string and therefore bounded". ---
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"const": "status"},
                    "args": {"type": "array", "items": {"type": "array"}},
                },
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="nested-array-item-with-no-items-defaults-open",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"const": "status"},
                    "args": {
                        "type": "array",
                        "prefixItems": [{"enum": ["fixed"]}],
                    },
                },
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="prefixitems-only-array-with-no-items-rest-is-open",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"const": "status"},
                    "args": {
                        "type": "array",
                        "items": {"enum": ["fixed"]},
                        "prefixItems": [{"type": "string"}],
                    },
                },
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="bounded-items-does-not-launder-an-unbounded-prefixitems-member",
        ),
        # --- A strong-executor-name schema with non-empty
        # `properties` is only actually bounded when the object is CLOSED
        # (`additionalProperties: False`, or an additionalProperties schema
        # itself restricted to a finite enum/const) -- JSON Schema's
        # implicit-open default otherwise still admits an undeclared
        # "command"-shaped key riding alongside a perfectly bounded
        # `operation`. ---
        pytest.param(
            "exec",
            {"type": "object", "properties": {"operation": {"const": "status"}}},
            None,
            "executor-identity-with-insufficient-schema",
            id="open-object-with-only-bounded-properties-is-still-insufficient",
        ),
        pytest.param(
            "exec",
            {
                "type": ["object", "string"],
                "properties": {"operation": {"const": "status"}},
                "additionalProperties": False,
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="root-type-union-with-free-form-string-alternative-is-insufficient",
        ),
        pytest.param(
            "exec",
            {
                "allOf": [
                    {"type": "object", "properties": {"operation": {"const": "status"}}},
                    {"type": "object"},
                ]
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="allof-open-object-branch-does-not-narrow-to-sufficient",
        ),
        # --- `$ref` SIBLINGS are evaluated in Draft 2020-12, not
        # discarded -- a free-form property declared alongside a `$ref` must
        # still be walked and caught, even though the ref target itself is
        # (partially) bounded. ---
        pytest.param(
            "exec",
            {
                "$ref": "#/$defs/open",
                "properties": {"command": {"type": "string"}},
                "$defs": {
                    "open": {
                        "type": "object",
                        "properties": {"operation": {"const": "status"}},
                    }
                },
            },
            None,
            "unbounded-command-input",
            id="ref-sibling-command-property-is-not-discarded",
        ),
        # --- The unknown-keyword whitelist's could-carry-a-
        # subschema test must recurse through nested lists (a list of lists
        # of mappings), not just one level -- otherwise wrapping a hidden
        # command channel in extra list nesting under an unrecognized
        # keyword bypasses deny-by-default entirely. ---
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {"operation": {"const": "status"}},
                "required": ["operation"],
                "additionalProperties": False,
                "future-extension": [[{"properties": {"command": {"type": "string"}}}]],
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="nested-list-of-list-of-mapping-unknown-keyword-is-unresolvable",
        ),
        # Scalar-only keyword recognition is an explicit enumeration of the
        # standardized numeric/size bounds, never a min*/max* spelling
        # heuristic -- an unknown vocabulary key that merely STARTS with
        # min/max must still reach the could-carry-subschema check.
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {"operation": {"const": "status"}},
                "required": ["operation"],
                "additionalProperties": False,
                "minCustomVocabulary": {"properties": {"command": {"type": "string"}}},
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="min-prefixed-unknown-keyword-hiding-a-subschema-is-unresolvable",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {"operation": {"const": "status"}},
                "required": ["operation"],
                "additionalProperties": False,
                "maxFuture": [[{"properties": {"command": {"type": "string"}}}]],
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="max-prefixed-unknown-keyword-nested-list-is-unresolvable",
        ),
        # Unknown-keyword and annotation value inspection consumes the same
        # node budget as schema traversal and fails closed on exhaustion --
        # a pathologically wide scalar list cannot force unbounded work and
        # cannot be admitted unexamined.
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {"operation": {"const": "status"}},
                "required": ["operation"],
                "additionalProperties": False,
                "future-extension": [0] * 100_000,
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="unknown-keyword-scalar-list-exceeding-node-budget-fails-closed",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {"operation": {"const": "status"}},
                "required": ["operation"],
                "additionalProperties": False,
                "x-huge": [0] * 100_000,
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="annotation-scalar-list-exceeding-node-budget-fails-closed",
        ),
        # --- Sufficiency-proof allowlist gate: a `patternProperties`
        # entry whose PATTERN does not match any of the walker's fixed
        # categorized key names (so the walker never even considers the
        # pattern's own subschema) is a caller-chosen-key command channel
        # the walker's key-name classifier structurally cannot see. The
        # sufficiency proof's allowlist gate denies it directly: a
        # `patternProperties` value is a mapping (schema-bearing) and the
        # keyword itself is not in the proof's modeled-keyword set. ---
        pytest.param(
            "exec",
            {
                "type": "object",
                "patternProperties": {"^command_custom$": {"type": "string"}},
                "additionalProperties": False,
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="patternproperties-non-categorized-key-denied-by-sufficiency-gate",
        ),
        # --- Sufficiency-proof type-gate: an object descriptor that omits
        # `type` entirely (a common hand-authored shape:
        # `properties`+`additionalProperties: false` with no explicit
        # `type`) never actually constrains the instance to an object --
        # a caller can submit a bare scalar and never reach the
        # `properties`/`additionalProperties` keywords at all. ---
        pytest.param(
            "exec",
            {
                "properties": {"operation": {"const": "status"}},
                "additionalProperties": False,
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="omitted-type-closed-object-denied-by-type-gate",
        ),
        # --- Sufficiency-proof property-value recursion: the OUTER object
        # being closed (`additionalProperties: False`) says nothing about a
        # DECLARED property's own value -- a nested object-shaped property
        # value that is itself open, or carries an unmodeled applicator, is a
        # hidden key channel the proof must independently re-check. ---
        pytest.param(
            "exec",
            {
                "type": "object",
                "required": ["operation"],
                "additionalProperties": False,
                "properties": {
                    "operation": {"const": "status"},
                    "options": {
                        "type": "object",
                        "additionalProperties": False,
                        "patternProperties": {"^command_custom$": {"type": "string"}},
                    },
                },
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="nested-patternproperties-custom-key-property-value-denies",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "operation": {"const": "status"},
                    "options": {"type": "object"},
                },
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="nested-open-object-property-value-denies",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "operation": {"const": "status"},
                    "options": {"$ref": "#/$defs/Open"},
                },
                "$defs": {"Open": {"type": "object"}},
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="nested-ref-to-open-object-property-value-denies",
        ),
        # --- A synthetic never-modeled applicator keyword
        # (`quorumProperties`-style, mapping-valued) nested one level deep
        # inside a closed property value's own schema must still deny -- the
        # property-value recursion re-runs the FULL sufficiency proof
        # (including its own allowlist gate) on the nested node, not merely
        # its type/closedness reasoning. ---
        pytest.param(
            "exec",
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "operation": {"const": "status"},
                    "options": {
                        "type": "object",
                        "properties": {"mode": {"type": "string", "enum": ["a", "b"]}},
                        "additionalProperties": False,
                        "quorumProperties": {"minMembers": 2},
                    },
                },
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="nested-unmodeled-applicator-keyword-in-property-value-denies",
        ),
        # --- The recursive structural scan reaches known-but-denied
        # applicators at DEPTH -- not only directly inside the first-level
        # property value. `patternProperties` here sits inside a property
        # value that is itself the value of another property (depth 2 from
        # the root), so only a recursion that re-applies itself at every
        # nested key channel -- not a single extra check bolted onto depth
        # 1 -- catches it. ---
        pytest.param(
            "exec",
            {
                "type": "object",
                "required": ["operation"],
                "additionalProperties": False,
                "properties": {
                    "operation": {"const": "status"},
                    "options": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "suboptions": {
                                "type": "object",
                                "additionalProperties": False,
                                "patternProperties": {"^command_custom$": {"type": "string"}},
                            }
                        },
                    },
                },
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="depth-2-nested-patternproperties-custom-key-denies",
        ),
        # --- `dependentSchemas` is a walker-known-to-be-unmodeled applicator
        # (it is not in the walker's own keyword whitelist either): nested
        # inside a property value with no closing `additionalProperties`,
        # the object remains genuinely open, and `dependentSchemas` adds a
        # conditional branch on top without narrowing it. ---
        pytest.param(
            "exec",
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "operation": {"const": "status"},
                    "options": {
                        "type": "object",
                        "properties": {"mode": {"type": "string"}},
                        "dependentSchemas": {"mode": {"type": "object"}},
                    },
                },
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="dependentschemas-nested-in-property-value-denies",
        ),
        # --- `unevaluatedProperties` nested inside a property value: an
        # explicit `unevaluatedProperties: true` alongside no
        # `additionalProperties` leaves every property not otherwise
        # evaluated open, exactly like the implicit-open default -- the
        # recursion must still deny it. ---
        pytest.param(
            "exec",
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "operation": {"const": "status"},
                    "options": {
                        "type": "object",
                        "properties": {"mode": {"type": "string"}},
                        "unevaluatedProperties": True,
                    },
                },
            },
            None,
            "executor-identity-with-insufficient-schema",
            id="unevaluatedproperties-nested-in-property-value-denies",
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
                # `additionalProperties: False` is required for the strong-name
                # sufficiency gate to treat this object as closed:
                # an object schema whose `properties` are all bounded is only
                # actually bounded if the object cannot also carry an
                # undeclared key (JSON Schema's implicit-open default would
                # otherwise still admit an arbitrary extra "command"-shaped
                # property alongside "operation").
                "additionalProperties": False,
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
                # See "strong-name-with-rich-bounded-schema-overrides-heuristic"
                # above: closedness is required for the strong-name
                # sufficiency gate; this test's own concern (the identifier-
                # suffix exemption for "service_id") is orthogonal.
                "additionalProperties": False,
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
                "additionalProperties": False,
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
                "additionalProperties": False,
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
        # Benign identifier-suffix fields remain admitted.
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["status", "restart"]},
                    "tenant_uuid": {"type": "string"},
                },
                "additionalProperties": False,
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
                "additionalProperties": False,
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
                "additionalProperties": False,
            },
            None,
            id="strong-name-fixed-operation-with-dynamic-page-slug",
        ),
        # Regression: array-of-strings free-form leaf channel
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
                "additionalProperties": False,
            },
            None,
            id="strong-name-bounded-schema-with-vendor-extension-annotation",
        ),
        # The standardized numeric/size-bound keywords stay recognized as
        # scalar-only after the explicit enumeration replaced the min*/max*
        # spelling heuristic -- a bounded executor descriptor using them must
        # not be denied.
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"const": "status"},
                    "count": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 10,
                        "multipleOf": 2,
                    },
                },
                "required": ["operation"],
                "additionalProperties": False,
                "minProperties": 1,
                "maxProperties": 3,
            },
            None,
            id="strong-name-standard-numeric-bounds-remain-scalar-only",
        ),
        # A small unknown-keyword value made purely of scalars carries no
        # subschema and stays admitted -- the node-budget fail-closed rule
        # applies to pathological width, not ordinary inert extensions.
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {"operation": {"const": "status"}},
                "required": ["operation"],
                "additionalProperties": False,
                "future-extension": [0, 1, 2],
            },
            None,
            id="strong-name-small-scalar-unknown-keyword-remains-admitted",
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
        # --- Anti-over-block: the exact open-object shape that
        # must now DENY (see "open-object-with-only-bounded-properties-is-
        # still-insufficient" in DENY_CASES), but with an explicit
        # `additionalProperties: False` closing it, must still ADMIT. ---
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {"operation": {"const": "status"}},
                "additionalProperties": False,
            },
            None,
            id="closed-object-with-only-bounded-properties-remains-admitted",
        ),
        # --- Anti-over-block: a `$ref` whose only siblings are
        # pure annotations (`description`) must still admit -- annotation
        # siblings constrain nothing and must not be treated as reopening an
        # otherwise-closed reference target. ---
        pytest.param(
            "exec",
            {
                "$ref": "#/$defs/BoundedOperation",
                "description": "Restart or check status of a managed process",
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
            id="ref-with-only-annotation-siblings-remains-admitted",
        ),
        # --- Type-gate literal-pin path: a top-level `const`/`enum`
        # pins the whole instance to author-declared literal value(s), so
        # it satisfies the type-gate on its own -- the caller cannot
        # inject beyond the enumerated set even with no `type` present. ---
        pytest.param(
            "exec",
            {"const": {"operation": "status"}},
            None,
            id="root-const-pins-instance-without-type",
        ),
        pytest.param(
            "exec",
            {"enum": [{"operation": "status"}]},
            None,
            id="root-enum-pins-instance-without-type",
        ),
        # --- Sufficiency-proof property-value recursion: a nested object
        # property value that is itself provably CLOSED (own `properties` +
        # `additionalProperties: False`, every member bounded) must remain
        # admitted -- the recursion denies only an unbounded/unmodeled
        # nested channel, never a genuinely closed one. ---
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"const": "status"},
                    "options": {
                        "type": "object",
                        "properties": {"mode": {"type": "string", "enum": ["a", "b"]}},
                        "additionalProperties": False,
                    },
                },
                "additionalProperties": False,
            },
            None,
            id="nested-closed-object-property-value-remains-admitted",
        ),
        # --- LOAD-BEARING anti-over-block: the object-boundedness proof's
        # recursion must NOT fold value-boundedness into the structural
        # gate. A scalar free-form identifier property (`service_id`)
        # alongside a fixed operation carries no object/applicator keyword
        # and is not `type: object` -- so
        # `_property_value_may_be_object_shaped` excludes it from the
        # boundedness recursion entirely, and the registry-driven
        # structural-coverage traversal finds nothing but bounding
        # keywords (`type`) on its own value; the walker's
        # identifier-suffix exemption alone governs it, exactly as before
        # this fix. If either proof recursed into scalar leaf values as if
        # they needed object-closedness, this case would flip to a false
        # DENY. ---
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["status", "restart"]},
                    "service_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
            None,
            id="scalar-identifier-property-value-not-recursed-remains-admitted",
        ),
        # --- `contentSchema` MAJOR fix: a mapping-valued Content-vocabulary
        # annotation on an otherwise-bounded, closed schema must not be
        # treated as an unmodeled schema-bearing keyword. ---
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {"operation": {"const": "status"}},
                "additionalProperties": False,
                "contentSchema": {
                    "type": "object",
                    "properties": {"x": {"type": "string"}},
                },
            },
            None,
            id="content-schema-annotation-on-bounded-schema-remains-admitted",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {"operation": {"const": "status"}},
                "additionalProperties": False,
            },
            None,
            id="content-schema-annotation-removed-still-remains-admitted",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"const": "status"},
                    "args": {"type": "array", "items": {"enum": ["a", "b"]}},
                },
                "additionalProperties": False,
            },
            None,
            id="enum-bounded-array-is-admitted",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"const": "status"},
                    "args": {
                        "type": "array",
                        "prefixItems": [{"enum": ["a"]}, {"const": "b"}],
                        "items": False,
                    },
                },
                "additionalProperties": False,
            },
            None,
            id="closed-bounded-tuple-is-admitted",
        ),
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
                "additionalProperties": False,
            },
            None,
            id="nested-bounded-array-is-admitted",
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

    def test_f1_patternproperties_custom_key_denies(self):
        """`patternProperties` keyed on a pattern the walker's fixed
        categorized-key list never matches is a caller-chosen-key command
        channel: `_consider_property` (the walker's command-detection path)
        is only reached for a pattern that matches one of the fixed
        `_CATEGORIZED_KEYS`, so a pattern like `^command_custom$` (never
        equal to the literal key "command") slips past the walker
        entirely. The sufficiency proof's allowlist gate denies it
        directly instead: `patternProperties` is not a modeled keyword and
        its value is a mapping. Documents necessity: the identical schema
        validates against a raw JSON Schema validator and ACCEPTS the
        injected key, proving the schema itself was never actually
        closed."""
        schema = {
            "type": "object",
            "patternProperties": {"^command_custom$": {"type": "string"}},
            "additionalProperties": False,
        }
        with pytest.raises(PermissionError) as exc_info:
            validate_mcp_tool_admission("exec", schema, None)
        message = str(exc_info.value)
        assert "exec" in message
        assert "executor-identity-with-insufficient-schema" in message

        validator = Draft202012Validator(schema)
        assert validator.is_valid({"command_custom": "rm -rf /"})

    def test_f2_omitted_type_denies(self):
        """An object descriptor with no `type` keyword never actually
        constrains the instance shape -- `properties`/`additionalProperties`
        are only evaluated against object instances, so a bare scalar
        satisfies the schema untouched, bypassing every object-shaped
        constraint. Documents necessity: the identical schema validates a
        malicious bare string."""
        schema = {
            "properties": {"operation": {"const": "status"}},
            "additionalProperties": False,
        }
        with pytest.raises(PermissionError) as exc_info:
            validate_mcp_tool_admission("exec", schema, None)
        message = str(exc_info.value)
        assert "exec" in message
        assert "executor-identity-with-insufficient-schema" in message

        validator = Draft202012Validator(schema)
        assert validator.is_valid("rm -rf /")

    def test_f3_unevaluated_properties_false_denies_in_core(self):
        """`unevaluatedProperties: false` is a legitimate Draft-2020-12
        closing mechanism -- the raw validator below REJECTS the injected
        `command` key -- but the sufficiency proof does not model
        `unevaluatedProperties` as a closing keyword: only
        `additionalProperties: false` (or an enum/const-restricted
        `additionalProperties`) closes an object here. This is an
        ACCEPTED over-block (a real bounded schema authored with
        `unevaluatedProperties` instead of `additionalProperties` is
        denied today). A strictly-additive recovery rule -- treat
        `unevaluatedProperties: false` as closing iff the node carries no
        `patternProperties`/`allOf`/`anyOf`/`oneOf`/`$ref`/`if` -- is a
        documented follow-up that must never relax the core allowlist;
        deferred until the support cost of this over-block proves real."""
        schema = {
            "type": "object",
            "properties": {"operation": {"const": "status"}},
            "required": ["operation"],
            "unevaluatedProperties": False,
        }
        with pytest.raises(PermissionError) as exc_info:
            validate_mcp_tool_admission("exec", schema, None)
        message = str(exc_info.value)
        assert "exec" in message
        assert "executor-identity-with-insufficient-schema" in message

        validator = Draft202012Validator(schema)
        assert not validator.is_valid({"operation": "status", "command": "rm -rf /"})

    def test_f4_depth_2_nested_patternproperties_denies(self):
        """The CRITICAL bypass at depth 2: `patternProperties` sits inside
        a property value that is itself the value of another property
        (`options.suboptions`, two levels below the root), keyed on a
        pattern the walker's fixed categorized-key list never matches. Only
        a recursion that re-applies the FULL sufficiency proof at every
        nested key channel -- not a single depth-1 check -- reaches it.
        Documents necessity: the identical schema validates against a raw
        JSON Schema validator and ACCEPTS the injected key at that depth."""
        schema = {
            "type": "object",
            "required": ["operation"],
            "additionalProperties": False,
            "properties": {
                "operation": {"const": "status"},
                "options": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "suboptions": {
                            "type": "object",
                            "additionalProperties": False,
                            "patternProperties": {"^command_custom$": {"type": "string"}},
                        }
                    },
                },
            },
        }
        with pytest.raises(PermissionError) as exc_info:
            validate_mcp_tool_admission("exec", schema, None)
        message = str(exc_info.value)
        assert "exec" in message
        assert "executor-identity-with-insufficient-schema" in message

        validator = Draft202012Validator(schema)
        assert validator.is_valid(
            {"operation": "status", "options": {"suboptions": {"command_custom": "rm -rf /"}}}
        )

    def test_f5_dependentschemas_nested_in_property_value_denies(self):
        """`dependentSchemas` nested inside a property value, with no
        closing `additionalProperties` at that level, leaves the nested
        object genuinely open -- the conditional branch it adds narrows
        nothing. Documents necessity: the identical schema validates
        against a raw JSON Schema validator and ACCEPTS an injected
        `command` key riding alongside the `dependentSchemas` trigger."""
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "operation": {"const": "status"},
                "options": {
                    "type": "object",
                    "properties": {"mode": {"type": "string"}},
                    "dependentSchemas": {"mode": {"type": "object"}},
                },
            },
        }
        with pytest.raises(PermissionError) as exc_info:
            validate_mcp_tool_admission("exec", schema, None)
        message = str(exc_info.value)
        assert "exec" in message
        assert "executor-identity-with-insufficient-schema" in message

        validator = Draft202012Validator(schema)
        assert validator.is_valid(
            {"operation": "status", "options": {"mode": "a", "command": "rm -rf /"}}
        )

    def test_f6_unevaluatedproperties_nested_in_property_value_denies(self):
        """`unevaluatedProperties: true` nested inside a property value is
        an explicit-open declaration, functionally identical to the
        implicit-open default for anything the object's own `properties`
        does not cover. Documents necessity: the identical schema validates
        against a raw JSON Schema validator and ACCEPTS an injected
        `command` key at that nesting level."""
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "operation": {"const": "status"},
                "options": {
                    "type": "object",
                    "properties": {"mode": {"type": "string"}},
                    "unevaluatedProperties": True,
                },
            },
        }
        with pytest.raises(PermissionError) as exc_info:
            validate_mcp_tool_admission("exec", schema, None)
        message = str(exc_info.value)
        assert "exec" in message
        assert "executor-identity-with-insufficient-schema" in message

        validator = Draft202012Validator(schema)
        assert validator.is_valid(
            {"operation": "status", "options": {"mode": "a", "command": "rm -rf /"}}
        )

    BOUNDED_ARRAY_CASES = [
        pytest.param(
            {
                "type": "object",
                "properties": {
                    "operation": {"const": "status"},
                    "args": {
                        "type": "array",
                        "items": {"type": "array", "items": {"enum": ["a", "b"]}},
                    },
                },
                "additionalProperties": False,
            },
            {"operation": "status", "args": [["a"]]},
            {"operation": "status", "args": [["rm -rf /"]]},
            id="nested-array-bounded-by-enum-items",
        ),
        pytest.param(
            {
                "type": "object",
                "properties": {
                    "operation": {"const": "status"},
                    "args": {
                        "type": "array",
                        "prefixItems": [{"enum": ["a"]}, {"const": "b"}],
                        "items": False,
                    },
                },
                "additionalProperties": False,
            },
            {"operation": "status", "args": ["a", "b"]},
            {"operation": "status", "args": ["rm -rf /", "b"]},
            id="closed-tuple-prefixitems-bounded-with-items-false",
        ),
        pytest.param(
            {
                "type": "object",
                "properties": {
                    "operation": {"const": "status"},
                    "args": {"type": "array", "items": {"enum": ["a", "b"]}},
                },
                "additionalProperties": False,
            },
            {"operation": "status", "args": ["a", "b"]},
            {"operation": "status", "args": ["rm -rf /"]},
            id="items-enum-with-no-prefixitems",
        ),
    ]

    @pytest.mark.parametrize("schema, honest_instance, injected_instance", BOUNDED_ARRAY_CASES)
    def test_bounded_array_admission_matches_oracle_rejection(
        self, schema, honest_instance, injected_instance
    ):
        assert validate_mcp_tool_admission("exec", schema, None) is None
        validator = Draft202012Validator(schema)
        assert validator.is_valid(honest_instance)
        assert not validator.is_valid(injected_instance)

    OPEN_ARRAY_CASES = [
        pytest.param(
            {
                "type": "object",
                "properties": {
                    "operation": {"const": "status"},
                    "args": {
                        "type": "array",
                        "prefixItems": [{"const": "a"}],
                    },
                },
                "additionalProperties": False,
            },
            {"operation": "status", "args": ["a", "rm -rf /"]},
            id="prefix-items-with-open-tail",
        ),
        pytest.param(
            {
                "type": "object",
                "properties": {
                    "operation": {"const": "status"},
                    "args": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": False,
            },
            {"operation": "status", "args": ["rm -rf /"]},
            id="unbounded-items-schema",
        ),
        pytest.param(
            {
                "type": "object",
                "properties": {
                    "operation": {"const": "status"},
                    "args": {
                        "type": "array",
                        "prefixItems": [{"const": "a"}],
                        "unevaluatedItems": True,
                    },
                },
                "additionalProperties": False,
            },
            {"operation": "status", "args": ["a", "rm -rf /"]},
            id="unevaluated-items-open-tail",
        ),
    ]

    @pytest.mark.parametrize("schema, injected_instance", OPEN_ARRAY_CASES)
    def test_open_array_denial_matches_oracle_acceptance(self, schema, injected_instance):
        with pytest.raises(PermissionError):
            validate_mcp_tool_admission("exec", schema, None)
        assert Draft202012Validator(schema).is_valid(injected_instance)

    SYNTHETIC_UNKNOWN_KEYWORD_DENY_CASES = [
        pytest.param(
            {
                "type": "object",
                "properties": {"operation": {"const": "status"}},
                "additionalProperties": False,
                "quorumProperties": {"minMembers": 2},
            },
            id="top-level-root",
        ),
        pytest.param(
            {
                "type": "object",
                "properties": {
                    "opts": {
                        "type": "object",
                        "properties": {"mode": {"type": "string", "enum": ["a", "b"]}},
                        "additionalProperties": False,
                        "quorumProperties": {"minMembers": 2},
                    }
                },
                "additionalProperties": False,
            },
            id="nested-in-property-value",
        ),
        pytest.param(
            {
                "type": "object",
                "allOf": [
                    {
                        "type": "object",
                        "properties": {"operation": {"const": "status"}},
                        "additionalProperties": False,
                        "quorumProperties": {"minMembers": 2},
                    }
                ],
            },
            id="inside-allof-branch",
        ),
        pytest.param(
            {
                "$ref": "#/$defs/BoundedOperation",
                "quorumProperties": {"minMembers": 2},
                "$defs": {
                    "BoundedOperation": {
                        "type": "object",
                        "properties": {"operation": {"const": "status"}},
                        "required": ["operation"],
                        "additionalProperties": False,
                    }
                },
            },
            id="ref-sibling",
        ),
        pytest.param(
            {
                "type": "object",
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {"operation": {"const": "status"}},
                        "additionalProperties": False,
                        "quorumProperties": {"minMembers": 2},
                    },
                    {
                        "type": "object",
                        "properties": {"operation": {"const": "restart"}},
                        "additionalProperties": False,
                    },
                ],
            },
            id="inside-anyof-branch",
        ),
        pytest.param(
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "operation": {"const": "status"},
                    "options": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "suboptions": {
                                "type": "object",
                                "properties": {"mode": {"type": "string", "enum": ["a", "b"]}},
                                "additionalProperties": False,
                                "quorumProperties": {"minMembers": 2},
                            }
                        },
                    },
                },
            },
            id="nested-in-property-value-depth-2",
        ),
    ]

    @pytest.mark.parametrize("input_schema", SYNTHETIC_UNKNOWN_KEYWORD_DENY_CASES)
    def test_synthetic_unknown_keyword_denies_at_every_position(self, input_schema):
        """`quorumProperties` is a spelling the classifier has never
        modeled (deliberately NOT a vendor `x-`/`$comment` prefix, which
        would be exempt) and its value is schema-bearing (a mapping) at
        every one of the six positions parametrized here (top-level root;
        nested inside a property value at depth 1; inside an `allOf`
        branch; as a `$ref` sibling; inside an `anyOf` branch; nested
        inside a property value at depth 2, i.e. the value of a property
        that is itself the value of another property). The union of the
        walker (property-value interiors) and the sufficiency proof's
        allowlist gate (root, `allOf`/`anyOf` branches, `$ref` siblings,
        and -- after the property-value recursion -- every nested key
        channel at any depth) must deny it regardless of WHERE in the
        shape skeleton it
        appears, proving the invariant holds independent of keyword
        spelling or position."""
        with pytest.raises(PermissionError) as exc_info:
            validate_mcp_tool_admission("exec", input_schema, None)
        assert "executor-identity-with-insufficient-schema" in str(exc_info.value)

    def test_synthetic_unknown_keyword_with_inert_scalar_value_remains_admitted(self):
        """Control for the case above: the SAME unmodeled keyword spelling
        with a provably-inert SCALAR value (not a mapping/list-of-mapping)
        on an otherwise-closed schema must still admit -- the gate keys
        off VALUE SHAPE, not keyword spelling, so this does not regress
        the existing `future-extension`-style admit."""
        schema = {
            "type": "object",
            "properties": {"operation": {"const": "status"}},
            "required": ["operation"],
            "additionalProperties": False,
            "quorumProperties": 3,
        }
        assert validate_mcp_tool_admission("exec", schema, None) is None

    # jsonschema-oracle differential harness (§Fork 4): every ADMIT case
    # whose tool name is a strong executor name compiles to a schema that
    # must still REJECT a battery of command-injection attempts -- proof,
    # independent of `validate_mcp_tool_admission` admitting the
    # descriptor, that the admitted shape is actually closed. Includes the
    # four §8 applicator-root ADMITs (`$ref`-only-root ×2, `anyOf`-only-
    # root, `$ref`-with-annotation-siblings) so the binding ordering
    # correction (applicator delegation before the omitted-type denial)
    # is caught by test, not just by review.
    ADMIT_ORACLE_CASES = [
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {"operation": {"type": "string", "enum": ["status", "restart"]}},
                "additionalProperties": False,
            },
            {},
            id="strong-name-with-rich-bounded-schema-overrides-heuristic",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["status", "restart"]},
                    "service_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
            {},
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
                "additionalProperties": False,
            },
            {},
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
                "additionalProperties": False,
            },
            {},
            id="strong-name-fixed-operation-with-dynamic-request-id",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["status", "restart"]},
                    "tenant_uuid": {"type": "string"},
                },
                "additionalProperties": False,
            },
            {},
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
                "additionalProperties": False,
            },
            {},
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
                "additionalProperties": False,
            },
            {},
            id="strong-name-fixed-operation-with-dynamic-page-slug",
        ),
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
            {"operation": "status"},
            id="strong-name-root-ref-to-bounded-closed-schema",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {"operation": {"type": "string", "enum": ["status", "restart"]}},
                "x-ui": {"widget": "select", "order": 1},
                "additionalProperties": False,
            },
            {},
            id="strong-name-bounded-schema-with-vendor-extension-annotation",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"const": "status"},
                    "count": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 10,
                        "multipleOf": 2,
                    },
                },
                "required": ["operation"],
                "additionalProperties": False,
                "minProperties": 1,
                "maxProperties": 3,
            },
            {"operation": "status"},
            id="strong-name-standard-numeric-bounds-remain-scalar-only",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {"operation": {"const": "status"}},
                "required": ["operation"],
                "additionalProperties": False,
                "future-extension": [0, 1, 2],
            },
            {"operation": "status"},
            id="strong-name-small-scalar-unknown-keyword-remains-admitted",
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
            {"operation": "status"},
            id="strong-name-spawn-process-root-ref-to-bounded-closed-schema",
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
                    {
                        "type": "object",
                        "properties": {"operation": {"const": "restart"}},
                        "required": ["operation"],
                        "additionalProperties": False,
                    },
                ]
            },
            {"operation": "status"},
            id="anyof-with-every-alternative-closed-bounded-remains-admitted",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {"operation": {"const": "status"}},
                "additionalProperties": False,
            },
            {},
            id="closed-object-with-only-bounded-properties-remains-admitted",
        ),
        pytest.param(
            "exec",
            {
                "$ref": "#/$defs/BoundedOperation",
                "description": "Restart or check status of a managed process",
                "$defs": {
                    "BoundedOperation": {
                        "type": "object",
                        "properties": {"operation": {"const": "status"}},
                        "required": ["operation"],
                        "additionalProperties": False,
                    }
                },
            },
            {"operation": "status"},
            id="ref-with-only-annotation-siblings-remains-admitted",
        ),
        pytest.param(
            "exec",
            {"const": {"operation": "status"}},
            {"operation": "status"},
            id="root-const-pins-instance-without-type",
        ),
        pytest.param(
            "exec",
            {"enum": [{"operation": "status"}]},
            {"operation": "status"},
            id="root-enum-pins-instance-without-type",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {
                    "operation": {"const": "status"},
                    "options": {
                        "type": "object",
                        "properties": {"mode": {"type": "string", "enum": ["a", "b"]}},
                        "additionalProperties": False,
                    },
                },
                "additionalProperties": False,
            },
            {"operation": "status"},
            id="nested-closed-object-property-value-remains-admitted",
        ),
        pytest.param(
            "exec",
            {
                "type": "object",
                "properties": {"operation": {"const": "status"}},
                "additionalProperties": False,
                "contentSchema": {
                    "type": "object",
                    "properties": {"x": {"type": "string"}},
                },
            },
            {"operation": "status"},
            id="content-schema-annotation-on-bounded-schema-remains-admitted",
        ),
    ]

    # The four §8 applicator-root ADMITs that a naive "omitted type ==
    # insufficient" gate placed BEFORE applicator delegation would
    # false-deny -- must be present in the oracle differential above.
    _APPLICATOR_ROOT_ORACLE_IDS = frozenset(
        {
            "strong-name-root-ref-to-bounded-closed-schema",
            "strong-name-spawn-process-root-ref-to-bounded-closed-schema",
            "anyof-with-every-alternative-closed-bounded-remains-admitted",
            "ref-with-only-annotation-siblings-remains-admitted",
        }
    )

    def test_applicator_root_admits_are_covered_by_oracle_differential(self):
        """Confirms the four §8 applicator-root ADMITs are actually
        present in `ADMIT_ORACLE_CASES` below -- the exact ordering
        regression (applicator delegation must run before the
        omitted-type denial) is caught by test, not just by review."""
        covered_ids = {case.id for case in self.ADMIT_ORACLE_CASES}
        assert self._APPLICATOR_ROOT_ORACLE_IDS <= covered_ids

    @pytest.mark.parametrize("tool_name, input_schema, minimal_instance", ADMIT_ORACLE_CASES)
    def test_admit_schemas_reject_command_injection(
        self, tool_name, input_schema, minimal_instance
    ):
        """jsonschema-oracle differential harness (test-only; the
        `jsonschema` import lives in this test module only, never on the
        core admission path). Any ADMIT case whose oracle ACCEPTS a
        command-injection attempt is a design failure surfaced here as a
        red test rather than a production incident."""
        assert validate_mcp_tool_admission(tool_name, input_schema, None) is None

        validator = Draft202012Validator(input_schema)
        assert not validator.is_valid("rm -rf /")
        assert not validator.is_valid({**minimal_instance, "command": "rm -rf /"})
        assert not validator.is_valid({"cmd": "rm -rf /"})
        assert not validator.is_valid({"command_custom": "rm -rf /"})

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


class TestSufficiencyProofKeywordRegistry:
    """Registry-GENERATED regression matrix for the total structural-
    coverage traversal (`_structural_coverage_insufficient`, invoked
    through `_schema_is_insufficient`).

    Every case below is parametrized by ITERATING THE REGISTRY FROZENSETS
    directly (`_MODELED_APPLICATOR_KEYWORDS`, `_DENIED_APPLICATOR_KEYWORDS`)
    -- never a hand-listed keyword table. This is the anti-hand-enumeration
    mechanism the design requires: enumerating "which keywords need a test"
    by hand is the exact defect class (enumerate positions/keywords one at a
    time, miss the next one) the registry-driven proof exists to close. A
    keyword added to either frozenset later gains matrix coverage here
    automatically, with no test-file edit required.

    Tests exercise `_schema_is_insufficient` directly (rather than the full
    `validate_mcp_tool_admission` pipeline) so the assertions are about the
    sufficiency proof's own coverage, isolated from the separately-tested
    key-name walker (which would otherwise sometimes deny for an unrelated
    reason -- e.g. a literal `command` key -- and mask what this matrix is
    actually proving)."""

    # ---- schema-construction helpers ----------------------------------

    # Object-shaped (not a bare scalar): a composition applicator
    # (`allOf`/`anyOf`/`oneOf`/`$ref`) can resolve to ANY instance type, and
    # the object-boundedness proof's own gate for recursing into a property
    # value (`_property_value_may_be_object_shaped`) triggers on the
    # PRESENCE of a modeled-applicator keyword, not on what it resolves to
    # -- so a composition wrapping a bare scalar would independently fail
    # the (orthogonal, unchanged) object-boundedness type-gate, which is
    # not what this matrix is testing. An object-shaped benign leaf keeps
    # object-boundedness satisfied at every wrapper while still exercising
    # structural-coverage's recursion through each modeled applicator.
    BENIGN_LEAF = {
        "type": "object",
        "properties": {"x": {"const": 1}},
        "additionalProperties": False,
    }
    # Carries a DENIED-applicator keyword (`not`) -- any value works, since
    # a denied applicator's mere PRESENCE denies, regardless of content.
    DENIED_LEAF = {"not": {"type": "null"}}
    # A keyword the registry has never seen, Mapping-valued (schema-bearing
    # in shape) -- must be denied as UNKNOWN, not silently admitted.
    UNKNOWN_LEAF = {"quorumProperties": {"minMembers": 2}}
    # Same synthetic keyword, but with a scalar value: cannot itself carry
    # a subschema, so it is the one tolerated UNKNOWN residual.
    UNKNOWN_LEAF_SCALAR_VALUE = 3

    @staticmethod
    def _nest_property(inner, depth):
        """Wrap `inner` `depth - 1` levels deep inside closed nested-object
        property values (`{"nested": ...}`), so the recursion under test is
        exercised at nesting depths 1 (direct), 2, and 3."""
        node = inner
        for _ in range(depth - 1):
            node = {
                "type": "object",
                "additionalProperties": False,
                "properties": {"nested": node},
            }
        return node

    @staticmethod
    def _root_with_target(target_value):
        """A closed, bounded root object with a fixed `operation` and one
        additional declared property (`target`) holding the position under
        test."""
        return {
            "type": "object",
            "required": ["operation"],
            "additionalProperties": False,
            "properties": {"operation": {"const": "status"}, "target": target_value},
        }

    @staticmethod
    def _inject_at_depth(depth):
        """A `command`-shaped injection payload buried `depth` levels deep
        under the SAME `{"nested": ...}` chain `_nest_property` builds --
        for the oracle differential proving an ADMIT verdict is not a
        security hole."""
        node = {"command": "rm -rf /"}
        for _ in range(depth - 1):
            node = {"nested": node}
        return node

    @classmethod
    def _build_modeled(cls, keyword, payload):
        """A minimal schema in which `keyword` (a MODELED applicator) is
        the applicator the sufficiency proof must recurse THROUGH to reach
        `payload`."""
        if keyword == "properties":
            return {
                "type": "object",
                "properties": {"inner": payload},
                "additionalProperties": False,
            }
        if keyword == "additionalProperties":
            # `additionalProperties` recursion is only reachable through a
            # Mapping value. Give the merged value an `enum` so the
            # (unrelated) object-boundedness closedness check for this
            # node doesn't independently deny regardless of payload --
            # this matrix is about structural-coverage recursion, not
            # closedness, and the two proofs are deliberately orthogonal.
            merged = {"enum": ["x"], **payload}
            return {
                "type": "object",
                "properties": {"mode": {"type": "string", "enum": ["a", "b"]}},
                "additionalProperties": merged,
            }
        if keyword == "allOf":
            return {"allOf": [payload]}
        if keyword in ("anyOf", "oneOf"):
            # UNION semantics: pair with one other independently-bounded
            # branch so a benign payload's admit isn't accidental.
            bounded_branch = {
                "type": "object",
                "properties": {"x": {"const": 1}},
                "additionalProperties": False,
            }
            return {keyword: [payload, bounded_branch]}
        raise AssertionError(f"unhandled modeled keyword in test helper: {keyword!r}")

    @classmethod
    def _build_schema(cls, keyword, payload, depth):
        """Build the FULL schema under test for the MODELED-applicator
        matrix. `$ref`/`$defs`/`definitions` need special handling here: a
        JSON Pointer `$ref` always resolves against the schema's DOCUMENT
        ROOT (`_resolve_local_ref` is called with the true root passed to
        `_schema_is_insufficient`, never with whichever local node the
        `$ref` happens to sit inside) -- so their `$defs`/`definitions`
        companion must live at the true root, not nested inside the
        `target` property alongside the `$ref` itself, or resolution fails
        closed regardless of payload. Every other modeled keyword is
        self-contained and uses the generic `properties`-nested wrapper."""
        if keyword in ("$ref", "$defs", "definitions"):
            defs_key = "$defs" if keyword != "definitions" else "definitions"
            schema = cls._root_with_target({"$ref": f"#/{defs_key}/Target"})
            schema[defs_key] = {"Target": cls._nest_property(payload, depth)}
            return schema
        return cls._root_with_target(
            cls._nest_property(cls._build_modeled(keyword, payload), depth)
        )

    @staticmethod
    def _keyword_only_schema(keyword):
        """A minimal schema carrying `keyword` (a DENIED applicator or a
        synthetic unknown keyword) with an arbitrary Mapping value -- the
        keyword's mere presence is what the assertion is about, not its
        content."""
        return {keyword: {"type": "null"}}

    # ---- (1) every MODELED applicator: benign payload admits ----------

    @pytest.mark.parametrize("depth", [1, 2, 3])
    @pytest.mark.parametrize("keyword", sorted(_MODELED_APPLICATOR_KEYWORDS))
    def test_modeled_applicator_benign_payload_admits(self, keyword, depth):
        schema = self._build_schema(keyword, self.BENIGN_LEAF, depth)
        assert not _schema_is_insufficient(schema)

        # Oracle differential: the admitted schema must still reject an
        # injected command-shaped instance buried at the matching depth.
        validator = Draft202012Validator(schema)
        instance = {"operation": "status", "target": self._inject_at_depth(depth)}
        assert not validator.is_valid(instance)

    # ---- (1) every MODELED applicator: denied/unknown payload denies --

    @pytest.mark.parametrize("depth", [1, 2, 3])
    @pytest.mark.parametrize("bad_payload_name", ["denied", "unknown"])
    @pytest.mark.parametrize("keyword", sorted(_MODELED_APPLICATOR_KEYWORDS))
    def test_modeled_applicator_bad_payload_denies(self, keyword, bad_payload_name, depth):
        bad_payload = self.DENIED_LEAF if bad_payload_name == "denied" else self.UNKNOWN_LEAF
        schema = self._build_schema(keyword, bad_payload, depth)
        assert _schema_is_insufficient(schema)

    # ---- (2) every DENIED applicator: denies at every subschema position ---

    @staticmethod
    def _denied_at_property_value(keyword, depth):
        return TestSufficiencyProofKeywordRegistry._root_with_target(
            TestSufficiencyProofKeywordRegistry._nest_property(
                TestSufficiencyProofKeywordRegistry._keyword_only_schema(keyword), depth
            )
        )

    @staticmethod
    def _denied_at_additional_properties(keyword):
        return {
            "type": "object",
            "properties": {"operation": {"const": "status"}},
            "additionalProperties": TestSufficiencyProofKeywordRegistry._keyword_only_schema(
                keyword
            ),
        }

    @staticmethod
    def _denied_at_composition_branch(keyword):
        return {
            "type": "object",
            "allOf": [TestSufficiencyProofKeywordRegistry._keyword_only_schema(keyword)],
        }

    @staticmethod
    def _denied_at_ref_target(keyword):
        return {
            "$ref": "#/$defs/Target",
            "$defs": {"Target": TestSufficiencyProofKeywordRegistry._keyword_only_schema(keyword)},
        }

    @staticmethod
    def _denied_at_defs_entry_unreferenced(keyword):
        return {
            "type": "object",
            "required": ["operation"],
            "additionalProperties": False,
            "properties": {"operation": {"const": "status"}},
            "$defs": {"Unused": TestSufficiencyProofKeywordRegistry._keyword_only_schema(keyword)},
        }

    @pytest.mark.parametrize("depth", [1, 2, 3])
    @pytest.mark.parametrize("keyword", sorted(_DENIED_APPLICATOR_KEYWORDS))
    def test_denied_applicator_denies_at_property_value(self, keyword, depth):
        assert _schema_is_insufficient(self._denied_at_property_value(keyword, depth))

    @pytest.mark.parametrize("keyword", sorted(_DENIED_APPLICATOR_KEYWORDS))
    def test_denied_applicator_denies_at_additional_properties(self, keyword):
        assert _schema_is_insufficient(self._denied_at_additional_properties(keyword))

    @pytest.mark.parametrize("keyword", sorted(_DENIED_APPLICATOR_KEYWORDS))
    def test_denied_applicator_denies_at_composition_branch(self, keyword):
        assert _schema_is_insufficient(self._denied_at_composition_branch(keyword))

    @pytest.mark.parametrize("keyword", sorted(_DENIED_APPLICATOR_KEYWORDS))
    def test_denied_applicator_denies_at_ref_target(self, keyword):
        assert _schema_is_insufficient(self._denied_at_ref_target(keyword))

    @pytest.mark.parametrize("keyword", sorted(_DENIED_APPLICATOR_KEYWORDS))
    def test_denied_applicator_denies_at_unreferenced_defs_entry(self, keyword):
        """`$defs` entries are visited UNCONDITIONALLY, not reachability-
        gated -- an entry never reached by any `$ref` in the document must
        still deny when it carries a denied applicator."""
        assert _schema_is_insufficient(self._denied_at_defs_entry_unreferenced(keyword))

    def test_denied_applicator_property_value_necessity_oracle_differential(self):
        """Documents necessity for a representative denied applicator
        (`patternProperties`, the F1/F4 case already covered by name
        elsewhere): the identical schema validates against a raw
        Draft2020-12 validator and ACCEPTS the injected key, proving the
        schema was never actually closed."""
        schema = self._denied_at_property_value("patternProperties", depth=2)
        assert _schema_is_insufficient(schema)
        # `patternProperties` here has no fixed key pattern matching a real
        # instance key, so the necessity differential uses the sibling
        # keyword form directly to keep the injected instance constructible.
        raw = {
            "type": "object",
            "required": ["operation"],
            "additionalProperties": False,
            "properties": {
                "operation": {"const": "status"},
                "target": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "nested": {
                            "type": "object",
                            "patternProperties": {"^injected$": {"type": "string"}},
                        }
                    },
                },
            },
        }
        validator = Draft202012Validator(raw)
        assert validator.is_valid(
            {"operation": "status", "target": {"nested": {"injected": "rm -rf /"}}}
        )

    # ---- (3) synthetic never-registered keyword at every position -----

    @pytest.mark.parametrize("depth", [1, 2, 3])
    def test_synthetic_unknown_keyword_mapping_value_denies_at_property_value(self, depth):
        schema = self._root_with_target(self._nest_property(self.UNKNOWN_LEAF, depth))
        assert _schema_is_insufficient(schema)

    def test_synthetic_unknown_keyword_mapping_value_denies_at_additional_properties(self):
        schema = {
            "type": "object",
            "properties": {"operation": {"const": "status"}},
            "additionalProperties": {"enum": ["x"], **self.UNKNOWN_LEAF},
        }
        assert _schema_is_insufficient(schema)

    def test_synthetic_unknown_keyword_mapping_value_denies_at_composition_branch(self):
        schema = {"type": "object", "allOf": [self.UNKNOWN_LEAF]}
        assert _schema_is_insufficient(schema)

    def test_synthetic_unknown_keyword_mapping_value_denies_at_ref_target(self):
        schema = {"$ref": "#/$defs/Target", "$defs": {"Target": self.UNKNOWN_LEAF}}
        assert _schema_is_insufficient(schema)

    def test_synthetic_unknown_keyword_mapping_value_denies_at_unreferenced_defs_entry(self):
        schema = {
            "type": "object",
            "required": ["operation"],
            "additionalProperties": False,
            "properties": {"operation": {"const": "status"}},
            "$defs": {"Unused": self.UNKNOWN_LEAF},
        }
        assert _schema_is_insufficient(schema)

    @pytest.mark.parametrize("depth", [1, 2, 3])
    def test_synthetic_unknown_keyword_scalar_value_control_admits_at_property_value(self, depth):
        """Control: the SAME never-registered keyword spelling with a
        provably-inert SCALAR value stays admitted at every depth -- the
        gate keys off VALUE SHAPE, not keyword spelling."""
        node = self._nest_property({"quorumProperties": 3}, depth)
        schema = self._root_with_target(node)
        assert not _schema_is_insufficient(schema)

    # ---- registry completeness/partition proof -------------------------

    def test_registry_partitions_with_no_keyword_in_two_classes(self):
        classes = (
            _INERT_ANNOTATION_KEYWORDS,
            _BOUNDING_KEYWORDS,
            _MODELED_APPLICATOR_KEYWORDS,
            _DENIED_APPLICATOR_KEYWORDS,
        )
        seen: set[str] = set()
        overlaps: set[str] = set()
        for keyword_set in classes:
            overlaps |= seen & keyword_set
            seen |= keyword_set
        assert not overlaps, f"keyword(s) classified in more than one registry class: {overlaps}"

    def test_registry_covers_representative_draft202012_keyword_list(self):
        """A representative Draft 2020-12 core-vocabulary keyword list must
        be fully covered by the union of the four registry classes -- no
        keyword this module is expected to understand falls through to the
        UNKNOWN default silently."""
        representative_keywords = {
            "type",
            "const",
            "enum",
            "required",
            "dependentRequired",
            "multipleOf",
            "maximum",
            "exclusiveMaximum",
            "minimum",
            "exclusiveMinimum",
            "maxLength",
            "minLength",
            "maxItems",
            "minItems",
            "uniqueItems",
            "maxContains",
            "minContains",
            "maxProperties",
            "minProperties",
            "properties",
            "patternProperties",
            "additionalProperties",
            "unevaluatedProperties",
            "unevaluatedItems",
            "propertyNames",
            "dependentSchemas",
            "allOf",
            "anyOf",
            "oneOf",
            "not",
            "if",
            "then",
            "else",
            "items",
            "prefixItems",
            "contains",
            "$ref",
            "$dynamicRef",
            "$recursiveRef",
            "$dynamicAnchor",
            "$recursiveAnchor",
            "$defs",
            "definitions",
            "$schema",
            "$id",
            "$anchor",
            "$vocabulary",
            "$comment",
            "title",
            "description",
            "default",
            "examples",
            "deprecated",
            "readOnly",
            "writeOnly",
            "format",
            "pattern",
            "contentEncoding",
            "contentMediaType",
            "contentSchema",
        }
        covered = (
            _INERT_ANNOTATION_KEYWORDS
            | _BOUNDING_KEYWORDS
            | _MODELED_APPLICATOR_KEYWORDS
            | _DENIED_APPLICATOR_KEYWORDS
        )
        missing = representative_keywords - covered
        assert not missing, f"registry does not classify: {sorted(missing)}"
        for keyword in representative_keywords:
            assert _classify_keyword(keyword) != "unknown"

    def test_classify_keyword_defaults_unknown_for_unregistered_name(self):
        assert _classify_keyword("this-spelling-was-never-registered") == "unknown"

    def test_classify_keyword_exact_class_per_registered_keyword(self):
        """A literal keyword -> expected-class mapping for the full union of
        all four registry sets, checked one keyword at a time against
        `_classify_keyword`. Stronger than the partition/coverage tests
        above: those prove no keyword is double-classified or silently
        unknown, but neither pins any SPECIFIC keyword to a SPECIFIC class --
        a keyword could be moved between the two non-applicator classes
        (inert annotation <-> bounding assertion) without either test
        noticing. This dict is a literal, so any future reclassification is
        a conscious, reviewed edit to this test, never a silent pass-through."""
        expected_class = {
            # inert annotation -- carries no assertion
            "title": "inert",
            "description": "inert",
            "default": "inert",
            "examples": "inert",
            "deprecated": "inert",
            "readOnly": "inert",
            "writeOnly": "inert",
            "$comment": "inert",
            "$schema": "inert",
            "$id": "inert",
            "$anchor": "inert",
            "$vocabulary": "inert",
            "format": "inert",
            "contentEncoding": "inert",
            "contentMediaType": "inert",
            "contentSchema": "inert",
            # bounding -- narrows the admitted set, no recursable subschema
            "type": "bounding",
            "const": "bounding",
            "enum": "bounding",
            "required": "bounding",
            "dependentRequired": "bounding",
            "multipleOf": "bounding",
            "maximum": "bounding",
            "exclusiveMaximum": "bounding",
            "minimum": "bounding",
            "exclusiveMinimum": "bounding",
            "maxLength": "bounding",
            "minLength": "bounding",
            "pattern": "bounding",
            "maxItems": "bounding",
            "minItems": "bounding",
            "uniqueItems": "bounding",
            "maxContains": "bounding",
            "minContains": "bounding",
            "maxProperties": "bounding",
            "minProperties": "bounding",
            # modeled applicator -- recursed into and credited
            "properties": "modeled",
            "additionalProperties": "modeled",
            "allOf": "modeled",
            "anyOf": "modeled",
            "oneOf": "modeled",
            "$ref": "modeled",
            "$defs": "modeled",
            "definitions": "modeled",
            # denied applicator -- presence alone denies
            "patternProperties": "denied",
            "propertyNames": "denied",
            "unevaluatedProperties": "denied",
            "unevaluatedItems": "denied",
            "dependentSchemas": "denied",
            "if": "denied",
            "then": "denied",
            "else": "denied",
            "not": "denied",
            "contains": "denied",
            "items": "denied",
            "prefixItems": "denied",
            "$dynamicRef": "denied",
            "$dynamicAnchor": "denied",
            "$recursiveRef": "denied",
            "$recursiveAnchor": "denied",
        }
        all_registered = (
            _INERT_ANNOTATION_KEYWORDS
            | _BOUNDING_KEYWORDS
            | _MODELED_APPLICATOR_KEYWORDS
            | _DENIED_APPLICATOR_KEYWORDS
        )
        assert set(expected_class) == all_registered, (
            "expected-class mapping in this test is out of sync with the "
            "registry's own union -- update the mapping alongside any "
            "registry change"
        )
        for keyword, expected in expected_class.items():
            actual = _classify_keyword(keyword)
            assert actual == expected, (
                f"{keyword!r} classified as {actual!r}, expected {expected!r}"
            )
