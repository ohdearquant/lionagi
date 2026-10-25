# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""CLI-level model construction helpers — re-exports service-layer tables plus iModel builders."""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lionagi import iModel
from lionagi._paths import (
    AmbiguousProfileNameError,
    _name_spellings,
    _resolve_profile_path,
    _unreadable_symlink_target,
)
from lionagi._paths import find_lionagi_dirs as _find_lionagi_dirs
from lionagi.libs.frontmatter import parse_frontmatter as _parse_frontmatter
from lionagi.libs.path_safety import validate_bare_name
from lionagi.ln import coerce_created_at
from lionagi.providers._subagent_admission import DEFAULT_WORKLOAD, WORKLOADS
from lionagi.providers.openai._codex_profile import (
    refuse_codex_profile_run_settings,
    resolve_codex_config_profile,
)
from lionagi.service.providers import (
    _CLAUDE_PROVIDER_NAMES,
    BACKENDS,
    CLI_PROVIDERS,
    EFFORT_LEVELS,
    PROVIDER_BYPASS_KWARGS,
    PROVIDER_EFFORT_KWARG,
    PROVIDER_FAST_KWARGS,
    PROVIDER_TO_ALIAS,
    PROVIDER_YOLO_KWARGS,
    PROVIDERS_EFFORT_VIA_MODEL_NAME,
    PROVIDERS_NO_EFFORT,
    ModelSpec,
    _clamp_claude_effort,
    _clamp_codex_effort,
    codex_unset_effort_default,
    normalize_effort,
    parse_model_spec,
)

__all__ = (
    "BACKENDS",
    "CLI_PROVIDERS",
    "EFFORT_LEVELS",
    "MAX_TIMEOUT_SECONDS",
    "ModelSpec",
    "PROVIDER_BYPASS_KWARGS",
    "PROVIDER_EFFORT_KWARG",
    "PROVIDER_FAST_KWARGS",
    "PROVIDER_TO_ALIAS",
    "PROVIDER_YOLO_KWARGS",
    "PROVIDERS_EFFORT_VIA_MODEL_NAME",
    "PROVIDERS_NO_EFFORT",
    "normalize_effort",
    "add_common_cli_args",
    "build_chat_model",
    "build_imodel_from_spec",
    "parse_model_spec",
    "resolve_codex_config_profile",
    "resolve_persisted_effort",
    "AgentProfile",
    "AgentProfileNotFoundError",
    "AmbiguousProfileNameError",
    "build_agent_profile_catalog",
    "build_deadline_preamble",
    "list_agents",
    "load_agent_profile",
    "profile_config",
    "_parse_profile",
    "_resolve_profile_path",
    "_validate_bare_name",
)

# ── iModel construction ───────────────────────────────────────────────────


# Re-exported from the provider layer, where it now lives so the LIBRARY entry
# point reaches it too. It sat here, under cli/, and a codex request built with
# Branch(chat_model="codex/<profile>") therefore never resolved: the profile
# NAME went to codex as a model id and codex rejected a model nobody asked for.
# Kept in this module's namespace and __all__ because callers import it here.


def _add_codex_account_integration_kwargs(
    extra: dict, account_connectors: tuple[str, ...], contain: bool
) -> None:
    # Both are left out when unset, so a codex leg's argv is unchanged unless a
    # role opts in.
    if account_connectors:
        extra["account_connectors"] = list(account_connectors)
    if contain:
        extra["contain_account_integrations"] = True


def _add_workload_kwarg(extra: dict, endpoint: Any, workload: str) -> None:
    # Only a CLI endpoint spawns an agent process to count or sandbox; left out at the
    # default, so a heavy leg's request is the one it was before workloads existed. The
    # endpoint decides, not the name: a slashless "openai-codex" is an API model.
    if workload != DEFAULT_WORKLOAD and endpoint.is_cli:
        extra["workload"] = workload


def _is_light_codex(imodel: iModel) -> bool:
    # A light codex worker writes only its working directory and the artifact directory
    # the run gives it, and codex refuses it any other writable root.
    from lionagi.service.connections.registry import EndpointRegistry

    config = imodel.endpoint.config
    return (
        EndpointRegistry.canonical_provider(config.provider) == "codex"
        and config.kwargs.get("workload") == "light"
    )


