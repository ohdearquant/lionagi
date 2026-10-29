# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lionagi._errors import ConfigurationError
from lionagi.ln import Unset
from lionagi.ln.concurrency import is_coro_func
from lionagi.ln.types import UnsetType
from lionagi.providers.openai._codex_profile import (
    CODEX_MCP_PROFILE_PREFIX as _CODEX_MCP_PROFILE_PREFIX,
)
from lionagi.providers.openai._codex_profile import (
    codex_home_for_env as _codex_home_for_env,
)
from lionagi.providers.openai._codex_profile import (
    is_generated_codex_profile as _is_generated_codex_profile,
)
from lionagi.providers.openai._codex_profile import (
    refuse_non_absolute_codex_home as _refuse_non_absolute_codex_home,
)
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

# Providers whose endpoint takes ``spec.max_tokens`` as its output-token limit, each with a
# test that reads the limit off the payload the endpoint would send.
_MAX_TOKENS_PROVIDERS = frozenset({"anthropic"})


class _AgentSettingError(ConfigurationError):
    """The agent being built cannot carry ``agent_env``, ``contain_account_integrations``,
    ``prompt_caching`` or ``max_tokens``."""

    __slots__ = ()


def _api_effort_kwargs(provider: str, model: str, effort: str, *, origin: str = "explicit") -> dict:
    """Map an explicitly requested effort to the provider's request fields.

    Known non-reasoning OpenAI models and providers without an effort field are
    refused for explicit effort; unlisted model IDs and values are validated by
    the provider. OpenAI Chat native tool calling is restricted by model and
    effort; for example, GPT-6 Luna accepts
    it only at none, while GPT-6.1 Terra requires Responses for native tools.
    Lionagi's agent tool loop renders tool descriptions in messages instead.
    Some newer reasoning models also refuse temperature, top_p, or logprobs
    with non-none effort; the provider's error is surfaced to the caller.
    """
    if origin == "default":
        return {}
    if provider == "anthropic":
        return {"output_config": {"effort": effort}}
    if provider == "openai":
        from lionagi.providers.openai._chat_schemas import CHAT_MODELS, REASONING_MODELS

        if model not in CHAT_MODELS or model in REASONING_MODELS:
            return {"reasoning_effort": effort}
    if origin in ("profile", "stored"):
        if origin == "profile":
            logger.debug(
                "Ignoring profile effort %r for provider %r model %r", effort, provider, model
            )
        return {}
    raise _AgentSettingError(
        f"provider {provider!r}, model {model!r} cannot carry effort {effort!r}"
    )


def _set_api_effort(
    config: dict, provider: str, model: str, effort: str, *, origin: str = "explicit"
) -> bool:
    update = _api_effort_kwargs(provider, model, effort, origin=origin)
    if "output_config" in update and isinstance(config.get("output_config"), dict):
        update["output_config"] = {**config["output_config"], **update["output_config"]}
    config.update(update)
    return bool(update)


