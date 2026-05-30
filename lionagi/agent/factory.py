# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from lionagi.session.branch import Branch

from .config import AgentConfig

if TYPE_CHECKING:
    from .spec import AgentSpec


async def create_agent(
    config: AgentConfig | AgentSpec,
    *,
    load_settings: bool = True,
    project_dir: str | None = None,
    trust_project_settings: bool = False,
    trusted_hook_modules: set[str] | frozenset[str] | None = None,
) -> Branch:
    """Create a fully configured Branch from an AgentConfig.

    Wires: settings → hooks → system prompt → model → tools.

    Args:
        config: Agent configuration.
        load_settings: If True, load hooks from .lionagi/settings.yaml
            (global always; project-local only when trust_project_settings=True).
        project_dir: Project root for settings resolution. Auto-detected if None.
        trust_project_settings: If True, load and apply hooks from project-local
            .lionagi/settings.yaml. Defaults to False (Finding 1: safe default).
        trusted_hook_modules: Python module paths allowed for import-based hooks.
            Defaults to {"lionagi.agent.hooks"}.

    Usage::

        config = AgentConfig.coding(model="openai/gpt-4.1")
        branch = await create_agent(config)
        response = await branch.chat("Fix the bug in utils.py")

    Returns:
        A Branch ready to use with tools registered and hooks applied.
    """
    from .spec import AgentSpec

    if isinstance(config, AgentSpec):
        return await _create_agent_from_spec(
            config,
            load_settings=load_settings,
            project_dir=project_dir,
            trust_project_settings=trust_project_settings,
            trusted_hook_modules=trusted_hook_modules,
        )

    if load_settings:
        from .settings import apply_hooks_from_settings
        from .settings import load_settings as _load

        # Finding 1: only load project-local hooks when explicitly trusted
        settings = _load(project_dir, include_project=trust_project_settings)
        apply_hooks_from_settings(
            config,
            settings,
            trusted_hook_modules=trusted_hook_modules,
        )

    from lionagi.service.imodel import iModel

    branch_kwargs = {}

    if config.model:
        from lionagi.cli._providers import (
            PROVIDER_EFFORT_KWARG,
            parse_model_spec,
        )

        ms = parse_model_spec(config.model)
        if "/" in ms.model:
            provider, model_name = ms.model.split("/", 1)
        else:
            provider = model_name = ms.model

        extra = {}
        effort = config.effort or ms.effort
        if effort:
            kwarg = PROVIDER_EFFORT_KWARG.get(provider)
            if kwarg:
                extra[kwarg] = effort
        if config.yolo:
            from lionagi.cli._providers import PROVIDER_YOLO_KWARGS

            extra.update(PROVIDER_YOLO_KWARGS.get(provider, {}))

        chat_model = iModel(
            provider=provider,
            model=model_name,
            api_key="dummy",
            **extra,
        )
        branch_kwargs["chat_model"] = chat_model

    branch = Branch(**branch_kwargs)

    system_message = config.build_system_message()
    if system_message:
        if config.lion_system:
            from lionagi.session.prompts import LION_SYSTEM_MESSAGE

            full_prompt = LION_SYSTEM_MESSAGE.strip() + "\n\n" + system_message
        else:
            full_prompt = system_message
        branch.msgs.set_system(branch.msgs.create_system(system=full_prompt))

    _apply_permissions(config)
    _register_tools(branch, config)
    await _load_mcp(branch, config, trust_project_settings=trust_project_settings)

    return branch