def build_imodel_from_spec(
    spec: str,
    *,
    yolo: bool = False,
    bypass: bool = False,
    verbose: bool = False,
    effort_override: str | None = None,
    theme: str | None = None,
    fast: bool = False,
    account_connectors: tuple[str, ...] = (),
    contain_account_integrations: bool = False,
    workload: str = DEFAULT_WORKLOAD,
    ignore_user_config: bool = False,
) -> iModel:
    """Parse spec, build iModel. Effort in spec unless overridden.

    ``ignore_user_config`` builds a codex request that loads no user config, so no
    effort is derived from that config either.
    """
    ms = parse_model_spec(spec)
    effort = normalize_effort(effort_override) if effort_override is not None else ms.effort

    from lionagi.service.connections.registry import EndpointRegistry

    extra: dict = {}

    # Resolve provider for yolo/effort kwarg lookup, folded as the endpoint match folds it, so
    # an alias such as "openai-codex" takes its provider's settings; the spec keeps the name as
    # written. A slashless name is a model, not a provider.
    provider_raw = ms.model.split("/")[0] if "/" in ms.model else ms.model
    provider = (
        EndpointRegistry.canonical_provider(provider_raw) if "/" in ms.model else provider_raw
    )
    resolved_model = ms.model

    # Before the effort clamp below, whose ceilings are keyed on the model: a
    # profile names a different model than the spec did.
    codex_profile_overrides: dict[str, Any] = {}
    if provider == "codex" and "/" in ms.model:
        from ._logging import progress

        profile_name = ms.model.split("/", 1)[1]
        resolved_profile = resolve_codex_config_profile(profile_name)
        if resolved_profile is not None:
            profile_model, codex_profile_overrides = resolved_profile
            if ignore_user_config:
                refuse_codex_profile_run_settings(profile_name, codex_profile_overrides)
            progress(f"codex profile {profile_name!r} resolves to model {profile_model!r}")
            resolved_model = f"codex/{profile_model}"
            ms = ModelSpec(model=resolved_model, effort=ms.effort)

    if bypass:
        extra.update(PROVIDER_BYPASS_KWARGS.get(provider, {}))
    elif yolo:
        extra.update(PROVIDER_YOLO_KWARGS.get(provider, {}))
    if fast:
        extra.update(PROVIDER_FAST_KWARGS.get(provider, {}))
    if verbose:
        extra["verbose_output"] = True
    if theme is not None:
        extra["cli_display_theme"] = theme
    if ignore_user_config and provider == "codex":
        extra["ignore_user_config"] = True
    if effort is None and provider == "codex" and not ignore_user_config:
        effort = codex_unset_effort_default(ms.model, codex_profile_overrides)
    if effort is not None:
        kwarg = PROVIDER_EFFORT_KWARG.get(provider)
        if kwarg is not None:
            if provider == "codex":
                effort = _clamp_codex_effort(effort, ms.model)
            elif provider in _CLAUDE_PROVIDER_NAMES:
                effort = _clamp_claude_effort(effort, ms.model)
            extra[kwarg] = effort
        elif provider in PROVIDERS_EFFORT_VIA_MODEL_NAME:
            # agy (Antigravity CLI) has no effort kwarg — fold effort into the
            # resolved --model name instead (see resolve_agy_model).
            from lionagi.providers.google.gemini_code import resolve_agy_model

            bare_model = ms.model.split("/", 1)[1] if "/" in ms.model else ms.model
            resolved_model = f"{provider_raw}/{resolve_agy_model(bare_model, effort=effort)}"

    if codex_profile_overrides:
        merged = dict(codex_profile_overrides)
        merged.update(extra.get("config_overrides") or {})
        extra["config_overrides"] = merged

    model_instance = iModel(
        model=resolved_model,
        endpoint="query_cli",
        api_key="dummy",
        **extra,
    )
    if account_connectors or contain_account_integrations:
        # Keyed on the endpoint, like the workload: a slashless "openai-codex" is an API
        # model, whose request would carry these settings as payload keys, and a slashless
        # name takes the default chat provider, which can be codex.
        endpoint = model_instance.endpoint
        if (
            endpoint.is_cli
            and EndpointRegistry.canonical_provider(endpoint.config.provider) == "codex"
        ):
            _add_codex_account_integration_kwargs(
                endpoint.config.kwargs, account_connectors, contain_account_integrations
            )
    if workload != DEFAULT_WORKLOAD:
        endpoint = model_instance.endpoint
        _add_workload_kwarg(endpoint.config.kwargs, endpoint, workload)
    return model_instance


