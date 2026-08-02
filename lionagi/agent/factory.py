# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Collection
from typing import TYPE_CHECKING, Any

from lionagi._errors import ConfigurationError
from lionagi.ln import Unset
from lionagi.ln.concurrency import is_coro_func
from lionagi.ln.types import UnsetType
from lionagi.service.providers import _CLAUDE_PROVIDER_NAMES
from lionagi.session.branch import Branch

from .spec import AgentSpec

if TYPE_CHECKING:
    from pathlib import Path

__all__ = (
    "create_agent",
    "CREATE_AGENT_BRANCH_ORIGIN_KEY",
    "_chain_pre_hooks",
    "_chain_post_hooks",
)

# Durable `branch.metadata` marker: a resumed leg checks this key on the
# persisted branch rather than re-deriving it from the resuming profile.
# See docs/internals/agent-runtime.md#create-agent-branch-origin
CREATE_AGENT_BRANCH_ORIGIN_KEY = "create_agent_origin"

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
    resolved_mcp_servers: dict[str, Any] | None | UnsetType = Unset,
    resolved_mcp_explicit: bool = False,
) -> Branch:
    """Create a fully wired Branch from AgentSpec: settings → hooks → prompt → model → tools.

    ``resolved_mcp_servers`` is for a caller that already read a server set of
    its own — it is handed to the CLI request verbatim and suppresses the
    discovery this factory would otherwise do from the agent's own working
    directory. ``None`` means "hand nothing over"; leaving it unset keeps the
    discovery behaviour. ``resolved_mcp_explicit`` says that set was named by
    the caller rather than found near its launch directory.
    """
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
            PROVIDER_REPO_KWARG,
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

        # CLI providers default their repo/workspace field to the calling
        # process cwd, so spec.cwd must be forwarded explicitly.
        if spec.cwd:
            repo_kwarg = PROVIDER_REPO_KWARG.get(provider)
            if repo_kwarg:
                extra[repo_kwarg] = spec.cwd

        chat_model = iModel(
            provider=provider,
            model=model_name,
            **extra,
        )
        branch_kwargs["chat_model"] = chat_model

    if log_config is not None:
        branch_kwargs["log_config"] = log_config

    branch = Branch(**branch_kwargs)
    branch.metadata[CREATE_AGENT_BRANCH_ORIGIN_KEY] = True

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
    _register_providers(branch, spec)
    await _load_mcp(branch, spec, trust_project_settings=trust_project_settings)
    _forward_mcp_to_cli_request(
        branch,
        spec,
        trust_project_settings=trust_project_settings,
        resolved_servers=resolved_mcp_servers,
        resolved_servers_explicit=resolved_mcp_explicit,
    )
    _wire_external_hooks(branch, spec)

    if op := spec.emission_operable():
        branch.grant_capabilities(op)

    return branch