async def _create_agent_from_spec(
    spec: AgentSpec,
    *,
    load_settings: bool = True,
    project_dir: str | None = None,
    trust_project_settings: bool = False,
    trusted_hook_modules: set[str] | frozenset[str] | None = None,
) -> Branch:
    """Create a Branch from an AgentSpec (orchestration-facing path).

    MAJ-1: load_settings / trust_project_settings / trusted_hook_modules are
    now live — settings hooks and MCP are applied on the spec path exactly as
    on the AgentConfig path.
    MAJ-2: spec.hook_handlers / spec.cwd / spec.yolo are threaded into the
    bridge AgentConfig so guard hooks and workspace survive the round-trip.
    """
    from lionagi.service.imodel import iModel

    branch_kwargs = {}

    if spec.model:
        from lionagi.cli._providers import (
            PROVIDER_EFFORT_KWARG,
            parse_model_spec,
        )

        ms = parse_model_spec(spec.model)
        if "/" in ms.model:
            provider, model_name = ms.model.split("/", 1)
        else:
            provider = model_name = ms.model

        extra = {}
        effort = spec.effort or ms.effort
        if effort:
            kwarg = PROVIDER_EFFORT_KWARG.get(provider)
            if kwarg:
                extra[kwarg] = effort
        # MIN-2: MAJ-2 — apply yolo kwargs on the spec path (mirrors config path)
        if spec.yolo:
            from lionagi.cli._providers import PROVIDER_YOLO_KWARGS

            extra.update(PROVIDER_YOLO_KWARGS.get(provider, {}))

        branch_kwargs["chat_model"] = iModel(
            provider=provider,
            model=model_name,
            api_key="dummy",
            **extra,
        )

    branch = Branch(**branch_kwargs)

    system_message = spec.build_system_message()
    if system_message:
        if spec.lion_system:
            from lionagi.session.prompts import LION_SYSTEM_MESSAGE

            full_prompt = LION_SYSTEM_MESSAGE.strip() + "\n\n" + system_message
        else:
            full_prompt = system_message
        branch.msgs.set_system(branch.msgs.create_system(system=full_prompt))

    # Build a bridge AgentConfig carrying hook_handlers and cwd so that
    # _register_tools/_apply_permissions/_load_mcp all receive the full spec.
    # MAJ-2: copy hook_handlers (shallow — lists are mutable, caller owns them)
    # and cwd so guard hooks and workspace root survive the round-trip.
    bridge = AgentConfig(
        tools=list(spec.tools),
        hook_handlers={k: list(v) for k, v in spec.hook_handlers.items()},
        cwd=spec.cwd,
    )
    if spec.permissions is not None:
        bridge.permissions = spec.permissions

    # MAJ-1: apply settings hooks onto the bridge when load_settings is True.
    if load_settings:
        from .settings import apply_hooks_from_settings
        from .settings import load_settings as _load

        settings = _load(project_dir, include_project=trust_project_settings)
        apply_hooks_from_settings(
            bridge,
            settings,
            trusted_hook_modules=trusted_hook_modules,
        )

    _apply_permissions(bridge)
    _register_tools(branch, bridge)

    # MAJ-1: load MCP tools on the spec path, mirroring the AgentConfig path.
    await _load_mcp(branch, bridge, trust_project_settings=trust_project_settings)

    if op := spec.capability_operable():
        branch.grant_capabilities(op)

    return branch


def _apply_permissions(config: AgentConfig) -> None:
    """Convert permission config into a security_pre hook on all tools.

    Finding 13: uses 'security_pre' phase so permission hooks always run
    before user-defined pre-hooks.
    """
    if not config.permissions:
        return

    from .permissions import PermissionPolicy

    if isinstance(config.permissions, PermissionPolicy):
        policy = config.permissions
    elif isinstance(config.permissions, dict):
        policy = PermissionPolicy.from_dict(config.permissions)
    else:
        return

    # Finding 13: insert permission hook into security_pre phase, not pre phase
    config.hook_handlers.setdefault("security_pre:*", []).insert(0, policy.to_pre_hook())


def _tool_hooks(config: AgentConfig, phase: str, tool_name: str) -> list[Callable]:
    """Finding 15: collect hooks for a tool from all relevant phase:name keys."""
    return [
        *config.hook_handlers.get(f"{phase}:*", []),
        *config.hook_handlers.get(f"{phase}:{tool_name}", []),
        *config.hook_handlers.get(f"{phase}:{tool_name}_tool", []),
    ]


def _chain_pre_hooks(
    tool_name: str,
    security_hooks: list[Callable],
    user_hooks: list[Callable] | None = None,
) -> Callable | None:
    user_hooks = user_hooks or []
    hooks = [*security_hooks, *user_hooks]
    if user_hooks:
        # User pre-hooks may rewrite args; validate the final args too.
        hooks.extend(security_hooks)
    if not hooks:
        return None

    async def chained(args: dict, **_kw) -> dict:
        for handler in hooks:
            result = await handler(tool_name, args.get("action", ""), args)
            if isinstance(result, dict):
                args = result
        return args

    return chained


def _chain_post_hooks(tool_name: str, hooks: list[Callable]) -> Callable | None:
    if not hooks:
        return None

    async def chained(result: Any, **_kw) -> Any:
        if not isinstance(result, dict):
            return result
        for handler in hooks:
            modified = await handler(tool_name, "", {}, result)
            if isinstance(modified, dict):
                result = modified
        return result

    return chained


