# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0022: provenance helpers.

These are tiny one-shot utilities that the CLI call sites use to write
the resolved model / provider / effort / agent_hash columns at session
creation time. Keeping them in one file makes the write-side responsibilities
easy to audit (every CLI entry point should pass through here).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# Length matches what the ADR pins for the column comment: 16 chars is
# enough for "same or different" comparisons without storing the whole
# 64-char digest.
_AGENT_HASH_LEN = 16


def agent_definition_hash(agent_name: str | None) -> str | None:
    """Return a short SHA-256 fingerprint of an agent profile's content.

    Uses the same resolution order as
    :func:`lionagi.cli._agents.load_agent_profile`: project-local
    ``.lionagi/agents/`` directories first (git root, then cwd walk),
    then ``~/.lionagi/agents/``. Returns ``None`` when the agent name is
    missing or no profile is found; callers should write ``None`` to
    ``sessions.agent_hash`` in that case.
    """
    if not agent_name:
        return None
    from lionagi.cli._agents import _find_lionagi_dirs, _resolve_profile_path

    for d in _find_lionagi_dirs():
        path = _resolve_profile_path(d / "agents", agent_name)
        if path is not None:
            return _hash_file(path)
    return None


def _hash_file(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(data).hexdigest()[:_AGENT_HASH_LEN]


def resolve_model_spec(provider: str | None, model: str | None) -> str | None:
    """Produce the canonical ``"provider/model"`` string for storage.

    ADR-0022 requires the stored value to be the resolved spec, not the
    user input. The CLI already does the parsing — this helper just
    re-joins the parts so callers don't need to remember the separator.
    Returns ``None`` when neither part is known.
    """
    if not provider and not model:
        return None
    if provider and model:
        # Already-qualified inputs ("claude/claude-sonnet-4-6") slip
        # through with their original prefix preserved.
        if "/" in model:
            return model
        return f"{provider}/{model}"
    return provider or model
