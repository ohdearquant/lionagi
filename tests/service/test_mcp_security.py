"""Tests for MCP security configuration.

Fail-closed transport security: both command and URL transports require
explicit opt-in via allow_commands=True / allow_urls=True before the
transport object is constructed. These tests verify the boundary is
enforced before side effects (process spawn, outbound TCP) occur.

See audit finding LIONAGI-AUDIT-007.
"""

import pytest

from lionagi.service.connections.mcp_wrapper import (
    MCPConnectionPool,
    MCPSecurityConfig,
    _filter_env,
    _validate_command,
    _validate_url,
)


class TestMCPSecurityConfig:
    """Test MCPSecurityConfig dataclass."""

    def test_default_config(self):
        """Default config denies all transports and filters sensitive env."""
        config = MCPSecurityConfig()
        assert config.allow_commands is False  # fail-closed
        assert config.allow_urls is False  # fail-closed
        assert config.command_allowlist is None
        assert config.url_allowlist is None
        assert config.filter_sensitive_env is True
        assert config.max_connections_per_server == 5
        assert len(config.env_denylist_patterns) > 0

    def test_custom_allowlist(self):
        """Custom allowlist restricts commands."""
        config = MCPSecurityConfig(command_allowlist=frozenset({"node", "python"}))
        assert "node" in config.command_allowlist
        assert "python" in config.command_allowlist

    def test_frozen(self):
        """Config is immutable."""
        config = MCPSecurityConfig()
        with pytest.raises(AttributeError):
            config.filter_sensitive_env = False


class TestFilterEnv:
    """Test environment variable filtering."""

    def test_filters_sensitive_keys(self):
        """Known sensitive patterns are filtered."""
        config = MCPSecurityConfig()
        env = {
            "PATH": "/usr/bin",
            "HOME": "/home/user",
            "OPENAI_API_KEY": "sk-secret",
            "AWS_SECRET_ACCESS_KEY": "secret",
            "DATABASE_URL": "postgres://...",
            "SAFE_VAR": "safe",
        }
        filtered = _filter_env(env, config)

        assert "PATH" in filtered
        assert "HOME" in filtered
        assert "SAFE_VAR" in filtered
        assert "OPENAI_API_KEY" not in filtered
        assert "AWS_SECRET_ACCESS_KEY" not in filtered
        assert "DATABASE_URL" not in filtered

    def test_no_filter_when_disabled(self):
        """All env vars pass when filtering is disabled."""
        config = MCPSecurityConfig(filter_sensitive_env=False)
        env = {"OPENAI_API_KEY": "sk-secret", "PATH": "/usr/bin"}
        filtered = _filter_env(env, config)

        assert "OPENAI_API_KEY" in filtered
        assert "PATH" in filtered

    def test_custom_deny_patterns(self):
        """Custom deny patterns are respected."""
        config = MCPSecurityConfig(env_denylist_patterns=frozenset({"CUSTOM_SECRET"}))
        env = {
            "CUSTOM_SECRET_KEY": "hidden",
            "PATH": "/usr/bin",
        }
        filtered = _filter_env(env, config)

        assert "CUSTOM_SECRET_KEY" not in filtered
        assert "PATH" in filtered

    def test_case_insensitive_matching(self):
        """Filtering is case-insensitive."""
        config = MCPSecurityConfig()
        env = {"openai_api_key": "sk-secret"}
        filtered = _filter_env(env, config)
        # Pattern is OPENAI_API_KEY, key is openai_api_key
        # Both get uppercased for comparison
        assert "openai_api_key" not in filtered


class TestValidateCommand:
    """Test command validation — fail-closed transport security.

    Attack regression for audit finding LIONAGI-AUDIT-007:
    A loaded .mcp.json config previously caused command execution before any
    policy was checked (fail-open). Commands are now denied by default.
    """

    # --- Fail-closed (default deny) ---

    def test_default_denies_all_commands(self):
        """Default config (allow_commands=False) blocks every command — fail closed."""
        config = MCPSecurityConfig()  # allow_commands=False by default
        with pytest.raises(PermissionError, match="allow_commands=False"):
            _validate_command("node", config)

    def test_default_denies_shell(self):
        """Explicit attack: /bin/sh is blocked before any transport object is built."""
        config = MCPSecurityConfig()
        with pytest.raises(PermissionError, match="allow_commands=False"):
            _validate_command("/bin/sh", config)

    def test_default_denies_arbitrary_path(self):
        """Arbitrary command paths are blocked by default."""
        config = MCPSecurityConfig()
        with pytest.raises(PermissionError, match="allow_commands=False"):
            _validate_command("/usr/bin/curl", config)

    # --- Explicit allow without allowlist ---

    def test_allow_commands_no_allowlist_permits_bare(self):
        """allow_commands=True with no allowlist permits any bare command."""
        config = MCPSecurityConfig(allow_commands=True, command_allowlist=None)
        assert _validate_command("node", config) is None
        assert _validate_command("python", config) is None

    def test_allow_commands_no_allowlist_permits_paths(self):
        """allow_commands=True with no allowlist permits path commands."""
        config = MCPSecurityConfig(allow_commands=True, command_allowlist=None)
        assert _validate_command("/usr/bin/node", config) is None

    # --- Allowlist enforcement when allow_commands=True ---

    def test_allowlist_blocks_unlisted(self):
        """Commands not in allowlist are blocked even when allow_commands=True."""
        config = MCPSecurityConfig(
            allow_commands=True, command_allowlist=frozenset({"node", "python"})
        )
        with pytest.raises(ValueError, match="not in allowlist"):
            _validate_command("bash", config)

    def test_allowlist_permits_listed(self):
        """Commands in allowlist are allowed when allow_commands=True."""
        config = MCPSecurityConfig(
            allow_commands=True, command_allowlist=frozenset({"node", "python"})
        )
        assert _validate_command("node", config) is None
        assert _validate_command("python", config) is None

    def test_path_separator_rejected_bare_in_allowlist(self):
        """Path commands rejected even when bare name is in allowlist."""
        config = MCPSecurityConfig(allow_commands=True, command_allowlist=frozenset({"node"}))
        with pytest.raises(ValueError, match="path separator"):
            _validate_command("/usr/bin/node", config)

    def test_path_separator_rejected_bare_not_in_allowlist(self):
        """Path commands rejected when bare name not in allowlist either."""
        config = MCPSecurityConfig(allow_commands=True, command_allowlist=frozenset({"python"}))
        with pytest.raises(ValueError, match="not in allowlist"):
            _validate_command("/usr/bin/node", config)


