"""Tests for lionagi.service.connections.mcp.wrapper module."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest

from lionagi.service.connections.mcp_wrapper import (
    MCPConnectionPool,
    MCPSecurityConfig,
    create_mcp_tool,
)


class TestMCPConnectionPoolContextManager:
    """Test MCPConnectionPool context manager."""

    @pytest.mark.asyncio
    async def test_aenter_returns_self(self):
        """Test __aenter__ returns self."""
        pool = MCPConnectionPool()
        result = await pool.__aenter__()
        assert result is pool

    @pytest.mark.asyncio
    async def test_aexit_calls_cleanup(self):
        """Test __aexit__ calls cleanup."""
        pool = MCPConnectionPool()

        with patch.object(MCPConnectionPool, "cleanup", new_callable=AsyncMock) as mock_cleanup:
            await pool.__aexit__(None, None, None)
            mock_cleanup.assert_called_once()


class TestMCPConnectionPoolLoadConfig:
    """Test MCPConnectionPool.load_config method."""

    def test_load_config_file_not_found(self):
        """Test raises FileNotFoundError if config file doesn't exist."""
        with pytest.raises(FileNotFoundError, match="MCP config file not found"):
            MCPConnectionPool.load_config("nonexistent.json")

    def test_load_config_invalid_json(self):
        """Test raises JSONDecodeError for invalid JSON."""
        invalid_json = "{invalid json"

        with patch("builtins.open", mock_open(read_data=invalid_json)):
            with patch.object(Path, "exists", return_value=True):
                with pytest.raises(json.JSONDecodeError, match="Invalid JSON"):
                    MCPConnectionPool.load_config(".mcp.json")

    def test_load_config_not_dict(self):
        """Test raises ValueError if config is not a dict."""
        json_data = "[]"  # Array instead of object

        with patch("builtins.open", mock_open(read_data=json_data)):
            with patch.object(Path, "exists", return_value=True):
                with pytest.raises(ValueError, match="MCP config must be a JSON object"):
                    MCPConnectionPool.load_config(".mcp.json")

    def test_load_config_mcpservers_not_dict(self):
        """Test raises ValueError if mcpServers is not a dict."""
        json_data = json.dumps({"mcpServers": []})

        with patch("builtins.open", mock_open(read_data=json_data)):
            with patch.object(Path, "exists", return_value=True):
                with pytest.raises(ValueError, match="mcpServers must be a dictionary"):
                    MCPConnectionPool.load_config(".mcp.json")

    def test_load_config_success(self):
        """Test successfully loads config."""
        MCPConnectionPool._configs = {}  # Reset configs

        config_data = {"mcpServers": {"test_server": {"command": "python", "args": ["server.py"]}}}
        json_data = json.dumps(config_data)

        with patch("builtins.open", mock_open(read_data=json_data)):
            with patch.object(Path, "exists", return_value=True):
                MCPConnectionPool.load_config(".mcp.json")

        assert "test_server" in MCPConnectionPool._configs
        assert MCPConnectionPool._configs["test_server"]["command"] == "python"


