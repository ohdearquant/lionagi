# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Provider/model-spec tables and ``parse_model_spec`` — strips effort suffix, expands aliases, shared across service and agent layers."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
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
    "codex_unset_effort_default",
    "normalize_effort",
    "parse_model_spec",
    "reclamp_effort_for_model",
    "split_effort_suffix",
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
# list for gpt-5.6, the provider's model pages for gpt-6). The sets are hand-typed:
# when codex ships a model, someone adds it here or it is treated as unknown. For an
# unknown model an EXPLICIT effort passes through unclamped (the caller asked for it),
# while codex_unset_effort_default emits nothing, since no ceiling knowledge means no
# guessing. See docs/internals/runtime.md.
_CODEX_ULTRA_MODELS = frozenset({"gpt-5.6-sol", "gpt-5.6-terra"})
_CODEX_MAX_ONLY_MODELS = frozenset(
    {"gpt-5.6-luna", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna"}
)
_CODEX_NO_MINIMAL_MODELS = frozenset({"gpt-6.1-terra", "gpt-6-sol", "gpt-6-luna"})
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
# Models whose effort ceiling the tables above actually know.
_CODEX_KNOWN_EFFORT_MODELS = (
    _CODEX_ULTRA_MODELS | _CODEX_MAX_ONLY_MODELS | _CODEX_XHIGH_CEILING_MODELS
)


_CLAMP_IGNORED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def _clamp_name(text: str) -> str:
    """``text`` as the effort clamp compares it: lowercased, without control or format
    characters and without surrounding whitespace, so a name carrying them is clamped
    as the bare name is rather than passing the clamp to the CLI."""
    kept = "".join(c for c in text if unicodedata.category(c) not in _CLAMP_IGNORED_CATEGORIES)
    return kept.strip().lower()


def _clamp_codex_effort(effort: str, model: str | None) -> str:
    """Clamp max/ultra down to the target codex model's supported ceiling."""
    if not isinstance(effort, str):
        # Left for the request's own validation to refuse.
        return effort
    name = _clamp_name(effort)
    if name not in ("max", "ultra"):
        return effort
    model_part = _clamp_name((model or "").split("/", 1)[-1])
    if model_part in _CODEX_XHIGH_CEILING_MODELS:
        return "xhigh"
    if name == "ultra" and model_part in _CODEX_MAX_ONLY_MODELS:
        return "max"
    return effort


def codex_unset_effort_default(
    model: str,
    profile_overrides: dict | None = None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Effort to emit for a codex request whose caller set none.

    With no explicit override the codex CLI falls back to the global config's
    ``model_reasoning_effort``, a value tuned for the config's own default
    model rather than the model this request resolved, and an over-ceiling
    value is rejected by the API (a global ``ultra`` reaching
    gpt-5.3-codex-spark, whose ceiling is ``xhigh``). Emitting that same global
    value pre-clamped keeps the intended depth while staying inside the
    resolved model's vocabulary. A profile that pins ``model_reasoning_effort``
    keeps its pin; no global value means codex's own built-in default applies.

    Ceilings come from the hand-typed tables above, and a model they don't know
    gets nothing emitted. For an unknown or custom (``model_providers``) model
    this returns None, so the child reads the global value unclamped and an
    over-ceiling setting is rejected exactly as it was before. This protects the
    models it knows, not every model. Guessing a ceiling would be worse: it
    would silently downgrade a model legitimately capable of the configured
    depth. The table is the boundary of the guarantee, and widening the
    guarantee means widening the table.

    Every seam that builds a codex request from an unset effort routes through
    here: the CLI builders, the Studio operator path, and the generic agent
    factory. A seam that skips it silently reinstates the defect.

    ``env`` is the environment the spawned codex process will run under, when
    the caller knows it. A request may carry a complete replacement
    environment, and the config codex reads is the one THAT environment names:
    resolving against our own would clamp against a file the child never opens
    while leaving unclamped the one it does.
    """
    if profile_overrides and "model_reasoning_effort" in profile_overrides:
        return None
    from lionagi.providers.openai._codex_profile import global_codex_reasoning_effort

    global_effort = normalize_effort(global_codex_reasoning_effort(env))
    if global_effort is None:
        return None
    model_part = (model or "").split("/", 1)[-1]
    if model_part not in _CODEX_KNOWN_EFFORT_MODELS:
        return None
    if model_part in _CODEX_NO_MINIMAL_MODELS and global_effort == "minimal":
        return None
    return _clamp_codex_effort(global_effort, model)


def reclamp_effort_for_model(provider: str, effort: str | None, model: str | None) -> str | None:
    """Re-clamp an effort that was resolved against a DIFFERENT model.

    A persisted effort carries the ceiling of the model it was resolved for;
    replaying it onto another model can exceed that model's ceiling (an
    ``ultra`` resolved for gpt-5.6-sol replayed on gpt-5.3-codex-spark).
    Callers that swap a model under an existing effort re-clamp here. An alias
    (``openai-codex``) is clamped as the provider it names.
    """
    if effort is None:
        return None
    from lionagi.service.connections.registry import EndpointRegistry

    provider = EndpointRegistry.canonical_provider(provider or "")
    if provider == "codex":
        return _clamp_codex_effort(effort, model)
    if provider in _CLAUDE_PROVIDER_NAMES:
        return _clamp_claude_effort(effort, model or "")
    return effort


# Claude effort ceilings vary by model; the documented xhigh models are
# allow-listed and other models clamp to high. Claude has no ultra tier, so
# ultra clamps to max. Keep both bare aliases and prefixed ids because callers
# pass either form.
_CLAUDE_XHIGH_MODELS = frozenset(
    {
        "opus",
        "opus-4-7",
        "claude-opus-4-7",
        "opus-4-8",
        "claude-opus-4-8",
        "opus-5",
        "claude-opus-5",
        "opus-5-5",
        "claude-opus-5-5",
        "sonnet",
        "sonnet-5",
        "claude-sonnet-5",
        "sonnet-5-5",
        "claude-sonnet-5-5",
        "haiku",
        "haiku-5-5",
        "claude-haiku-5-5",
        "fable",
        "fable-5",
        "claude-fable-5",
        "fable-5-1",
        "claude-fable-5-1",
    }
)


def _clamp_claude_effort(effort: str, model: str) -> str:
    """Clamp ultra to max, and xhigh to high for Claude models without an xhigh tier."""
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
    "claude-code": {"permission_mode": "bypassPermissions"},
    "claude_code": {"permission_mode": "bypassPermissions"},
    "claude": {"permission_mode": "bypassPermissions"},
    "codex": {"full_auto": True, "skip_git_repo_check": True},
    "gemini-cli": {"yolo": True},
    "gemini_cli": {"yolo": True},
    "gemini_code": {"yolo": True},
    "gemini-code": {"yolo": True},
    "pi": {"no_tools": False},
}

PROVIDER_BYPASS_KWARGS: dict[str, dict] = {
    "claude-code": {"permission_mode": "bypassPermissions"},
    "claude_code": {"permission_mode": "bypassPermissions"},
    "claude": {"permission_mode": "bypassPermissions"},
    "codex": {"bypass_approvals": True, "skip_git_repo_check": True},
    "gemini-cli": {"yolo": True},
    "gemini_cli": {"yolo": True},
    "gemini_code": {"yolo": True},
    "gemini-code": {"yolo": True},
    "pi": {"no_tools": False},
}


def _validate_provider_permission_tables(
    cli_providers: set[str] | frozenset[str],
    yolo_kwargs: dict[str, dict],
    bypass_kwargs: dict[str, dict],
) -> None:
    """Raise when a CLI provider is absent from a permission capability table."""
    missing = {
        "PROVIDER_YOLO_KWARGS": sorted(cli_providers - yolo_kwargs.keys()),
        "PROVIDER_BYPASS_KWARGS": sorted(cli_providers - bypass_kwargs.keys()),
    }
    missing = {table: providers for table, providers in missing.items() if providers}
    if missing:
        details = "; ".join(
            f"{table} missing {providers!r}" for table, providers in missing.items()
        )
        raise RuntimeError(f"Provider permission table incomplete: {details}")


# Missing entries silently downgrade execution permissions, so capability table
# drift must fail at import rather than at the first tool call.
_validate_provider_permission_tables(
    CLI_PROVIDERS,
    PROVIDER_YOLO_KWARGS,
    PROVIDER_BYPASS_KWARGS,
)

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
    "gemini-code": "gemini_code/gemini-4-argon",
    "gemini_code": "gemini_code/gemini-4-argon",
    "gemini-cli": "gemini_code/gemini-4-argon",
    "gemini_cli": "gemini_code/gemini-4-argon",
    "pi": "pi/gemini-2.5-flash",
    "pi-code": "pi/gemini-2.5-flash",
    "pi_code": "pi/gemini-2.5-flash",
}


# ── Parsing ───────────────────────────────────────────────────────────────

_EFFORT_SUFFIX_RE = re.compile(
    r"^(.+?)-(" + "|".join(sorted(EFFORT_LEVELS, key=len, reverse=True)) + r")$",
    re.IGNORECASE,
)


def split_effort_suffix(model: str) -> tuple[str, str] | None:
    """Split a bare model name into ``(name, effort)``, or None when it carries none.

    A model name containing "/" is a literal vendor id, not lionagi's own
    ``provider/model-effort`` grammar, so it is never split even if it ends in
    a word that spells an effort level. See
    docs/internals/service-layer.md#effort-suffix-routing.
    """
    if "/" in model:
        return None
    m = _EFFORT_SUFFIX_RE.match(model)
    if m is None:
        return None
    return m.group(1), m.group(2)


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
    from lionagi.service.connections.registry import EndpointRegistry

    # Folded as the endpoint match folds it, so "Claude/opus-4-7" builds as "claude/opus-4-7".
    if (
        provider_hint
        and EndpointRegistry.canonical_provider(provider_hint) in _CLAUDE_PROVIDER_NAMES
    ):
        for prefix in _CLAUDE_MODEL_PREFIXES:
            if model.startswith(prefix) and model != prefix:
                return f"claude-{model}"
    return model


def parse_model_spec(spec: str) -> ModelSpec:
    """Parse provider/model-effort spec: strip effort suffix, expand aliases, validate effort support."""
    if spec in BACKENDS:
        return ModelSpec(model=BACKENDS[spec], effort=None)

    has_provider = "/" in spec
    provider_raw = spec.split("/")[0] if has_provider else spec
    # Match against the model part alone: the provider's own slash must not count
    # against split_effort_suffix's namespaced-id test, or no spec would ever split.
    model_part = spec.split("/", 1)[1] if has_provider else spec

    split = split_effort_suffix(model_part)
    if split is not None:
        name, raw_effort = split
        effort = normalize_effort(raw_effort)

        from lionagi.service.connections.registry import EndpointRegistry

        # Folded as the endpoint match folds it, so an alias such as "gemini-api" is refused
        # as the provider it names.
        provider = EndpointRegistry.canonical_provider(provider_raw) if has_provider else None
        if provider in PROVIDERS_NO_EFFORT:
            raise ValueError(
                f"Provider '{provider_raw}' does not support effort levels. "
                f"Remove '-{effort}' from '{spec}'."
            )
        model_clean = f"{provider_raw}/{name}" if has_provider else name
        return ModelSpec(model=_normalize_model(model_clean, provider_raw), effort=effort)

    return ModelSpec(model=_normalize_model(spec, provider_raw), effort=None)