def _parse_agent_model_spec(value: str, *, origin: str = "explicit"):
    """Translate an unsupported API effort suffix into the agent setting error."""
    from lionagi.service.providers import (
        PROVIDERS_NO_EFFORT,
        ModelSpec,
        normalize_effort,
        parse_model_spec,
        split_effort_suffix,
    )

    try:
        return parse_model_spec(value)
    except ValueError:
        if "/" in value:
            provider, model = value.split("/", 1)
            split = split_effort_suffix(model)
            canonical = _canonical_provider(provider)
            if split and canonical in PROVIDERS_NO_EFFORT:
                if origin == "profile":
                    return ModelSpec(
                        model=f"{provider}/{split[0]}", effort=normalize_effort(split[1])
                    )
                _api_effort_kwargs(canonical, split[0], normalize_effort(split[1]))
        raise


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
    agent_env: Mapping[str, str] | None = None,
    _command_env_names_out: Callable[[frozenset[str]], None] | None = None,
) -> Branch:
    """Create a fully wired Branch from AgentSpec: settings → hooks → prompt → model → tools.

    ``resolved_mcp_servers`` is for a caller that already read a server set of
    its own — it is handed to the CLI request verbatim and suppresses the
    discovery this factory would otherwise do from the agent's own working
    directory. ``None`` means "hand nothing over"; leaving it unset keeps the
    discovery behaviour. ``resolved_mcp_explicit`` says that set was named by
    the caller rather than found near its launch directory.

    ``agent_env`` is an environment overlay for the spawned CLI child. It is
    merged over ``os.environ`` here, because the CLI request hands its env to
    the child as the complete environment: a bare overlay would strip PATH,
    which shows up as a broken toolchain rather than a missing setting.
    ``os.environ`` is read once, now, so a later change to this process's
    environment does not reach the agent. Every case where the overlay could
    not reach a child raises instead of dropping it: a caller-supplied
    ``chat_model``, no ``spec.model``, or an endpoint that takes no
    environment. A codex agent's overlay may not set ``CODEX_HOME`` or
    ``HOME``: this process resolves the codex home for the agent's MCP server
    profile and default reasoning effort, and would read a different one than
    the child. Only the overlay is checked: an ``env`` passed with a later call
    replaces the child environment this factory built, and one naming another
    ``CODEX_HOME`` or ``HOME`` is not refused. The overlay is runtime state and
    is not serialized, so a model restored with ``iModel.from_dict`` spawns its
    child with this process's environment; an agent rebuilt with
    ``create_agent`` gets the overlay only when ``agent_env`` is passed again.

    ``spec.contain_account_integrations`` turns a codex agent's account
    ``apps`` and ``plugins`` features off by default. It is applied to the
    endpoint this factory builds, whatever spelling of the provider built it, so
    a caller-supplied ``chat_model`` or a missing ``spec.model`` (which leaves
    the branch to build its default model) with it set raises rather than
    running uncontained. It is the endpoint's default, not a limit on later
    calls: a call that passes ``contain_account_integrations=False`` or names
    ``account_connectors``, or a model put on the branch in place of the one
    built here, is not held to it. The endpoint is recognised by its class, so
    a registered provider that wraps codex in another endpoint class is not
    contained.

    ``spec.prompt_caching`` turns the endpoint's ``prompt_caching`` option
    (ADR-0127) on for the model this factory builds. Only an endpoint that
    declares prefix caching places the marks; any other endpoint, a CLI agent
    included, sends its requests unchanged without an error. Like the
    containment default, it raises with a caller-supplied ``chat_model`` or
    with no ``spec.model``, because either way this factory builds no endpoint
    to set it on.

    ``spec.max_tokens`` sets the output-token limit on the Anthropic model this
    factory builds, which sends 8192 when no layer names one. It raises with a
    caller-supplied ``chat_model``, with no ``spec.model``, for a CLI provider,
    which has no such limit, for any other API provider (the field is named
    differently there, and only Anthropic's mapping is tested), and when the
    endpoint built does not keep the limit in its request settings: each of
    those would otherwise send the provider's default while the spec read as set.
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

    if agent_env:
        # The request model checks env only when a call is built, so a bad
        # entry would surface at the first call instead of here. Neither check
        # prints anything from the mapping: an entry is reported by its position,
        # since an environment routinely carries credentials and a key can hold
        # one as easily as a value.
        from lionagi.providers._cli_subprocess import raise_if_env_is_not_a_string_map

        if not isinstance(agent_env, Mapping):
            raise _AgentSettingError(
                f"agent_env must be a mapping of strings to strings, got {type(agent_env).__name__}"
            )
        try:
            raise_if_env_is_not_a_string_map(agent_env)
        except TypeError as exc:
            raise _AgentSettingError(f"agent_env: {exc}") from exc

        def _spawnable(text: str) -> bool:
            # The spawn encodes each entry with os.fsencode, which refuses a lone
            # surrogate outside U+DC80 to U+DCFF. One inside that range is a surrogate
            # escape for an undecodable byte, so it passes and reaches the child as
            # that byte.
            try:
                os.fsencode(text)
            except UnicodeEncodeError:
                return False
            return "\0" not in text

        unusable = [
            f"entry {index}"
            for index, (key, val) in enumerate(agent_env.items())
            if not key or "=" in key or not _spawnable(key) or not _spawnable(val)
        ]
        if unusable:
            raise _AgentSettingError(
                "agent_env: a variable needs a non-empty name with no '=', and a name and "
                "value with no NUL that encode to the filesystem encoding; these entries "
                f"are not: {', '.join(unusable)}"
            )
        if chat_model is not None:
            raise _AgentSettingError(
                "agent_env cannot be combined with a caller-supplied chat_model: "
                "the overlay is applied while this factory builds the endpoint. "
                "Set env on the model you built, or let this factory build it."
            )
        if not spec.model:
            raise _AgentSettingError(
                "agent_env requires spec.model: with no model this factory "
                "builds no endpoint, so no child process receives the overlay."
            )

    if spec.contain_account_integrations and chat_model is not None:
        raise _AgentSettingError(
            "spec.contain_account_integrations cannot be combined with a caller-supplied "
            "chat_model: it is applied while this factory builds the endpoint. Set "
            "contain_account_integrations on the codex model you built, or let this "
            "factory build it."
        )
    if spec.contain_account_integrations and chat_model is None and not spec.model:
        raise _AgentSettingError(
            "spec.contain_account_integrations requires spec.model: with no model this "
            "factory builds no endpoint, and the model the branch builds by default "
            "is not contained."
        )
    if spec.prompt_caching and chat_model is not None:
        raise _AgentSettingError(
            "spec.prompt_caching cannot be combined with a caller-supplied chat_model: it "
            "is set on the endpoint this factory builds. Set endpoint.config.prompt_caching "
            "on the model you built, or let this factory build it."
        )
    if spec.prompt_caching and chat_model is None and not spec.model:
        raise _AgentSettingError(
            "spec.prompt_caching requires spec.model: with no model this factory builds "
            "no endpoint, and the model the branch builds by default does not cache."
        )
    if spec.max_tokens is not None and chat_model is not None:
        raise _AgentSettingError(
            "spec.max_tokens cannot be combined with a caller-supplied chat_model: it is "
            "set on the endpoint this factory builds. Pass max_tokens when you build that "
            "model, or let this factory build it."
        )
    if spec.max_tokens is not None and chat_model is None and not spec.model:
        raise _AgentSettingError(
            "spec.max_tokens requires spec.model: with no model this factory builds no "
            "endpoint, and the model the branch builds by default would not carry the limit."
        )

    branch_kwargs = {}
    # The request field holding an effort this factory derived from the codex user
    # config, which the MCP hand-off below may turn off.
    derived_effort_kwarg: str | None = None

    if chat_model is not None:
        branch_kwargs["chat_model"] = chat_model
    elif spec.model:
        from lionagi.service.providers import (
            CLI_PROVIDERS,
            PROVIDER_EFFORT_KWARG,
            PROVIDER_REPO_KWARG,
            PROVIDER_YOLO_KWARGS,
        )

        spec_origin = getattr(spec, "_effort_origin", "explicit")
        model_origin = getattr(spec, "_model_origin", "explicit")
        ms = _parse_agent_model_spec(spec.model, origin=model_origin)
        if "/" in ms.model:
            provider, model_name = ms.model.split("/", 1)
        else:
            from lionagi.config import settings

            provider = settings.LIONAGI_CHAT_PROVIDER
            model_name = ms.model
        # Settings are looked up under the provider the name resolves to, so an alias such as
        # "openai-codex" takes its provider's; the model is built under the name as written.
        provider_key = _canonical_provider(provider)

        extra = {}
        default_effort = spec_origin == "default"
        if provider_key not in CLI_PROVIDERS and default_effort:
            effort = ms.effort
            if effort:
                spec_origin = model_origin
        else:
            effort = spec.effort or ms.effort
            if not spec.effort and ms.effort:
                spec_origin = model_origin
        derived = False
        if not effort and provider_key == "codex":
            # Unset is not "no opinion": codex would read the global config's
            # effort, tuned for that config's own model, and 400 when it
            # exceeds this one's ceiling. Same seam the CLI builders use.
            from lionagi.service.providers import codex_unset_effort_default

            effort = codex_unset_effort_default(model_name)
            derived = effort is not None
        if effort:
            kwarg = PROVIDER_EFFORT_KWARG.get(provider_key)
            if kwarg:
                extra[kwarg] = effort
                if derived:
                    derived_effort_kwarg = kwarg
            elif provider_key not in CLI_PROVIDERS:
                extra.update(
                    _api_effort_kwargs(provider_key, model_name, effort, origin=spec_origin)
                )
        if spec.yolo:
            extra.update(PROVIDER_YOLO_KWARGS.get(provider_key, {}))
        if spec.max_tokens is not None:
            # A CLI request has no output limit to set: refuse, never send a limit the
            # endpoint would drop (as the effort of a provider without an effort kwarg is).
            if provider_key in CLI_PROVIDERS:
                raise _AgentSettingError(
                    f"spec.max_tokens is not carried by the {provider_key!r} CLI provider "
                    f"(model {model_name!r}): a CLI agent has no output-token limit to set."
                )
            # An API provider names its limit field differently (OpenAI chat
            # max_completion_tokens, Responses max_output_tokens), so only a provider whose
            # mapping is tested here takes the limit; any other is refused, not sent as a
            # key the endpoint may drop or reject.
            if provider_key not in _MAX_TOKENS_PROVIDERS:
                raise _AgentSettingError(
                    f"spec.max_tokens is mapped only for {sorted(_MAX_TOKENS_PROVIDERS)}: the "
                    f"{provider_key!r} provider (model {model_name!r}) has no tested mapping "
                    "for an output-token limit."
                )
            extra["max_tokens"] = spec.max_tokens

        # CLI providers auth via subprocess, so a placeholder api_key is fine;
        # API providers need their real key or auth silently breaks.
        if provider_key in CLI_PROVIDERS:
            extra["api_key"] = "dummy"

        # CLI providers default their repo/workspace field to the calling
        # process cwd, so spec.cwd must be forwarded explicitly.
        if spec.cwd:
            repo_kwarg = PROVIDER_REPO_KWARG.get(provider_key)
            if repo_kwarg:
                extra[repo_kwarg] = spec.cwd

        if agent_env:
            extra["env"] = {**os.environ, **agent_env}

        chat_model = iModel(
            provider=provider,
            model=model_name,
            **extra,
        )
        # A CLI endpoint takes env as runtime state; an API endpoint has no
        # child and would leave it unread in its config. Ask the endpoint.
        if agent_env and "env" not in getattr(chat_model.endpoint, "_runtime_state", {}):
            raise _AgentSettingError(
                f"agent_env was not taken by the {type(chat_model.endpoint).__name__} "
                f"built for model {model_name!r}: this endpoint takes no environment for "
                "a child process (the codex and claude_code CLI endpoints do)."
            )
        # Ask the endpoint, as for agent_env: a limit it did not keep would be sent as
        # the provider's default while the spec read as set.
        if (
            spec.max_tokens is not None
            and chat_model.endpoint.config.kwargs.get("max_tokens") != spec.max_tokens
        ):
            raise _AgentSettingError(
                f"spec.max_tokens was not taken by the {type(chat_model.endpoint).__name__} "
                f"built for model {model_name!r}: the endpoint did not keep it in its request "
                "settings."
            )
        if spec.prompt_caching:
            chat_model.endpoint.config.prompt_caching = True
        if spec.contain_account_integrations or agent_env:
            from lionagi.providers.openai.codex import CodexCLIEndpoint

            # Decided by the endpoint that was built, so an alias or another case of
            # the provider name ("openai-codex", "Codex") is treated as codex.
            if isinstance(chat_model.endpoint, CodexCLIEndpoint):
                moved = sorted(k for k in ("CODEX_HOME", "HOME") if agent_env and k in agent_env)
                if moved:
                    raise _AgentSettingError(
                        f"agent_env cannot set {' or '.join(moved)} for a codex agent: this "
                        "process resolves the codex home for the agent's MCP server profile "
                        "and default reasoning effort, and would read a different one than "
                        "the child."
                    )
                # Codex attaches the signed-in account's app connectors and plugins
                # whatever MCP servers the request carries, and a connector acts as
                # the account. Kept in the endpoint's config, so a restored model
                # stays contained.
                if spec.contain_account_integrations:
                    chat_model.endpoint.config.kwargs["contain_account_integrations"] = True
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
    prepared_mcp = _prepare_mcp_forwarding(
        branch,
        spec,
        trust_project_settings=trust_project_settings,
        resolved_servers=resolved_mcp_servers,
    )
    from .hooks import auto_format_python

    formatter_bound = any(
        handler is auto_format_python
        for key, handlers in spec.hook_handlers.items()
        if key.startswith("post:")
        for handler in handlers
    )
    command_names = None
    command_env_supplier = None
    if (
        {"coding", "bash", "search"}.intersection(spec.tools)
        or formatter_bound
        or _command_env_names_out is not None
    ):
        from lionagi._command_env import normalize_command_env_allowlist
        from lionagi._command_env_metadata import command_environment, configured_command_env_names

        active_reference = branch.chat_model.endpoint.config.api_key_env
        command_names = frozenset(
            configured_command_env_names(
                active_reference_names=(active_reference,) if active_reference is not None else (),
                mcp_auth_env_names=_selected_mcp_auth_env_names(prepared_mcp),
            )
        )
        command_allowlist = normalize_command_env_allowlist(spec.command_env_allowlist)

        def prepared_command_environment() -> Mapping[str, str]:
            return command_environment(
                declared_names=command_names, command_env_allowlist=command_allowlist
            )

        command_env_supplier = prepared_command_environment

    _register_tools(
        branch,
        spec,
        command_env_names=command_names,
        _command_env_supplier=command_env_supplier,
    )
    _register_providers(branch, spec)
    native_mcp_servers: frozenset[str] = frozenset()
    if resolved_mcp_servers is Unset:
        native_mcp_servers = await _load_mcp(
            branch,
            spec,
            trust_project_settings=trust_project_settings,
            _command_env_supplier=command_env_supplier,
        )
    _forward_mcp_to_cli_request(
        branch,
        spec,
        trust_project_settings=trust_project_settings,
        resolved_servers=resolved_mcp_servers,
        resolved_servers_explicit=resolved_mcp_explicit,
        native_mcp_servers=native_mcp_servers,
        _prepared=prepared_mcp,
    )
    if derived_effort_kwarg:
        request_kwargs = branch.chat_model.endpoint.config.kwargs
        if request_kwargs.get("ignore_user_config"):
            # The derived effort stood in for the user config's own; the hand-off
            # left a request that loads no user config, so nothing is derived from it.
            request_kwargs.pop(derived_effort_kwarg, None)
    _wire_external_hooks(branch, spec)

    if op := spec.emission_operable():
        branch.grant_capabilities(op)

    if _command_env_names_out is not None:
        assert command_names is not None
        _command_env_names_out(command_names)
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


def _chain_post_hooks(
    tool_name: str,
    hooks: list[Callable],
    *,
    _command_env_supplier: Callable[[], Mapping[str, str]] | None = None,
) -> Callable | None:
    if not hooks:
        return None

    async def chained(result: Any, **_kw) -> Any:
        if not isinstance(result, dict):
            return result
        for handler in hooks:
            from .hooks import auto_format_python

            if handler is auto_format_python and _command_env_supplier is not None:
                modified = await handler(
                    tool_name, "", {}, result, _command_env=_command_env_supplier()
                )
            else:
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


def _attach_hooks(
    tool: Any,
    spec: AgentSpec,
    canonical_name: str,
    *,
    _command_env_supplier: Callable[[], Mapping[str, str]] | None = None,
) -> Any:
    security_hooks = _tool_hooks(spec, "security_pre", canonical_name)
    user_pre_hooks = _tool_hooks(spec, "pre", canonical_name)
    post_hooks = _tool_hooks(spec, "post", canonical_name)
    pre = _chain_pre_hooks(canonical_name, security_hooks, user_pre_hooks)
    post = _chain_post_hooks(
        canonical_name, post_hooks, _command_env_supplier=_command_env_supplier
    )
    if pre is not None:
        tool.preprocessor = _compose_preprocessor(tool.preprocessor, pre)
    if post is not None:
        tool.postprocessor = _compose_postprocessor(tool.postprocessor, post)
    return tool


def _register_tools(
    branch: Branch,
    spec: AgentSpec,
    *,
    command_env_names: frozenset[str] | None = None,
    _command_env_supplier: Callable[[], Mapping[str, str]] | None = None,
) -> None:
    for tool_spec in spec.tools:
        if tool_spec == "coding":
            _register_coding_tools(branch, spec, command_env_names=command_env_names)
        elif tool_spec == "reader":
            from lionagi.tools.file.reader import ReaderTool

            tool = _attach_hooks(
                ReaderTool().to_tool(), spec, "reader", _command_env_supplier=_command_env_supplier
            )
            branch.register_tools(tool)
        elif tool_spec == "editor":
            from lionagi.tools.file.editor import EditorTool

            tool = _attach_hooks(
                EditorTool().to_tool(), spec, "editor", _command_env_supplier=_command_env_supplier
            )
            branch.register_tools(tool)
        elif tool_spec == "bash":
            from lionagi.tools.code.bash import BashTool

            tool = _attach_hooks(
                BashTool(
                    command_env_allowlist=spec.command_env_allowlist,
                    _command_env_names=command_env_names,
                ).to_tool(),
                spec,
                "bash",
                _command_env_supplier=_command_env_supplier,
            )
            branch.register_tools(tool)
        elif tool_spec == "search":
            from pathlib import Path

            from lionagi.tools.code.search import SearchTool

            workspace_root = str(Path(spec.cwd) if spec.cwd else Path.cwd())
            tool = _attach_hooks(
                SearchTool(
                    workspace_root=workspace_root,
                    _command_env_names=command_env_names,
                    _command_env_allowlist=spec.command_env_allowlist,
                ).to_tool(),
                spec,
                "search",
                _command_env_supplier=_command_env_supplier,
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


def _register_coding_tools(
    branch: Branch, spec: AgentSpec, *, command_env_names: frozenset[str] | None = None
) -> None:
    from pathlib import Path

    from lionagi.tools.coding import DEFAULT_CODING_TOOLS, CodingToolkit

    workspace_root = Path(spec.cwd) if spec.cwd else Path.cwd()
    context_management = getattr(spec, "context_management", True)
    selected = getattr(spec, "coding_tools", None)
    if selected is None:
        tools = (
            None if context_management else tuple(t for t in DEFAULT_CODING_TOOLS if t != "context")
        )
    else:
        tools = tuple(t for t in selected if context_management or t != "context")
    toolkit = CodingToolkit(
        workspace_root=workspace_root,
        tools=tools,
        seatbelt=getattr(spec, "seatbelt", None),
        command_env_allowlist=spec.command_env_allowlist,
    )

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

    tools = toolkit.bind(branch, _command_env_names=command_env_names)
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

    from lionagi._paths import is_account_home, is_account_lionagi_dir

    candidates = []
    cwd = Path(spec.cwd) if spec.cwd else Path.cwd()

    if trust_project_settings:
        for parent in [cwd, *cwd.parents]:
            lionagi_dir = parent / ".lionagi"
            # The account home's own files are user-level configuration, reached
            # below through HOME or not at all; its `.lionagi` still ends the walk.
            if not is_account_lionagi_dir(lionagi_dir):
                candidates.append(lionagi_dir / ".mcp.json")
            if not is_account_home(parent):
                candidates.append(parent / ".mcp.json")
            if lionagi_dir.is_dir():
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
    _command_env_supplier: Callable[[], Mapping[str, str]] | None = None,
) -> frozenset[str]:
    mcp_path = _resolve_mcp_path(spec, trust_project_settings=trust_project_settings)
    if mcp_path is None:
        return frozenset()

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

    # MCP-discovered tools register after static ones; reuse _attach_hooks so
    # both paths apply the same spec-derived hook chain.
    for server_name, tool_names in loaded.items():
        for tool_name in tool_names:
            tool = branch.acts.registry.get(tool_name)
            if tool is None:
                raise RuntimeError(
                    f"MCP server {server_name!r} returned registered tool "
                    f"{tool_name!r}, but the branch registry does not contain it"
                )
            _attach_hooks(tool, spec, tool_name, _command_env_supplier=_command_env_supplier)

    return frozenset(loaded)


_MCP_FORWARDING_PROVIDERS = frozenset({*_CLAUDE_PROVIDER_NAMES, "codex"})
_ANTIGRAVITY_PROVIDER_NAMES = frozenset({"gemini-cli", "gemini_cli", "gemini-code", "gemini_code"})


def _canonical_provider(provider: str | None) -> str:
    """Fold a provider string the way endpoint resolution does, aliases included
    ("openai-codex" is codex), so a spelling that resolves to an endpoint is never
    one these predicates fail to recognise."""
    if not isinstance(provider, str):
        return ""
    from lionagi.service.connections.registry import EndpointRegistry

    return EndpointRegistry.canonical_provider(provider.strip())


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
    flag = kwargs.get("ignore_user_config")
    if flag is not None and not isinstance(flag, bool):
        # Read as the codex request's own field reads it (`"true"` and `1` are True there).
        from pydantic import ValidationError

        from lionagi.providers.openai.codex import _BOOL_ADAPTER

        try:
            flag = _BOOL_ADAPTER.validate_python(flag)
        except ValidationError:
            flag = None
    if flag is True:
        # Codex states an exclusive empty set by loading no user config at all,
        # so the request carries the set with no per-server entry.
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
    env: Mapping[str, str] | None = None,
) -> bool:
    """Write a resolved MCP server set into a CLI request's kwargs.

    Returns whether *servers* reached the request (False: provider has no
    transport for a caller-resolved set). See
    docs/internals/agent-runtime.md#mcp-server-forwarding for what
    *exclusive*/*allowed_names* mean. An exclusive set bounds the agent's
    configuration; an explicit per-call option can replace it for that call.
    *env* is the environment the child will run under when the model holds one
    outside *kwargs*; without it, an `env` in *kwargs* is read, and without
    that, this process's.
    """
    if servers is None and not exclusive:
        # No set and no claim to the whole set: the request is left as it was.
        return False
    # A caller declaring the whole set with no set declares the empty one.
    servers = servers or {}
    if not provider_accepts_forwarded_mcp(provider):
        return False

    if exclusive:
        _drop_forwarded_mcp_servers(kwargs)

    # Folded like the capability check above: a spelling that resolves to the
    # Claude CLI must not fall through to the codex override form.
    if _canonical_provider(provider) in _CLAUDE_PROVIDER_NAMES:
        if exclusive and "mcp_config" in kwargs:
            dropped_config = kwargs.pop("mcp_config")
            if dropped_config is not None:
                if isinstance(dropped_config, str):
                    config_text = dropped_config
                elif isinstance(dropped_config, os.PathLike):
                    try:
                        config_text = os.fsdecode(dropped_config)
                    except (TypeError, ValueError, OSError):
                        config_text = None
                else:
                    config_text = None
                if config_text is None:
                    description = "non-path config"
                elif config_text.lstrip("\ufeff \t\r\n").startswith(("{", "[", '"')):
                    description = "inline JSON config"
                else:
                    try:
                        json.loads(config_text)
                    except (TypeError, ValueError):
                        description = config_text
                    else:
                        description = "inline JSON config"
                logger.warning(
                    "Dropping mcp_config %s because an exclusive MCP server set was supplied",
                    description,
                )
        kwargs["mcp_servers"] = servers
        if exclusive:
            # Strict mode bounds discovery for the configured set. A caller
            # may still explicitly replace MCP options for an individual call.
            kwargs["strict_mcp_config"] = True
        return True

    from lionagi.providers._cli_subprocess import reveal_redacted

    # The config the child reads is the one its own environment names.
    child_env = reveal_redacted(env if env is not None else kwargs.get("env"))
    child_env = child_env if isinstance(child_env, Mapping) else None

    flag = kwargs.get("ignore_user_config")
    if flag is not None and not isinstance(flag, bool):
        # Read as the codex request's own field reads it (`"false"` is False there), so the
        # checks below and the command line agree. A value that field refuses is left for
        # it to refuse.
        from pydantic import ValidationError

        from lionagi.providers.openai.codex import _BOOL_ADAPTER

        try:
            kwargs["ignore_user_config"] = _BOOL_ADAPTER.validate_python(flag)
        except ValidationError:
            pass

    # Discovered once: the allowlist check below and the by-name disables share it.
    configured: set[str] | None = None
    flag = kwargs.get("ignore_user_config")
    if exclusive and not servers and (flag is None or flag is False):
        # The empty set is the hermetic codex run: the user config, and every server,
        # hook and profile it declares, is not loaded at all. Codex keeps its own
        # authentication store under the flag. An allowlist naming a server codex's
        # own config declares keeps that server, served from that config, so it is
        # not this case; an allowlist naming none of them leaves nothing to serve.
        # A profile the caller named is user config too, so under the flag the codex
        # provider refuses that request. A flag value the request refuses is not
        # replaced here: it is left for the request to refuse.
        if allowed_names:
            configured = _discover_ambient_codex_mcp_server_names(child_env)
        if not allowed_names or not configured & set(allowed_names):
            kwargs["ignore_user_config"] = True

    # A generated profile was dropped above, so a profile still here is the caller's: on the
    # request, or selected by an override (`-c profile=...`), whose key codex trims and
    # splits at every `.` before reading it. With neither, a root `profile` key in codex's
    # config.toml selects one: codex before 0.135, given no `-p`, makes it the active
    # profile.
    named_profile = kwargs.get("profile")
    remedy = "Drop the profile"
    if named_profile is None and exclusive and not kwargs.get("ignore_user_config"):
        from lionagi.providers.openai._codex_profile import (
            codex_home_for_env,
            configured_codex_profile,
        )
        from lionagi.providers.openai.codex import _TRIMMED

        for key, value in (kwargs.get("config_overrides") or {}).items():
            # Read through `str.__str__`, as the codex request reads an override key.
            if isinstance(key, str) and str.__str__(key).strip(_TRIMMED).split(".")[0] == "profile":
                named_profile = value
        if named_profile is None:
            _refuse_non_absolute_codex_home(os.environ if child_env is None else child_env)
            try:
                named_profile = configured_codex_profile(child_env)
            except ValueError:
                # A file this parser cannot read is left to the discovery below, as before,
                # which reads its server list with its own parser or, when that one cannot read
                # it either, from `codex mcp list --json`; a root key in it is not seen here.
                named_profile = None
            if named_profile is not None:
                remedy = (
                    "Remove the root `profile` key from "
                    f"{str(codex_home_for_env(child_env) / 'config.toml')!r}"
                )
    if exclusive and named_profile is not None and not kwargs.get("ignore_user_config"):
        # Without the flag the set is made exclusive by disabling, by name, the servers
        # codex's config.toml declares, and a profile (its own file, or its table in
        # config.toml) can declare servers no such disable reaches. This covers a non-empty
        # set and an allowlist that keeps a server the config declares, the two exclusive
        # runs that load the user config.
        # A value that is not a string is named by its type: its content is not echoed.
        named = (
            f"profile={named_profile!r}"
            if isinstance(named_profile, str)
            else f"a profile of type {type(named_profile).__name__}"
        )
        raise ConfigurationError(
            f"Cannot hand codex an exclusive MCP server set with {named}: outside the set, "
            "only the servers codex's config.toml declares are disabled by name, and a server "
            f"the profile declares would still load. {remedy}, or hand the set as an "
            "addition rather than the whole set."
        )

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
        # server left out of the caller's set that codex would load on its own
        # is disabled by name instead. Only names codex's own config declares:
        # `mcp_servers.<name>.enabled=false` for any other name is a table with
        # no server in it, and codex refuses to start with one.
        # A run that ignores the user config loads none of its servers, and an
        # `enabled=false` for one of them is then such a table too.
        allowed = set(servers) if allowed_names is None else set(allowed_names)
        if kwargs.get("ignore_user_config"):
            configured = set()
        elif configured is None:
            configured = _discover_ambient_codex_mcp_server_names(child_env)
        for excluded_name in sorted(configured - allowed):
            overrides[f"mcp_servers.{excluded_name}.enabled"] = False

    if secret_fields and kwargs.get("ignore_user_config"):
        # The secret fields travel in a generated `-p` profile, and `--ignore-user-config`
        # drops profile files with the rest of the user config: the server would start
        # without its `env` or `http_headers`.
        raise ConfigurationError(
            f"MCP server(s) {sorted(secret_fields)!r} set `env` or `http_headers`, which reach "
            "codex only through a config profile, and this run ignores the user config "
            "(--ignore-user-config), profiles included, so the server would start without "
            "them. Drop `env` and `http_headers` from the server config."
        )
    if secret_fields and child_env is not None:
        from lionagi.providers.openai._codex_profile import codex_home_for_env

        # The profile file is written under this process's codex home, and codex looks for
        # `-p <name>` under the home its own environment names.
        _refuse_non_absolute_codex_home(child_env)
        if codex_home_for_env(child_env).resolve() != _codex_home().resolve():
            raise ConfigurationError(
                f"MCP server(s) {sorted(secret_fields)!r} set `env` or `http_headers`, which "
                "reach codex only through a profile file written under this process's codex "
                "home, and the child's environment names a different one (CODEX_HOME or HOME), "
                "where codex would not find it. Run the child under this process's codex home, "
                "or drop `env` and `http_headers` from the server config."
            )
    if secret_fields:
        _write_codex_mcp_secret_profile(kwargs, secret_fields)
    if overrides:
        kwargs["config_overrides"] = overrides
    return True


def _drop_forwarded_mcp_servers(kwargs: dict[str, Any]) -> None:
    """Remove every server set a request already carries.

    A resumed leg's request is the persisted one, so it holds whatever the
    previous run was handed: the Claude `mcp_servers` map, codex
    `mcp_servers.<name>.<field>` overrides or a whole `mcp_servers` table, and a
    generated codex profile, which holds nothing but servers. A profile the
    caller named is theirs and stays.
    """
    kwargs.pop("mcp_servers", None)
    kwargs.pop("strict_mcp_config", None)
    overrides = kwargs.get("config_overrides")
    if overrides:
        kept = {
            key: value
            for key, value in overrides.items()
            if key != "mcp_servers" and not key.startswith("mcp_servers.")
        }
        if kept:
            kwargs["config_overrides"] = kept
        else:
            kwargs.pop("config_overrides", None)
    if _is_generated_codex_profile(kwargs.get("profile")):
        kwargs.pop("profile", None)


@dataclass(frozen=True)
class _PreparedMCPForwarding:
    path: str | None
    has_config: bool
    servers: dict[str, Any]
    skip: bool = False


def _prepare_mcp_forwarding(
    branch: Branch,
    spec: AgentSpec,
    *,
    trust_project_settings: bool = False,
    resolved_servers: dict[str, Any] | None | UnsetType = Unset,
) -> _PreparedMCPForwarding:
    """Select the CLI mapping before tools bind, without activating transports."""
    caller_resolved = resolved_servers is not Unset
    mcp_path = (
        None
        if caller_resolved
        else _resolve_mcp_path(spec, trust_project_settings=trust_project_settings)
    )
    has_config = resolved_servers is not None if caller_resolved else mcp_path is not None
    if not has_config and spec.mcp_servers is None:
        return _PreparedMCPForwarding(mcp_path, has_config, {}, skip=True)
    provider = getattr(branch.chat_model.endpoint.config, "provider", None)
    if not provider_accepts_forwarded_mcp(provider):
        return _PreparedMCPForwarding(mcp_path, has_config, {})

    if caller_resolved:
        servers: dict = dict(resolved_servers or {})
    elif mcp_path is None:
        servers = {}
    else:
        import json
        from pathlib import Path

        try:
            data = json.loads(Path(mcp_path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
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
            return _PreparedMCPForwarding(mcp_path, has_config, {}, skip=True)
        servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}

    if spec.mcp_servers is not None:
        servers = {name: cfg for name, cfg in servers.items() if name in spec.mcp_servers}

    return _PreparedMCPForwarding(mcp_path, has_config, servers)


def _selected_mcp_auth_env_names(prepared: _PreparedMCPForwarding) -> frozenset[str]:
    """Read authentication-variable declarations from the selected CLI mapping."""
    names: set[str] = set()
    for server in prepared.servers.values():
        if not isinstance(server, Mapping):
            continue
        bearer = server.get("bearer_token_env_var")
        if isinstance(bearer, str) and bearer:
            names.add(bearer)
        headers = server.get("env_http_headers")
        if isinstance(headers, Mapping):
            names.update(name for name in headers.values() if isinstance(name, str) and name)
    return frozenset(names)


def _forward_mcp_to_cli_request(
    branch: Branch,
    spec: AgentSpec,
    *,
    trust_project_settings: bool = False,
    resolved_servers: dict[str, Any] | None | UnsetType = Unset,
    resolved_servers_explicit: bool = False,
    native_mcp_servers: Collection[str] = (),
    _prepared: _PreparedMCPForwarding | None = None,
) -> None:
    """Forward an MCP server set into the CLI provider's own request.

    Unlike ``_load_mcp`` (lionagi-native tools, inert for CLI providers),
    this reaches the per-turn request kwargs a CLI provider subprocess
    actually reads. With ``resolved_servers`` given, no config file is
    looked for — that set is handed over as-is. See
    docs/internals/agent-runtime.md#mcp-server-forwarding.
    """
    caller_resolved = resolved_servers is not Unset
    prepared = (
        _prepare_mcp_forwarding(
            branch,
            spec,
            trust_project_settings=trust_project_settings,
            resolved_servers=resolved_servers,
        )
        if _prepared is None
        else _prepared
    )
    has_config = prepared.has_config
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
        # A caller that resolved no servers asked for none: nothing is lost.
        if asked_for_servers:
            if native_mcp_servers:
                logger.debug(
                    "The active provider (%s) has no MCP passthrough, but server(s) "
                    "%s are available to this branch through LionAGI's native MCP "
                    "registration (role %s, branch %s).",
                    provider,
                    sorted(native_mcp_servers),
                    spec.profile.role.name,
                    branch.id,
                )
            else:
                # Scope the claim to what this call can see: one branch, whose
                # provider was resolved from this one spec. Sibling branches in the
                logger.warning(
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

    if prepared.skip:
        return
    servers = prepared.servers

    # Branch keeps a caller-supplied chat_model by reference; copy before
    # mutating config.kwargs to avoid cross-contaminating other branches.
    # The factory takes no parse model of its own, so the branch's parse model
    # is its chat model (the same object, or the twin Branch builds from the
    # same str or dict). Parse repair runs on it, so it moves to the copy too:
    # otherwise a repair call starts the CLI without the forwarded server set.
    forwarded = branch.chat_model.copy(share_session=True, share_executor=True)
    branch.chat_model = forwarded
    branch.parse_model = forwarded
    apply_forwarded_mcp_servers(
        branch.chat_model.endpoint.config.kwargs,
        servers,
        provider=provider,
        # A caller handing over an empty set has declared the whole set, as
        # build_chat_model reads it for a plain leg.
        exclusive=spec.mcp_servers is not None or (caller_resolved and not servers),
        allowed_names=spec.mcp_servers,
        # A CLI endpoint holds the child's env outside its config kwargs.
        env=getattr(forwarded.endpoint, "_runtime_state", {}).get("env"),
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


def _discover_ambient_codex_mcp_server_names(env: Mapping[str, str] | None = None) -> set[str]:
    """Discover names of ambient/profile-configured codex MCP servers codex
    would load on its own, so an explicit allowlist can disable them too.

    *env* is the environment the child runs under: its config and its
    `codex mcp list` are the ones read. None reads this process's.

    Raises ``ConfigurationError`` if discovery fails entirely — an allowlist
    that can't be enforced must fail closed, not silently pass servers
    through. See docs/internals/agent-runtime.md#mcp-server-forwarding.
    """
    import json
    import subprocess

    from lionagi.providers.openai._codex_profile import _read_codex_config

    codex_home = _codex_home_for_env(env)
    config_path = codex_home / "config.toml"
    try:
        # The reader the root `profile` key goes through. Its parser decodes a quoted key
        # as TOML defines it, so `"mcp\u005fservers"` declares the same table as `mcp_servers`.
        data = _read_codex_config(config_path)
    except ValueError:
        # Unreadable, not UTF-8, nested past the parser's recursion limit, or not TOML
        # the parser reads: the fallback lists the servers codex itself loads.
        data = None
    if data is not None:
        mcp_servers = data.get("mcp_servers")
        if isinstance(mcp_servers, dict):
            return set(mcp_servers)
        return set()

    from lionagi.providers.openai.codex import CODEX_CLI

    if CODEX_CLI is None:
        # No binary for the run to start, so none to list with: a bare `codex` would resolve on
        # the child's PATH to a binary no run starts.
        raise _AgentSettingError(
            "Cannot enforce the explicit codex MCP server allowlist: "
            f"{str(config_path)!r} could not be read and the Codex CLI was not found for "
            "`codex mcp list --json`. Install it with: npm i -g @openai/codex"
        )
    try:
        result = subprocess.run(  # noqa: S603 -- the argv is the binary below and literals
            # The binary the run itself starts, found on this process's PATH or in the
            # directories discover_cli searches after it: the child's environment may carry
            # no PATH at all, and the run still starts.
            [CODEX_CLI, "mcp", "list", "--json"],
            env=None if env is None else dict(env),
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        listed = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise _AgentSettingError(
            "Cannot enforce the explicit codex MCP server allowlist: failed "
            f"to discover ambient/profile-configured servers via both "
            f"{str(config_path)!r} and `codex mcp list --json` "
            f"({type(exc).__name__}: {exc}). An allowlist that cannot be "
            "enforced must fail closed rather than silently leaving "
            "unlisted servers enabled."
        ) from exc

    if not isinstance(listed, list):
        raise _AgentSettingError(
            "Cannot enforce the explicit codex MCP server allowlist: "
            f"`codex mcp list --json` returned unexpected output "
            f"(expected a JSON array, got {type(listed).__name__})."
        )
    return {entry["name"] for entry in listed if isinstance(entry, dict) and "name" in entry}


def _codex_home() -> Path:
    return _codex_home_for_env(None)


def _missing_generated_codex_profile(kwargs: dict[str, Any]) -> str | None:
    """The generated profile a request names whose file is gone, if any.

    That file holds the `env`/`http_headers` of the servers a run was handed
    and is removed when the process that wrote it exits, so a resumed request
    still naming it would hand codex a profile whose file is gone.
    """
    name = kwargs.get("profile")
    if _is_generated_codex_profile(name) and not (_codex_home() / f"{name}.config.toml").is_file():
        return name
    return None


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
    from lionagi.providers.openai.codex import _TRIMMED

    for key in kwargs.get("config_overrides") or {}:
        # Read through `str.__str__`, as the codex request reads an override key. Which of
        # `-p` and a `profile` override codex applies is not settled here, so neither is
        # left to shadow the other.
        if isinstance(key, str) and str.__str__(key).strip(_TRIMMED).split(".")[0] == "profile":
            raise ConfigurationError(
                "Cannot forward MCP server secret fields for codex: the request's config "
                "overrides select a profile, and the fields reach codex through a `-p` profile "
                "of their own. Remove the `profile` override or drop `env`/`http_headers` from "
                "the MCP server config."
            )

    codex_home = _codex_home()
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