def _attach_hooks(tool: Any, config: AgentConfig, canonical_name: str) -> Any:
    """Finding 15: attach security_pre + pre + post hooks to a standalone tool."""
    security_hooks = _tool_hooks(config, "security_pre", canonical_name)
    user_pre_hooks = _tool_hooks(config, "pre", canonical_name)
    post_hooks = _tool_hooks(config, "post", canonical_name)
    pre = _chain_pre_hooks(canonical_name, security_hooks, user_pre_hooks)
    post = _chain_post_hooks(canonical_name, post_hooks)
    if pre is not None:
        tool.preprocessor = pre
    if post is not None:
        tool.postprocessor = post
    return tool


def _register_tools(branch: Branch, config: AgentConfig) -> None:
    """Register tools based on config.tools list, applying hooks."""
    for tool_spec in config.tools:
        if tool_spec == "coding":
            _register_coding_tools(branch, config)
        elif tool_spec == "reader":
            from lionagi.tools.file.reader import ReaderTool

            # Finding 15: attach config hooks to standalone reader tool
            tool = _attach_hooks(ReaderTool().to_tool(), config, "reader")
            branch.register_tools(tool)
        elif tool_spec == "editor":
            from lionagi.tools.file.editor import EditorTool

            # Finding 15: attach config hooks to standalone editor tool
            tool = _attach_hooks(EditorTool().to_tool(), config, "editor")
            branch.register_tools(tool)
        elif tool_spec == "bash":
            from lionagi.tools.code.bash import BashTool

            # Finding 15: attach config hooks to standalone bash tool
            tool = _attach_hooks(BashTool().to_tool(), config, "bash")
            branch.register_tools(tool)
        elif tool_spec == "search":
            from lionagi.tools.code.search import SearchTool

            # Finding 15: attach config hooks to standalone search tool
            tool = _attach_hooks(SearchTool().to_tool(), config, "search")
            branch.register_tools(tool)


def _register_coding_tools(branch: Branch, config: AgentConfig) -> None:
    """Register CodingToolkit with hooks from config."""
    from pathlib import Path

    from lionagi.tools.coding import CodingToolkit

    workspace_root = Path(config.cwd) if config.cwd else Path.cwd()
    toolkit = CodingToolkit(workspace_root=workspace_root)

    for key, handlers in config.hook_handlers.items():
        parts = key.split(":", 1)
        if len(parts) != 2:
            continue
        phase, tool_name = parts
        for handler in handlers:
            if phase == "security_pre":
                # Finding 13: wire security_pre hooks into CodingToolkit's dedicated phase
                toolkit.security_pre(tool_name, handler)
            elif phase == "pre":
                toolkit.pre(tool_name, handler)
            elif phase == "post":
                toolkit.post(tool_name, handler)
            elif phase == "error":
                toolkit.on_error(tool_name, handler)

    tools = toolkit.bind(branch)
    branch.register_tools(tools)


async def _load_mcp(
    branch: Branch,
    config: AgentConfig,
    *,
    trust_project_settings: bool = False,
) -> None:
    """Auto-discover and load MCP tools from .mcp.json files.

    Discovery order:
        1. config.mcp_config_path (explicit)
        2. .lionagi/.mcp.json (project-local, only when trusted)
        3. cwd/.mcp.json (current directory, only when trusted)
        4. ~/.lionagi/.mcp.json (global)
    """
    from pathlib import Path

    mcp_path = None

    if config.mcp_config_path:
        p = Path(config.mcp_config_path)
        if p.is_file():
            mcp_path = str(p)
    else:
        candidates = []
        cwd = Path(config.cwd) if config.cwd else Path.cwd()

        if trust_project_settings:
            for parent in [cwd, *cwd.parents]:
                candidates.append(parent / ".lionagi" / ".mcp.json")
                candidates.append(parent / ".mcp.json")
                if (parent / ".lionagi").is_dir():
                    break

        candidates.append(Path.home() / ".lionagi" / ".mcp.json")

        for candidate in candidates:
            if candidate.is_file():
                mcp_path = str(candidate)
                break

    if mcp_path is None:
        return

    await branch.acts.load_mcp_config(
        mcp_path,
        server_names=config.mcp_servers,
    )
