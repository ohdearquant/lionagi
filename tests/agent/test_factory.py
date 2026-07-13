# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for create_agent: wiring tools, permissions, hooks."""

import json

import pytest

from lionagi.agent.factory import create_agent
from lionagi.agent.spec import AgentSpec
from lionagi.session.branch import Branch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make(config: AgentSpec) -> Branch:
    return await create_agent(config, load_settings=False)


# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------


async def test_create_agent_default_config_returns_branch():
    config = AgentSpec.compose("implementer")
    branch = await _make(config)
    assert isinstance(branch, Branch)


async def test_create_agent_default_no_coding_tools():
    """Default config with no tools= list should not register coding tools."""
    config = AgentSpec.compose("implementer")
    branch = await _make(config)
    coding_tools = {
        "reader",
        "editor",
        "bash",
        "search",
        "context",
        "sandbox",
        "subagent",
    }
    assert not coding_tools.intersection(branch.acts.registry.keys())


# ---------------------------------------------------------------------------
# Coding preset
# ---------------------------------------------------------------------------


# Lean default plus context — sandbox/subagent are opt-in, not registered by default.
_CODING_TOOLS = {"reader", "editor", "bash", "search", "context"}
_EXTRA_CODING_TOOLS = {"sandbox", "subagent"}


async def test_create_agent_coding_preset_registers_core_tools():
    config = AgentSpec.coding()
    branch = await _make(config)
    registry = set(branch.acts.registry.keys())
    assert _CODING_TOOLS.issubset(registry)
    # extras must NOT be registered by default
    assert not _EXTRA_CODING_TOOLS.intersection(registry)


async def test_create_agent_coding_preset_tool_names():
    config = AgentSpec.coding()
    branch = await _make(config)
    assert _CODING_TOOLS.issubset(branch.acts.registry.keys())


async def test_create_agent_coding_all_tools_async():
    """Every registered tool's callable must be a coroutine function."""
    import asyncio

    config = AgentSpec.coding()
    branch = await _make(config)
    for name, tool in branch.acts.registry.items():
        assert asyncio.iscoroutinefunction(tool.func_callable), f"Tool '{name}' is not async"


# ---------------------------------------------------------------------------
# Permissions wired as preprocessor
# ---------------------------------------------------------------------------


async def test_create_agent_with_permissions_sets_preprocessor():
    from lionagi.agent.permissions import PermissionPolicy

    config = AgentSpec.coding()
    config.permissions = PermissionPolicy.read_only()
    branch = await _make(config)

    # No MCP servers are configured/resolvable in this test, so only the
    # statically-registered coding tools exist to check here; MCP-discovered
    # tools get the same preprocessor chain applied (see
    # test_mcp_discovered_tool_gets_permission_preprocessor below).
    for name in _CODING_TOOLS:
        tool = branch.acts.registry.get(name)
        assert tool is not None, f"Coding tool '{name}' not registered"
        assert tool.preprocessor is not None, f"Tool '{name}' missing preprocessor"


async def test_create_agent_permission_deny_all_preprocessor_raises():
    """If deny_all policy is set, preprocessor on any tool should raise PermissionError."""
    from lionagi.agent.permissions import PermissionPolicy

    config = AgentSpec.coding()
    config.permissions = PermissionPolicy.deny_all()
    branch = await _make(config)

    reader_tool = branch.acts.registry["reader"]
    assert reader_tool.preprocessor is not None
    with pytest.raises(PermissionError):
        await reader_tool.preprocessor(
            {"action": "read", "path": "/tmp/x.py"},
        )


async def test_mcp_discovered_tool_gets_permission_preprocessor(tmp_path, monkeypatch):
    """ADR-0041 delta row 2: a permission rule that blocks a static tool must
    equally block a same-shaped MCP-discovered tool. MCP registration happens
    after built-in tool interception (_register_tools) and must not bypass
    the resolved permission/interceptor chain -- _load_mcp applies the same
    _attach_hooks() used for static tools to every tool name MCP discovery
    reports, not a copied/parallel chain."""
    from lionagi.agent.permissions import PermissionPolicy
    from lionagi.protocols.action.manager import ActionManager
    from lionagi.protocols.action.tool import Tool

    mcp_file = tmp_path / "custom.mcp.json"
    mcp_file.write_text('{"mcpServers": {"demo": {"command": "true"}}}')

    async def fake_load_mcp_config(
        self, config_path, server_names=None, update=False, mcp_security=None
    ):
        # Mimic what register_mcp_server does after real discovery: put a
        # plain Tool straight into the registry, bypassing hook attachment
        # entirely -- exactly the gap this fix closes.
        async def demo_tool(**kwargs):
            return "ok"

        demo_tool.__name__ = "demo_tool"
        self.register_tool(Tool(func_callable=demo_tool), update=update)
        return {"demo": ["demo_tool"]}

    monkeypatch.setattr(ActionManager, "load_mcp_config", fake_load_mcp_config)

    config = AgentSpec.compose("implementer")
    config.mcp_config_path = str(mcp_file)
    config.permissions = PermissionPolicy.deny_all()
    branch = await create_agent(config, load_settings=False)

    mcp_tool = branch.acts.registry["demo_tool"]
    assert mcp_tool.preprocessor is not None, "MCP-discovered tool missing the spec's hook chain"
    with pytest.raises(PermissionError):
        await mcp_tool.preprocessor({"action": "call", "foo": "bar"})


