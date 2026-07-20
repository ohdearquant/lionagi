# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Provider/model-spec tables and ``parse_model_spec`` — strips effort suffix, expands aliases, shared across service and agent layers."""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = (
    "BACKENDS",
    "CLI_PROVIDERS",
    "EFFORT_LEVELS",
    "ModelSpec",
    "PROVIDER_BYPASS_KWARGS",
    "PROVIDER_EFFORT_KWARG",
    "PROVIDER_FAST_KWARGS",
    "PROVIDER_REPO_KWARG",
    "PROVIDER_TO_ALIAS",
    "PROVIDER_YOLO_KWARGS",
    "PROVIDERS_EFFORT_VIA_MODEL_NAME",
    "PROVIDERS_NO_EFFORT",
    "normalize_effort",
    "parse_model_spec",
)

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
        "ultra",
    }
)


def normalize_effort(effort: str | None) -> str | None:
    """Case-fold a raw effort string to lionagi's lowercase vocabulary. Call once at
    each entry boundary — clamp tables silently misclamp un-normalized values. See docs/internals/runtime.md."""
    return effort.lower() if isinstance(effort, str) else effort


# Codex reasoning-effort ceilings are model-dependent (source: codex CLI's live model
# list); unrecognized (future) models pass through unclamped. See docs/internals/runtime.md.
_CODEX_ULTRA_MODELS = frozenset({"gpt-5.6-sol", "gpt-5.6-terra"})
_CODEX_MAX_ONLY_MODELS = frozenset({"gpt-5.6-luna"})
_CODEX_XHIGH_CEILING_MODELS = frozenset(
    {
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.3-codex",
        "gpt-5.3-codex-spark",
        "codex-auto-review",
    }
)


def _clamp_codex_effort(effort: str, model: str | None) -> str:
    """Clamp max/ultra down to the target codex model's supported ceiling."""
    if effort not in ("max", "ultra"):
        return effort
    model_part = (model or "").split("/", 1)[-1]
    if model_part in _CODEX_XHIGH_CEILING_MODELS:
        return "xhigh"
    if effort == "ultra" and model_part in _CODEX_MAX_ONLY_MODELS:
        return "max"
    return effort


# Claude: only opus-4-7 accepts xhigh. All other models clamp to high.
# Claude has no ultra tier at all: ultra clamps to max for every model.
_CLAUDE_XHIGH_MODELS = frozenset({"opus", "opus-4-7", "claude-opus-4-7"})


def _clamp_claude_effort(effort: str, model: str) -> str:
    """Clamp ultra to max, and xhigh to high for non-opus-4-7 Claude models."""
    if effort == "ultra":
        return "max"
    if effort != "xhigh":
        return effort
    model_part = model.split("/", 1)[-1] if "/" in model else model
    if model_part in _CLAUDE_XHIGH_MODELS:
        return effort
    return "high"


# agy has no effort kwarg — effort is baked into the --model name as Low/Medium/High,
# and Gemini 3.1 Pro has no Medium tier. See docs/internals/runtime.md.
_GEMINI_EFFORT_CLAMP: dict[str, str] = {
    "none": "Low",
    "minimal": "Low",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "High",
    "max": "High",
    "ultra": "High",
}


def _clamp_gemini_effort(effort: str, is_pro: bool) -> str:
    """Map lionagi's effort vocabulary onto agy's Low/Medium/High tiers; Pro has no Medium."""
    tier = _GEMINI_EFFORT_CLAMP.get(effort, "Medium")
    if is_pro and tier == "Medium":
        return "High"
    return tier


# CLI providers use subprocess auth; api_key is a placeholder. Passing a placeholder to API providers OVERRIDES key resolution.
CLI_PROVIDERS: frozenset[str] = frozenset(
    {
        "claude_code",
        "claude-code",
        "claude",
        "codex",
        "gemini_code",
        "gemini-code",
        "gemini_cli",
        "gemini-cli",
        "pi",
    }
)


