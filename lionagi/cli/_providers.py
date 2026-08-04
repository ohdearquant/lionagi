# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""CLI-level model construction helpers — re-exports service-layer tables plus iModel builders."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lionagi import iModel
from lionagi._paths import find_lionagi_dirs as _find_lionagi_dirs
from lionagi.libs.frontmatter import parse_frontmatter as _parse_frontmatter
from lionagi.libs.path_safety import validate_bare_name
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
    normalize_effort,
    parse_model_spec,
)

__all__ = (
    "BACKENDS",
    "CLI_PROVIDERS",
    "EFFORT_LEVELS",
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


def resolve_codex_config_profile(model: str) -> tuple[str, dict[str, Any]] | None:
    """Resolve a codex model part that names a codex config profile.

    codex reaches models from other providers through a config profile:
    ``-p <name>`` layers ``$CODEX_HOME/<name>.config.toml`` over the base
    config, and such a file names a ``model`` and the ``model_provider`` that
    serves it. So ``codex/<name>`` should mean "run that profile", not "run a
    model literally called ``<name>``".

    lionagi cannot forward this by passing ``-p``. codex accepts exactly one
    profile per invocation and lionagi already spends that slot on a generated
    profile carrying MCP server secrets, which is why supplying both is
    refused outright. The file is therefore read here and its settings applied
    directly: the profile's ``model`` becomes the model, and its remaining
    scalars become ``-c`` overrides, which outrank config either way.

    Two deliberate limits, both narrowing rather than widening:

    * Only a bare name is looked up, so an ordinary vendor model id such as
      ``deepseek/deepseek-v4-flash-0731`` is never treated as a path. Bare
      excludes dots as well as separators, so a profile must be named like
      ``deepseek-flash`` and one named ``gpt-5.6-sol`` is not resolved at all.
      That cuts the other way usefully: model ids carry dots and version
      numbers, so a profile name cannot collide with a real one and quietly
      stand in for it.
    * Table-valued keys (notably ``mcp_servers``) are not applied, and are
      logged. lionagi decides a leg's MCP server set explicitly, and quietly
      re-introducing servers from a config file would go around that.

    Returns ``None`` when no such profile file exists, leaving the name to be
    treated as a model id exactly as before.
    """
    try:
        validate_bare_name(model, label="codex config profile name")
    except ValueError:
        return None

    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    profile_path = codex_home / f"{model}.config.toml"
    if not profile_path.is_file():
        # A symlink whose target is unreadable is not the same as no profile.
        # `is_file()` follows the link and answers False for both, so falling
        # through here would send the name to codex as a model id — the exact
        # silent substitution this function exists to prevent, arriving through
        # a file the operator can see sitting right there.
        broken_target = _unreadable_symlink_target(profile_path)
        if broken_target is not None:
            raise ValueError(
                f"codex config profile {str(profile_path)!r} is a symlink whose "
                f"target {broken_target!r} is unreadable. Repair or remove the "
                f"link; running without it would send {model!r} to codex as a "
                f"model id and silently run something else."
            )
        return None

    import toml

    try:
        data = toml.loads(profile_path.read_text())
    except (OSError, toml.TomlDecodeError) as exc:
        raise ValueError(
            f"codex config profile {str(profile_path)!r} could not be read "
            f"({type(exc).__name__}: {exc}). Fix or remove the file; running "
            f"without it would send {model!r} as a model id instead."
        ) from exc

    resolved = data.get("model")
    if not isinstance(resolved, str) or not resolved:
        raise ValueError(
            f"codex config profile {str(profile_path)!r} declares no 'model'. "
            f"Add one, or use a model id instead of the profile name — "
            f"without it {model!r} would be sent to codex as a model id and "
            f"silently run something other than the profile."
        )

    overrides: dict[str, Any] = {}
    skipped: list[str] = []
    for key, value in data.items():
        if key == "model":
            continue
        if isinstance(value, (str, int, float, bool)):
            overrides[key] = value
        else:
            skipped.append(key)
    if skipped:
        from ._logging import warn

        warn(
            f"codex config profile {model!r}: ignoring {', '.join(sorted(skipped))} "
            "— lionagi applies a profile's model and scalar settings, and sets "
            "a leg's MCP servers itself"
        )

    # Say which model is actually being run. A profile whose name collides with
    # a real model id would otherwise substitute without a word, which is the
    # quiet half of the same failure this function fixes: the caller asks for
    # one thing, a different thing runs, and the leg looks healthy either way.
    from ._logging import progress

    progress(f"codex profile {model!r} resolves to model {resolved!r}")
    return resolved, overrides


def build_imodel_from_spec(
    spec: str,
    *,
    yolo: bool = False,
    bypass: bool = False,
    verbose: bool = False,
    effort_override: str | None = None,
    theme: str | None = None,
    fast: bool = False,
) -> iModel:
    """Parse spec, build iModel. Effort in spec unless overridden."""
    ms = parse_model_spec(spec)
    effort = normalize_effort(effort_override) if effort_override is not None else ms.effort

    extra: dict = {}

    # Resolve provider for yolo/effort kwarg lookup
    provider_raw = ms.model.split("/")[0] if "/" in ms.model else ms.model
    resolved_model = ms.model

    # Before the effort clamp below, whose ceilings are keyed on the model: a
    # profile names a different model than the spec did.
    codex_profile_overrides: dict[str, Any] = {}
    if provider_raw == "codex" and "/" in ms.model:
        resolved_profile = resolve_codex_config_profile(ms.model.split("/", 1)[1])
        if resolved_profile is not None:
            profile_model, codex_profile_overrides = resolved_profile
            resolved_model = f"codex/{profile_model}"
            ms = ModelSpec(model=resolved_model, effort=ms.effort)

    if bypass:
        extra.update(PROVIDER_BYPASS_KWARGS.get(provider_raw, {}))
    elif yolo:
        extra.update(PROVIDER_YOLO_KWARGS.get(provider_raw, {}))
    if fast:
        extra.update(PROVIDER_FAST_KWARGS.get(provider_raw, {}))
    if verbose:
        extra["verbose_output"] = True
    if theme is not None:
        extra["cli_display_theme"] = theme
    if effort is not None:
        kwarg = PROVIDER_EFFORT_KWARG.get(provider_raw)
        if kwarg is not None:
            if provider_raw == "codex":
                effort = _clamp_codex_effort(effort, ms.model)
            elif provider_raw in _CLAUDE_PROVIDER_NAMES:
                effort = _clamp_claude_effort(effort, ms.model)
            extra[kwarg] = effort
        elif provider_raw in PROVIDERS_EFFORT_VIA_MODEL_NAME:
            # agy (Antigravity CLI) has no effort kwarg — fold effort into the
            # resolved --model name instead (see resolve_agy_model).
            from lionagi.providers.google.gemini_code import resolve_agy_model

            bare_model = ms.model.split("/", 1)[1] if "/" in ms.model else ms.model
            resolved_model = f"{provider_raw}/{resolve_agy_model(bare_model, effort=effort)}"

    if codex_profile_overrides:
        merged = dict(codex_profile_overrides)
        merged.update(extra.get("config_overrides") or {})
        extra["config_overrides"] = merged

    return iModel(
        model=resolved_model,
        endpoint="query_cli",
        api_key="dummy",
        **extra,
    )


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
) -> iModel | str:
    """Legacy: for agent.py compat. Returns bare spec string when no flags."""
    effort = normalize_effort(effort)
    extra: dict = {}
    # Before anything keyed on the model — the effort clamp below included,
    # since its ceilings belong to specific models and a profile names a
    # different one than the spec did.
    codex_profile_overrides: dict[str, Any] = {}
    if provider == "codex":
        resolved_profile = resolve_codex_config_profile(model)
        if resolved_profile is not None:
            model, codex_profile_overrides = resolved_profile
    if mcp_servers is not None:
        from lionagi.agent.factory import apply_forwarded_mcp_servers

        # Whether/how this provider accepts a server set (e.g. codex via config
        # overrides) is decided by apply_forwarded_mcp_servers, not here. An
        # empty set means the caller states the whole set; non-empty adds to
        # whatever the provider finds for itself.
        apply_forwarded_mcp_servers(
            extra, mcp_servers, provider=provider, exclusive=not mcp_servers
        )
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
    if effort is not None:
        kwarg = PROVIDER_EFFORT_KWARG.get(provider)
        if kwarg is not None:
            if provider == "codex":
                effort = _clamp_codex_effort(effort, model)
            elif provider in _CLAUDE_PROVIDER_NAMES:
                effort = _clamp_claude_effort(effort, model)
            extra[kwarg] = effort
        elif provider in PROVIDERS_EFFORT_VIA_MODEL_NAME:
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

    if extra:
        return iModel(
            provider=provider,
            endpoint="query_cli",
            model=model,
            api_key="dummy",
            **extra,
        )
    return f"{provider}/{model}"


