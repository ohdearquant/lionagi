# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0077: provenance helpers for writing model/provider/agent_hash columns at session creation."""

from __future__ import annotations

from pathlib import Path

from lionagi.ln._hash import compute_hash

_AGENT_HASH_LEN = 16


def agent_definition_hash(agent_name: str | None) -> str | None:
    """Return a short SHA-256 fingerprint of the agent profile, or None if not found."""
    if not agent_name:
        return None
    from lionagi._paths import find_lionagi_dirs
    from lionagi.cli._providers import _resolve_profile_path

    for d in find_lionagi_dirs():
        path = _resolve_profile_path(d / "agents", agent_name)
        if path is not None:
            return _hash_file(path)
    return None


def _hash_file(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return compute_hash(data)[:_AGENT_HASH_LEN]


def resolve_model_spec(provider: str | None, model: str | None) -> str | None:
    """Return the canonical "provider/model" string for ADR-0077 storage, or None if both absent."""
    if not provider and not model:
        return None
    if provider and model:
        # Already-qualified inputs ("claude/claude-sonnet-4-6") slip
        # through with their original prefix preserved.
        if "/" in model:
            return model
        return f"{provider}/{model}"
    return provider or model