PROVIDER_EFFORT_KWARG: dict[str, str] = {
    "claude-code": "effort",
    "claude_code": "effort",
    "claude": "effort",
    "codex": "reasoning_effort",
    "pi": "thinking",
}

# Every CLI provider's request model runs its subprocess against a `repo`
# field that defaults to the calling process's cwd (CodexCodeRequest,
# ClaudeCodeRequest, GeminiCodeRequest, PiCodeRequest all declare
# `repo: Path = Field(default_factory=Path.cwd, exclude=True)`), so an agent
# assigned a workspace needs that value forwarded explicitly.
PROVIDER_REPO_KWARG: dict[str, str] = {p: "repo" for p in CLI_PROVIDERS}

# agy-backed aliases fold effort into the resolved --model name via resolve_agy_model
# instead of a kwarg — classified separately from PROVIDER_EFFORT_KWARG below.
PROVIDERS_EFFORT_VIA_MODEL_NAME: frozenset[str] = frozenset(
    {
        "gemini_code",
        "gemini-code",
        "gemini_cli",
        "gemini-cli",
    }
)

# Bare "gemini" is the direct Google API provider, distinct from the agy CLI
# above, and has no effort concept at all.
PROVIDERS_NO_EFFORT: frozenset[str] = frozenset(
    {
        "gemini",
    }
)

# Invariant: the three provider-effort classifications above are mutually exclusive; RuntimeError (not assert) survives -O.
_overlap = (
    (PROVIDERS_NO_EFFORT & set(PROVIDER_EFFORT_KWARG))
    | (PROVIDERS_NO_EFFORT & PROVIDERS_EFFORT_VIA_MODEL_NAME)
    | (set(PROVIDER_EFFORT_KWARG) & PROVIDERS_EFFORT_VIA_MODEL_NAME)
)
if _overlap:
    raise RuntimeError(
        f"Provider classification conflict: {_overlap!r} appear in more than one "
        "of PROVIDERS_NO_EFFORT, PROVIDER_EFFORT_KWARG, PROVIDERS_EFFORT_VIA_MODEL_NAME"
    )
del _overlap

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
    "gemini-code": "gemini_code/gemini-3.5-flash",
    "gemini_code": "gemini_code/gemini-3.5-flash",
    "gemini-cli": "gemini_code/gemini-3.5-flash",
    "gemini_cli": "gemini_code/gemini-3.5-flash",
    "pi": "pi/gemini-2.5-flash",
    "pi-code": "pi/gemini-2.5-flash",
    "pi_code": "pi/gemini-2.5-flash",
}


# ── Parsing ───────────────────────────────────────────────────────────────

_EFFORT_SUFFIX_RE = re.compile(
    r"^(.+?)-(" + "|".join(sorted(EFFORT_LEVELS, key=len, reverse=True)) + r")$",
    re.IGNORECASE,
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


_CLAUDE_MODEL_PREFIXES = ("opus", "sonnet", "haiku", "fable")


_CLAUDE_PROVIDER_NAMES = frozenset(
    {
        "claude",
        "claude-code",
        "claude_code",
    }
)


def _normalize_model(spec_or_model: str, provider_hint: str | None = None) -> str:
    """Normalize model name: prefixes bare Claude model names (e.g. 'opus-4-7' → 'claude-opus-4-7')."""
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
    """Parse provider/model-effort spec: strip effort suffix, expand aliases, validate effort support."""
    if spec in BACKENDS:
        return ModelSpec(model=BACKENDS[spec], effort=None)

    provider_raw = spec.split("/")[0] if "/" in spec else spec

    m = _EFFORT_SUFFIX_RE.match(spec)
    if m:
        model_clean = m.group(1)
        effort = normalize_effort(m.group(2))

        if provider_raw in PROVIDERS_NO_EFFORT:
            raise ValueError(
                f"Provider '{provider_raw}' does not support effort levels. "
                f"Remove '-{effort}' from '{spec}'."
            )
        return ModelSpec(model=_normalize_model(model_clean, provider_raw), effort=effort)

    return ModelSpec(model=_normalize_model(spec, provider_raw), effort=None)
