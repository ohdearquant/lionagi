# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lionagi._errors import ConfigurationError
from lionagi.session.branch import Branch

from .spec import AgentSpec

__all__ = ("create_agent", "_chain_pre_hooks", "_chain_post_hooks")


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
    """Compose security controls and user pre-hooks into one preprocessor.

    Every security hook (an explicit PermissionPolicy's pre-hook, or a
    built-in guard such as guard_destructive/guard_paths) is adapted into a
    GateResult evaluator and run through the shared gate pass runner: each
    control evaluates exactly once per pass, and an evaluator that raises
    unexpectedly is treated as a fail-closed deny rather than propagating an
    unrelated exception (ADR-0086 delta row 1).

    When user pre-hooks are present, the security pass runs twice — once
    before the user hooks and once after, against the final, possibly
    mutated arguments — so a user hook cannot rewrite arguments past a
    control that already approved them. With no user pre-hooks it runs once.
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

    Shared by ``_load_mcp`` (island 1: lionagi-native ``branch.acts`` tools) and
    ``_forward_mcp_to_cli_request`` (island 2: the CLI-native request) so
    both islands agree on which file is authoritative and under what trust
    gate. An explicit ``spec.mcp_config_path`` always wins; otherwise a
    project-scoped ``.lionagi/.mcp.json`` / ``.mcp.json`` is only considered
    when ``trust_project_settings=True`` (mirrors the settings-loading trust
    gate), while the user-home ``~/.lionagi/.mcp.json`` candidate is trusted
    unconditionally, since it lives in the user's own home directory rather
    than an arbitrary project checkout.

    An explicit ``spec.mcp_config_path`` that does not resolve to an existing
    file raises ``ConfigurationError`` — the caller declared intent, so a
    missing/typo'd path is a configuration error, not a soft no-op. Only the
    auto-discovered candidates below fall through silently when none exist.
    """
    from pathlib import Path

    if spec.mcp_config_path is not None:
        # Presence check, not truthiness: an explicit empty string is still a
        # declared path and must fail loudly below, never fall through into
        # auto-discovery (which could silently load a different config).
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

    ``_load_mcp`` above only reaches island 1 (lionagi-native ``branch.acts``
    tools, consulted by API function-calling endpoints) — inert for CLI
    providers, which spawn their own subprocess and parse only that
    subprocess's own tool_use/tool_result chunks, never calling back into
    ``branch.acts``. This reaches island 2: the per-turn request kwargs a CLI
    provider builds (``ClaudeCodeRequest.mcp_servers``, which ``as_cmd_args()``
    turns into a literal ``--mcp-config`` flag for the ``claude`` CLI
    subprocess). Setting it onto the chat_model endpoint's ``config.kwargs``
    makes it land in every per-turn payload (``Endpoint.create_payload``
    starts from ``config.kwargs`` and merges the per-call request on top),
    the same mechanism already used for provider-specific extras like a
    placeholder ``api_key``.

    Why ``mcp_servers`` (dict) rather than ``mcp_config`` (path): although
    ``as_cmd_args()`` prefers ``mcp_config`` over ``mcp_servers`` when both
    are set, ``mcp_config``'s field validator
    (``claude_code.py`` ``_validate_path_fields`` -> ``check_path_safe``)
    unconditionally rejects absolute paths, and both resolved candidates
    here (``~/.lionagi/.mcp.json`` and a project ``.mcp.json`` found via
    parent-directory search) are absolute and not generally repo-relative —
    so setting ``mcp_config`` would raise a ValidationError on the very next
    turn for the common case. ``mcp_servers`` (a plain dict field) carries no
    such validator, so this always loads the resolved file and forwards the
    (optionally filtered) dict — never the path.

    Only ``claude_code`` has an MCP-capable request model today; other
    providers get a logged warning
    rather than a silent no-op — but only when there is actually something
    to forward (a resolvable config), so a caller can tell "no passthrough
    exists" apart from "nothing was configured" (mirrors ``_load_mcp``,
    which no-ops the same way for island 1 when nothing resolves).

    ``spec.mcp_servers`` set (even to an explicit empty list) is itself
    caller intent independent of whether any config file resolves: an
    explicit allowlist must be enforced regardless of config presence, so a
    ``claude_code`` leg with ``mcp_servers=[]`` and no resolvable
    ``.mcp.json`` anywhere still forwards ``{}`` (filtering nothing against
    an empty selection), forcing zero MCP servers rather than leaving the
    per-turn request untouched and letting the CLI fall back to its own MCP
    discovery.
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
        # provider != claude_code has no MCP-capable request model to forward
        # into at all — an explicit mcp_servers filter with no resolvable
        # config file has nothing to filter and nowhere to land, so this
        # stays a silent no-op (mirrors _load_mcp's own no-op shape).
        return

    if mcp_path is None:
        # claude_code + an explicit spec.mcp_servers filter (possibly empty)
        # but no config file resolves: there is nothing to read, so the
        # "servers available" set is empty — filtering it by the allowlist
        # is still {}, which is exactly the explicit-empty-allowlist case.
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
                # An explicitly configured path (as opposed to an auto-discovered
                # candidate) failing to read/parse is a configuration error, not
                # a soft "nothing to forward" — the caller declared intent.
                raise ConfigurationError(
                    f"spec.mcp_config_path={spec.mcp_config_path!r} could not be "
                    f"read or parsed as JSON: {type(exc).__name__}: {exc}"
                ) from exc
            return
        servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}

    if spec.mcp_servers is not None:
        # A server-name filter is set (possibly an explicit empty list): the
        # two islands must agree on selection (mirrors _load_mcp's
        # server_names semantics, where server_names=[] loads nothing).
        servers = {name: cfg for name, cfg in servers.items() if name in spec.mcp_servers}

    # Mutating config.kwargs in place would corrupt any OTHER branch sharing
    # this same iModel instance (Branch.__init__ keeps a caller-supplied
    # chat_model by reference, not by copy — two create_agent calls given the
    # same iModel would otherwise cross-contaminate each other's MCP server
    # filter through the shared config.kwargs dict). Give this branch its own
    # copy of the chat_model/endpoint/config before mutating so the change is
    # branch-local. share_session/share_executor keep the copy's CLI session
    # and the caller-supplied rate limiter/queue shared with the original —
    # only the endpoint config (and thus the MCP filter) is branch-local.
    branch.chat_model = branch.chat_model.copy(share_session=True, share_executor=True)
    branch.chat_model.endpoint.config.kwargs["mcp_servers"] = servers