def build_chat_model(
    provider: str,
    model: str,
    yolo: bool,
    verbose: bool,
    theme: str | None,
    effort: str | None = None,
    fast: bool = False,
    bypass: bool = False,
    mcp_servers: dict | None = None,
    mcp_exclusive: bool = False,
    account_connectors: tuple[str, ...] = (),
    contain_account_integrations: bool = False,
    workload: str = DEFAULT_WORKLOAD,
) -> iModel | str:
    """Legacy: for agent.py compat.

    Returns the bare spec string only when no flag is set and no effort was
    given or derived (a codex request may derive one from the global config);
    any such setting needs an iModel to carry it.
    """
    from lionagi.service.connections.registry import EndpointRegistry

    effort = normalize_effort(effort)
    extra: dict = {}
    # Settings are looked up under the provider the name resolves to, so an alias such as
    # "openai-codex" takes its provider's; the model is built under the name as written.
    canonical = EndpointRegistry.canonical_provider(provider)
    # Before anything keyed on the model — the effort clamp below included,
    # since its ceilings belong to specific models and a profile names a
    # different one than the spec did.
    codex_profile_overrides: dict[str, Any] = {}
    if canonical == "codex":
        from ._logging import progress

        resolved_profile = resolve_codex_config_profile(model)
        if resolved_profile is not None:
            profile_name = model
            model, codex_profile_overrides = resolved_profile
            progress(f"codex profile {profile_name!r} resolves to model {model!r}")
    if mcp_servers is not None or mcp_exclusive:
        from lionagi.agent.factory import apply_forwarded_mcp_servers

        # Whether/how this provider accepts a server set (e.g. codex via config
        # overrides) is decided by apply_forwarded_mcp_servers, not here. An
        # empty set means the caller states the whole set; non-empty adds to
        # whatever the provider finds for itself. `mcp_exclusive` says the
        # caller's set is the whole set even when it is null: `--no-mcp-config`
        # arrives as the same null as a search that found nothing.
        apply_forwarded_mcp_servers(
            extra,
            mcp_servers,
            provider=provider,
            exclusive=mcp_exclusive or not mcp_servers,
        )
    if bypass:
        extra.update(PROVIDER_BYPASS_KWARGS.get(canonical, {}))
    elif yolo:
        extra.update(PROVIDER_YOLO_KWARGS.get(canonical, {}))
    if fast:
        extra.update(PROVIDER_FAST_KWARGS.get(canonical, {}))
    if canonical == "codex":
        _add_codex_account_integration_kwargs(
            extra, account_connectors, contain_account_integrations
        )
    if verbose:
        extra["verbose_output"] = True
    if theme is not None:
        extra["cli_display_theme"] = theme
    if effort is None and canonical == "codex" and not extra.get("ignore_user_config"):
        # The derived default exists to keep the user config's own effort under the
        # model's ceiling. A request that ignores the user config never loads that
        # value, so there is nothing to derive from it.
        effort = codex_unset_effort_default(model, codex_profile_overrides)
    if effort is not None:
        kwarg = PROVIDER_EFFORT_KWARG.get(canonical)
        if kwarg is not None:
            if canonical == "codex":
                effort = _clamp_codex_effort(effort, model)
            elif canonical in _CLAUDE_PROVIDER_NAMES:
                effort = _clamp_claude_effort(effort, model)
            extra[kwarg] = effort
        elif canonical in PROVIDERS_EFFORT_VIA_MODEL_NAME:
            # agy (Antigravity CLI) has no effort kwarg — fold effort into the
            # resolved --model name instead (see resolve_agy_model).
            from lionagi.providers.google.gemini_code import resolve_agy_model

            model = resolve_agy_model(model, effort=effort)

    if codex_profile_overrides:
        # Merged, never assigned: MCP server forwarding may already have put
        # its own overrides here, and replacing the dict would drop a leg's
        # server set on the floor.
        merged = dict(codex_profile_overrides)
        merged.update(extra.get("config_overrides") or {})
        extra["config_overrides"] = merged

    if extra or workload != DEFAULT_WORKLOAD:
        model_instance = iModel(
            provider=provider,
            endpoint="query_cli",
            model=model,
            api_key="dummy",
            **extra,
        )
        if workload != DEFAULT_WORKLOAD:
            endpoint = model_instance.endpoint
            _add_workload_kwarg(endpoint.config.kwargs, endpoint, workload)
        return model_instance
    return f"{provider}/{model}"


def resolve_persisted_effort(
    provider: str,
    chat_model: Any,
    requested_effort: str | None,
) -> str | None:
    """Return the post-clamp effort to persist; None for providers in PROVIDERS_NO_EFFORT. An
    alias (``openai-codex``) is read as the provider it names, whose effort kwarg it carries."""
    from lionagi.service.connections.registry import EndpointRegistry

    provider = EndpointRegistry.canonical_provider(provider or "")
    effort = requested_effort
    if isinstance(chat_model, iModel):
        _ep_kwargs = chat_model.endpoint.config.kwargs or {}
        _kwarg = PROVIDER_EFFORT_KWARG.get(provider)
        if _kwarg and _kwarg in _ep_kwargs:
            effort = _ep_kwargs[_kwarg]
    if provider in PROVIDERS_NO_EFFORT:
        effort = None
    return effort


# ── CLI common args ───────────────────────────────────────────────────────