class TestMCPConnectionPoolGetClient:
    """Test MCPConnectionPool.get_client method."""

    @pytest.fixture(autouse=True)
    def reset_pool(self):
        """Reset pool state before each test."""
        MCPConnectionPool._clients = {}
        MCPConnectionPool._configs = {}
        yield
        MCPConnectionPool._clients = {}
        MCPConnectionPool._configs = {}

    @pytest.mark.asyncio
    async def test_get_client_with_server_reference_not_found(self):
        """Test raises ValueError for unknown server reference."""
        server_config = {"server": "unknown_server"}

        with patch.object(MCPConnectionPool, "load_config") as mock_load:
            mock_load.return_value = None  # Config still empty after load

            with pytest.raises(ValueError, match="Unknown MCP server"):
                await MCPConnectionPool.get_client(server_config)

    @pytest.mark.asyncio
    async def test_get_client_with_server_reference_success(self):
        """Test successfully gets client with server reference."""
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
        """Test successfully gets client with inline config."""
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
        """Test reuses existing connected client from pool."""
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
        """Test removes stale disconnected client and creates new one."""
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
    """Test MCPConnectionPool._create_client method."""

    @pytest.fixture(autouse=True)
    def reset_pool_security(self):
        """Reset pool security config before each test."""
        original = MCPConnectionPool._security
        yield
        MCPConnectionPool._security = original

    @pytest.mark.asyncio
    async def test_create_client_invalid_config_type(self):
        """Test raises ValueError for non-dict config."""
        with pytest.raises(ValueError, match="Config must be a dictionary"):
            await MCPConnectionPool._create_client("not a dict")

    @pytest.mark.asyncio
    async def test_create_client_missing_url_and_command(self):
        """Test raises ValueError if neither url nor command provided."""
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
        """Test raises ImportError if fastmcp not installed."""
        # Need allow_urls to get past the security gate to the import check
        MCPConnectionPool._security = MCPSecurityConfig(allow_urls=True)
        config = {"url": "https://api.example.com/mcp"}

        with patch("builtins.__import__", side_effect=ImportError):
            with pytest.raises((ImportError, Exception)):
                await MCPConnectionPool._create_client(config)

    @pytest.mark.asyncio
    async def test_create_client_with_url(self):
        """Test creates client with URL config when allow_urls=True."""
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
        """Test creates client with command config when allow_commands=True."""
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
        """Test raises ValueError if args is not a list."""
        MCPConnectionPool._security = MCPSecurityConfig(allow_commands=True)
        config = {"command": "python", "args": "not_a_list"}  # Invalid

        with patch("fastmcp.Client"):
            with pytest.raises(ValueError, match="Config 'args' must be a list"):
                await MCPConnectionPool._create_client(config)

    @pytest.mark.asyncio
    async def test_create_client_command_debug_mode(self):
        """Test debug mode doesn't suppress logging."""
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
    """Test MCPConnectionPool.cleanup method."""

    @pytest.fixture(autouse=True)
    def reset_pool(self):
        """Reset pool state."""
        MCPConnectionPool._clients = {}
        yield
        MCPConnectionPool._clients = {}

    @pytest.mark.asyncio
    async def test_cleanup_empty_pool(self):
        """Test cleanup with no clients."""
        await MCPConnectionPool.cleanup()
        assert len(MCPConnectionPool._clients) == 0

    @pytest.mark.asyncio
    async def test_cleanup_multiple_clients(self):
        """Test cleanup calls __aexit__ on all clients."""
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
        """Test cleanup continues even if one client raises error."""
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
    """Test create_mcp_tool function."""

    @pytest.mark.asyncio
    async def test_create_mcp_tool_basic(self):
        """Test creates callable MCP tool."""
        mcp_config = {"url": "http://localhost:8080"}
        tool_name = "test_tool"

        tool = create_mcp_tool(mcp_config, tool_name)

        assert callable(tool)
        assert tool.__name__ == tool_name
        assert "MCP tool" in tool.__doc__

    @pytest.mark.asyncio
    async def test_mcp_tool_execution(self):
        """Test MCP tool execution calls client.call_tool."""
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
        """Test MCP tool uses _original_tool_name if present."""
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
        """Test MCP tool handles dict result with text type."""
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
        """Test MCP tool returns result as-is if no text extraction."""
        mcp_config = {"url": "http://localhost:8080"}
        tool_name = "test_tool"

        mock_client = AsyncMock()
        mock_result = {"custom": "data"}
        mock_client.call_tool.return_value = mock_result

        with patch.object(MCPConnectionPool, "get_client", return_value=mock_client):
            tool = create_mcp_tool(mcp_config, tool_name)
            result = await tool()

            assert result == {"custom": "data"}


