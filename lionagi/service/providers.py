# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The effort vocabulary and the model-name effort suffix, shared by the core and the hub.

The CLI providers' tables and ``parse_model_spec`` live in ``lionhub.providers.model_spec``.
Their old names here resolve until 2.0: the first access warns once, naming the new module.
See docs/internals/service-layer.md#effort-suffix-routing.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = (
    "BACKENDS",  # noqa: F822 -- served by __getattr__
    "CLI_PROVIDERS",  # noqa: F822 -- served by __getattr__
    "EFFORT_LEVELS",
    "ModelSpec",  # noqa: F822 -- served by __getattr__
    "PROVIDER_BYPASS_KWARGS",  # noqa: F822 -- served by __getattr__
    "PROVIDER_EFFORT_KWARG",  # noqa: F822 -- served by __getattr__
    "PROVIDER_FAST_KWARGS",  # noqa: F822 -- served by __getattr__
    "PROVIDER_REPO_KWARG",  # noqa: F822 -- served by __getattr__
    "PROVIDER_TO_ALIAS",  # noqa: F822 -- served by __getattr__
    "PROVIDER_YOLO_KWARGS",  # noqa: F822 -- served by __getattr__
    "PROVIDERS_EFFORT_VIA_MODEL_NAME",  # noqa: F822 -- served by __getattr__
    "PROVIDERS_NO_EFFORT",  # noqa: F822 -- served by __getattr__
    "codex_unset_effort_default",  # noqa: F822 -- served by __getattr__
    "normalize_effort",
    "parse_model_spec",  # noqa: F822 -- served by __getattr__
    "reclamp_effort_for_model",  # noqa: F822 -- served by __getattr__
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


# The names that moved to ``lionhub.providers.model_spec``. ``__getattr__`` serves each by
# importing this old name, which the moved-module table aliases to the new module, so the
# first access warns at the caller's line and returns the hub's own object. Removed with its
# row at 2.0.
_MODEL_SPEC_OLD_MODULE = "lionagi.service._model_spec"
_MODEL_SPEC_NAMES = frozenset(
    {
        "BACKENDS",
        "CLI_PROVIDERS",
        "ModelSpec",
        "PROVIDERS_EFFORT_VIA_MODEL_NAME",
        "PROVIDERS_NO_EFFORT",
        "PROVIDER_BYPASS_KWARGS",
        "PROVIDER_EFFORT_KWARG",
        "PROVIDER_FAST_KWARGS",
        "PROVIDER_REPO_KWARG",
        "PROVIDER_TO_ALIAS",
        "PROVIDER_YOLO_KWARGS",
        "codex_unset_effort_default",
        "parse_model_spec",
        "reclamp_effort_for_model",
        "_CLAMP_IGNORED_CATEGORIES",
        "_CLAUDE_MODEL_PREFIXES",
        "_CLAUDE_PROVIDER_NAMES",
        "_CLAUDE_XHIGH_MODELS",
        "_CODEX_KNOWN_EFFORT_MODELS",
        "_CODEX_MAX_ONLY_MODELS",
        "_CODEX_NO_MINIMAL_MODELS",
        "_CODEX_ULTRA_MODELS",
        "_CODEX_UNSET_EFFORT_METHOD",
        "_CODEX_XHIGH_CEILING_MODELS",
        "_GEMINI_EFFORT_CLAMP",
        "_GEMINI_XHIGH_EFFORTS",
        "_clamp_claude_effort",
        "_clamp_codex_effort",
        "_clamp_gemini_effort",
        "_clamp_name",
        "_codex_unset_effort_from_reader",
        "_normalize_model",
        "_normalize_model_name",
        "_validate_provider_permission_tables",
    }
)


def __getattr__(name: str) -> Any:
    if name in _MODEL_SPEC_NAMES:
        import importlib

        return getattr(importlib.import_module(_MODEL_SPEC_OLD_MODULE), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