def _wire_external_hooks(branch: Branch, spec: AgentSpec) -> None:
    """Attach ``hooks_external`` entries to the seam their event maps to.

    See docs/internals/agent-runtime.md#external-hook-wiring for the
    attach/queue/reparent contract.
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
        else:
            branch._pending_hook_bus_entries.append((event_to_point[entry["event"]], handler))
            if branch._hooks is not None:
                branch.attach_hook_bus(branch._hooks)
            else:
                logger.debug(
                    "hooks_external: %r configured on a branch with no HookBus "
                    "attached yet (not part of a Session yet) -- queued for "
                    "attachment once it joins one",
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


def _compose_preprocessor(original: Callable | None, new: Callable) -> Callable:
    """Compose a spec-derived preprocessor in front of a tool's existing one.

    Ordering keeps the security recheck closest to the actual invocation:
    the tool's own preprocessor (if any) runs first, then the spec chain.
    """
    if original is None:
        return new

    async def composed(args: dict, **kw) -> Any:
        args = await original(args, **kw) if is_coro_func(original) else original(args, **kw)
        return await new(args, **kw) if is_coro_func(new) else new(args, **kw)

    return composed


def _compose_postprocessor(original: Callable | None, new: Callable) -> Callable:
    """Compose a spec-derived postprocessor around a tool's existing one.

    Ordering mirrors `_compose_preprocessor`: the spec chain runs immediately
    after the tool call (closest to invocation), then the tool's own
    postprocessor (if any) runs last.
    """
    if original is None:
        return new

    async def composed(result: Any, **kw) -> Any:
        result = await new(result, **kw) if is_coro_func(new) else new(result, **kw)
        return await original(result, **kw) if is_coro_func(original) else original(result, **kw)

    return composed


def _attach_hooks(tool: Any, spec: AgentSpec, canonical_name: str) -> Any:
    security_hooks = _tool_hooks(spec, "security_pre", canonical_name)
    user_pre_hooks = _tool_hooks(spec, "pre", canonical_name)
    post_hooks = _tool_hooks(spec, "post", canonical_name)
    pre = _chain_pre_hooks(canonical_name, security_hooks, user_pre_hooks)
    post = _chain_post_hooks(canonical_name, post_hooks)
    if pre is not None:
        tool.preprocessor = _compose_preprocessor(tool.preprocessor, pre)
    if post is not None:
        tool.postprocessor = _compose_postprocessor(tool.postprocessor, post)
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


def _register_providers(branch: Branch, spec: AgentSpec) -> None:
    # LIONAGI_KHIVE_INJECTION is the fleet-wide injection kill-switch.
    env_setting = os.getenv("LIONAGI_KHIVE_INJECTION")
    if env_setting is not None and env_setting.strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return

    configured = spec.khive_injection
    # Only None/False disable — an empty mapping is a valid opt-in that must
    # still receive the fleet defaults (derived profile_id + writeback on).
    if configured is None or configured is False:
        return

    from lionagi.tools.khive_injection import (
        ComposePolicy,
        KhiveInjectionPolicy,
        KhiveInjectionProvider,
        RecallPolicy,
        WritebackPolicy,
    )

    # Provider construction can fail on a bad policy (e.g. an unsupported
    # snapshot_id) — that must degrade to "no injection this turn", matching
    # KhiveInjectionProvider.provide()'s own transport-failure fail-open, not
    # abort the whole agent/run.
    try:
        if isinstance(configured, KhiveInjectionPolicy):
            policy = configured
        elif isinstance(configured, dict):
            policy_kwargs = dict(configured)
            nested_policy_types = {
                "recall": RecallPolicy,
                "compose": ComposePolicy,
                "writeback": WritebackPolicy,
            }
            for field_name, policy_type in nested_policy_types.items():
                value = policy_kwargs.get(field_name)
                if isinstance(value, dict):
                    policy_kwargs[field_name] = policy_type(**value)
            defaults = {}
            if "profile_id" not in policy_kwargs:
                defaults["profile_id"] = f"{spec.profile.role.name}-recall-v1"
            if "writeback" not in policy_kwargs:
                defaults["writeback"] = WritebackPolicy(enabled=True)
            policy = KhiveInjectionPolicy(**{**defaults, **policy_kwargs})
        elif configured is True:
            policy = KhiveInjectionPolicy(
                profile_id=f"{spec.profile.role.name}-recall-v1",
                writeback=WritebackPolicy(enabled=True),
            )
        else:
            raise TypeError(
                "khive_injection must be None, a bool, a mapping, or a KhiveInjectionPolicy"
            )
        provider = KhiveInjectionProvider(policy)
    except Exception as exc:
        import logging

        logging.getLogger(__name__).warning(
            "khive injection provider construction failed (%s: %s); continuing "
            "without context injection for this agent",
            type(exc).__name__,
            exc,
        )
        return

    branch.providers.register(provider)


def register_profile_injection(branch: Branch, role_name: str, profile: Any) -> None:
    """Register a CLI agent profile's khive injection provider onto ``branch``.

    Shared by the orchestrate path and the bare ``li agent`` path so both honor a
    profile's ``khive_injection`` opt-in without routing through the coding preset —
    injection is a context-provider concern, orthogonal to CodingToolkit/path-guards.
    The provider is keyed on ``{role_name}-recall-v1`` (role_name is the invoked
    profile name). None/False disables; the env kill-switch is honored by
    ``_register_providers``.
    """
    configured = getattr(profile, "khive_injection", None)
    # Only None/False disable — an empty mapping is a valid opt-in (see _register_providers).
    if configured is None or configured is False:
        return

    from lionagi.casts.pattern import Role
    from lionagi.casts.profile import Profile

    identity = Profile(name=role_name, role=Role(name=role_name, description="", body=""))
    provider_spec = AgentSpec(profile=identity, pack=None, khive_injection=configured)
    _register_providers(branch, provider_spec)


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

    from lionagi.service.connections.mcp_wrapper import MCPSecurityConfig

    # Reaching this point already required an explicit trust act (explicit
    # mcp_config_path, a home-level .mcp.json, or trust_project_settings),
    # so trusted() is safe here even though load_mcp_config's own default is
    # fail-closed. See docs/internals/agent-runtime.md#mcp-trust-decision.
    loaded = await branch.acts.load_mcp_config(
        mcp_path,
        server_names=spec.mcp_servers,
        mcp_security=MCPSecurityConfig.trusted(),
    )

    # MCP-discovered tools register after static ones and would otherwise
    # keep their bare default pre/postprocessor; reuse _attach_hooks so both
    # paths apply the same spec-derived hook chain.
    for tool_names in loaded.values():
        for tool_name in tool_names:
            tool = branch.acts.registry.get(tool_name)
            if tool is not None:
                _attach_hooks(tool, spec, tool_name)


_MCP_FORWARDING_PROVIDERS = frozenset({*_CLAUDE_PROVIDER_NAMES, "codex"})
_ANTIGRAVITY_PROVIDER_NAMES = frozenset({"gemini-cli", "gemini_cli", "gemini-code", "gemini_code"})


def _canonical_provider(provider: str | None) -> str:
    """Fold a provider string the way endpoint resolution does, so a spelling that
    resolves to an endpoint is never one these predicates fail to recognise."""
    return provider.strip().lower() if isinstance(provider, str) else ""


def provider_accepts_forwarded_mcp(provider: str | None) -> bool:
    """Whether a provider's request can carry an MCP server set resolved by the caller.

    Answers capability, not whether a given spawn actually forwarded one —
    for that, use ``request_carries_forwarded_mcp``.
    """
    return _canonical_provider(provider) in _MCP_FORWARDING_PROVIDERS


def _reject_unforwardable_explicit_mcp(
    provider: str | None,
    *,
    named_explicitly: bool,
    asked_for_servers: bool,
) -> None:
    """Reject an explicit MCP server set that Antigravity cannot receive."""
    if (
        named_explicitly
        and asked_for_servers
        and _canonical_provider(provider) in _ANTIGRAVITY_PROVIDER_NAMES
    ):
        raise ConfigurationError(
            f"The {provider!r} provider runs the Antigravity CLI (`agy`), "
            "which does not support MCP servers; the explicitly supplied "
            "MCP configuration cannot be forwarded."
        )


def request_kwargs_carry_forwarded_mcp(kwargs: dict[str, Any] | None) -> bool:
    """Whether CLI request kwargs actually carry a forwarded MCP server set.

    Read counterpart of ``apply_forwarded_mcp_servers``. Codex entries that
    only disable a server by name don't count as a forwarded set.
    """
    kwargs = kwargs or {}
    if kwargs.get("mcp_servers"):
        return True
    overrides = kwargs.get("config_overrides") or {}
    return any(
        key.startswith("mcp_servers.") and not (key.endswith(".enabled") and value is False)
        for key, value in overrides.items()
    )


def request_carries_forwarded_mcp(branch: Branch) -> bool:
    """``request_kwargs_carry_forwarded_mcp`` for a branch's built CLI request."""
    config = getattr(getattr(branch.chat_model, "endpoint", None), "config", None)
    return request_kwargs_carry_forwarded_mcp(getattr(config, "kwargs", None))


