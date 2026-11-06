# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"Fast model option parsing and forwarding."

from __future__ import annotations

import argparse

from lionhub.orchestration.spawn import (
    PROVIDER_FAST_KWARGS,
    _parse_profile,
    add_common_cli_args,
    build_imodel_from_spec,
)


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
    import lionhub.orchestration.spawn as pmod
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


def test_provider_fast_kwargs_codex():
    """PROVIDER_FAST_KWARGS maps codex → fast_mode=True."""
    assert "codex" in PROVIDER_FAST_KWARGS
    assert PROVIDER_FAST_KWARGS["codex"] == {"fast_mode": True}


def test_build_imodel_from_spec_fast_flag(monkeypatch):
    """build_imodel_from_spec with fast=True sets fast_mode on the iModel."""
    import lionhub.orchestration.spawn as pmod
    from lionagi.testing import IModelKwargCaptor

    captor = IModelKwargCaptor.fresh()
    monkeypatch.setattr(pmod, "iModel", captor)

    build_imodel_from_spec("codex/gpt-5.5-xhigh", fast=True)

    assert len(captor.captures) == 1
    kw = captor.captures[0]
    assert kw.get("fast_mode") is True
    # Effort should still be set
    assert kw.get("reasoning_effort") == "xhigh"
