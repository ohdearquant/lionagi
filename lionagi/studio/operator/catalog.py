# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The Operator's model catalog: which models exist, which provider each runs
through, and which reasoning-effort levels each accepts.

This is the single source of truth the frontend renders from (``GET
/operator/models``) and the coordinator validates a turn's selection against
before it ever reaches a provider CLI. Model identifiers and effort ceilings
are grounded in the provider request models themselves — see
``lionagi/providers/anthropic/claude_code.py`` (``ClaudeEffort``),
``lionagi/providers/openai/codex.py`` plus the codex effort-ceiling tables in
``lionagi/service/providers.py``, and
``lionagi/providers/google/gemini_code.py`` (effort folds into the model
name via ``resolve_agy_model`` rather than a separate parameter).
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
    OperatorModelSpec("gemini-3.6-flash", "Gemini 3.6 Flash", "gemini_code"),
    OperatorModelSpec("gemini-3.5-flash", "Gemini 3.5 Flash", "gemini_code"),
    OperatorModelSpec("gemini-3.1-pro", "Gemini 3.1 Pro", "gemini_code"),
)

_BY_ID: dict[str, OperatorModelSpec] = {spec.id: spec for spec in OPERATOR_MODEL_CATALOG}


class OperatorSelectionError(ValueError):
    """A requested provider/model/effort combination the Operator cannot honor."""


def effort_choices(provider: str) -> tuple[OperatorEffort, ...]:
    return _PROVIDER_EFFORTS.get(provider, ())  # type: ignore[arg-type]


def catalog_entries() -> list[dict[str, object]]:
    """The wire-serializable catalog: id/label/provider/efforts per model."""
    return [
        {
            "id": spec.id,
            "label": spec.label,
            "provider": spec.provider,
            "efforts": list(_PROVIDER_EFFORTS[spec.provider]),
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
    an effort the resolved provider cannot honor.
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
        allowed = _PROVIDER_EFFORTS.get(resolved_provider)  # type: ignore[arg-type]
        if allowed is None or effort not in allowed:
            raise OperatorSelectionError(
                f"Provider '{resolved_provider}' does not accept effort '{effort}'"
            )

    return resolved_provider, model, effort