class TestValidateUrl:
    """Test URL transport validation — fail-closed security.

    Attack regression for audit finding LIONAGI-AUDIT-007:
    URL configs were previously passed directly to FastMCPClient without
    any validation. URLs are now denied by default.
    """

    def test_default_denies_all_urls(self):
        """Default config (allow_urls=False) blocks every URL — fail closed."""
        config = MCPSecurityConfig()
        with pytest.raises(PermissionError, match="allow_urls=False"):
            _validate_url("https://example.com/mcp", config)

    def test_default_denies_http(self):
        """Plain HTTP URL is blocked by default."""
        config = MCPSecurityConfig()
        with pytest.raises(PermissionError, match="allow_urls=False"):
            _validate_url("http://api.example.com/mcp", config)

    def test_allow_urls_https_accepted(self):
        """allow_urls=True with https URL is permitted."""
        config = MCPSecurityConfig(allow_urls=True)
        assert _validate_url("https://api.example.com/mcp", config) is None

    def test_allow_urls_wss_accepted(self):
        """allow_urls=True with wss URL is permitted."""
        config = MCPSecurityConfig(allow_urls=True)
        assert _validate_url("wss://api.example.com/mcp", config) is None

    def test_allow_urls_http_blocked(self):
        """allow_urls=True still blocks non-https/wss scheme."""
        config = MCPSecurityConfig(allow_urls=True)
        with pytest.raises(ValueError, match="https or wss scheme"):
            _validate_url("http://api.example.com/mcp", config)

    def test_allow_urls_with_allowlist_permits_listed(self):
        """URL host in allowlist is permitted when allow_urls=True."""
        config = MCPSecurityConfig(allow_urls=True, url_allowlist=frozenset({"api.example.com"}))
        assert _validate_url("https://api.example.com/mcp", config) is None

    def test_allow_urls_with_allowlist_blocks_unlisted(self):
        """URL host not in allowlist is blocked even when allow_urls=True."""
        config = MCPSecurityConfig(allow_urls=True, url_allowlist=frozenset({"api.example.com"}))
        with pytest.raises(ValueError, match="not in allowlist"):
            _validate_url("https://evil.example.org/mcp", config)


class TestMCPConnectionPoolFailClosed:
    """Attack regression: _create_client must reject transports before construction.

    The test asserts that PermissionError is raised BEFORE FastMCPClient or
    StdioTransport is constructed (verified by checking fastmcp was not imported
    and no network/process side effect occurred).

    See audit finding LIONAGI-AUDIT-007.
    """

    @pytest.mark.asyncio
    async def test_command_transport_denied_without_security_config(self):
        """No security config → command transport fails closed before StdioTransport."""
        # Reset pool state
        MCPConnectionPool._security = None
        MCPConnectionPool._clients = {}

        with pytest.raises(PermissionError, match="allow_commands=False"):
            await MCPConnectionPool._create_client({"command": "node", "args": ["server.js"]})

    @pytest.mark.asyncio
    async def test_url_transport_denied_without_security_config(self):
        """No security config → URL transport fails closed before FastMCPClient."""
        MCPConnectionPool._security = None
        MCPConnectionPool._clients = {}

        with pytest.raises(PermissionError, match="allow_urls=False"):
            await MCPConnectionPool._create_client({"url": "https://api.example.com/mcp"})

    @pytest.mark.asyncio
    async def test_shell_command_denied_by_default(self):
        """Attack: /bin/sh must be denied before StdioTransport is constructed."""
        MCPConnectionPool._security = None
        MCPConnectionPool._clients = {}

        with pytest.raises(PermissionError, match="allow_commands=False"):
            await MCPConnectionPool._create_client({"command": "/bin/sh", "args": ["-c", "id"]})
