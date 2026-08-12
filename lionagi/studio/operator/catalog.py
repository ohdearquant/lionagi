# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The Operator's model catalog: which models exist, which provider each runs
through, and which reasoning-effort levels each accepts.

Single source of truth for ``GET /operator/models`` and for the selection
the coordinator validates before a turn reaches a provider CLI. See
docs/internals/studio.md ("Model catalog, provider selection, and effort
clamping") for how ids and effort ceilings are grounded in the provider
request models.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

OperatorProvider = Literal["claude_code", "codex", "gemini_code"]
OperatorEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]

# Claude has no none/minimal tier and no ultra tier (ultra clamps to max).
CLAUDE_EFFORTS: tuple[OperatorEffort, ...] = ("low", "medium", "high", "xhigh", "max")
# Codex additionally accepts none/minimal; max/ultra are clamped per model at
# request-build time (see _clamp_codex_effort in lionagi/service/providers.py).
CODEX_EFFORTS: tuple[OperatorEffort, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)
# agy (the gemini-code CLI) has no effort kwarg at all -- effort folds into the
# --model name as Low/Medium/High, so only those three tiers are meaningful.
GEMINI_EFFORTS: tuple[OperatorEffort, ...] = ("low", "medium", "high")

_PROVIDER_EFFORTS: dict[OperatorProvider, tuple[OperatorEffort, ...]] = {
    "claude_code": CLAUDE_EFFORTS,
    "codex": CODEX_EFFORTS,
    "gemini_code": GEMINI_EFFORTS,
}


@dataclass(frozen=True, slots=True)
class OperatorModelSpec:
    id: str
    label: str
    provider: OperatorProvider


# Curated, not exhaustive: every id here is a model name a provider's CLI
# request model or effort-ceiling table actually names (see module docstring).
# Adding a model is a backend-only edit to this tuple.
OPERATOR_MODEL_CATALOG: tuple[OperatorModelSpec, ...] = (
    OperatorModelSpec("sonnet", "Claude Sonnet", "claude_code"),
    OperatorModelSpec("opus", "Claude Opus", "claude_code"),
    OperatorModelSpec("haiku", "Claude Haiku", "claude_code"),
    OperatorModelSpec("claude-fable-5", "Claude Fable", "claude_code"),
    OperatorModelSpec("gpt-5.3-codex", "Codex (gpt-5.3)", "codex"),
    OperatorModelSpec("gpt-5.3-codex-spark", "Codex Spark (gpt-5.3)", "codex"),
    OperatorModelSpec("gpt-5.4", "Codex (gpt-5.4)", "codex"),
    OperatorModelSpec("gpt-5.5", "Codex (gpt-5.5)", "codex"),
    OperatorModelSpec("gpt-5.6-sol", "Codex Sol (gpt-5.6)", "codex"),
    OperatorModelSpec("gpt-5.6-terra", "Codex Terra (gpt-5.6)", "codex"),
    OperatorModelSpec("gpt-5.6-luna", "Codex Luna (gpt-5.6)", "codex"),
    OperatorModelSpec("gemini-3.6-flash", "Gemini 3.6 Flash", "gemini_code"),
    OperatorModelSpec("gemini-3.5-flash", "Gemini 3.5 Flash", "gemini_code"),
    OperatorModelSpec("gemini-3.1-pro", "Gemini 3.1 Pro", "gemini_code"),
)

_BY_ID: dict[str, OperatorModelSpec] = {spec.id: spec for spec in OPERATOR_MODEL_CATALOG}


class OperatorSelectionError(ValueError):
    """A requested provider/model/effort combination the Operator cannot honor."""


def effort_choices(provider: str) -> tuple[OperatorEffort, ...]:
    return _PROVIDER_EFFORTS.get(provider, ())  # type: ignore[arg-type]


def model_effort_choices(model: str) -> tuple[OperatorEffort, ...]:
    """The efforts a specific model actually honors, derived from the same
    clamp functions the request-build path applies rather than restated here
    -- an effort is offered only when clamping it for this model is a no-op.
    See docs/internals/studio.md ("Model catalog, provider selection, and
    effort clamping") for why offering a value the request would silently
    change is worse than not offering it.
    """
    spec = _BY_ID.get(model)
    if spec is None:
        return ()
    from lionagi.service.providers import (
        _GEMINI_EFFORT_CLAMP,
        _clamp_claude_effort,
        _clamp_codex_effort,
        _clamp_gemini_effort,
    )

    offered = _PROVIDER_EFFORTS[spec.provider]
    if spec.provider == "claude_code":
        return tuple(e for e in offered if _clamp_claude_effort(e, spec.id) == e)
    if spec.provider == "codex":
        return tuple(e for e in offered if _clamp_codex_effort(e, spec.id) == e)
    if spec.provider == "gemini_code":
        # Gemini has no effort kwarg: the level becomes part of the model name,
        # so "honored" means the tier this effort names is the tier that gets
        # requested.
        is_pro = "pro" in spec.id
        return tuple(
            e for e in offered if _clamp_gemini_effort(e, is_pro) == _GEMINI_EFFORT_CLAMP.get(e)
        )
    return offered


def catalog_entries() -> list[dict[str, object]]:
    """The wire-serializable catalog: id/label/provider/efforts per model."""
    return [
        {
            "id": spec.id,
            "label": spec.label,
            "provider": spec.provider,
            "efforts": list(model_effort_choices(spec.id)),
        }
        for spec in OPERATOR_MODEL_CATALOG
    ]


def resolve_selection(
    *,
    provider: str | None,
    model: str | None,
    effort: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Validate a client-requested (provider, model, effort) against the catalog.

    Returns the resolved ``(provider, model, effort)`` -- ``None`` for any of
    the three the caller did not specify, so the env-var/default fallback path
    in ``build_operator_branch`` is unchanged for a turn that specifies none of
    them. Raises ``OperatorSelectionError`` for an unknown model, an unknown
    provider, a model that does not belong to an explicitly given provider, or
    an effort the selection cannot honor. When a model is named, effort is
    checked against that model's own ceiling rather than its provider's whole
    vocabulary -- see ``model_effort_choices``.
    """
    resolved_provider = provider
    if model is not None:
        spec = _BY_ID.get(model)
        if spec is None:
            raise OperatorSelectionError(f"Unknown Operator model '{model}'")
        if provider is not None and provider != spec.provider:
            raise OperatorSelectionError(
                f"Model '{model}' belongs to provider '{spec.provider}', not '{provider}'"
            )
        resolved_provider = spec.provider
    elif provider is not None and provider not in _PROVIDER_EFFORTS:
        raise OperatorSelectionError(f"Unknown Operator provider '{provider}'")

    if effort is not None:
        if resolved_provider is None:
            raise OperatorSelectionError("An effort selection requires a provider or model")
        if model is not None:
            if effort not in model_effort_choices(model):
                raise OperatorSelectionError(f"Model '{model}' does not accept effort '{effort}'")
        else:
            allowed = _PROVIDER_EFFORTS.get(resolved_provider)  # type: ignore[arg-type]
            if allowed is None or effort not in allowed:
                raise OperatorSelectionError(
                    f"Provider '{resolved_provider}' does not accept effort '{effort}'"
                )

    return resolved_provider, model, effort
