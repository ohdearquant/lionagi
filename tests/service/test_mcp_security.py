"""Tests for MCP security: fail-closed transport security requiring explicit opt-in before transport construction."""

import pytest

from lionagi.service.connections.mcp_wrapper import (
    MCPConnectionPool,
    MCPSecurityConfig,
    _filter_env,
    _validate_command,
    _validate_url,
)


class TestMCPSecurityConfig:
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

    def test_trusted_preset_allows_command_and_url_transports(self):
        """MCPSecurityConfig.trusted() is the named, observable transport-
        trust decision (ADR-0011 delta row 3) -- a caller reaches for it
        deliberately; it is not the default."""
        config = MCPSecurityConfig.trusted()
        assert config.allow_commands is True
        assert config.allow_urls is True
        # Everything else keeps the fail-closed field defaults.
        assert config == MCPSecurityConfig(allow_commands=True, allow_urls=True)
        # The plain default constructor is unaffected by the preset existing.
        assert MCPSecurityConfig().allow_commands is False
        assert MCPSecurityConfig().allow_urls is False


class TestFilterEnv:
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
    """Test command validation: .mcp.json configs previously caused execution before policy checks; commands now denied by default."""

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
    """Test URL transport validation: configs previously passed to FastMCPClient without validation; URLs now denied by default."""

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
    """load_mcp_config's omitted policy must preserve the fail-closed default
    (ADR-0011 delta row 3) while an explicit trust decision still threads
    through and denials still surface loudly, never swallowed to []."""

    async def test_default_load_registers_only_the_loaded_files_servers(
        self, tmp_path, monkeypatch
    ):
        """server_names=None means the servers declared in THE FILE being
        loaded — never every config accumulated in the process-global pool.

        The pool retains configs across loads, so defaulting to its keys
        would silently re-register servers from previously loaded, unrelated
        configs (e.g. a home-level config loaded by an earlier agent in the
        same process) into this manager.
        """
        import json

        from lionagi.protocols.action.manager import ActionManager
        from lionagi.service.connections.mcp_wrapper import MCPConnectionPool

        # Simulate an unrelated, earlier config load in the same process.
        monkeypatch.setitem(MCPConnectionPool._configs, "earlier-server", {"command": "x"})

        cfg = tmp_path / ".mcp.json"
        cfg.write_text(json.dumps({"mcpServers": {"local": {"command": "echo", "args": ["hi"]}}}))

        mgr = ActionManager()

        async def fake_register(server_config, update=False, security=None):
            return ["local_echo"]

        monkeypatch.setattr(mgr, "register_mcp_server", fake_register)

        result = await mgr.load_mcp_config(str(cfg))
        assert result == {"local": ["local_echo"]}
        assert "earlier-server" not in result

    async def test_default_load_leaves_policy_unset(self, tmp_path, monkeypatch):
        """Omitting mcp_security no longer manufactures a permissive policy --
        it stays None and is threaded through unchanged, so a fail-closed
        downstream default (MCPConnectionPool.get_client/_create_client)
        applies unless the caller opts in via MCPSecurityConfig.trusted()."""
        import json

        from lionagi.protocols.action.manager import ActionManager

        cfg = tmp_path / ".mcp.json"
        cfg.write_text(json.dumps({"mcpServers": {"local": {"command": "echo", "args": ["hi"]}}}))

        mgr = ActionManager()
        seen = {}

        async def fake_register(server_config, update=False, security=None):
            seen["security"] = security
            return ["local_echo"]

        monkeypatch.setattr(mgr, "register_mcp_server", fake_register)

        result = await mgr.load_mcp_config(str(cfg))
        # Normal usage must still register tools, not silently return [].
        assert result == {"local": ["local_echo"]}
        assert seen["security"] is None

    async def test_explicit_trusted_preset_threads_through(self, tmp_path, monkeypatch):
        """The named, observable trusted mode (MCPSecurityConfig.trusted())
        allows command/URL transports when a caller opts in explicitly."""
        import json

        from lionagi.protocols.action.manager import ActionManager

        cfg = tmp_path / ".mcp.json"
        cfg.write_text(json.dumps({"mcpServers": {"local": {"command": "echo", "args": ["hi"]}}}))

        mgr = ActionManager()
        seen = {}

        async def fake_register(server_config, update=False, security=None):
            seen["allow_commands"] = security.allow_commands
            seen["allow_urls"] = security.allow_urls
            return ["local_echo"]

        monkeypatch.setattr(mgr, "register_mcp_server", fake_register)

        result = await mgr.load_mcp_config(str(cfg), mcp_security=MCPSecurityConfig.trusted())
        assert result == {"local": ["local_echo"]}
        assert seen["allow_commands"] is True
        assert seen["allow_urls"] is True

    async def test_default_load_denies_command_transport_without_explicit_trust(self, tmp_path):
        """End-to-end (no mocking of register_mcp_server): a command-based
        server with no mcp_security passed must raise PermissionError, not
        register anything -- an MCP server cannot contribute callable tools
        without a recorded transport-trust decision."""
        import json

        from lionagi.protocols.action.manager import ActionManager

        # Reset pool state and use a server name unused elsewhere in this
        # file, so no cached client/policy from another test leaks in.
        MCPConnectionPool._security = None
        MCPConnectionPool._clients = {}
        MCPConnectionPool._server_security.pop("server:trustcheck", None)

        cfg = tmp_path / ".mcp.json"
        cfg.write_text(
            json.dumps({"mcpServers": {"trustcheck": {"command": "echo", "args": ["hi"]}}})
        )

        mgr = ActionManager()
        with pytest.raises(PermissionError, match="allow_commands=False"):
            await mgr.load_mcp_config(str(cfg))
        assert mgr.registry == {}

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
        """An explicit policy is threaded as an arg, not set on the global; concurrent loads must not share trust scope."""
        import json

        from lionagi.protocols.action.manager import ActionManager

        cfg = tmp_path / ".mcp.json"
        cfg.write_text(json.dumps({"mcpServers": {"local": {"command": "echo"}}}))

        # Known prior default: fail-closed.
        MCPConnectionPool._security = None
        try:
            mgr = ActionManager()

            async def fake_register(server_config, update=False, security=None):
                # An explicit, opted-in trusted policy arrives as an argument;
                # the global is NEVER mutated.
                assert security.allow_commands is True
                assert MCPConnectionPool._security is None
                return ["local_echo"]

            monkeypatch.setattr(mgr, "register_mcp_server", fake_register)
            await mgr.load_mcp_config(str(cfg), mcp_security=MCPSecurityConfig.trusted())

            # The global default is untouched throughout.
            assert MCPConnectionPool._security is None
        finally:
            MCPConnectionPool._security = None

    async def test_concurrent_loads_do_not_cross_contaminate(self, monkeypatch):
        """Two concurrent loads with different policies must not observe each other's policy when interleaved."""
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

    async def test_load_mcp_tools_helper_omitted_policy_stays_unset(self, tmp_path, monkeypatch):
        """load_mcp_tools mirrors load_mcp_config: an omitted policy is no
        longer manufactured into a permissive one -- it stays None, global
        untouched -- while an explicit trusted policy still threads through
        and denials still surface loudly, never swallowed to []."""
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
                seen["security"] = security
                assert MCPConnectionPool._security is None
                return ["local_echo"]

            monkeypatch.setattr(ActionManager, "register_mcp_server", fake_register)

            # Normal load, no explicit policy: no raise, security stays None
            # (fail-closed downstream), global untouched.
            await load_mcp_tools(str(cfg))
            assert seen["security"] is None
            assert MCPConnectionPool._security is None

            # Explicit trusted policy: threaded through as-is.
            monkeypatch.setattr(ActionManager, "register_mcp_server", fake_register)
            await load_mcp_tools(str(cfg), mcp_security=MCPSecurityConfig.trusted())
            assert seen["security"] == MCPSecurityConfig.trusted()

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
    """The authorized policy must reach the stored tool-call path, not only the discovery client; lazy and reconnect invocations re-apply it."""

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
            MCPConnectionPool.remember_security({"command": "echo", "args": ["a"]}, policy)
            # Stored callable invokes get_client WITHOUT a policy, with a fresh
            # dict of the SAME content (the real flow strips only `_`-prefixed
            # metadata, so transport fields are identical) — the recorded policy
            # must be recovered.
            await MCPConnectionPool.get_client({"command": "echo", "args": ["a"]})
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

    async def test_shared_command_different_args_do_not_share_policy(self, monkeypatch):
        """Policy key must fingerprint the whole transport: a trusted config must NOT authorize a different args set sharing the same executable."""
        self._reset()
        seen = []

        async def fake_create(config, security=None):
            seen.append(security)
            return object()

        monkeypatch.setattr(MCPConnectionPool, "_create_client", staticmethod(fake_create))
        try:
            policy = MCPSecurityConfig(allow_commands=True)
            safe = {"command": "python", "args": ["safe_server.py"]}
            evil = {"command": "python", "args": ["-c", "import os; os.system('x')"]}

            # Distinct fingerprints — the leak would make these collide.
            assert MCPConnectionPool._policy_key(safe) != MCPConnectionPool._policy_key(evil)

            # Trust only the safe config.
            MCPConnectionPool.remember_security(safe, policy)

            # The evil config must NOT recover the safe policy → stays fail-closed.
            await MCPConnectionPool.get_client(evil)
            assert seen[-1] is None, "different args must not inherit another config's policy"

            # The safe config still recovers its own policy.
            await MCPConnectionPool.get_client(safe)
            assert seen[-1] is policy
        finally:
            self._reset()

    async def test_shared_url_different_headers_do_not_share_policy(self, monkeypatch):
        """Same leak for HTTP transports keyed only on URL."""
        self._reset()
        seen = []

        async def fake_create(config, security=None):
            seen.append(security)
            return object()

        monkeypatch.setattr(MCPConnectionPool, "_create_client", staticmethod(fake_create))
        try:
            policy = MCPSecurityConfig(allow_urls=True)
            trusted = {"url": "https://api.example.com", "headers": {"X-Tenant": "a"}}
            other = {"url": "https://api.example.com", "headers": {"X-Tenant": "b"}}

            assert MCPConnectionPool._policy_key(trusted) != MCPConnectionPool._policy_key(other)
            MCPConnectionPool.remember_security(trusted, policy)
            await MCPConnectionPool.get_client(other)
            assert seen[-1] is None
        finally:
            self._reset()
