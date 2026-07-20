# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from lionagi._errors import ConfigurationError
from lionagi.ln.concurrency import is_coro_func
from lionagi.session.branch import Branch

from .spec import AgentSpec

__all__ = (
    "create_agent",
    "CREATE_AGENT_BRANCH_ORIGIN_KEY",
    "_chain_pre_hooks",
    "_chain_post_hooks",
)

# Stamped into `branch.metadata` for every Branch this factory produces. The
# key's presence (not the current invocation's profile) is the durable,
# immutable record that a branch's system message was composed via
# create_agent (role header + policy block) rather than a bare profile body.
# It round-trips through Branch.to_dict()/from_dict() with the rest of
# `metadata`, so a later resume/continue-last leg can consult the PERSISTED
# branch itself instead of re-deriving "was this create_agent-composed?"
# from whatever profile happens to be supplied on the resuming invocation
# (which may differ, or may have since dropped its `role:` key) — see
# lionagi/cli/agent.py `_run_agent`'s system-prompt reapply guard.
CREATE_AGENT_BRANCH_ORIGIN_KEY = "create_agent_origin"


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

        # Codex executes `-C <repo>`; the request model's repo defaults to the
        # calling process cwd, so an agent assigned a workspace via spec.cwd
        # would otherwise run against whatever directory the host process
        # happens to be in.
        if provider == "codex" and spec.cwd:
            extra["repo"] = spec.cwd

        chat_model = iModel(
            provider=provider,
            model=model_name,
            **extra,
        )
        branch_kwargs["chat_model"] = chat_model

    if log_config is not None:
        branch_kwargs["log_config"] = log_config

    branch = Branch(**branch_kwargs)
    # Immutable branch-origin marker (see CREATE_AGENT_BRANCH_ORIGIN_KEY):
    # stamped once, here, on every branch this factory builds; never read or
    # written anywhere else in the branch's lifetime.
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

    # ActionManager.load_mcp_config() no longer implies trust when its
    # `mcp_security` argument is omitted (ADR-0011 delta row 3) -- an
    # omitted policy now falls through to the wrapper's fail-closed
    # default instead. Reaching this point already required an explicit
    # trust act: either `spec.mcp_config_path` was set directly, or
    # `mcp_path` resolved from the operator's own home-level `.mcp.json`
    # (the same "global config is inherently trusted" precedent as
    # settings.yaml's always-loaded global file), or the caller opted into
    # `trust_project_settings=True` for a project-level file. This is the
    # one, explicit, documented compatibility decision for lionagi's own
    # MCP auto-load consumer -- not a silent default buried in the generic
    # library call.
    loaded = await branch.acts.load_mcp_config(
        mcp_path,
        server_names=spec.mcp_servers,
        mcp_security=MCPSecurityConfig.trusted(),
    )

    # Apply the same hook chain static tools get (security_pre/pre/post from
    # spec.hook_handlers, wired via _attach_hooks in _register_tools) to
    # MCP-discovered tools too -- they are registered after built-in tool
    # interception and would otherwise keep their bare default preprocessor
    # (ADR-0041 delta row 2). Reuses _attach_hooks, the same function static
    # registration uses, so both paths stay on one shared chain-application
    # path rather than a copied block.
    for tool_names in loaded.values():
        for tool_name in tool_names:
            tool = branch.acts.registry.get(tool_name)
            if tool is not None:
                _attach_hooks(tool, spec, tool_name)


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

    if provider not in ("claude_code", "codex"):
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

    # Keep the pre-filter server set: an explicit (possibly empty) allowlist
    # needs to know which discovered servers it excluded, not just which it
    # kept (see the codex disabling block below).
    resolved_servers = servers

    if spec.mcp_servers is not None:
        # Explicit filter set (possibly empty): mirrors _load_mcp's
        # server_names semantics, where an empty list loads nothing.
        servers = {name: cfg for name, cfg in servers.items() if name in spec.mcp_servers}

    # Copy chat_model before mutating config.kwargs: Branch keeps a
    # caller-supplied chat_model by reference, so mutating in place would
    # cross-contaminate other branches sharing the same iModel.
    branch.chat_model = branch.chat_model.copy(share_session=True, share_executor=True)
    if provider == "claude_code":
        branch.chat_model.endpoint.config.kwargs["mcp_servers"] = servers
        return

    # codex: the CLI takes no JSON MCP-config input; each server is forwarded
    # as `-c mcp_servers.<name>.<field>=<value>` config overrides, which the
    # request model already serializes onto the command line as TOML. Only
    # the fields the codex CLI's own McpServerConfig schema understands are
    # forwarded (verified against the installed codex CLI: `codex mcp list
    # --json` echoes back exactly this field set); a field outside that set
    # is a caller mistake, not a value to silently drop, so it's a loud
    # ConfigurationError. Server shapes lacking both `command` and `url`
    # aren't a real MCP server transport at all and are skipped outright.
    overrides = dict(branch.chat_model.endpoint.config.kwargs.get("config_overrides") or {})
    # `env` carries arbitrary secret-bearing values (API keys/tokens) and
    # must never land on argv (visible via `ps`, request logs, etc). Every
    # other supported field is a name, path, flag, or timeout -- safe to
    # pass as a `-c` override.
    secret_env: dict[str, dict] = {}
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
            if field_key == "env":
                secret_env[server_name] = value
            else:
                overrides[f"mcp_servers.{server_name}.{field_key}"] = value

    if spec.mcp_servers is not None:
        # Explicit allowlist (including an explicit empty one): every
        # discovered server it excluded must be actively disabled, or codex
        # keeps loading it from ambient/profile config regardless. codex has
        # no wholesale "clear mcp_servers" override -- `-c mcp_servers={}`
        # merges onto, rather than replaces, the existing table (verified
        # against the installed CLI) -- so each excluded server is disabled
        # by name instead.
        for excluded_name in resolved_servers:
            if excluded_name not in servers:
                overrides[f"mcp_servers.{excluded_name}.enabled"] = False

    if secret_env:
        _write_codex_mcp_env_profile(branch, secret_env)
    if overrides:
        branch.chat_model.endpoint.config.kwargs["config_overrides"] = overrides