def add_common_cli_args(
    parser: argparse.ArgumentParser,
    *,
    resume_on_timeout_supported: bool = True,
) -> None:
    """Add shared CLI flags to any subparser.

    ``--resume-on-timeout`` is an agent-only execution contract. Commands whose
    handlers do not implement it still accept the option, because removing a
    flag that appears in ``--help`` breaks callers whose invocations parse
    today, and instead say at parse time that it does nothing here.
    """
    parser.add_argument(
        "--yolo",
        action="store_true",
        help="Auto-approve the agent's tool calls, so an unattended run is never left waiting.",
    )
    parser.add_argument(
        "--bypass",
        action="store_true",
        help="Bypass all codex approvals and sandbox (for cloud/codespace environments).",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help=(
            "Route codex requests through OpenAI's priority service tier "
            "(lower latency; requires account eligibility). "
            "Does not change model or reasoning effort."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help=(
            "Stream the agent's output as it is produced instead of printing only the final "
            "result, and silence the progress lines that would interleave with it."
        ),
    )
    parser.add_argument(
        "--theme",
        choices=("light", "dark"),
        default=None,
        help="Pick the colour scheme printed output is tuned for: 'light' or 'dark' terminal.",
    )
    parser.add_argument(
        "--effort",
        metavar="LEVEL",
        default=None,
        help=(
            "Override effort (overrides spec suffix). "
            "claude: low|medium|high|xhigh|max. "
            "codex: none|minimal|low|medium|high|xhigh|max|ultra "
            "(max/ultra clamp per model support). "
            "gemini-code/gemini-cli: folded into --model as Low|Medium|High "
            "(Gemini 3.1 Pro has no Medium)."
        ),
    )
    parser.add_argument(
        "--cwd",
        metavar="DIR",
        default=None,
        help=(
            "Directory the agent's process runs in — the repo or worktree it acts on. "
            "Defaults to the current directory."
        ),
    )
    parser.add_argument(
        "--timeout",
        metavar="SECONDS",
        type=int,
        default=None,
        help=(
            "Hard wall-clock timeout in seconds. "
            "When set, a [DEADLINE] preamble is injected into the agent's "
            "prompt so the agent knows its time budget and can pace reasoning "
            "accordingly."
        ),
    )
    # Set by a skill via ``li invoke start``; groups sessions under one /show row.
    parser.add_argument(
        "--invocation",
        dest="invocation",
        metavar="ID",
        default=os.getenv("LIONAGI_INVOCATION_ID") or None,
        help=(
            "Parent invocation id (from `li invoke start`). Groups this "
            "session under a skill orchestration record. Optional."
        ),
    )
    parser.add_argument(
        "--project",
        metavar="NAME",
        default=None,
        help=(
            "Explicit project name for this session. Overrides auto-detection "
            "from .lionagi/config.toml or git remote."
        ),
    )
    if resume_on_timeout_supported:
        parser.add_argument(
            "--resume-on-timeout",
            dest="resume_on_timeout",
            action="store_true",
            default=False,
            help=(
                "If the run terminates on --timeout, automatically fire one "
                "resume of the same session with 'continue and conclude the "
                "task' and report the combined result. Bounded to a single "
                "auto-resume; a timeout on the resumed leg terminates normally. "
                "Same effect as an agent profile's 'resume_on_timeout: once'."
            ),
        )
    else:
        # Registered with the same action as everywhere else, deliberately. The
        # MCP projection matches action classes exactly and treats any subclass
        # as untranslatable, so carrying the notice in a custom action would
        # drop this command from the MCP surface entirely -- trading the break
        # this deprecation exists to avoid for the same break somewhere else.
        # The notice is emitted once argv is parsed, in run_orchestrate.
        parser.add_argument(
            "--resume-on-timeout",
            dest="resume_on_timeout",
            action="store_true",
            default=False,
            help=(
                "Deprecated and ignored on this command: it is accepted so existing "
                "invocations keep working, but nothing here reads it and it will be "
                "removed in a future release. Only `li agent` implements it."
            ),
        )
    parser.add_argument(
        "--notify",
        metavar="CMD",
        default=None,
        help=(
            "Shell command template run once this run reaches its terminal "
            "status. Overrides .lionagi/settings.yaml notify.on_terminal. "
            "Substitutes {payload} (full JSON), {status}, {invocation_id}."
        ),
    )


# ── Agent profile loading (absorbed from _agents.py) ─────────────────────────


def _validate_bare_name(name: str) -> None:
    validate_bare_name(name, label="agent profile name")


def build_deadline_preamble(timeout_seconds: int) -> str:
    """Build a [DEADLINE] preamble injected as the first user message when --timeout is set."""
    import time as _time

    minutes = max(1, int(timeout_seconds / 60))
    deadline_ts = _time.time() + timeout_seconds
    deadline_iso = coerce_created_at(deadline_ts).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"[DEADLINE]\n"
        f"You have {minutes} minute{'s' if minutes != 1 else ''} "
        f"(until {deadline_iso}) to complete this task.\n"
        f"Pace your reasoning accordingly. Prefer decisive verdicts over exhaustive\n"
        f"deliberation. If you're more than 60% through your time budget and\n"
        f"still in research mode, switch to writing the deliverable.\n\n"
        f"You can check the current time with: `date -Iseconds`\n"
        f"[/DEADLINE]\n"
    )


@dataclass
class AgentProfile:
    name: str
    system_prompt: str = ""
    raw_body: str = ""
    """Body as written in the file, before LION_SYSTEM_MESSAGE is prepended; use this when composing into AgentSpec.extra_prompt to avoid double-prepend."""
    model: str | None = None
    effort: str | None = None
    yolo: bool = False
    bypass: bool = False
    fast_mode: bool = False
    lion_system: bool = True
    khive_injection: Any = None
    artifact_defaults: dict | None = None
    timeout: int | None = None
    """Default --timeout (seconds) used when the CLI flag is not given."""
    resume_on_timeout: bool = False
    """Auto-resume-once on a timeout terminal status (profile 'resume_on_timeout: once')."""
    extra: dict = field(default_factory=dict)
    # Last, so a positional construction keeps the meaning it had before these existed.
    contain_account_integrations: bool = False
    """Run a codex leg of this role with the account's app and plugin integrations turned off."""
    account_connectors: tuple[str, ...] = ()
    """Account app connectors a codex leg of this role may reach; naming any lifts the containment."""
    idle_timeout: int | None = None
    """Default --idle-timeout (seconds) used when the CLI flag is not given."""
    workload: str = DEFAULT_WORKLOAD
    """'light' (held to what its engine can enforce, counted against the total and engine caps)
    or 'heavy' (also counted against the heavy cap); see ``li agent --workload``."""
    contain_user_settings: bool = False
    """Run a Claude flow or fan-out worker of this role with no settings files read, so the
    operator's hooks and permission rules do not gate it; see ``_contain_claude_worker``."""


class AgentProfileNotFoundError(FileNotFoundError):
    """No profile of that name exists on the search path.

    A subclass rather than a bare FileNotFoundError so a caller can tell "there
    is no such profile" from "a profile was found and then could not be read".
    The two are the same exception type otherwise, and a caller acting on the
    first would silently swallow the second.
    """


