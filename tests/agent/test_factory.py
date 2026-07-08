# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for create_agent: wiring tools, permissions, hooks."""

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

    # Only coding tools get permission preprocessors (MCP tools from ambient env are unaffected)
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

    async def fake_load_mcp_config(self, config_path, server_names=None, update=False):
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

    async def fake_load_mcp_config(self, config_path, server_names=None, update=False):
        calls.append((config_path, server_names, update))
        return {}

    monkeypatch.setattr(ActionManager, "load_mcp_config", fake_load_mcp_config)

    await create_agent(
        AgentSpec.compose("implementer", cwd=str(project)),
        load_settings=False,
        trust_project_settings=True,
    )

    assert calls == [(str(mcp_path), None, False)]


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

    async def fake_load_mcp(self, config_path, server_names=None, update=False):
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

    async def fake_load_mcp(self, config_path, server_names=None, update=False):
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
