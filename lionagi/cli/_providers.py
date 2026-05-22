# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Model spec parsing — strip effort suffix, pass the rest to iModel.

Model spec format: ``provider/model-effort``

    claude/opus-4-7-high   → model="claude/opus-4-7", effort="high"
    codex/gpt-5.4-xhigh   → model="codex/gpt-5.4", effort="xhigh"
    claude/sonnet          → model="claude/sonnet", effort=None

iModel handles provider/model splitting internally. We only strip effort.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass

from lionagi import iModel

# ── Effort levels (stripped from spec, mapped to provider kwarg) ──────────

EFFORT_LEVELS = frozenset(
    {
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    }
)

# Codex accepts none|minimal|low|medium|high|xhigh — NOT "max".
# Profiles/orchestrators may emit "max"; clamp to "xhigh" for codex.
_CODEX_EFFORT_CLAMP: dict[str, str] = {"max": "xhigh"}

# Claude: only opus-4-7 accepts xhigh. All other models clamp to high.
_CLAUDE_XHIGH_MODELS = frozenset({"opus", "opus-4-7", "claude-opus-4-7"})


def _clamp_claude_effort(effort: str, model: str) -> str:
    """Clamp xhigh to high for non-opus-4-7 Claude models."""
    if effort != "xhigh":
        return effort
    model_part = model.split("/", 1)[-1] if "/" in model else model
    if model_part in _CLAUDE_XHIGH_MODELS:
        return effort
    return "high"


# provider name → kwarg name for effort
PROVIDER_EFFORT_KWARG: dict[str, str] = {
    "claude-code": "effort",
    "claude_code": "effort",
    "claude": "effort",
    "codex": "reasoning_effort",
    "pi": "thinking",
}

# providers that do NOT support effort
PROVIDERS_NO_EFFORT: frozenset[str] = frozenset(
    {
        "gemini_code",
        "gemini-code",
        "gemini_cli",
        "gemini-cli",
        "gemini",
    }
)

# ── Per-provider yolo kwargs ──────────────────────────────────────────────

PROVIDER_YOLO_KWARGS: dict[str, dict] = {
    "claude_code": {"permission_mode": "bypassPermissions"},
    "claude": {"permission_mode": "bypassPermissions"},
    "codex": {"full_auto": True, "skip_git_repo_check": True},
    "gemini_code": {"yolo": True},
    "gemini-code": {"yolo": True},
    "pi": {"no_tools": False},
}

PROVIDER_BYPASS_KWARGS: dict[str, dict] = {
    "claude_code": {"permission_mode": "bypassPermissions"},
    "claude": {"permission_mode": "bypassPermissions"},
    "codex": {"bypass_approvals": True, "skip_git_repo_check": True},
    "gemini_code": {"yolo": True},
    "gemini-code": {"yolo": True},
    "pi": {"no_tools": False},
}

# fast_mode: route codex via OpenAI priority tier (lower latency, same effort)
# No-op for providers that don't support service_tier.
PROVIDER_FAST_KWARGS: dict[str, dict] = {
    "codex": {"fast_mode": True},
}

PROVIDER_TO_ALIAS: dict[str, str] = {
    "claude_code": "claude",
    "codex": "codex",
    "gemini_code": "gemini-code",
    "pi": "pi",
}

# ── Aliases (bare name → provider/model) ──────────────────────────────────

BACKENDS: dict[str, str] = {
    "claude": "claude_code/sonnet",
    "claude-code": "claude_code/sonnet",
    "claude_code": "claude_code/sonnet",
    "codex": "codex/gpt-5.3-codex-spark",
    "gemini-code": "gemini_code/gemini-3.1-flash-lite-preview",
    "gemini_code": "gemini_code/gemini-3.1-flash-lite-preview",
    "gemini-cli": "gemini_code/gemini-3.1-flash-lite-preview",
    "gemini_cli": "gemini_code/gemini-3.1-flash-lite-preview",
    "pi": "pi/gemini-2.5-flash",
    "pi-code": "pi/gemini-2.5-flash",
    "pi_code": "pi/gemini-2.5-flash",
}


# ── Parsing ───────────────────────────────────────────────────────────────

_EFFORT_SUFFIX_RE = re.compile(
    r"^(.+?)-(" + "|".join(sorted(EFFORT_LEVELS, key=len, reverse=True)) + r")$"
)


@dataclass(frozen=True)
class ModelSpec:
    """Parsed model spec: raw model string (for iModel) + extracted effort."""

    model: str  # "claude/opus-4-7" or "codex/gpt-5.4" — passed to iModel as-is
    effort: str | None  # extracted effort or None

    def __str__(self) -> str:
        if self.effort:
            return f"{self.model}-{self.effort}"
        return self.model


_CLAUDE_MODEL_PREFIXES = ("opus", "sonnet", "haiku")


_CLAUDE_PROVIDER_NAMES = frozenset(
    {
        "claude",
        "claude-code",
        "claude_code",
    }
)


def _normalize_model(spec_or_model: str, provider_hint: str | None = None) -> str:
    """Normalize model name for the target provider.

    Claude Code CLI accepts: 'sonnet', 'opus', 'haiku' (aliases)
    or full names like 'claude-sonnet-4-6', 'claude-opus-4-7'.
    'opus-4-7' is neither — normalize to 'claude-opus-4-7'.

    Handles both 'provider/model' and bare 'model' inputs.
    """
    if "/" in spec_or_model:
        prov, model = spec_or_model.split("/", 1)
        normalized = _normalize_model_name(model, prov)
        return f"{prov}/{normalized}"
    return _normalize_model_name(spec_or_model, provider_hint)


def _normalize_model_name(model: str, provider_hint: str | None = None) -> str:
    """Normalize bare model name (no provider prefix)."""
    if provider_hint and provider_hint in _CLAUDE_PROVIDER_NAMES:
        for prefix in _CLAUDE_MODEL_PREFIXES:
            if model.startswith(prefix) and model != prefix and not model.startswith("claude-"):
                return f"claude-{model}"
    return model


def parse_model_spec(spec: str) -> ModelSpec:
    """Parse effort suffix from spec. Everything else stays intact for iModel.

    Examples::
        "claude/opus-4-7-high"   → ModelSpec("claude/opus-4-7", "high")
        "codex/gpt-5.4-xhigh"   → ModelSpec("codex/gpt-5.4", "xhigh")
        "claude/sonnet"          → ModelSpec("claude/sonnet", None)
        "claude"                 → ModelSpec("claude_code/sonnet", None)  # alias
        "gemini-code/gemini-3.1-pro-high" → ERROR (gemini has no effort)
    """
    # Alias expansion
    if spec in BACKENDS:
        return ModelSpec(model=BACKENDS[spec], effort=None)

    # Split provider for effort validation
    provider_raw = spec.split("/")[0] if "/" in spec else spec

    # Try to strip effort suffix from the full spec
    m = _EFFORT_SUFFIX_RE.match(spec)
    if m:
        model_clean = m.group(1)
        effort = m.group(2)

        # Validate: provider supports effort?
        if provider_raw in PROVIDERS_NO_EFFORT:
            raise ValueError(
                f"Provider '{provider_raw}' does not support effort levels. "
                f"Remove '-{effort}' from '{spec}'."
            )
        return ModelSpec(model=_normalize_model(model_clean, provider_raw), effort=effort)

    return ModelSpec(model=_normalize_model(spec, provider_raw), effort=None)


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
) -> iModel | str:
    """Legacy: for agent.py compat. Returns bare spec string when no flags."""
    extra: dict = {}
    if yolo:
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
        help="Timeout in seconds.",
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