def _profile_path_candidates(agents_dir: Path, name: str) -> tuple[Path, ...]:
    """Where NAME may live in one agents dir, in the order resolution tries them.

    Two layouts share a directory, and either may spell the name with '-' or
    '_', so a root can hold more than one declaration of the same name.
    Anything reporting which files declare a profile has to walk the same
    candidates in the same order as the resolver, or it reports a displaced
    file in one root while missing one in another.
    """
    return tuple(
        path
        for spelling in _name_spellings(name)
        for path in (agents_dir / spelling / f"{spelling}.md", agents_dir / f"{spelling}.md")
    )


def _plugin_agent_profiles() -> dict[str, tuple[str, Path]]:
    """``<plugin>/<name>`` -> (plugin name, profile path) for every ACTIVE plugin (trusted+enabled+compatible).

    A plugin's agent profiles only ever join the search *after* project and
    global profiles: local files always win, and an untrusted or disabled
    plugin contributes nothing here at all.
    """
    from lionagi.plugins import PluginRegistry

    return PluginRegistry.active_agent_profile_files()


def list_agents() -> list[str]:
    """List available agent profile names, merged across .lionagi/ dirs and active plugins.

    Plugin-declared profiles are namespaced as ``<plugin>/<name>``.
    """
    seen: set[str] = set()
    for d in _find_lionagi_dirs():
        agents_dir = d / "agents"
        if not agents_dir.is_dir():
            continue
        # Directory layout
        for child in agents_dir.iterdir():
            profile_path = child / f"{child.name}.md"
            if _unreadable_symlink_target(profile_path) is not None:
                continue
            if child.is_dir() and profile_path.is_file():
                seen.add(child.name)
        # Flat legacy layout
        for p in agents_dir.glob("*.md"):
            if _unreadable_symlink_target(p) is not None:
                continue
            if p.is_file():
                seen.add(p.stem)
    seen.update(_plugin_agent_profiles().keys())
    return sorted(seen)


def profile_config(profile: AgentProfile) -> dict[str, Any]:
    """The runtime configuration a loaded profile contributes, as plain JSON values.

    The one place that decides which fields a discovery surface reports — and
    which it withholds. Prompt bodies are deliberately absent: discovery is not a
    second path for exposing or copying profile instructions. Every reader of the
    roster goes through here so they cannot disagree about either half.
    """
    config: dict[str, Any] = {
        "model": profile.model,
        "effort": profile.effort,
        "role": profile.extra.get("role"),
        "pack": profile.extra.get("pack"),
        "yolo": profile.yolo,
        "bypass": profile.bypass,
        "fast_mode": profile.fast_mode,
        "lion_system": profile.lion_system,
        "khive_injection": profile.khive_injection,
        "timeout": profile.timeout,
        "resume_on_timeout": profile.resume_on_timeout,
    }
    # Only when set, so a role that uses none of them reports exactly what it did before.
    if profile.idle_timeout is not None:
        config["idle_timeout"] = profile.idle_timeout
    if profile.contain_account_integrations:
        config["contain_account_integrations"] = True
    if profile.account_connectors:
        config["account_connectors"] = list(profile.account_connectors)
    if profile.workload != DEFAULT_WORKLOAD:
        config["workload"] = profile.workload
    if profile.contain_user_settings:
        config["contain_user_settings"] = True
    return config


def build_agent_profile_catalog() -> dict[str, dict[str, Any]]:
    """Index discoverable profiles by name and resolved runtime configuration."""
    return {name: profile_config(load_agent_profile(name)) for name in list_agents()}