async def test_mcp_discovered_tool_composes_existing_preprocessor(tmp_path, monkeypatch):
    """_attach_hooks() must compose with a pre-existing tool preprocessor
    instead of replacing it outright: an MCP-discovered Tool that already
    carries one (e.g. an arg normalizer wired at construction) must still
    run it, and the spec's permission gate must still block."""
    from lionagi.agent.permissions import PermissionPolicy
    from lionagi.protocols.action.manager import ActionManager
    from lionagi.protocols.action.tool import Tool

    mcp_file = tmp_path / "custom.mcp.json"
    mcp_file.write_text('{"mcpServers": {"demo": {"command": "true"}}}')

    calls = []

    async def existing_preprocessor(args, **kw):
        calls.append(dict(args))
        return args

    async def fake_load_mcp_config(
        self, config_path, server_names=None, update=False, mcp_security=None
    ):
        async def demo_tool(**kwargs):
            return "ok"

        demo_tool.__name__ = "demo_tool"
        self.register_tool(
            Tool(func_callable=demo_tool, preprocessor=existing_preprocessor),
            update=update,
        )
        return {"demo": ["demo_tool"]}

    monkeypatch.setattr(ActionManager, "load_mcp_config", fake_load_mcp_config)

    config = AgentSpec.compose("implementer")
    config.mcp_config_path = str(mcp_file)
    config.permissions = PermissionPolicy.deny_all()
    branch = await create_agent(config, load_settings=False)

    mcp_tool = branch.acts.registry["demo_tool"]
    assert mcp_tool.preprocessor is not existing_preprocessor, (
        "the spec's hook chain must be composed in, not left as a bare passthrough"
    )
    with pytest.raises(PermissionError):
        await mcp_tool.preprocessor({"action": "call", "foo": "bar"})

    # The tool's own preprocessor ran before the permission gate raised.
    assert calls == [{"action": "call", "foo": "bar"}]


async def test_create_agent_coding_permissions_recheck_user_mutated_args(tmp_path):
    """User pre-hooks must not be able to rewrite safe args after permission checks."""
    from lionagi.agent.permissions import PermissionPolicy

    config = AgentSpec.coding(cwd=str(tmp_path))
    config.permissions = PermissionPolicy(
        mode="rules",
        allow={"bash": ["echo *"]},
        deny={"bash": ["rm *"]},
    )

    async def rewrite_to_denied(tool_name, action, args):
        return {**args, "command": "rm /tmp/important"}

    config.pre("bash", rewrite_to_denied)
    branch = await _make(config)

    bash_tool = branch.acts.registry["bash"]
    with pytest.raises(PermissionError, match="denied by rule"):
        await bash_tool.preprocessor({"action": "run", "command": "echo ok"})


async def test_create_agent_standalone_permissions_recheck_user_mutated_args():
    """Standalone tools get the same post-mutation permission validation."""
    from lionagi.agent.permissions import PermissionPolicy

    config = AgentSpec.compose("implementer", tools=["bash"])
    config.permissions = PermissionPolicy(
        mode="rules",
        allow={"bash": ["echo *"]},
        deny={"bash": ["rm *"]},
    )

    async def rewrite_to_denied(tool_name, action, args):
        return {**args, "command": "rm /tmp/important"}

    config.pre("bash", rewrite_to_denied)
    branch = await _make(config)

    bash_tool = branch.acts.registry["bash_tool"]
    with pytest.raises(PermissionError, match="denied by rule"):
        await bash_tool.preprocessor({"action": "run", "command": "echo ok"})


# ---------------------------------------------------------------------------
# load_settings=False — no side effects
# ---------------------------------------------------------------------------


async def test_create_agent_load_settings_false_no_side_effects(monkeypatch):
    """load_settings=False must not read .lionagi/settings.yaml."""
    called = []

    def fake_load(project_dir, include_project):
        called.append(True)
        return {}

    monkeypatch.setattr("lionagi.agent.settings.load_settings", fake_load, raising=False)

    config = AgentSpec.compose("implementer")
    await create_agent(config, load_settings=False)
    assert called == [], "load_settings was called despite load_settings=False"


async def test_create_agent_does_not_autoload_project_mcp_without_trust(tmp_path, monkeypatch):
    from lionagi.protocols.action.manager import ActionManager

    project = tmp_path / "project"
    project.mkdir()
    (project / ".mcp.json").write_text('{"mcpServers": {"demo": {"command": "true"}}}')
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    calls = []

    async def fake_load_mcp_config(
        self, config_path, server_names=None, update=False, mcp_security=None
    ):
        calls.append((config_path, server_names, update))
        return {}

    monkeypatch.setattr(ActionManager, "load_mcp_config", fake_load_mcp_config)

    await create_agent(
        AgentSpec.compose("implementer", cwd=str(project)),
        load_settings=False,
        trust_project_settings=False,
    )

    assert calls == []


