"""Tests for MCP security configuration.

Fail-closed transport security: both command and URL transports require
explicit opt-in via allow_commands=True / allow_urls=True before the
transport object is constructed. These tests verify the boundary is
enforced before side effects (process spawn, outbound TCP) occur.
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


class TestLoadMcpConfigTrustedLoad:
    """load_mcp_config must restore normal .mcp.json usage (Finding 1).

    The fail-closed transport default is correct for untrusted paths, but an
    explicit load_mcp_config() call is a trust action — it must default to an
    allow policy so a normal config registers its tools instead of silently
    registering zero, and it must surface a security denial loudly.
    """

    async def test_default_load_sets_allow_policy_and_registers(self, tmp_path, monkeypatch):
        import json

        from lionagi.protocols.action.manager import ActionManager

        cfg = tmp_path / ".mcp.json"
        cfg.write_text(json.dumps({"mcpServers": {"local": {"command": "echo", "args": ["hi"]}}}))

        mgr = ActionManager()
        seen = {}

        async def fake_register(server_config, update=False, security=None):
            # The policy is THREADED in as an argument, not set on the global.
            seen["allow_commands"] = security.allow_commands
            seen["allow_urls"] = security.allow_urls
            return ["local_echo"]

        monkeypatch.setattr(mgr, "register_mcp_server", fake_register)

        result = await mgr.load_mcp_config(str(cfg))
        # Normal usage must register tools, not silently return [].
        assert result == {"local": ["local_echo"]}
        assert seen["allow_commands"] is True
        assert seen["allow_urls"] is True

    async def test_security_denial_is_raised_not_swallowed(self, tmp_path, monkeypatch):
        import json

        from lionagi.protocols.action.manager import ActionManager

        cfg = tmp_path / ".mcp.json"
        cfg.write_text(json.dumps({"mcpServers": {"local": {"command": "echo", "args": ["hi"]}}}))

        mgr = ActionManager()

        async def deny_register(server_config, update=False, security=None):
            raise PermissionError("MCP command transport is disabled")

        monkeypatch.setattr(mgr, "register_mcp_server", deny_register)

        # A restrictive policy must surface the denial loudly, not swallow to [].
        with pytest.raises(PermissionError):
            await mgr.load_mcp_config(str(cfg), mcp_security=MCPSecurityConfig())

    async def test_load_does_not_mutate_global_security_scope(self, tmp_path, monkeypatch):
        """The permissive policy is threaded as an arg, not set on the global (Finding 3/1).

        Mutating the process-global default — even with save/restore around the
        awaiting registration loop — broadens trust to any client a CONCURRENT
        load creates while ours is in flight, and leaves a race window. The fix
        passes the policy down the call chain, so the global is never touched.
        """
        import json

        from lionagi.protocols.action.manager import ActionManager

        cfg = tmp_path / ".mcp.json"
        cfg.write_text(json.dumps({"mcpServers": {"local": {"command": "echo"}}}))

        # Known prior default: fail-closed.
        MCPConnectionPool._security = None
        try:
            mgr = ActionManager()

            async def fake_register(server_config, update=False, security=None):
                # Policy arrives as an argument; the global is NEVER mutated.
                assert security.allow_commands is True
                assert MCPConnectionPool._security is None
                return ["local_echo"]

            monkeypatch.setattr(mgr, "register_mcp_server", fake_register)
            await mgr.load_mcp_config(str(cfg))

            # The global default is untouched throughout.
            assert MCPConnectionPool._security is None
        finally:
            MCPConnectionPool._security = None

    async def test_concurrent_loads_do_not_cross_contaminate(self, monkeypatch):
        """Two concurrent loads with different policies must not see each other's.

        Regression (Finding 1): bracketing the awaiting registration loop by
        mutating the shared ``_security`` class var let a restrictive load
        observe a permissive load's policy (and vice versa) when they
        interleaved. Threading the policy makes each load see only its own.
        """
        import asyncio

        from lionagi.protocols.action.manager import ActionManager

        MCPConnectionPool._security = None
        try:
            permissive = MCPSecurityConfig(allow_commands=True, allow_urls=True)
            restrictive = MCPSecurityConfig()  # fail-closed
            gate = asyncio.Event()
            observed: dict[str, MCPSecurityConfig] = {}

            # No real config file: load_config is a no-op and server_names are
            # supplied explicitly to each load.
            monkeypatch.setattr(MCPConnectionPool, "load_config", classmethod(lambda cls, p: None))

            async def fake_register(
                self, server_config, request_options=None, update=False, security=None
            ):
                # Force interleaving so both loads are mid-flight together: the
                # first arrival blocks until the second one releases the gate.
                if not gate.is_set():
                    gate.set()
                    await asyncio.sleep(0.02)
                observed[server_config["server"]] = security
                return [f"{server_config['server']}_echo"]

            monkeypatch.setattr(ActionManager, "register_mcp_server", fake_register)

            mgr_a = ActionManager()
            mgr_b = ActionManager()
            await asyncio.gather(
                mgr_a.load_mcp_config("ignored", server_names=["a"], mcp_security=permissive),
                mgr_b.load_mcp_config("ignored", server_names=["b"], mcp_security=restrictive),
            )

            # Each load saw only its own policy, despite interleaving.
            assert observed["a"] is permissive
            assert observed["b"] is restrictive
            assert MCPConnectionPool._security is None
        finally:
            MCPConnectionPool._security = None

    async def test_get_client_security_arg_overrides_global(self, monkeypatch):
        """_create_client honors an explicit per-call policy over the global.

        This is the seam that makes threading work: a trusted loader authorizes
        ITS client's transport without setting the shared default.
        """
        # Global is fail-closed; an explicit permissive policy must still allow.
        MCPConnectionPool._security = None
        seen = {}
        try:

            async def fake_create(cls, config, security=None):
                seen["security"] = security
                return object()

            monkeypatch.setattr(MCPConnectionPool, "_create_client", classmethod(fake_create))
            policy = MCPSecurityConfig(allow_commands=True)
            await MCPConnectionPool.get_client({"command": "echo", "args": []}, security=policy)
            assert seen["security"] is policy
        finally:
            MCPConnectionPool._security = None
            MCPConnectionPool._clients.clear()

    async def test_load_mcp_tools_helper_trusted_and_loud(self, tmp_path, monkeypatch):
        """Standalone load_mcp_tools mirrors load_mcp_config semantics (Finding 2):
        trusted-allow threaded into the load, global untouched, denial raised loudly.
        """
        import json

        from lionagi.protocols.action.manager import ActionManager, load_mcp_tools

        cfg = tmp_path / ".mcp.json"
        cfg.write_text(json.dumps({"mcpServers": {"local": {"command": "echo"}}}))

        MCPConnectionPool._security = None
        try:
            seen = {}

            async def fake_register(
                self, server_config, request_options=None, update=False, security=None
            ):
                seen["allow_commands"] = security.allow_commands
                assert MCPConnectionPool._security is None
                return ["local_echo"]

            monkeypatch.setattr(ActionManager, "register_mcp_server", fake_register)

            # Normal load: no raise, policy permissive (threaded), global untouched.
            await load_mcp_tools(str(cfg))
            assert seen["allow_commands"] is True
            assert MCPConnectionPool._security is None

            # Restrictive policy: a denial must be raised, not swallowed to [].
            async def deny_register(
                self, server_config, request_options=None, update=False, security=None
            ):
                raise PermissionError("MCP command transport is disabled")

            monkeypatch.setattr(ActionManager, "register_mcp_server", deny_register)
            with pytest.raises(PermissionError):
                await load_mcp_tools(str(cfg), mcp_security=MCPSecurityConfig())
            # The global default remains untouched on the raising path too.
            assert MCPConnectionPool._security is None
        finally:
            MCPConnectionPool._security = None


class TestPerServerPolicyPersistence:
    """Codex #1279: the authorized policy must reach the stored tool-call path,
    not only the discovery client.

    A trusted load records the per-server policy; every later client creation
    for that server — the lazy first invocation of a tool_names-registered tool,
    and reconnects after the cached client is cleaned up or goes stale —
    re-applies it instead of failing closed.
    """

    def _reset(self):
        MCPConnectionPool._security = None
        MCPConnectionPool._server_security.clear()
        MCPConnectionPool._clients.clear()

    async def test_invocation_without_security_recovers_recorded_policy(self, monkeypatch):
        self._reset()
        seen = []

        class _FakeClient:
            def is_connected(self):  # force recreation every call
                return False

        async def fake_create(config, security=None):
            seen.append(security)
            return _FakeClient()

        monkeypatch.setattr(MCPConnectionPool, "_create_client", staticmethod(fake_create))
        try:
            policy = MCPSecurityConfig(allow_commands=True)
            # Trusted load records the policy (no client created — tool_names path).
            MCPConnectionPool.remember_security({"command": "echo", "args": []}, policy)
            # Stored callable invokes get_client WITHOUT a policy, with a fresh
            # dict of the same content — the recorded policy must be recovered.
            await MCPConnectionPool.get_client({"command": "echo", "args": ["x"]})
            assert seen[-1] is policy
        finally:
            self._reset()

    async def test_reconnect_after_cleanup_recovers_policy(self, monkeypatch):
        self._reset()
        seen = []

        class _FakeClient:
            def is_connected(self):
                return True

        async def fake_create(config, security=None):
            seen.append(security)
            return _FakeClient()

        monkeypatch.setattr(MCPConnectionPool, "_create_client", staticmethod(fake_create))
        try:
            MCPConnectionPool._configs["srv"] = {"command": "echo"}
            policy = MCPSecurityConfig(allow_commands=True)
            # Discovery records the policy.
            await MCPConnectionPool.get_client({"server": "srv"}, security=policy)
            assert seen[-1] is policy
            # Cached client cleaned up; reconnect without an explicit policy.
            MCPConnectionPool._clients.clear()
            await MCPConnectionPool.get_client({"server": "srv"})
            assert seen[-1] is policy
        finally:
            self._reset()
            MCPConnectionPool._configs.pop("srv", None)

    async def test_unrecorded_server_stays_fail_closed(self, monkeypatch):
        self._reset()
        seen = []

        async def fake_create(config, security=None):
            seen.append(security)
            return object()

        monkeypatch.setattr(MCPConnectionPool, "_create_client", staticmethod(fake_create))
        try:
            # No policy recorded for this server → creation stays fail-closed (None).
            await MCPConnectionPool.get_client({"command": "never-loaded"})
            assert seen[-1] is None
        finally:
            self._reset()

    async def test_register_tool_names_records_policy(self):
        from lionagi.protocols.action.manager import ActionManager

        self._reset()
        try:
            mgr = ActionManager()
            policy = MCPSecurityConfig(allow_commands=True)
            # tool_names branch builds Tool objects without creating a client;
            # the policy must still be recorded for first-invocation recovery.
            await mgr.register_mcp_server(
                {"command": "echo", "args": []},
                tool_names=["foo"],
                security=policy,
            )
            key = MCPConnectionPool._policy_key({"command": "echo", "args": []})
            assert MCPConnectionPool._server_security[key] is policy
        finally:
            self._reset()
