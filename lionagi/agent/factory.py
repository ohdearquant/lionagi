# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from lionagi._errors import ConfigurationError
from lionagi.session.branch import Branch

from .spec import AgentSpec

__all__ = ("create_agent", "_chain_pre_hooks", "_chain_post_hooks")

logger = logging.getLogger(__name__)


async def create_agent(
    config: AgentSpec,
    *,
    load_settings: bool = True,
    project_dir: str | None = None,
    trust_project_settings: bool = False,
    trusted_hook_modules: set[str] | frozenset[str] | None = None,
    chat_model: Any = None,
    log_config: Any = None,
) -> Branch:
    """Create a fully wired Branch from AgentSpec: settings → hooks → prompt → model → tools."""
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

    if chat_model is not None:
        branch_kwargs["chat_model"] = chat_model
    elif spec.model:
        from lionagi.service.providers import (
            CLI_PROVIDERS,
            PROVIDER_EFFORT_KWARG,
            PROVIDER_YOLO_KWARGS,
            parse_model_spec,
        )

        ms = parse_model_spec(spec.model)
        if "/" in ms.model:
            provider, model_name = ms.model.split("/", 1)
        else:
            from lionagi.config import settings

            provider = settings.LIONAGI_CHAT_PROVIDER
            model_name = ms.model

        extra = {}
        effort = spec.effort or ms.effort
        if effort:
            kwarg = PROVIDER_EFFORT_KWARG.get(provider)
            if kwarg:
                extra[kwarg] = effort
        if spec.yolo:
            extra.update(PROVIDER_YOLO_KWARGS.get(provider, {}))

        # CLI providers auth via subprocess, so a placeholder api_key is fine;
        # API providers need their real key or auth silently breaks.
        if provider in CLI_PROVIDERS:
            extra["api_key"] = "dummy"

        chat_model = iModel(
            provider=provider,
            model=model_name,
            **extra,
        )
        branch_kwargs["chat_model"] = chat_model

    if log_config is not None:
        branch_kwargs["log_config"] = log_config

    branch = Branch(**branch_kwargs)

    system_message = spec.build_system_message()
    if "coding" in spec.tools and getattr(spec, "context_management", True):
        one_liner = (
            "You can curate your own context with the context tool "
            "(status/evict/compact/restore); guidance arrives when relevant."
        )
        system_message = f"{system_message}\n\n{one_liner}" if system_message else one_liner
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
    _forward_mcp_to_cli_request(branch, spec, trust_project_settings=trust_project_settings)
    _wire_external_hooks(branch, spec)

    if op := spec.emission_operable():
        branch.grant_capabilities(op)

    return branch


def _wire_external_hooks(branch: Branch, spec: AgentSpec) -> None:
    """Attach ``hooks_external`` entries (parsed by ``apply_hooks_from_settings``)
    to the seam their event maps to.

    ``PreToolUse``/``PostToolUse`` attach to ``branch.acts`` (always present).
    The remaining supported events attach to ``branch._hooks`` (a ``HookBus``)
    -- present only once the branch is owned by a ``Session``; a standalone
    branch built via ``create_agent`` has none yet, so those entries are
    skipped with a warning rather than raising, matching the existing
    no-hooks-bus-attached behavior elsewhere in the runtime.
    """
    if not spec.external_hooks:
        return

    from lionagi.hooks.bus import HookPoint
    from lionagi.hooks.external import external_hook_adapter

    session_id = str(branch._owning_session_id or branch.id)
    event_to_point = {
        "SessionStart": HookPoint.SESSION_START,
        "SessionEnd": HookPoint.SESSION_END,
        "UserPromptSubmit": HookPoint.USER_PROMPT_SUBMIT,
        "PostToolUseFailure": HookPoint.TOOL_ERROR,
    }

    for entry in spec.external_hooks:
        handler = external_hook_adapter(
            event=entry["event"],
            command=entry["command"],
            timeout=entry["timeout"],
            matcher=entry.get("matcher"),
            source=entry.get("source"),
            cwd=spec.cwd,
            session_id=session_id,
        )
        if entry["event"] == "PreToolUse":
            branch.acts.add_tool_pre_hook(handler)
        elif entry["event"] == "PostToolUse":
            branch.acts.add_tool_post_hook(handler)
        elif branch._hooks is not None:
            branch._hooks.on(event_to_point[entry["event"]], handler)
        else:
            logger.warning(
                "hooks_external: %r configured but this branch has no HookBus "
                "attached (not part of a Session yet) -- skipping until it is",
                entry["event"],
            )


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
    """Compose security controls and user pre-hooks into one preprocessor.

    With user pre-hooks present, the security pass runs twice (before and
    after) so a user hook cannot rewrite args past an already-approved
    control. See docs/internals/runtime.md.
    """
    from .gate import GateDeniedError, adapt_legacy_hook, run_gate_pass

    user_hooks = user_hooks or []
    if not security_hooks and not user_hooks:
        return None

    evaluators = [
        adapt_legacy_hook(getattr(hook, "__name__", "security_control"), hook)
        for hook in security_hooks
    ]

    async def chained(args: dict, **_kw) -> dict:
        action = args.get("action", "")
        args, deny = await run_gate_pass(evaluators, tool_name, action, args)
        if deny is not None:
            raise GateDeniedError(deny)

        for handler in user_hooks:
            result = await handler(tool_name, args.get("action", ""), args)
            if isinstance(result, dict):
                args = result

        if user_hooks and evaluators:
            args, deny = await run_gate_pass(
                evaluators, tool_name, args.get("action", action), args
            )
            if deny is not None:
                raise GateDeniedError(deny)

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
            from pathlib import Path

            from lionagi.tools.code.search import SearchTool

            workspace_root = str(Path(spec.cwd) if spec.cwd else Path.cwd())
            tool = _attach_hooks(
                SearchTool(workspace_root=workspace_root).to_tool(), spec, "search"
            )
            branch.register_tools(tool)