async def test_create_agent_autoloads_project_mcp_when_trusted(tmp_path, monkeypatch):
    from lionagi.protocols.action.manager import ActionManager

    project = tmp_path / "project"
    project.mkdir()
    mcp_path = project / ".mcp.json"
    mcp_path.write_text('{"mcpServers": {"demo": {"command": "true"}}}')
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    calls = []
    security_seen = []

    async def fake_load_mcp_config(
        self, config_path, server_names=None, update=False, mcp_security=None
    ):
        calls.append((config_path, server_names, update))
        security_seen.append(mcp_security)
        return {}

    monkeypatch.setattr(ActionManager, "load_mcp_config", fake_load_mcp_config)

    await create_agent(
        AgentSpec.compose("implementer", cwd=str(project)),
        load_settings=False,
        trust_project_settings=True,
    )

    assert calls == [(str(mcp_path), None, False)]
    # _load_mcp makes the transport-trust decision explicit at its one call
    # site (ADR-0011 delta row 3) rather than relying on an implicit default.
    from lionagi.service.connections.mcp_wrapper import MCPSecurityConfig

    assert security_seen == [MCPSecurityConfig.trusted()]


# ---------------------------------------------------------------------------
# Hooks wired into tools
# ---------------------------------------------------------------------------


async def test_pre_hook_registered_on_tool():
    config = AgentSpec.coding()
    calls = []

    async def my_hook(tool_name, action, args):
        calls.append(tool_name)
        return None  # pass through

    config.pre("bash", my_hook)
    branch = await _make(config)

    bash_tool = branch.acts.registry["bash"]
    assert bash_tool.preprocessor is not None
    # Invoke the preprocessor to verify our hook is wired
    await bash_tool.preprocessor({"action": "run", "command": "echo hi"})
    assert "bash" in calls


async def test_post_hook_registered_on_tool():
    config = AgentSpec.coding()
    calls = []

    async def my_post(tool_name, action, args, result):
        calls.append(tool_name)
        return result

    config.post("reader", my_post)
    branch = await _make(config)

    reader_tool = branch.acts.registry["reader"]
    assert reader_tool.postprocessor is not None
    result = {"success": True}
    await reader_tool.postprocessor(result)
    assert "reader" in calls


# ---------------------------------------------------------------------------
# Model string parsed into provider / model / effort / yolo kwargs
# ---------------------------------------------------------------------------


async def test_create_agent_parses_model_provider_effort_and_yolo_kwargs(monkeypatch):
    import lionagi.cli._providers as providers_mod
    import lionagi.service.imodel as imodel_mod

    monkeypatch.setitem(providers_mod.PROVIDER_EFFORT_KWARG, "openai", "reasoning_effort")
    monkeypatch.setitem(providers_mod.PROVIDER_YOLO_KWARGS, "openai", {"stream": True})

    real_init = imodel_mod.iModel.__init__
    captured = {}

    def spy_init(self, *args, **kwargs):
        captured.update(kwargs)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(imodel_mod.iModel, "__init__", spy_init)

    config = AgentSpec.compose("implementer", model="openai/gpt-4.1-mini", effort="high", yolo=True)
    branch = await create_agent(config, load_settings=False)

    assert isinstance(branch, Branch)
    assert captured.get("provider") == "openai"
    assert captured.get("model") == "gpt-4.1-mini"
    assert captured.get("reasoning_effort") == "high"
    assert captured.get("stream") is True


# ---------------------------------------------------------------------------
# trust_project_settings=False prevents project settings from loading
# ---------------------------------------------------------------------------


async def test_create_agent_does_not_load_project_settings_without_trust(tmp_path, monkeypatch):
    import lionagi.agent.settings as settings_mod

    (tmp_path / ".lionagi").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))

    calls = []
    real_load = settings_mod.load_settings

    def spy_load(project_dir=None, *, include_project=True):
        calls.append(include_project)
        return real_load(project_dir, include_project=include_project)

    monkeypatch.setattr(settings_mod, "load_settings", spy_load)

    config = AgentSpec.compose("implementer")
    await create_agent(config, load_settings=True, trust_project_settings=False)

    assert calls == [False], f"load_settings called with include_project={calls}"


# ---------------------------------------------------------------------------
# _chain_post_hooks ignores non-dict hook returns; dict returns update result
# ---------------------------------------------------------------------------


async def test_agent_post_hooks_ignore_non_dict_results_and_keep_previous_result():
    """Non-dict hook return is ignored; a subsequent dict return is applied."""
    from lionagi.agent.factory import _chain_post_hooks

    async def hook_returns_string(tool_name, op, kwargs, result):
        return "not a dict — should be ignored"

    async def hook_returns_dict(tool_name, op, kwargs, result):
        return {"ok": 2}

    chained = _chain_post_hooks("mytool", [hook_returns_string, hook_returns_dict])
    assert chained is not None

    final = await chained({"ok": 1})
    assert final == {"ok": 2}


# ---------------------------------------------------------------------------
# model spec without "/" — provider resolves from settings default, not the
# bare model string (a bare model used to become its own garbage provider,
# which construction never rejected — it silently fell through to a generic
# Endpoint and only failed later with a missing-API-key error).
# ---------------------------------------------------------------------------


async def test_create_agent_model_without_slash_uses_settings_default_provider(monkeypatch):
    import lionagi.config as config_mod
    import lionagi.service.imodel as imodel_mod

    monkeypatch.setattr(
        config_mod, "settings", config_mod.AppSettings(LIONAGI_CHAT_PROVIDER="anthropic")
    )

    captured = {}
    real_init = imodel_mod.iModel.__init__

    def spy_init(self, *args, **kwargs):
        captured.update(kwargs)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(imodel_mod.iModel, "__init__", spy_init)

    config = AgentSpec.compose("implementer", model="gpt-4o")
    await create_agent(config, load_settings=False)

    assert captured.get("provider") == "anthropic"
    assert captured.get("model") == "gpt-4o"


