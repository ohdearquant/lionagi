# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Provider/model-spec tables and parsing — shared across service and agent layers.

Model spec format: ``provider/model-effort``

    claude/opus-4-7-high   → model="claude/opus-4-7", effort="high"
    codex/gpt-5.4-xhigh   → model="codex/gpt-5.4", effort="xhigh"
    claude/sonnet          → model="claude/sonnet", effort=None

iModel handles provider/model splitting internally. We only strip effort.
"""

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
    "PROVIDER_TO_ALIAS",
    "PROVIDER_YOLO_KWARGS",
    "PROVIDERS_NO_EFFORT",
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


# CLI providers authenticate via a local subprocess (ChatGPT/Claude
# subscription), so their api_key is an irrelevant placeholder. API providers
# (openai, deepseek, anthropic, …) resolve a real key from settings — passing a
# placeholder there OVERRIDES that resolution and breaks auth.
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

# Module-level invariant: a provider cannot be in both sets simultaneously.
# A future maintainer adding a provider to the wrong set gets an ImportError
# at startup instead of silent effort-kwarg corruption at runtime.
# Using raise RuntimeError (not assert) so the check survives `python -O`.
_overlap = PROVIDERS_NO_EFFORT & set(PROVIDER_EFFORT_KWARG)
if _overlap:
    raise RuntimeError(
        f"Provider classification conflict: {_overlap!r} appear in both "
        "PROVIDERS_NO_EFFORT and PROVIDER_EFFORT_KWARG"
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
    "gemini-code": "gemini_code/gemini-3-flash-preview",
    "gemini_code": "gemini_code/gemini-3-flash-preview",
    "gemini-cli": "gemini_code/gemini-3-flash-preview",
    "gemini_cli": "gemini_code/gemini-3-flash-preview",
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