def apply_forwarded_mcp_servers(
    kwargs: dict[str, Any],
    servers: dict[str, Any] | None,
    *,
    provider: str | None,
    exclusive: bool = False,
    allowed_names: Collection[str] | None = None,
    known_server_names: Collection[str] = (),
) -> bool:
    """Write a resolved MCP server set into a CLI request's kwargs.

    Returns whether *servers* reached the request (False: provider has no
    transport for a caller-resolved set). See
    docs/internals/agent-runtime.md#mcp-server-forwarding for what
    *exclusive*/*allowed_names*/*known_server_names* mean.
    """
    if servers is None:
        return False
    if not provider_accepts_forwarded_mcp(provider):
        return False

    if provider in _CLAUDE_PROVIDER_NAMES:
        kwargs["mcp_servers"] = servers
        if exclusive:
            # The handed set alone is merged with whatever the CLI discovers
            # for itself. The strict flag is what makes it the entire set.
            kwargs["strict_mcp_config"] = True
        return True

    # codex takes no JSON MCP-config input; each server is forwarded as
    # `-c mcp_servers.<name>.<field>=<value>` overrides. A field outside the
    # McpServerConfig schema is a caller mistake (loud ConfigurationError),
    # not a value to silently drop. See docs/internals/agent-runtime.md#mcp-server-forwarding.
    overrides = dict(kwargs.get("config_overrides") or {})
    # `env`/`http_headers` may carry secrets and must never land on argv, so
    # they route to the on-disk profile instead of a `-c` override.
    secret_fields: dict[str, dict[str, Any]] = {}
    for server_name, server_cfg in servers.items():
        if not isinstance(server_cfg, dict) or not ("command" in server_cfg or "url" in server_cfg):
            continue
        unsupported = [k for k in server_cfg if k not in _CODEX_MCP_SERVER_FIELDS]
        if unsupported:
            raise ConfigurationError(
                f"MCP server {server_name!r} sets field(s) {unsupported!r} that "
                "the codex CLI's `-c mcp_servers.<name>.<field>` passthrough "
                f"does not support. Supported fields: {sorted(_CODEX_MCP_SERVER_FIELDS)!r}."
            )
        for field_key in _CODEX_MCP_SERVER_FIELDS:
            value = server_cfg.get(field_key)
            if value is None:
                continue
            if field_key in _SECRET_CODEX_MCP_FIELDS:
                secret_fields.setdefault(server_name, {})[field_key] = value
            else:
                overrides[f"mcp_servers.{server_name}.{field_key}"] = value

    if exclusive:
        # codex has no wholesale "clear mcp_servers" override, so every
        # server left out of the caller's set (including ambient ones codex
        # would load on its own) is disabled by name instead.
        allowed = set(servers) if allowed_names is None else set(allowed_names)
        discovered = set(known_server_names) | _discover_ambient_codex_mcp_server_names()
        for excluded_name in sorted(discovered - allowed):
            overrides[f"mcp_servers.{excluded_name}.enabled"] = False

    if secret_fields:
        _write_codex_mcp_secret_profile(kwargs, secret_fields)
    if overrides:
        kwargs["config_overrides"] = overrides
    return True


