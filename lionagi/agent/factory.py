# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from lionagi.session.branch import Branch

from .spec import AgentSpec

if TYPE_CHECKING:
    from .config import AgentConfig

__all__ = ("create_agent",)


async def create_agent(
    config: AgentSpec | AgentConfig,
    *,
    load_settings: bool = True,
    project_dir: str | None = None,
    trust_project_settings: bool = False,
    trusted_hook_modules: set[str] | frozenset[str] | None = None,
) -> Branch:
    """Create a fully configured Branch from an AgentSpec (or legacy AgentConfig).

    Wires: settings -> hooks -> system prompt -> model -> tools -> emissions.

    Args:
        config: Agent specification. AgentConfig is accepted for back-compat
            and converted internally via AgentSpec.from_legacy().
        load_settings: If True, load hooks from .lionagi/settings.yaml.
        project_dir: Project root for settings resolution. Auto-detected if None.
        trust_project_settings: If True, load project-local settings.
        trusted_hook_modules: Python module paths allowed for import-based hooks.

    Returns:
        A Branch ready to use with tools registered and hooks applied.
    """
    from .config import AgentConfig

    if isinstance(config, AgentConfig):
        spec = AgentSpec.from_legacy(config)
    else:
        spec = config

    if load_settings:
        from .settings import apply_hooks_from_settings
        from .settings import load_settings as _load

        settings = _load(project_dir, include_project=trust_project_settings)
        apply_hooks_from_settings(
            spec,
            settings,
            trusted_hook_modules=trusted_hook_modules,
        )

    from lionagi.service.imodel import iModel

    branch_kwargs = {}

    if spec.model:
        from lionagi.cli._providers import (
            CLI_PROVIDERS,
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
        if spec.yolo:
            from lionagi.cli._providers import PROVIDER_YOLO_KWARGS

            extra.update(PROVIDER_YOLO_KWARGS.get(provider, {}))

        # CLI providers (codex/claude_code) auth via subprocess — a placeholder
        # api_key is fine. API providers must resolve their real key from
        # settings; forcing "dummy" there silently breaks auth (the model can
        # never call out). So only pin the placeholder for CLI providers.
        if provider in CLI_PROVIDERS:
            extra["api_key"] = "dummy"

        chat_model = iModel(
            provider=provider,
            model=model_name,
            **extra,
        )
        branch_kwargs["chat_model"] = chat_model

    branch = Branch(**branch_kwargs)

    system_message = spec.build_system_message()
    if system_message:
        if spec.lion_system:
            from lionagi.session.prompts import LION_SYSTEM_MESSAGE

            full_prompt = LION_SYSTEM_MESSAGE.strip() + "\n\n" + system_message
        else:
            full_prompt = system_message
        branch.msgs.set_system(branch.msgs.create_system(system=full_prompt))

    _apply_permissions(spec)
    _register_tools(branch, spec)
    await _load_mcp(branch, spec, trust_project_settings=trust_project_settings)

    if op := spec.emission_operable():
        branch.grant_capabilities(op)

    return branch


def _apply_permissions(spec: AgentSpec) -> None:
    """Convert permission config into a security_pre hook on all tools."""
    if spec.permissions is None:
        return

    from .permissions import PermissionPolicy

    if isinstance(spec.permissions, PermissionPolicy):
        policy = spec.permissions
    else:
        return

    spec.hook_handlers.setdefault("security_pre:*", []).insert(0, policy.to_pre_hook())


def _tool_hooks(spec: AgentSpec, phase: str, tool_name: str) -> list[Callable]:
    return [
        *spec.hook_handlers.get(f"{phase}:*", []),
        *spec.hook_handlers.get(f"{phase}:{tool_name}", []),
        *spec.hook_handlers.get(f"{phase}:{tool_name}_tool", []),
    ]


def _chain_pre_hooks(
    tool_name: str,
    security_hooks: list[Callable],
    user_hooks: list[Callable] | None = None,
) -> Callable | None:
    user_hooks = user_hooks or []
    hooks = [*security_hooks, *user_hooks]
    if user_hooks:
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


def _attach_hooks(tool: Any, spec: AgentSpec, canonical_name: str) -> Any:
    security_hooks = _tool_hooks(spec, "security_pre", canonical_name)
    user_pre_hooks = _tool_hooks(spec, "pre", canonical_name)
    post_hooks = _tool_hooks(spec, "post", canonical_name)
    pre = _chain_pre_hooks(canonical_name, security_hooks, user_pre_hooks)
    post = _chain_post_hooks(canonical_name, post_hooks)
    if pre is not None:
        tool.preprocessor = pre
    if post is not None:
        tool.postprocessor = post
    return tool


def _register_tools(branch: Branch, spec: AgentSpec) -> None:
    for tool_spec in spec.tools:
        if tool_spec == "coding":
            _register_coding_tools(branch, spec)
        elif tool_spec == "reader":
            from lionagi.tools.file.reader import ReaderTool

            tool = _attach_hooks(ReaderTool().to_tool(), spec, "reader")
            branch.register_tools(tool)
        elif tool_spec == "editor":
            from lionagi.tools.file.editor import EditorTool

            tool = _attach_hooks(EditorTool().to_tool(), spec, "editor")
            branch.register_tools(tool)
        elif tool_spec == "bash":
            from lionagi.tools.code.bash import BashTool

            tool = _attach_hooks(BashTool().to_tool(), spec, "bash")
            branch.register_tools(tool)
        elif tool_spec == "search":
            from lionagi.tools.code.search import SearchTool

            tool = _attach_hooks(SearchTool().to_tool(), spec, "search")
            branch.register_tools(tool)


def _register_coding_tools(branch: Branch, spec: AgentSpec) -> None:
    from pathlib import Path

    from lionagi.tools.coding import CodingToolkit

    workspace_root = Path(spec.cwd) if spec.cwd else Path.cwd()
    toolkit = CodingToolkit(workspace_root=workspace_root)

    for key, handlers in spec.hook_handlers.items():
        parts = key.split(":", 1)
        if len(parts) != 2:
            continue
        phase, tool_name = parts
        for handler in handlers:
            if phase == "security_pre":
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
    spec: AgentSpec,
    *,
    trust_project_settings: bool = False,
) -> None:
    from pathlib import Path

    mcp_path = None

    if spec.mcp_config_path:
        p = Path(spec.mcp_config_path)
        if p.is_file():
            mcp_path = str(p)
    else:
        candidates = []
        cwd = Path(spec.cwd) if spec.cwd else Path.cwd()

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
        server_names=spec.mcp_servers,
    )