async def test_create_agent_model_with_slash_provider_unchanged(monkeypatch):
    import lionagi.service.imodel as imodel_mod

    captured = {}
    real_init = imodel_mod.iModel.__init__

    def spy_init(self, *args, **kwargs):
        captured.update(kwargs)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(imodel_mod.iModel, "__init__", spy_init)

    config = AgentSpec.compose("implementer", model="anthropic/claude-sonnet-4")
    await create_agent(config, load_settings=False)

    assert captured.get("provider") == "anthropic"
    assert captured.get("model") == "claude-sonnet-4"


async def test_create_agent_backends_alias_unaffected(monkeypatch):
    """BACKENDS aliases (e.g. 'claude') are already expanded to provider/model by
    parse_model_spec, so they keep hitting the '/' branch untouched."""
    import lionagi.service.imodel as imodel_mod

    captured = {}
    real_init = imodel_mod.iModel.__init__

    def spy_init(self, *args, **kwargs):
        captured.update(kwargs)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(imodel_mod.iModel, "__init__", spy_init)

    config = AgentSpec.compose("implementer", model="claude")
    await create_agent(config, load_settings=False)

    assert captured.get("provider") == "claude_code"
    assert captured.get("model") == "sonnet"


# ---------------------------------------------------------------------------
# system_prompt without lion_system (line 104)
# ---------------------------------------------------------------------------


async def test_create_agent_system_prompt_without_lion_system():
    config = AgentSpec.compose("implementer", system_prompt="You are a helpful assistant.")
    config.lion_system = False
    branch = await create_agent(config, load_settings=False)
    sys_msg = branch.msgs.system
    assert sys_msg is not None
    assert "helpful assistant" in sys_msg.rendered


# ---------------------------------------------------------------------------
# _apply_permissions: non-PermissionPolicy non-dict → returns early (lines 127-130)
# ---------------------------------------------------------------------------


async def test_apply_permissions_invalid_type_returns_early():
    from lionagi.agent.factory import _apply_permissions

    config = AgentSpec.compose("implementer")
    config.permissions = "invalid_permissions_type"
    _apply_permissions(config)
    assert config.hook_handlers.get("security_pre:*", []) == []


# ---------------------------------------------------------------------------
# _chain_pre_hooks: no hooks → returns None (line 158)
# ---------------------------------------------------------------------------


def test_chain_pre_hooks_no_hooks_returns_none():
    from lionagi.agent.factory import _chain_pre_hooks

    result = _chain_pre_hooks("tool", [], [])
    assert result is None


# ---------------------------------------------------------------------------
# _chain_pre_hooks: hook returns dict → args updated (line 165)
# ---------------------------------------------------------------------------


async def test_chain_pre_hooks_dict_return_updates_args():
    from lionagi.agent.factory import _chain_pre_hooks

    async def rewrite(tool_name, action, args):
        return {**args, "extra": "added"}

    chained = _chain_pre_hooks("tool", [], [rewrite])
    result = await chained({"cmd": "ls"})
    assert result["extra"] == "added"
    assert result["cmd"] == "ls"


# ---------------------------------------------------------------------------
# _chain_post_hooks: non-dict initial result bypasses hooks (line 176)
# ---------------------------------------------------------------------------


async def test_chain_post_hooks_non_dict_result_returned_unchanged():
    from lionagi.agent.factory import _chain_post_hooks

    async def hook(tool_name, op, args, result):
        return {"should": "not be used"}

    chained = _chain_post_hooks("tool", [hook])
    result = await chained("plain string result")
    assert result == "plain string result"


# ---------------------------------------------------------------------------
# standalone tools: reader, editor, search registration (lines 196, 206-228)
# ---------------------------------------------------------------------------


async def test_create_agent_registers_standalone_reader():
    config = AgentSpec.compose("implementer", tools=["reader"])
    branch = await _make(config)
    assert "reader_tool" in branch.acts.registry


async def test_create_agent_registers_standalone_editor():
    config = AgentSpec.compose("implementer", tools=["editor"])
    branch = await _make(config)
    assert "editor_tool" in branch.acts.registry


async def test_create_agent_registers_standalone_search():
    config = AgentSpec.compose("implementer", tools=["search"])
    branch = await _make(config)
    assert "search_tool" in branch.acts.registry


async def test_attach_hooks_adds_postprocessor_for_standalone_tool():
    config = AgentSpec.compose("implementer", tools=["reader"])

    async def my_post(tool_name, action, args, result):
        return result

    config.post("reader", my_post)
    branch = await _make(config)
    tool = branch.acts.registry["reader_tool"]
    assert tool.postprocessor is not None


# ---------------------------------------------------------------------------
# _register_coding_tools: malformed key (line 243) and error phase (lines 253-254)
# ---------------------------------------------------------------------------


async def test_register_coding_tools_skips_malformed_keys():
    config = AgentSpec.coding()
    config.hook_handlers["malformed_no_colon"] = [lambda *a: None]
    branch = await _make(config)
    assert isinstance(branch, Branch)


async def test_register_coding_tools_error_hook_wired():
    config = AgentSpec.coding()
    error_calls = []

    async def my_error(tool_name, action, args, error):
        error_calls.append(tool_name)

    config.on_error("bash", my_error)
    branch = await _make(config)
    assert isinstance(branch, Branch)