def _resolve_plugin_profile_path(name: str) -> Path | None:
    """Resolve *name* against active plugins: an explicit ``<plugin>/<agent>`` token always

    resolves; a bare name resolves only when exactly one active plugin declares it
    (ambiguous bare names are left unresolved — namespacing exists precisely so a
    caller can disambiguate).
    """
    plugin_profiles = _plugin_agent_profiles()
    if "/" in name:
        entry = plugin_profiles.get(name)
        return entry[1] if entry is not None else None

    matches = [
        path for token, (_plugin, path) in plugin_profiles.items() if token.endswith(f"/{name}")
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _warn_if_shadowing_plugin_profile(name: str) -> None:
    """Log a shadow warning when a local agent profile hides a same-named plugin profile.

    The user's own explicit local file always wins on a bare-name collision;
    this only makes the shadowing visible instead of silent.
    """
    plugin_profiles = _plugin_agent_profiles()
    owners = sorted(
        {token.split("/", 1)[0] for token in plugin_profiles if token.endswith(f"/{name}")}
    )
    if not owners:
        return
    from ._logging import warn

    warn(
        f"agent profile {name!r} is also declared by plugin(s) {', '.join(owners)}; "
        f"using the local file (load the plugin's version with '<plugin>/{name}')."
    )


def load_agent_profile(name: str, *, cwd: Path | None = None) -> AgentProfile:
    """Load a named agent profile, searching project-local then global ~/.lionagi/agents/,

    then active plugin bundles: a plugin's agent profiles join the search
    only after a project/global miss, and only for a plugin that is trusted +
    enabled + version-compatible (see ``lionagi.plugins``). Project-local
    directories are found from *cwd*, the process working directory by default.
    """
    plugin_token = "/" in name
    if plugin_token:
        # A `<plugin>/<name>` token is opaque to the bare-name validator
        # (which forbids '/'); validate each component instead.
        plugin_part, _, agent_part = name.partition("/")
        _validate_bare_name(plugin_part)
        _validate_bare_name(agent_part)
    else:
        _validate_bare_name(name)

    dirs = _find_lionagi_dirs() if cwd is None else _find_lionagi_dirs(cwd)
    unreadable_symlinks: list[tuple[Path, str]] = []
    if not plugin_token:
        for d in dirs:
            path = _resolve_profile_path(
                d / "agents",
                name,
                unreadable_symlinks=unreadable_symlinks,
            )
            if path is not None:
                _warn_if_shadowing_plugin_profile(name)
                text = path.read_text()
                return _parse_profile(name, text)

    plugin_path = _resolve_plugin_profile_path(name)
    if plugin_path is not None:
        return _parse_profile(name, plugin_path.read_text())

    if not dirs and not plugin_token:
        raise AgentProfileNotFoundError(
            "No .lionagi/ directory found. Create .lionagi/agents/ in your repo "
            "or ~/.lionagi/agents/ globally."
        )

    available = sorted(_plugin_agent_profiles().keys()) if plugin_token else list_agents()
    msg = f"Agent profile '{name}' not found"
    for path, target in unreadable_symlinks:
        msg += f"\n{path} exists but its symlink target is unreadable: {target}"
    if available:
        msg += f"\nAvailable: {', '.join(available)}"
    raise AgentProfileNotFoundError(msg)


#: The largest whole number of seconds a timeout may name: the largest a float holds exactly,
#: which is what the run's deadline arithmetic and a JSON-number MCP caller both carry.
MAX_TIMEOUT_SECONDS = 2**53 - 1


def _parse_profile_timeout(name: str, raw: Any, field: str = "timeout") -> int | None:
    """Validate a profile seconds field ('timeout', 'idle_timeout'); warn and ignore garbage
    rather than raising.

    See docs/internals/cli.md for why bool/float are rejected, not coerced.
    """
    if raw is None:
        return None
    from ._logging import warn

    if isinstance(raw, bool) or not isinstance(raw, int):
        warn(f"agent profile {name!r}: ignoring invalid {field} {raw!r} (must be a positive int)")
        return None
    if raw <= 0:
        warn(f"agent profile {name!r}: ignoring non-positive {field} {raw!r}")
        return None
    if raw > MAX_TIMEOUT_SECONDS:
        warn(
            f"agent profile {name!r}: ignoring {field} {raw!r} "
            f"(must be at most {MAX_TIMEOUT_SECONDS})"
        )
        return None
    return raw


def _parse_profile_resume_on_timeout(name: str, raw: Any) -> bool:
    """Validate the profile 'resume_on_timeout' field; only the literal string 'once' opts in."""
    if raw is None or raw is False:
        return False
    if isinstance(raw, str) and raw.strip().lower() == "once":
        return True
    from ._logging import warn

    warn(
        f"agent profile {name!r}: ignoring unrecognized resume_on_timeout {raw!r} (expected 'once')"
    )
    return False


def _parse_profile_contain_account_integrations(name: str, raw: Any) -> bool:
    """Validate the profile flag; a connector grant can lift containment when a request is built.

    An unreadable value warns and defaults to contained.
    """
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    from ._logging import warn

    warn(
        f"agent profile {name!r}: contain_account_integrations {raw!r} is not true or false; "
        "containing"
    )
    return True


def _parse_profile_contain_user_settings(name: str, frontmatter: dict) -> bool:
    """Validate the profile flag. Absent, it is off; a value other than true or false, an
    empty one included, warns and defaults to contained."""
    if "contain_user_settings" not in frontmatter:
        return False
    raw = frontmatter["contain_user_settings"]
    if isinstance(raw, bool):
        return raw
    from ._logging import warn

    warn(f"agent profile {name!r}: contain_user_settings {raw!r} is not true or false; containing")
    return True


def _parse_profile_account_connectors(name: str, raw: Any) -> tuple[str, ...]:
    """Validate the profile 'account_connectors' list; warn and grant none when it does not
    read as connector names, rather than read it as all."""
    if raw is None:
        return ()
    if isinstance(raw, list) and all(isinstance(c, str) and c.strip() for c in raw):
        return tuple(dict.fromkeys(c.strip() for c in raw))
    from ._logging import warn

    warn(
        f"agent profile {name!r}: ignoring account_connectors {raw!r} "
        "(must be a list of connector names)"
    )
    return ()


def _parse_profile_workload(name: str, raw: Any) -> str:
    """Validate the profile 'workload'; warn and read anything else as heavy, the class
    that keeps the configured sandbox and counts against both caps."""
    if raw is None:
        return DEFAULT_WORKLOAD
    if isinstance(raw, str) and raw in WORKLOADS:
        return raw
    from ._logging import warn

    warn(
        f"agent profile {name!r}: workload {raw!r} is not one of {', '.join(WORKLOADS)}; "
        f"reading it as {DEFAULT_WORKLOAD}"
    )
    return DEFAULT_WORKLOAD


_CONTAIN_KEY = "contain_user_settings"
# A line that sets the flag as a key, in a mapping or a list item, quoted or not.
_FLAG_KEY_LINE = re.compile(rf"^[ \t]*(?:-[ \t]+)*[\"']?{_CONTAIN_KEY}[\"']?[ \t]*:", re.M)
_DASHES = re.compile(r"-{3,}")


def _contain_flag_values(block: str) -> list[Any]:
    """The value each top-level ``contain_user_settings`` key of *block*, a frontmatter block
    that reads as YAML, gives the flag. Keys are found on the parsed nodes, so a quoted key, an
    indented block, ``? contain_user_settings`` and a merge key all count; an explicit key wins
    over a merged one, as it does in YAML. A boolean spelled other than ``true`` or ``false``,
    such as ``off`` or ``no``, keeps its spelling, which reads as neither and so contains."""
    import yaml

    def value_nodes(node: Any, seen: set[int]) -> list[Any]:
        if id(node) in seen or not isinstance(node, yaml.MappingNode):
            return []
        seen.add(id(node))
        explicit: list[Any] = []
        merged: list[Any] = []
        for key, value in node.value:
            if key.tag == "tag:yaml.org,2002:merge":
                sources = value.value if isinstance(value, yaml.SequenceNode) else [value]
                for source in sources:
                    merged.extend(value_nodes(source, seen))
            elif isinstance(key, yaml.ScalarNode) and key.value == _CONTAIN_KEY:
                explicit.append(value)
        return explicit or merged

    loader = yaml.SafeLoader(block)
    try:
        values: list[Any] = []
        for node in value_nodes(loader.get_single_node(), set()):
            if (
                isinstance(node, yaml.ScalarNode)
                and node.tag == "tag:yaml.org,2002:bool"
                and node.value.lower() not in ("true", "false")
            ):
                values.append(node.value)
            else:
                values.append(loader.construct_object(node, deep=True))
        return values
    finally:
        loader.dispose()


def _contain_flag_of(values: list[Any]) -> Any:
    """The flag several keys give it: a key turning containment off loses to any other, so
    it is off only when every one reads false."""
    return next((value for value in values if value is not False), False)


def _keys_like_the_contain_flag(document: Any) -> list[str]:
    """The keys of a parsed frontmatter, at any depth, that read as ``contain_user_settings``
    once case and dashes are set aside, each named by its path. Asked of a frontmatter no
    top-level key of which sets the flag, every one it names is a key that does not."""
    found: list[str] = []
    seen: set[int] = set()

    def walk(node: Any, path: str) -> None:
        if id(node) in seen or not isinstance(node, (dict, list)):
            return
        seen.add(id(node))
        if isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")
            return
        for key, value in node.items():
            where = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and key.casefold().replace("-", "_") == _CONTAIN_KEY:
                found.append(where)
            walk(value, where)

    walk(document, "")
    return found


def _settings_of_unread_frontmatter(name: str, text: str) -> dict[str, Any]:
    """The settings a profile keeps when no frontmatter block that reads opens it: none, with a
    warning when a block with settings in it was there to lose. The worker is contained when
    that block names ``contain_user_settings``: as a key when the block reads as YAML settings,
    anywhere in its text when it does not read or reads as a list or a line of text, so neither
    a broken line in the block nor text on the delimiter's line can turn containment off. A
    block that is empty or comments only has none to lose. Text on lines of its own ahead of
    the first ``---`` line leaves the profile all prompt, as before, and contains nothing: any
    block after it, closed or not, that sets the flag as a key or does not read and names it
    warns that its worker is not contained. A profile with no line of dashes has no block, and
    all of it is the prompt."""
    from itertools import pairwise

    import yaml

    from lionagi.libs.frontmatter import _FM_SPLIT, parse_frontmatter_strict

    from ._logging import warn

    def read(block: str) -> tuple[bool, Any]:
        try:
            return True, yaml.safe_load(block)
        except yaml.YAMLError:
            return False, None

    if text.startswith("---"):
        try:
            parse_frontmatter_strict(text)
            return {}
        except yaml.YAMLError as exc:
            reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        parts = _FM_SPLIT.split(text, maxsplit=2)
        # An unclosed block runs to the end, since where it was meant to end is not known.
        block = parts[1] if len(parts) == 3 else text[3:]
        reads, document = read(block)
        # A block with no settings in it, empty or comments only, has none to lose.
        if reads and document is None:
            return {}
        named = _CONTAIN_KEY in block
        what = f"its frontmatter does not read as YAML settings ({reason})"
    else:
        lines = text.splitlines()
        if not (lines and lines[0].rstrip().endswith("---")):
            # Text on lines of its own ahead of a ``---`` line is the prompt of a profile with
            # no block, as before, so nothing after it applies, the flag included. A block after
            # that text that names the flag says so rather than lose it in silence: one with a
            # line setting it as a key, one that reads as settings and sets it, or one that does
            # not read and names it anywhere. A block runs from a line of three or more dashes
            # to the next such line or, unclosed, to the end.
            delimiters = []
            # Fenced examples are prompt text, not misplaced frontmatter.
            fence: str | None = None
            for i, line in enumerate(lines):
                marker = re.match(r" {0,3}(`{3,}|~{3,})(.*)$", line)
                if fence is not None:
                    if (
                        marker
                        and marker[1][0] == fence[0]
                        and len(marker[1]) >= len(fence)
                        and not marker[2].strip()
                    ):
                        fence = None
                    continue
                if marker and (marker[1][0] == "~" or "`" not in marker[2]):
                    fence = marker[1]
                    continue
                if _DASHES.fullmatch(line.rstrip()):
                    delimiters.append(i)
            for opening, end in pairwise([*delimiters, len(lines)]):
                block = "\n".join(lines[opening + 1 : end])
                reads, document = read(block)
                if (
                    _FLAG_KEY_LINE.search(block)
                    or (reads and isinstance(document, dict) and _contain_flag_values(block))
                    or (not reads and _CONTAIN_KEY in block)
                ):
                    warn(
                        f"agent profile {name!r}: the --- block on line {opening + 1} follows "
                        "text on lines of its own, so the profile is all prompt and no setting "
                        "in the block applies; its worker is not contained, though the block "
                        "names contain_user_settings"
                    )
                    break
            return {}
        # Text on the line of the opening delimiter hides the block it opens, which runs to
        # the next ``---`` line or, unclosed, to the end.
        rest = lines[1:]
        closing = next((i for i, line in enumerate(rest) if line.rstrip() == "---"), None)
        block = "\n".join(rest if closing is None else rest[:closing])
        reads, document = read(block)
        if reads and isinstance(document, dict):
            if not document:
                return {}
            named = bool(_contain_flag_values(block))
        elif reads and document is None:
            # Empty or comments only, a block has no settings to lose.
            return {}
        else:
            # A hidden block that reads as a list or a line of text holds no settings, and
            # one that does not read has none to apply: either counts, as a frontmatter
            # that does not read does, when it names the flag anywhere in its text.
            named = _CONTAIN_KEY in block
            if not named and (reads or closing is None):
                return {}
        what = "text ahead of the --- on line 1 hides the frontmatter after it"
    warn(
        f"agent profile {name!r}: {what}; every setting in it is ignored"
        + (", and its worker is contained, since it names contain_user_settings" if named else "")
    )
    return {_CONTAIN_KEY: True} if named else {}


def _parse_profile(name: str, text: str) -> AgentProfile:
    import unicodedata

    # Whitespace or an invisible format character (a byte-order mark, a zero-width
    # space) ahead of the opening delimiter would hide it and, with it, every setting.
    start = 0
    while start < len(text) and (
        text[start].isspace() or unicodedata.category(text[start]) == "Cf"
    ):
        start += 1
    text = text[start:]
    frontmatter, body = _parse_frontmatter(text)
    if not frontmatter:
        frontmatter = _settings_of_unread_frontmatter(name, text)
    else:
        from lionagi.libs.frontmatter import _FM_SPLIT

        values = _contain_flag_values(_FM_SPLIT.split(text.strip(), maxsplit=2)[1])
        if len(values) > 1:
            # YAML keeps the last of a repeated key; the flag is off only when every one reads
            # false, so moving a line cannot turn containment off.
            from ._logging import warn

            warn(
                f"agent profile {name!r}: contain_user_settings is set {len(values)} times; "
                "it is off only when every one reads false"
            )
        if values:
            frontmatter = {**frontmatter, _CONTAIN_KEY: _contain_flag_of(values)}
        elif misplaced := _keys_like_the_contain_flag(frontmatter):
            # A key spelled like the flag, or the flag under another setting, does not set it;
            # say so rather than leave the worker on your settings in silence.
            from ._logging import warn

            warn(
                f"agent profile {name!r}: contain_user_settings counts only as a top-level key "
                f"spelled exactly so; it is not set by {', '.join(map(repr, misplaced))}, and "
                "its worker is not contained"
            )

    lion_system = bool(frontmatter.get("lion_system", True))
    raw_body = body  # always the body as written, before any expansion
    if lion_system:
        from lionagi.session.prompts import LION_SYSTEM_MESSAGE

        expanded = LION_SYSTEM_MESSAGE.strip() + "\n\n" + body
    else:
        expanded = body

    return AgentProfile(
        name=name,
        system_prompt=expanded,
        raw_body=raw_body,
        model=frontmatter.get("model"),
        effort=frontmatter.get("effort"),
        yolo=bool(frontmatter.get("yolo", False)),
        bypass=bool(frontmatter.get("bypass", False)),
        fast_mode=bool(frontmatter.get("fast_mode", False)),
        lion_system=lion_system,
        khive_injection=frontmatter.get("khive_injection"),
        artifact_defaults=frontmatter.get("artifact_defaults"),
        timeout=_parse_profile_timeout(name, frontmatter.get("timeout")),
        resume_on_timeout=_parse_profile_resume_on_timeout(
            name, frontmatter.get("resume_on_timeout")
        ),
        contain_account_integrations=_parse_profile_contain_account_integrations(
            name, frontmatter.get("contain_account_integrations")
        ),
        account_connectors=_parse_profile_account_connectors(
            name, frontmatter.get("account_connectors")
        ),
        idle_timeout=_parse_profile_timeout(
            name, frontmatter.get("idle_timeout"), field="idle_timeout"
        ),
        workload=_parse_profile_workload(name, frontmatter.get("workload")),
        contain_user_settings=_parse_profile_contain_user_settings(name, frontmatter),
        extra={
            k: v
            for k, v in frontmatter.items()
            if k
            not in (
                "model",
                "effort",
                "yolo",
                "bypass",
                "fast_mode",
                "lion_system",
                "khive_injection",
                "artifact_defaults",
                "timeout",
                "resume_on_timeout",
                "idle_timeout",
                "contain_account_integrations",
                "account_connectors",
                "workload",
                "contain_user_settings",
            )
        },
    )
