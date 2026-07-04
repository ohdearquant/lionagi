# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""CLI-level model construction helpers — re-exports service-layer tables plus iModel builders."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lionagi import iModel
from lionagi._paths import find_lionagi_dirs as _find_lionagi_dirs
from lionagi.libs.frontmatter import parse_frontmatter as _parse_frontmatter
from lionagi.libs.path_safety import validate_bare_name
from lionagi.service.providers import (
    _CLAUDE_PROVIDER_NAMES,
    _CODEX_EFFORT_CLAMP,
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
    "resolve_model_spec",
    "resolve_persisted_effort",
    "AgentProfile",
    "build_deadline_preamble",
    "list_agents",
    "load_agent_profile",
    "_parse_profile",
    "_resolve_profile_path",
    "_validate_bare_name",
)

# ── iModel construction ───────────────────────────────────────────────────


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
                effort = _CODEX_EFFORT_CLAMP.get(effort, effort)
            elif provider_raw in _CLAUDE_PROVIDER_NAMES:
                effort = _clamp_claude_effort(effort, ms.model)
            extra[kwarg] = effort
        elif provider_raw in PROVIDERS_EFFORT_VIA_MODEL_NAME:
            # agy (Antigravity CLI) has no effort kwarg — fold effort into the
            # resolved --model name instead (see resolve_agy_model).
            from lionagi.providers.google.gemini_code import resolve_agy_model

            bare_model = ms.model.split("/", 1)[1] if "/" in ms.model else ms.model
            resolved_model = f"{provider_raw}/{resolve_agy_model(bare_model, effort=effort)}"

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
) -> iModel | str:
    """Legacy: for agent.py compat. Returns bare spec string when no flags."""
    effort = normalize_effort(effort)
    extra: dict = {}
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
                effort = _CODEX_EFFORT_CLAMP.get(effort, effort)
            elif provider in _CLAUDE_PROVIDER_NAMES:
                effort = _clamp_claude_effort(effort, model)
            extra[kwarg] = effort
        elif provider in PROVIDERS_EFFORT_VIA_MODEL_NAME:
            # agy (Antigravity CLI) has no effort kwarg — fold effort into the
            # resolved --model name instead (see resolve_agy_model).
            from lionagi.providers.google.gemini_code import resolve_agy_model

            model = resolve_agy_model(model, effort=effort)

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


def resolve_model_spec(spec: str) -> tuple[str, str]:
    """Legacy compat — returns (provider, model) by splitting on /."""
    ms = parse_model_spec(spec)
    if "/" in ms.model:
        return ms.model.split("/", 1)
    return ms.model, ms.model


# ── CLI common args ───────────────────────────────────────────────────────


def add_common_cli_args(parser: argparse.ArgumentParser) -> None:
    """Add shared CLI flags to any subparser."""
    parser.add_argument("--yolo", action="store_true", help="Auto-approve tool calls.")
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
    parser.add_argument("-v", "--verbose", action="store_true", help="Stream real-time output.")
    parser.add_argument("--theme", choices=("light", "dark"), default=None, help="Terminal theme.")
    parser.add_argument(
        "--effort",
        metavar="LEVEL",
        default=None,
        help=(
            "Override effort (overrides spec suffix). "
            "claude: low|medium|high|xhigh|max. "
            "codex: none|minimal|low|medium|high|xhigh. "
            "gemini-code/gemini-cli: folded into --model as Low|Medium|High "
            "(Gemini 3.1 Pro has no Medium)."
        ),
    )
    parser.add_argument(
        "--cwd",
        metavar="DIR",
        default=None,
        help="Working directory for CLI endpoints.",
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
    # ADR-0020: opt-in skill-orchestration grouping. Set by a skill via
    # ``li invoke start``; threaded through to the session row so the
    # Studio /invocations page can show 14 sessions under one /show row.
    parser.add_argument(
        "--invocation",
        dest="invocation",
        metavar="ID",
        default=None,
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
    fast_mode: bool = False
    lion_system: bool = True
    artifact_defaults: dict | None = None
    timeout: int | None = None
    """Default --timeout (seconds) used when the CLI flag is not given."""
    resume_on_timeout: bool = False
    """Auto-resume-once on a timeout terminal status (profile 'resume_on_timeout: once')."""
    extra: dict = field(default_factory=dict)


def _find_lionagi_dir() -> Path | None:
    """Find first .lionagi/ directory (backward compat)."""
    dirs = _find_lionagi_dirs()
    return dirs[0] if dirs else None


def _resolve_profile_path(agents_dir: Path, name: str) -> Path | None:
    """Return profile path for NAME: directory layout (<name>/<name>.md) before flat (<name>.md)."""
    dir_candidate = agents_dir / name / f"{name}.md"
    if dir_candidate.is_file():
        return dir_candidate
    flat_candidate = agents_dir / f"{name}.md"
    if flat_candidate.is_file():
        return flat_candidate
    return None


def list_agents() -> list[str]:
    """List available agent profile names (merged across all .lionagi/ dirs).

    Discovers both directory (<name>/<name>.md) and flat (<name>.md) layouts.
    """
    seen: set[str] = set()
    for d in _find_lionagi_dirs():
        agents_dir = d / "agents"
        if not agents_dir.is_dir():
            continue
        # Directory layout
        for child in agents_dir.iterdir():
            if child.is_dir() and (child / f"{child.name}.md").is_file():
                seen.add(child.name)
        # Flat legacy layout
        for p in agents_dir.glob("*.md"):
            if p.is_file():
                seen.add(p.stem)
    return sorted(seen)


def load_agent_profile(name: str) -> AgentProfile:
    """Load a named agent profile, searching project-local then global ~/.lionagi/agents/."""
    _validate_bare_name(name)
    dirs = _find_lionagi_dirs()
    if not dirs:
        raise FileNotFoundError(
            "No .lionagi/ directory found. Create .lionagi/agents/ in your repo "
            "or ~/.lionagi/agents/ globally."
        )

    for d in dirs:
        path = _resolve_profile_path(d / "agents", name)
        if path is not None:
            text = path.read_text()
            return _parse_profile(name, text)

    available = list_agents()
    msg = f"Agent profile '{name}' not found"
    if available:
        msg += f"\nAvailable: {', '.join(available)}"
    raise FileNotFoundError(msg)


def _parse_profile_timeout(name: str, raw: Any) -> int | None:
    """Validate the profile 'timeout' field; warn and ignore garbage rather than raising.

    Only a genuine positive int is accepted — YAML booleans (True/False are
    ints in Python) and floats (which int() would silently truncate) are
    rejected rather than coerced.
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
        fast_mode=bool(frontmatter.get("fast_mode", False)),
        lion_system=lion_system,
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
                "fast_mode",
                "lion_system",
                "artifact_defaults",
                "timeout",
                "resume_on_timeout",
            )
        },
    )