# ---------------------------------------------------------------------------
# _load_mcp: explicit mcp_config_path (lines 279-281)
# ---------------------------------------------------------------------------


async def test_load_mcp_explicit_config_path_used(tmp_path, monkeypatch):
    from lionagi.protocols.action.manager import ActionManager

    mcp_file = tmp_path / "custom.mcp.json"
    mcp_file.write_text('{"mcpServers": {}}')

    calls = []

    async def fake_load_mcp(self, config_path, server_names=None, update=False, mcp_security=None):
        calls.append(config_path)
        return {}

    monkeypatch.setattr(ActionManager, "load_mcp_config", fake_load_mcp)

    config = AgentSpec.compose("implementer")
    config.mcp_config_path = str(mcp_file)
    await create_agent(config, load_settings=False)

    assert calls == [str(mcp_file)]


# ---------------------------------------------------------------------------
# _load_mcp: trust_project_settings + .lionagi dir stops search (line 291)
# ---------------------------------------------------------------------------


async def test_load_mcp_breaks_at_lionagi_dir(tmp_path, monkeypatch):
    from lionagi.protocols.action.manager import ActionManager

    project = tmp_path / "proj"
    project.mkdir()
    lionagi_dir = project / ".lionagi"
    lionagi_dir.mkdir()
    mcp_file = lionagi_dir / ".mcp.json"
    mcp_file.write_text('{"mcpServers": {}}')
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    calls = []

    async def fake_load_mcp(self, config_path, server_names=None, update=False, mcp_security=None):
        calls.append(config_path)
        return {}

    monkeypatch.setattr(ActionManager, "load_mcp_config", fake_load_mcp)

    await create_agent(
        AgentSpec.compose("implementer", cwd=str(project)),
        load_settings=False,
        trust_project_settings=True,
    )

    assert calls and calls[0] == str(mcp_file)


# ---------------------------------------------------------------------------
# Search tool workspace containment wiring (regression)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Forwarding AgentSpec MCP fields into the claude_code CLI's own request
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_mcp_pool_state():
    """MCPConnectionPool accumulates configs process-globally; tests here load
    real config files through create_agent, so snapshot and restore the pool's
    class-level state to keep those loads from leaking into other test files
    on the same worker."""
    from lionagi.service.connections.mcp_wrapper import MCPConnectionPool

    saved_configs = dict(MCPConnectionPool._configs)
    saved_security = dict(MCPConnectionPool._server_security)
    yield
    MCPConnectionPool._configs.clear()
    MCPConnectionPool._configs.update(saved_configs)
    MCPConnectionPool._server_security.clear()
    MCPConnectionPool._server_security.update(saved_security)


def _write_mcp_config(tmp_path, servers: dict) -> str:
    import json

    p = tmp_path / ".mcp.json"
    p.write_text(json.dumps({"mcpServers": servers}))
    return str(p)


async def test_forward_mcp_populates_claude_code_request_mcp_servers(tmp_path):
    """Test plan item 5: claude_code leg + mcp_config_path -> ClaudeCodeRequest
    carries the same servers, and --mcp-config shows up in as_cmd_args()."""
    from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

    mcp_path = _write_mcp_config(tmp_path, {"khive": {"command": "khive-mcp"}})

    config = AgentSpec.compose("reviewer", model="claude_code/sonnet")
    config.mcp_config_path = mcp_path
    branch = await create_agent(config, load_settings=False)

    kwargs = branch.chat_model.endpoint.config.kwargs
    assert kwargs.get("mcp_servers") == {"khive": {"command": "khive-mcp"}}

    payload, _ = branch.chat_model.endpoint.create_payload({"prompt": "hi"})
    request = payload["request"]
    assert isinstance(request, ClaudeCodeRequest)
    args = request.as_cmd_args()
    assert "--mcp-config" in args
    assert json.loads(args[args.index("--mcp-config") + 1]) == {
        "mcpServers": {"khive": {"command": "khive-mcp"}}
    }


async def test_forward_mcp_filters_by_spec_mcp_servers(tmp_path):
    """spec.mcp_servers is a name filter, consistent with island 1's server_names."""
    mcp_path = _write_mcp_config(
        tmp_path,
        {"khive": {"command": "khive-mcp"}, "other": {"command": "other-mcp"}},
    )

    config = AgentSpec.compose("reviewer", model="claude_code/sonnet")
    config.mcp_config_path = mcp_path
    config.mcp_servers = ["khive"]
    branch = await create_agent(config, load_settings=False)

    kwargs = branch.chat_model.endpoint.config.kwargs
    assert kwargs.get("mcp_servers") == {"khive": {"command": "khive-mcp"}}


