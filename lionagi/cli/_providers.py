# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""CLI-level model construction helpers — re-exports service-layer tables plus iModel builders."""

from __future__ import annotations

import argparse
from typing import Any

from lionagi import iModel
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
    PROVIDERS_NO_EFFORT,
    ModelSpec,
    _clamp_claude_effort,
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
    "PROVIDERS_NO_EFFORT",
    "add_common_cli_args",
    "build_chat_model",
    "build_imodel_from_spec",
    "parse_model_spec",
    "resolve_model_spec",
    "resolve_persisted_effort",
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
    effort = effort_override if effort_override is not None else ms.effort

    extra: dict = {}

    # Resolve provider for yolo/effort kwarg lookup
    provider_raw = ms.model.split("/")[0] if "/" in ms.model else ms.model

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

    return iModel(
        model=ms.model,
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
            "codex: none|minimal|low|medium|high|xhigh."
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
