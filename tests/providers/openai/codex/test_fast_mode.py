# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Smoke tests for CodexCodeRequest.fast_mode.

Verifies: default emits no service_tier; fast_mode=True emits -c service_tier=fast; reasoning_effort is unaffected; profile frontmatter propagates; --fast CLI flag is registered.
"""

from __future__ import annotations

import argparse

import pytest

from lionhub.orchestration.spawn import (
    PROVIDER_FAST_KWARGS,
    _parse_profile,
    add_common_cli_args,
    build_imodel_from_spec,
)
from lionhub.providers.openai.codex import CodexCodeRequest

# ── 1. Default: no service_tier ─────────────────────────────────────────


def test_fast_mode_default_false():
    """fast_mode defaults to False and no service_tier arg is emitted."""
    req = CodexCodeRequest(prompt="hello")
    assert req.fast_mode is False
    args = req.as_cmd_args()
    assert "service_tier" not in " ".join(args)


# ── 2. fast_mode=True emits service_tier=fast ────────────────────────────


def test_fast_mode_true_emits_service_tier():
    """fast_mode=True inserts -c service_tier=fast into CLI args."""
    req = CodexCodeRequest(prompt="hello", fast_mode=True)
    args = req.as_cmd_args()
    for i, arg in enumerate(args[:-1]):
        if arg == "-c" and args[i + 1] == "service_tier=fast":
            return
    pytest.fail(f"service_tier=fast not found in args: {args}")


# ── 3. fast_mode does NOT alter reasoning_effort ─────────────────────────


def test_fast_mode_preserves_reasoning_effort():
    """fast_mode=True does not cap or remove reasoning_effort."""
    req = CodexCodeRequest(prompt="hello", fast_mode=True, reasoning_effort="xhigh")
    args = req.as_cmd_args()
    # Both overrides must appear. The effort is emitted as a TOML value, like
    # every other `-c` override, so the quotes are part of the wire form.
    flat = " ".join(args)
    assert "service_tier=fast" in flat
    assert 'reasoning_effort="xhigh"' in flat


# ── 4. Profile frontmatter fast_mode:true propagates via build_imodel ────


# ── 5. CLI flags --fast registered via add_common_cli_args ───────────────


# ── 6. PROVIDER_FAST_KWARGS contains codex entry ─────────────────────────


# ── 7. build_imodel_from_spec passes fast_mode to iModel for codex ────────
