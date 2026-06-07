# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Smoke tests for CodexCodeRequest.fast_mode (issue #964).

Tests verify that:
1. fast_mode=False (default) emits no service_tier arg.
2. fast_mode=True emits ``-c service_tier=fast`` in the command args.
3. fast_mode does NOT cap or remove reasoning_effort.
4. Profile frontmatter ``fast_mode: true`` is parsed and propagated to
   CodexCodeRequest via build_imodel_from_spec (mocked iModel).
5. ``li agent --fast`` and ``li play --fast`` flags are registered in
   the argparse namespace via add_common_cli_args.
"""

from __future__ import annotations

import argparse

import pytest

from lionagi.cli._agents import _parse_profile
from lionagi.cli._providers import (
    PROVIDER_FAST_KWARGS,
    add_common_cli_args,
    build_imodel_from_spec,
)
from lionagi.providers.openai.codex.models import CodexCodeRequest

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
    # Both service_tier=fast and reasoning_effort=xhigh must appear
    flat = " ".join(args)
    assert "service_tier=fast" in flat
    assert "reasoning_effort=xhigh" in flat


# ── 4. Profile frontmatter fast_mode:true propagates via build_imodel ────


def test_profile_fast_mode_parsed(monkeypatch):
    """Profile with fast_mode: true sets fast_mode=True on CodexCodeRequest."""
    profile_text = """\
---
model: codex/gpt-5.5
effort: high
fast_mode: true
---

You are an implementer.
"""
    profile = _parse_profile("test-fast", profile_text)
    assert profile.fast_mode is True

    # Verify it propagates through build_imodel_from_spec → CodexCodeRequest
    import lionagi.cli._providers as pmod
    from lionagi.testing import IModelKwargCaptor

    captor = IModelKwargCaptor.fresh()
    monkeypatch.setattr(pmod, "iModel", captor)

    build_imodel_from_spec(
        "codex/gpt-5.5",
        fast=profile.fast_mode,
        effort_override=profile.effort,
    )

    assert len(captor.captures) == 1
    kw = captor.captures[0]
    # PROVIDER_FAST_KWARGS["codex"] = {"fast_mode": True}
    assert kw.get("fast_mode") is True
    assert kw.get("reasoning_effort") == "high"


# ── 5. CLI flags --fast registered via add_common_cli_args ───────────────


def test_add_common_cli_args_registers_fast_flag():
    """--fast flag is added by add_common_cli_args."""
    parser = argparse.ArgumentParser()
    add_common_cli_args(parser)
    args = parser.parse_args(["--fast"])
    assert args.fast is True


def test_add_common_cli_args_fast_defaults_false():
    """--fast defaults to False when not provided."""
    parser = argparse.ArgumentParser()
    add_common_cli_args(parser)
    args = parser.parse_args([])
    assert args.fast is False


# ── 6. PROVIDER_FAST_KWARGS contains codex entry ─────────────────────────


def test_provider_fast_kwargs_codex():
    """PROVIDER_FAST_KWARGS maps codex → fast_mode=True."""
    assert "codex" in PROVIDER_FAST_KWARGS
    assert PROVIDER_FAST_KWARGS["codex"] == {"fast_mode": True}


# ── 7. build_imodel_from_spec passes fast_mode to iModel for codex ────────


def test_build_imodel_from_spec_fast_flag(monkeypatch):
    """build_imodel_from_spec with fast=True sets fast_mode on the iModel."""
    import lionagi.cli._providers as pmod
    from lionagi.testing import IModelKwargCaptor

    captor = IModelKwargCaptor.fresh()
    monkeypatch.setattr(pmod, "iModel", captor)

    build_imodel_from_spec("codex/gpt-5.5-xhigh", fast=True)

    assert len(captor.captures) == 1
    kw = captor.captures[0]
    assert kw.get("fast_mode") is True
    # Effort should still be set
    assert kw.get("reasoning_effort") == "xhigh"