def resolve_persisted_effort(
    provider: str,
    chat_model: Any,
    requested_effort: str | None,
) -> str | None:
    """Return the post-clamp effort to persist; None for providers in PROVIDERS_NO_EFFORT."""
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


def add_common_cli_args(parser: argparse.ArgumentParser) -> None:
    """Add shared CLI flags to any subparser."""
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
    from datetime import datetime, timezone

    minutes = max(1, int(timeout_seconds / 60))
    deadline_ts = _time.time() + timeout_seconds
    deadline_iso = datetime.fromtimestamp(deadline_ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
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


def _unreadable_symlink_target(path: Path) -> str | None:
    """Return a broken/non-file symlink's declared target, if applicable."""
    if not path.is_symlink() or path.is_file():
        return None
    try:
        return str(path.readlink())
    except OSError:
        return "<unreadable>"


class AmbiguousProfileNameError(ValueError):
    """One agents dir declares a name under both '-' and '_' spellings."""


class AgentProfileNotFoundError(FileNotFoundError):
    """No profile of that name exists on the search path.

    A subclass rather than a bare FileNotFoundError so a caller can tell "there
    is no such profile" from "a profile was found and then could not be read".
    The two are the same exception type otherwise, and a caller acting on the
    first would silently swallow the second.
    """


def _name_spellings(name: str) -> tuple[str, ...]:
    """NAME plus its separator spellings, requested spelling first.

    '-' and '_' are interchangeable in a profile name, so a role written
    ``postmortem-lead`` everywhere else still finds ``postmortem_lead.md``.
    """
    return tuple(dict.fromkeys((name, name.replace("-", "_"), name.replace("_", "-"))))


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


def _resolve_profile_path(
    agents_dir: Path,
    name: str,
    *,
    unreadable_symlinks: list[tuple[Path, str]] | None = None,
) -> Path | None:
    """Return profile path for NAME, recording unreadable candidate symlinks.

    The spelling actually asked for wins outright when it exists. Only a
    request that does *not* name an existing file falls back to the other
    separator spelling, so a directory deliberately holding two profiles that
    differ only in separator keeps resolving both, exactly as it did before
    the spellings became interchangeable.

    Failing that, two spellings matching one request in a single directory are
    an error rather than a ranking: whichever one lost would be invisible to
    the caller. Two spellings in *different* roots are ordinary shadowing,
    decided by root order, and are resolved by the caller walking the roots.
    """
    resolved: dict[str, Path] = {}
    for spelling in _name_spellings(name):
        for candidate in (agents_dir / spelling / f"{spelling}.md", agents_dir / f"{spelling}.md"):
            if candidate.is_file():
                resolved.setdefault(spelling, candidate)
                continue
            target = _unreadable_symlink_target(candidate)
            if target is not None and unreadable_symlinks is not None:
                unreadable_symlinks.append((candidate, target))

    if name in resolved:
        return resolved[name]
    if len(resolved) > 1:
        matched = ", ".join(str(p) for p in resolved.values())
        raise AmbiguousProfileNameError(
            f"Agent profile name '{name}' is ambiguous in {agents_dir}: "
            f"'-' and '_' are interchangeable, and both spellings exist ({matched}). "
            "Rename or remove one of them."
        )
    return next(iter(resolved.values()), None)


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
    return {
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


def load_agent_profile(name: str) -> AgentProfile:
    """Load a named agent profile, searching project-local then global ~/.lionagi/agents/,

    then active plugin bundles: a plugin's agent profiles join the search
    only after a project/global miss, and only for a plugin that is trusted +
    enabled + version-compatible (see ``lionagi.plugins``).
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

    dirs = _find_lionagi_dirs()
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


def _parse_profile_timeout(name: str, raw: Any) -> int | None:
    """Validate the profile 'timeout' field; warn and ignore garbage rather than raising.

    See docs/internals/cli.md for why bool/float are rejected, not coerced.
    """
    if raw is None:
        return None
    from ._logging import warn

    if isinstance(raw, bool) or not isinstance(raw, int):
        warn(f"agent profile {name!r}: ignoring invalid timeout {raw!r} (must be a positive int)")
        return None
    if raw <= 0:
        warn(f"agent profile {name!r}: ignoring non-positive timeout {raw!r}")
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


def _parse_profile(name: str, text: str) -> AgentProfile:
    frontmatter, body = _parse_frontmatter(text)

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
            )
        },
    )