def _forward_mcp_to_cli_request(
    branch: Branch,
    spec: AgentSpec,
    *,
    trust_project_settings: bool = False,
    resolved_servers: dict[str, Any] | None | UnsetType = Unset,
    resolved_servers_explicit: bool = False,
) -> None:
    """Forward an MCP server set into the CLI provider's own request.

    Unlike ``_load_mcp`` (lionagi-native tools, inert for CLI providers),
    this reaches the per-turn request kwargs a CLI provider subprocess
    actually reads. With ``resolved_servers`` given, no config file is
    looked for — that set is handed over as-is. See
    docs/internals/agent-runtime.md#mcp-server-forwarding.
    """
    caller_resolved = resolved_servers is not Unset
    mcp_path = (
        None
        if caller_resolved
        else _resolve_mcp_path(spec, trust_project_settings=trust_project_settings)
    )
    has_config = resolved_servers is not None if caller_resolved else mcp_path is not None
    if not has_config and spec.mcp_servers is None:
        return

    provider = getattr(branch.chat_model.endpoint.config, "provider", None)

    if not provider_accepts_forwarded_mcp(provider):
        # Refusing is only right when someone asked for these servers by name;
        # a merely-discovered or empty set can't turn a spawn into a failure.
        named_explicitly = bool(spec.mcp_config_path) or resolved_servers_explicit
        asked_for_servers = bool(resolved_servers) if caller_resolved else has_config
        _reject_unforwardable_explicit_mcp(
            provider,
            named_explicitly=named_explicitly,
            asked_for_servers=asked_for_servers,
        )
        if has_config:
            import logging

            # Scope the claim to what this call can see: one branch, whose
            # provider was resolved from this one spec. Sibling branches in the
            logging.getLogger(__name__).warning(
                "MCP config present in AgentSpec but the active provider (%s) has "
                "no MCP passthrough; MCP servers will not be reachable for this "
                "branch (role %s, branch %s). Other branches resolve their own "
                "providers and are not covered by this warning.",
                provider,
                spec.profile.role.name,
                branch.id,
            )
        # No MCP-capable request model for this provider to forward into,
        # so this stays a silent no-op (mirrors _load_mcp's own shape).
        return

    if caller_resolved:
        servers: dict = dict(resolved_servers or {})
    elif mcp_path is None:
        servers = {}
    else:
        import json
        from pathlib import Path

        try:
            data = json.loads(Path(mcp_path).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            import logging

            logging.getLogger(__name__).warning(
                "Could not read/parse MCP config %r for forwarding to the %s "
                "CLI request (%s: %s); MCP servers will not be reachable for "
                "this branch (role %s, branch %s).",
                mcp_path,
                provider,
                type(exc).__name__,
                exc,
                spec.profile.role.name,
                branch.id,
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

    # Keep the pre-filter set: an explicit allowlist needs to know which
    # discovered servers it excluded, not just which it kept.
    available_servers = servers

    if spec.mcp_servers is not None:
        servers = {name: cfg for name, cfg in servers.items() if name in spec.mcp_servers}

    # Branch keeps a caller-supplied chat_model by reference; copy before
    # mutating config.kwargs to avoid cross-contaminating other branches.
    branch.chat_model = branch.chat_model.copy(share_session=True, share_executor=True)
    apply_forwarded_mcp_servers(
        branch.chat_model.endpoint.config.kwargs,
        servers,
        provider=provider,
        exclusive=spec.mcp_servers is not None,
        allowed_names=spec.mcp_servers,
        known_server_names=tuple(available_servers),
    )


# Fields the codex CLI's MCP server config schema accepts, verified against
# the installed `codex` CLI (`codex mcp list --json` output field names).
# `env` and `http_headers` are handled separately -- see
# `_write_codex_mcp_secret_profile`.
_CODEX_MCP_SERVER_FIELDS = frozenset(
    {
        "command",
        "args",
        "env",
        "url",
        "cwd",
        "env_vars",
        "startup_timeout_ms",
        "enabled",
        "required",
        "bearer_token_env_var",
        "http_headers",
        "env_http_headers",
    }
)

# Fields whose values may themselves be secrets (API keys, tokens, a static
# `Authorization: Bearer ...` header) rather than names/paths/flags -- routed
# to the on-disk profile file instead of the `-c` command line.
_SECRET_CODEX_MCP_FIELDS = frozenset({"env", "http_headers"})


def _discover_ambient_codex_mcp_server_names() -> set[str]:
    """Discover names of ambient/profile-configured codex MCP servers codex
    would load on its own, so an explicit allowlist can disable them too.

    Raises ``ConfigurationError`` if discovery fails entirely — an allowlist
    that can't be enforced must fail closed, not silently pass servers
    through. See docs/internals/agent-runtime.md#mcp-server-forwarding.
    """
    import json
    import subprocess
    from pathlib import Path

    import toml

    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    config_path = codex_home / "config.toml"
    try:
        data = toml.loads(config_path.read_text())
    except (OSError, toml.TomlDecodeError):
        pass
    else:
        mcp_servers = data.get("mcp_servers") if isinstance(data, dict) else None
        if isinstance(mcp_servers, dict):
            return set(mcp_servers)
        return set()

    try:
        result = subprocess.run(
            ["codex", "mcp", "list", "--json"],  # noqa: S607 — relies on PATH resolution for "codex", intentional
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        listed = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise ConfigurationError(
            "Cannot enforce the explicit codex MCP server allowlist: failed "
            f"to discover ambient/profile-configured servers via both "
            f"{str(config_path)!r} and `codex mcp list --json` "
            f"({type(exc).__name__}: {exc}). An allowlist that cannot be "
            "enforced must fail closed rather than silently leaving "
            "unlisted servers enabled."
        ) from exc

    if not isinstance(listed, list):
        raise ConfigurationError(
            "Cannot enforce the explicit codex MCP server allowlist: "
            f"`codex mcp list --json` returned unexpected output "
            f"(expected a JSON array, got {type(listed).__name__})."
        )
    return {entry["name"] for entry in listed if isinstance(entry, dict) and "name" in entry}


# Fixed prefix + 32 hex chars marks a profile as lionagi-generated (the name
# has to carry this since a resumed leg's profile was written by a different
# process). A caller name in this exact shape is treated as ours and
# replaced; anything else is the caller's and refused, never overwritten.
_CODEX_MCP_PROFILE_PREFIX = "lionagi-mcp-"
_GENERATED_CODEX_PROFILE_RE = re.compile(rf"^{re.escape(_CODEX_MCP_PROFILE_PREFIX)}[0-9a-f]{{32}}$")


def _is_generated_codex_profile(name: object) -> bool:
    """Whether *name* is a profile name `_write_codex_mcp_secret_profile` minted."""
    return isinstance(name, str) and _GENERATED_CODEX_PROFILE_RE.fullmatch(name) is not None


# Profile files older than this are considered abandoned (the process that
# wrote them was killed before its `atexit` cleanup ran, e.g. SIGKILL/crash)
# and safe to reap on the next write.
_STALE_PROFILE_MAX_AGE_SECONDS = 24 * 60 * 60


def _reap_stale_codex_mcp_profiles(codex_home: Path) -> None:
    """Delete generated profile files older than 24h.

    `_write_codex_mcp_secret_profile`'s own cleanup is `atexit`-based, so a
    killed-not-terminated process (SIGKILL, crash) leaves its profile file
    on disk indefinitely. Reaping stale files on the next write bounds how
    long an abandoned credential file can sit under `$CODEX_HOME`.
    """
    import time

    cutoff = time.time() - _STALE_PROFILE_MAX_AGE_SECONDS
    for stale in codex_home.glob(f"{_CODEX_MCP_PROFILE_PREFIX}*.config.toml"):
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink(missing_ok=True)
        except OSError:
            continue


def _write_codex_mcp_secret_profile(
    kwargs: dict[str, Any], secret_fields: dict[str, dict[str, Any]]
) -> None:
    """Route MCP server fields that may carry secrets (`env`, `http_headers`)
    to codex via a private, on-disk config profile (`-p <name>`) instead of
    the `-c` command line, keeping them out of `ps` and request logs.
    """
    import atexit
    import uuid
    from pathlib import Path

    import toml

    # Only a caller-named profile is a conflict; a profile of our own
    # generated shape (from a resumed leg's persisted request) is a spent
    # one to replace, not a second profile to refuse.
    existing_profile = kwargs.get("profile")
    if existing_profile and not _is_generated_codex_profile(existing_profile):
        raise ConfigurationError(
            "Cannot forward MCP server secret fields for codex: the request "
            f"already has an explicit profile={existing_profile!r}, and codex "
            "accepts only one `-p` profile per invocation. Remove the "
            "explicit profile or drop `env`/`http_headers` from the MCP "
            "server config."
        )

    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    codex_home.mkdir(parents=True, exist_ok=True)
    _reap_stale_codex_mcp_profiles(codex_home)
    profile_name = f"{_CODEX_MCP_PROFILE_PREFIX}{uuid.uuid4().hex}"
    profile_path = codex_home / f"{profile_name}.config.toml"

    profile_doc = {"mcp_servers": {name: dict(fields) for name, fields in secret_fields.items()}}
    # The profile carries secrets: create it 0600 from the first byte instead
    # of write-then-chmod, which leaves a umask-permission window where the
    # contents are readable (and a wrong-mode file if the write is interrupted).
    fd = os.open(profile_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(toml.dumps(profile_doc))
    atexit.register(lambda: profile_path.unlink(missing_ok=True))

    kwargs["profile"] = profile_name