def _register_coding_tools(branch: Branch, spec: AgentSpec) -> None:
    from pathlib import Path

    from lionagi.tools.coding import DEFAULT_CODING_TOOLS, CodingToolkit

    workspace_root = Path(spec.cwd) if spec.cwd else Path.cwd()
    context_management = getattr(spec, "context_management", True)
    tools = None if context_management else tuple(t for t in DEFAULT_CODING_TOOLS if t != "context")
    toolkit = CodingToolkit(workspace_root=workspace_root, tools=tools)

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


def _resolve_mcp_path(spec: AgentSpec, *, trust_project_settings: bool = False) -> str | None:
    """Resolve the ``.mcp.json`` path an AgentSpec's MCP fields point at.

    Shared by ``_load_mcp`` and ``_forward_mcp_to_cli_request`` so both agree
    on the authoritative file and trust gate. See docs/internals/runtime.md.
    """
    from pathlib import Path

    if spec.mcp_config_path is not None:
        # Presence check, not truthiness: an explicit empty string must fail
        # loudly below, never fall through into auto-discovery.
        p = Path(spec.mcp_config_path)
        if p.is_file():
            return str(p)
        import logging

        logging.getLogger(__name__).warning(
            "spec.mcp_config_path=%r does not resolve to an existing file",
            spec.mcp_config_path,
        )
        raise ConfigurationError(
            f"spec.mcp_config_path={spec.mcp_config_path!r} does not resolve to an existing file"
        )

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
            return str(candidate)
    return None


async def _load_mcp(
    branch: Branch,
    spec: AgentSpec,
    *,
    trust_project_settings: bool = False,
) -> None:
    mcp_path = _resolve_mcp_path(spec, trust_project_settings=trust_project_settings)
    if mcp_path is None:
        return

    await branch.acts.load_mcp_config(
        mcp_path,
        server_names=spec.mcp_servers,
    )


def _forward_mcp_to_cli_request(
    branch: Branch,
    spec: AgentSpec,
    *,
    trust_project_settings: bool = False,
) -> None:
    """Forward AgentSpec MCP fields into the claude_code CLI's own request.

    ``_load_mcp`` only reaches lionagi-native ``branch.acts`` tools (inert for
    CLI providers); this reaches the per-turn request kwargs a CLI provider
    subprocess actually reads. See docs/internals/runtime.md.
    """
    mcp_path = _resolve_mcp_path(spec, trust_project_settings=trust_project_settings)
    if mcp_path is None and spec.mcp_servers is None:
        # Nothing configured at all: no config file resolves and no explicit
        # server-name filter was set. Nothing to forward.
        return

    provider = getattr(branch.chat_model.endpoint.config, "provider", None)

    if provider != "claude_code":
        if mcp_path is not None:
            import logging

            logging.getLogger(__name__).warning(
                "MCP config present in AgentSpec but the active provider (%s) has "
                "no MCP passthrough; MCP servers will not be reachable for this "
                "run.",
                provider,
            )
        # No MCP-capable request model for this provider to forward into,
        # so this stays a silent no-op (mirrors _load_mcp's own shape).
        return

    if mcp_path is None:
        # No config file resolves: the available-servers set is empty, which
        # matches the explicit-empty-allowlist case.
        servers: dict = {}
    else:
        import json
        from pathlib import Path

        try:
            data = json.loads(Path(mcp_path).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            import logging

            logging.getLogger(__name__).warning(
                "Could not read/parse MCP config %r for forwarding to the "
                "claude_code CLI request (%s: %s); MCP servers will not be "
                "reachable for this run.",
                mcp_path,
                type(exc).__name__,
                exc,
            )
            if spec.mcp_config_path:
                # An explicitly configured path failing to parse is a
                # configuration error, not a soft no-op.
                raise ConfigurationError(
                    f"spec.mcp_config_path={spec.mcp_config_path!r} could not be "
                    f"read or parsed as JSON: {type(exc).__name__}: {exc}"
                ) from exc
            return
        servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}

    if spec.mcp_servers is not None:
        # Explicit filter set (possibly empty): mirrors _load_mcp's
        # server_names semantics, where an empty list loads nothing.
        servers = {name: cfg for name, cfg in servers.items() if name in spec.mcp_servers}

    # Copy chat_model before mutating config.kwargs: Branch keeps a
    # caller-supplied chat_model by reference, so mutating in place would
    # cross-contaminate other branches sharing the same iModel.
    branch.chat_model = branch.chat_model.copy(share_session=True, share_executor=True)
    branch.chat_model.endpoint.config.kwargs["mcp_servers"] = servers