async def test_forward_mcp_noop_when_spec_has_no_mcp_fields(tmp_path, monkeypatch):
    """No explicit mcp fields and nothing auto-resolvable (isolated HOME/cwd
    with no .mcp.json anywhere) -> no forwarding, no warning."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = AgentSpec.compose(
        "reviewer", model="claude_code/sonnet", cwd=str(tmp_path / "elsewhere")
    )
    branch = await create_agent(config, load_settings=False)
    assert "mcp_servers" not in branch.chat_model.endpoint.config.kwargs


async def test_forward_mcp_noop_for_non_claude_code_when_no_mcp_fields(tmp_path, monkeypatch):
    """Provider without MCP passthrough + nothing auto-resolvable: no warning fires
    (mirrors _load_mcp's own no-op — nothing to forward at all, not a passthrough gap)."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = AgentSpec.compose("reviewer", model="codex/gpt-5.5", cwd=str(tmp_path / "elsewhere"))
    branch = await create_agent(config, load_settings=False)
    assert "mcp_servers" not in branch.chat_model.endpoint.config.kwargs


async def test_forward_mcp_codex_provider_warns_and_noops(tmp_path, caplog):
    """Test plan item 6: codex/gemini provider + MCP fields set -> logged
    warning, no passthrough field populated (no MCP field exists to set)."""
    import logging

    mcp_path = _write_mcp_config(tmp_path, {"khive": {"command": "khive-mcp"}})

    config = AgentSpec.compose("reviewer", model="codex/gpt-5.5")
    config.mcp_config_path = mcp_path

    with caplog.at_level(logging.WARNING, logger="lionagi.agent.factory"):
        branch = await create_agent(config, load_settings=False)

    assert "mcp_servers" not in branch.chat_model.endpoint.config.kwargs
    assert any(
        "no MCP passthrough" in rec.message and "codex" in rec.message for rec in caplog.records
    )


async def test_forward_mcp_gemini_provider_warns_and_noops(tmp_path, caplog):
    import logging

    mcp_path = _write_mcp_config(tmp_path, {"khive": {"command": "khive-mcp"}})

    config = AgentSpec.compose("reviewer", model="gemini_code/gemini-3.5-flash")
    config.mcp_config_path = mcp_path

    with caplog.at_level(logging.WARNING, logger="lionagi.agent.factory"):
        branch = await create_agent(config, load_settings=False)

    assert "mcp_servers" not in branch.chat_model.endpoint.config.kwargs
    assert any(
        "no MCP passthrough" in rec.message and "gemini_code" in rec.message
        for rec in caplog.records
    )


async def test_forward_mcp_gated_by_trust_project_settings_for_project_scope(tmp_path, monkeypatch):
    """LC3: a project-scoped .mcp.json only forwards when trust_project_settings=True
    (mirrors _load_mcp's own gate); the global ~/.lionagi/.mcp.json candidate is
    trusted by default and forwards unconditionally."""
    project = tmp_path / "project"
    project.mkdir()
    _write_mcp_config(project, {"proj-server": {"command": "x"}})
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    config = AgentSpec.compose("reviewer", model="claude_code/sonnet", cwd=str(project))
    branch_untrusted = await create_agent(config, load_settings=False, trust_project_settings=False)
    assert "mcp_servers" not in branch_untrusted.chat_model.endpoint.config.kwargs

    config2 = AgentSpec.compose("reviewer", model="claude_code/sonnet", cwd=str(project))
    branch_trusted = await create_agent(config2, load_settings=False, trust_project_settings=True)
    assert branch_trusted.chat_model.endpoint.config.kwargs.get("mcp_servers") == {
        "proj-server": {"command": "x"}
    }


async def test_forward_mcp_global_candidate_trusted_by_default(tmp_path, monkeypatch):
    home = tmp_path / "home"
    lionagi_dir = home / ".lionagi"
    lionagi_dir.mkdir(parents=True)
    _write_mcp_config(lionagi_dir, {"global-server": {"command": "y"}})
    monkeypatch.setenv("HOME", str(home))

    config = AgentSpec.compose(
        "reviewer", model="claude_code/sonnet", cwd=str(tmp_path / "elsewhere")
    )
    branch = await create_agent(config, load_settings=False, trust_project_settings=False)
    assert branch.chat_model.endpoint.config.kwargs.get("mcp_servers") == {
        "global-server": {"command": "y"}
    }


async def test_forward_mcp_explicit_empty_allowlist_forces_zero_servers(tmp_path):
    """spec.mcp_servers=[] is an EXPLICIT empty selection, not 'no filter'.

    Before the fix, `if spec.mcp_servers:` treated an empty list the same as
    None (no filter at all) and forwarded every configured server; here it
    must forward zero servers, and the resulting ClaudeCodeRequest must still
    emit `--mcp-config {"mcpServers": {}}` (not silently omit the flag and
    let the claude CLI fall back to its own MCP discovery).
    """
    from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

    mcp_path = _write_mcp_config(
        tmp_path,
        {"khive": {"command": "khive-mcp"}, "other": {"command": "other-mcp"}},
    )

    config = AgentSpec.compose("reviewer", model="claude_code/sonnet")
    config.mcp_config_path = mcp_path
    config.mcp_servers = []  # explicit empty allowlist, distinct from None
    branch = await create_agent(config, load_settings=False)

    kwargs = branch.chat_model.endpoint.config.kwargs
    assert kwargs.get("mcp_servers") == {}, (
        "explicit empty allowlist must forward zero servers, not every configured server"
    )

    payload, _ = branch.chat_model.endpoint.create_payload({"prompt": "hi"})
    request = payload["request"]
    assert isinstance(request, ClaudeCodeRequest)
    args = request.as_cmd_args()
    assert "--mcp-config" in args, (
        "an explicit empty selection must still emit --mcp-config (forcing zero "
        "servers), not fall back to the CLI's own MCP discovery"
    )
    assert json.loads(args[args.index("--mcp-config") + 1]) == {"mcpServers": {}}


async def test_forward_mcp_explicit_empty_allowlist_enforced_with_no_resolvable_config(
    tmp_path, monkeypatch
):
    """spec.mcp_servers=[] must be enforced even when NO config file resolves.

    Before the fix, an unresolvable mcp_path made `_forward_mcp_to_cli_request`
    return early, leaving `mcp_servers` unset on the request entirely — the
    claude CLI would then fall back to its OWN MCP discovery instead of
    honoring the explicit zero-server allowlist. Isolated HOME + cwd with no
    .mcp.json anywhere (nothing auto-discoverable) reproduces "no resolvable
    config" while spec.mcp_servers=[] still declares explicit caller intent.
    """
    from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = AgentSpec.compose(
        "reviewer", model="claude_code/sonnet", cwd=str(tmp_path / "elsewhere")
    )
    config.mcp_servers = []  # explicit zero-server allowlist, no config file exists anywhere
    branch = await create_agent(config, load_settings=False)

    kwargs = branch.chat_model.endpoint.config.kwargs
    assert kwargs.get("mcp_servers") == {}, (
        "an explicit empty allowlist must be enforced even with no resolvable "
        "MCP config file, not silently left unset"
    )

    payload, _ = branch.chat_model.endpoint.create_payload({"prompt": "hi"})
    request = payload["request"]
    assert isinstance(request, ClaudeCodeRequest)
    args = request.as_cmd_args()
    assert "--mcp-config" in args, (
        "with no config file present, an explicit empty allowlist must still "
        "emit --mcp-config (forcing zero servers) rather than omitting the "
        "flag and letting the claude CLI fall back to its own MCP discovery"
    )
    assert json.loads(args[args.index("--mcp-config") + 1]) == {"mcpServers": {}}


async def test_forward_mcp_does_not_mutate_shared_chat_model_across_branches(tmp_path):
    """Two create_agent calls sharing one iModel must get independent MCP filters.

    Branch.__init__ keeps a caller-supplied chat_model by reference (no copy),
    so mutating branch.chat_model.endpoint.config.kwargs in place would leak
    one branch's MCP server selection into the other's payload.
    """
    from lionagi.service.imodel import iModel

    mcp_path = _write_mcp_config(
        tmp_path,
        {"khive": {"command": "khive-mcp"}, "other": {"command": "other-mcp"}},
    )

    shared_chat_model = iModel(provider="claude_code", model="sonnet", api_key="dummy")

    config_a = AgentSpec.compose("reviewer", model="claude_code/sonnet")
    config_a.mcp_config_path = mcp_path
    config_a.mcp_servers = ["khive"]
    branch_a = await create_agent(config_a, load_settings=False, chat_model=shared_chat_model)

    config_b = AgentSpec.compose("reviewer", model="claude_code/sonnet")
    config_b.mcp_config_path = mcp_path
    config_b.mcp_servers = ["other"]
    branch_b = await create_agent(config_b, load_settings=False, chat_model=shared_chat_model)

    assert branch_a.chat_model.endpoint.config.kwargs.get("mcp_servers") == {
        "khive": {"command": "khive-mcp"}
    }, "branch_a's filter must not have been overwritten by branch_b's create_agent call"
    assert branch_b.chat_model.endpoint.config.kwargs.get("mcp_servers") == {
        "other": {"command": "other-mcp"}
    }
    # The original caller-supplied iModel itself must be untouched — both
    # branches must have been given their own copy before mutation.
    assert "mcp_servers" not in shared_chat_model.endpoint.config.kwargs


async def test_forward_mcp_preserves_shared_executor_and_session(tmp_path):
    """Branch-local MCP filtering must not silently drop the caller-supplied
    iModel's shared rate limiter or CLI session_id.

    Before the fix, ``branch.chat_model.copy()`` (with no share_session/
    share_executor kwargs) always built a FRESH RateLimitedAPIExecutor and,
    since share_session defaulted False, dropped any pre-existing CLI
    session_id — silently changing the runtime semantics of a caller-supplied
    iModel that two branches were meant to share (rate limits/queue capacity,
    and mid-session continuation).
    """
    from lionagi.service.imodel import iModel

    mcp_path = _write_mcp_config(
        tmp_path,
        {"khive": {"command": "khive-mcp"}, "other": {"command": "other-mcp"}},
    )

    shared_chat_model = iModel(provider="claude_code", model="sonnet", api_key="dummy")
    shared_chat_model.endpoint.session_id = "session-abc"
    original_executor = shared_chat_model.executor

    config_a = AgentSpec.compose("reviewer", model="claude_code/sonnet")
    config_a.mcp_config_path = mcp_path
    config_a.mcp_servers = ["khive"]
    branch_a = await create_agent(config_a, load_settings=False, chat_model=shared_chat_model)

    config_b = AgentSpec.compose("reviewer", model="claude_code/sonnet")
    config_b.mcp_config_path = mcp_path
    config_b.mcp_servers = ["other"]
    branch_b = await create_agent(config_b, load_settings=False, chat_model=shared_chat_model)

    # (a) independent mcp_servers kwargs per branch, sharing one caller iModel.
    assert branch_a.chat_model.endpoint.config.kwargs.get("mcp_servers") == {
        "khive": {"command": "khive-mcp"}
    }
    assert branch_b.chat_model.endpoint.config.kwargs.get("mcp_servers") == {
        "other": {"command": "other-mcp"}
    }

    # (b) the branch's model retains the caller's executor (shared rate
    # limiter/queue) and the caller's CLI session_id.
    assert branch_a.chat_model.executor is original_executor
    assert branch_b.chat_model.executor is original_executor
    assert branch_a.chat_model.endpoint.session_id == "session-abc"
    assert branch_b.chat_model.endpoint.session_id == "session-abc"


async def test_forward_mcp_explicit_path_read_failure_raises(tmp_path):
    """An explicitly configured mcp_config_path that fails to read/parse is a
    configuration error (caller declared intent), not a silent skip.

    Exercises ``_forward_mcp_to_cli_request`` directly (island 2) rather than
    through the full ``create_agent`` flow: island 1's ``_load_mcp`` already
    raises its own (unrelated, pre-existing) json.JSONDecodeError for the
    same malformed file before island 2 ever runs, which would make this
    regression test pass for the wrong reason if routed through create_agent.
    """
    from lionagi._errors import ConfigurationError
    from lionagi.agent.factory import _forward_mcp_to_cli_request
    from lionagi.service.imodel import iModel
    from lionagi.session.branch import Branch

    bad_path = tmp_path / "not-json.mcp.json"
    bad_path.write_text("{not valid json")

    config = AgentSpec.compose("reviewer", model="claude_code/sonnet")
    config.mcp_config_path = str(bad_path)

    branch = Branch(chat_model=iModel(provider="claude_code", model="sonnet", api_key="dummy"))

    with pytest.raises(ConfigurationError):
        _forward_mcp_to_cli_request(branch, config)


async def test_forward_mcp_auto_discovered_path_read_failure_soft_skips(tmp_path, monkeypatch):
    """An auto-discovered (not explicitly configured) MCP candidate that fails
    to read/parse must soft-skip (no forwarding), not raise — only an
    explicit spec.mcp_config_path carries enough caller intent to escalate."""
    from lionagi.agent.factory import _forward_mcp_to_cli_request
    from lionagi.service.imodel import iModel
    from lionagi.session.branch import Branch

    home = tmp_path / "home"
    lionagi_dir = home / ".lionagi"
    lionagi_dir.mkdir(parents=True)
    (lionagi_dir / ".mcp.json").write_text("{not valid json")
    monkeypatch.setenv("HOME", str(home))

    config = AgentSpec.compose(
        "reviewer", model="claude_code/sonnet", cwd=str(tmp_path / "elsewhere")
    )
    branch = Branch(chat_model=iModel(provider="claude_code", model="sonnet", api_key="dummy"))

    _forward_mcp_to_cli_request(branch, config)  # must not raise
    assert "mcp_servers" not in branch.chat_model.endpoint.config.kwargs


async def test_explicit_mcp_config_path_missing_file_raises_configuration_error(tmp_path):
    """An explicitly set spec.mcp_config_path pointing at a nonexistent path
    is a configuration error, not a silent no-op.

    Before the fix, `_resolve_mcp_path` returned None for ANY unresolved
    mcp_config_path — indistinguishable from "no path configured at all" —
    so both `_load_mcp` and `_forward_mcp_to_cli_request` silently no-opped
    even though the caller explicitly declared intent to load a specific
    file. Exercised through the full create_agent() flow since either island
    raising is sufficient evidence of the fix (island 1's _load_mcp runs
    first and shares the same _resolve_mcp_path).
    """
    from lionagi._errors import ConfigurationError

    config = AgentSpec.compose("reviewer", model="claude_code/sonnet")
    config.mcp_config_path = "/nonexistent/mcp.json"

    with pytest.raises(ConfigurationError):
        await create_agent(config, load_settings=False)


async def test_explicit_empty_string_mcp_config_path_raises_not_autodiscovers(
    tmp_path, monkeypatch
):
    """An explicit empty-string mcp_config_path is a declared (malformed)
    path, not absence: it must raise, never fall through into auto-discovery.

    Presence is checked with `is not None`, not truthiness — otherwise
    `mcp_config_path=""` silently auto-discovers whatever candidate exists
    (e.g. ~/.lionagi/.mcp.json) and loads a config the caller never pointed
    at.
    """
    from lionagi._errors import ConfigurationError
    from lionagi.agent.factory import _resolve_mcp_path

    # A discoverable home candidate that MUST NOT be returned.
    home = tmp_path / "home"
    (home / ".lionagi").mkdir(parents=True)
    (home / ".lionagi" / ".mcp.json").write_text('{"mcpServers": {}}')
    monkeypatch.setenv("HOME", str(home))

    config = AgentSpec.compose("reviewer", model="claude_code/sonnet")
    config.mcp_config_path = ""

    with pytest.raises(ConfigurationError):
        _resolve_mcp_path(config)


async def test_search_tool_gets_workspace_root_from_cwd(tmp_path):
    """tools=["search"] must wire spec.cwd into SearchTool.workspace_root.

    Regression: the standalone search branch registered SearchTool() with no
    workspace_root, so normal agents got no containment. Here a search outside
    the configured cwd must be rejected fail-closed.
    """
    ws = tmp_path / "workspace"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    config = AgentSpec.compose("implementer", tools=["search"], cwd=str(ws))
    branch = await _make(config)

    key = next(k for k in branch.acts.registry.keys() if "search" in k)
    tool = branch.acts.registry[key]

    result = await tool.func_callable(action="grep", pattern="x", path=str(outside))
    assert result["success"] is False
    assert "workspace root" in (result.get("error") or "")