# ---------------------------------------------------------------------------
# Edge cases: max_connections_per_server, concurrent get_client race,
# tool timeout, large arguments, server error
# ---------------------------------------------------------------------------


class TestMCPConnectionPoolEdgeCases:
    @pytest.fixture(autouse=True)
    def reset_pool(self):
        MCPConnectionPool._clients = {}
        MCPConnectionPool._configs = {}
        MCPConnectionPool._security = None
        MCPConnectionPool._server_security = {}
        yield
        MCPConnectionPool._clients = {}
        MCPConnectionPool._configs = {}
        MCPConnectionPool._security = None
        MCPConnectionPool._server_security = {}

    def test_max_connections_per_server_default(self):
        config = MCPSecurityConfig()
        assert config.max_connections_per_server == 5

    def test_max_connections_per_server_custom(self):
        config = MCPSecurityConfig(max_connections_per_server=10)
        assert config.max_connections_per_server == 10

    @pytest.mark.asyncio
    async def test_concurrent_get_client_for_same_server_creates_one_client(self):
        MCPConnectionPool._configs = {"shared_server": {"command": "python", "args": ["s.py"]}}
        create_count = [0]
        lock = asyncio.Lock()

        async def tracked_create(config, security=None):
            async with lock:
                create_count[0] += 1
                mock_client = MagicMock()
                mock_client.is_connected.return_value = True
                return mock_client

        with patch.object(MCPConnectionPool, "_create_client", side_effect=tracked_create):
            results = await asyncio.gather(
                *[MCPConnectionPool.get_client({"server": "shared_server"}) for _ in range(5)]
            )

        assert create_count[0] == 1
        assert all(r is results[0] for r in results)

    @pytest.mark.asyncio
    async def test_mcp_tool_with_large_arguments(self):
        mcp_config = {"url": "https://api.example.com/mcp"}
        tool_name = "process_text"

        large_text = "x" * 100000
        mock_client = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = [MagicMock(text="done")]
        mock_client.call_tool.return_value = mock_result

        with patch.object(MCPConnectionPool, "get_client", return_value=mock_client):
            tool = create_mcp_tool(mcp_config, tool_name)
            result = await tool(text=large_text)

        mock_client.call_tool.assert_called_once_with(tool_name, {"text": large_text})
        assert result == "done"

    @pytest.mark.asyncio
    async def test_mcp_tool_server_returns_error_propagates(self):
        mcp_config = {"url": "https://api.example.com/mcp"}
        tool_name = "failing_tool"

        mock_client = AsyncMock()
        mock_client.call_tool.side_effect = RuntimeError("server error: tool not found")

        with patch.object(MCPConnectionPool, "get_client", return_value=mock_client):
            tool = create_mcp_tool(mcp_config, tool_name)
            with pytest.raises(RuntimeError, match="server error"):
                await tool(arg="value")

    @pytest.mark.asyncio
    async def test_mcp_tool_timeout_propagates(self):
        mcp_config = {"url": "https://api.example.com/mcp"}
        tool_name = "slow_tool"

        mock_client = AsyncMock()
        mock_client.call_tool.side_effect = asyncio.TimeoutError()

        with patch.object(MCPConnectionPool, "get_client", return_value=mock_client):
            tool = create_mcp_tool(mcp_config, tool_name)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(tool(), timeout=0.1)

    @pytest.mark.asyncio
    async def test_mcp_tool_with_large_result(self):
        mcp_config = {"url": "https://api.example.com/mcp"}
        tool_name = "large_result_tool"

        large_response = "y" * 500000
        mock_client = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = [MagicMock(text=large_response)]
        mock_client.call_tool.return_value = mock_result

        with patch.object(MCPConnectionPool, "get_client", return_value=mock_client):
            tool = create_mcp_tool(mcp_config, tool_name)
            result = await tool()

        assert result == large_response
        assert len(result) == 500000