# Fields the codex CLI's MCP server config schema accepts, verified against
# the installed `codex` CLI (`codex mcp list --json` output field names).
# `env` is handled separately -- see `_write_codex_mcp_env_profile`.
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


def _write_codex_mcp_env_profile(branch: Branch, secret_env: dict[str, dict]) -> None:
    """Route MCP server `env` maps (may carry secrets) to codex via a
    private, on-disk config profile instead of the `-c` command line.

    codex layers ``$CODEX_HOME/<name>.config.toml`` on top of its base
    config for any invocation given ``-p <name>`` (confirmed against the
    installed CLI: a `sandbox_mode` set only in such a profile file took
    effect on a real `codex exec` run). Writing the env map there, rather
    than putting it on argv, keeps it out of process listings (`ps`) and
    serialized request records.
    """
    import atexit
    import uuid
    from pathlib import Path

    import toml

    existing_profile = branch.chat_model.endpoint.config.kwargs.get("profile")
    if existing_profile:
        raise ConfigurationError(
            "Cannot forward MCP server `env` secrets for codex: the request "
            f"already has an explicit profile={existing_profile!r}, and codex "
            "accepts only one `-p` profile per invocation. Remove the "
            "explicit profile or drop `env` from the MCP server config."
        )

    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    codex_home.mkdir(parents=True, exist_ok=True)
    profile_name = f"lionagi-mcp-{uuid.uuid4().hex}"
    profile_path = codex_home / f"{profile_name}.config.toml"

    profile_doc = {"mcp_servers": {name: {"env": env} for name, env in secret_env.items()}}
    profile_path.write_text(toml.dumps(profile_doc))
    os.chmod(profile_path, 0o600)
    atexit.register(lambda: profile_path.unlink(missing_ok=True))

    branch.chat_model.endpoint.config.kwargs["profile"] = profile_name
