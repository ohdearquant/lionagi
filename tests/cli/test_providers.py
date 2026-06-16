# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.cli._providers — parse_model_spec, build_imodel_from_spec,
and resolve_persisted_effort."""

import pytest

from lionagi.cli._providers import build_imodel_from_spec, parse_model_spec


def test_parse_model_spec_rejects_effort_for_gemini_provider():
    """Gemini providers do not support effort levels — ValueError is raised."""
    with pytest.raises(ValueError, match="does not support effort"):
        parse_model_spec("gemini-code/gemini-3.1-pro-high")


def test_build_imodel_from_spec_maps_effort_and_yolo_without_network(monkeypatch):
    """build_imodel_from_spec passes correct kwargs to iModel for codex+xhigh+yolo."""
    import lionagi.cli._providers as pmod
    from lionagi.testing import IModelKwargCaptor

    captor = IModelKwargCaptor.fresh()
    monkeypatch.setattr(pmod, "iModel", captor)

    build_imodel_from_spec("codex/gpt-5.4-xhigh", yolo=True, theme="dark")

    assert len(captor.captures) == 1
    kwargs = captor.captures[0]
    assert kwargs["model"] == "codex/gpt-5.4"
    assert kwargs.get("reasoning_effort") == "xhigh"
    assert kwargs.get("full_auto") is True
    assert kwargs.get("skip_git_repo_check") is True
    assert kwargs.get("cli_display_theme") == "dark"


# ── resolve_persisted_effort — tests against the helper directly ──────────
# These test the actual production function, not a hand-rolled copy of the
# agent.py logic. Moving the PROVIDERS_NO_EFFORT reset back under the iModel
# guard in the helper will break these tests immediately.


def test_resolve_persisted_effort_gemini_returns_none():
    """Gemini + effort='high' must resolve to None regardless of chat_model type (gemini returns str)."""
    from lionagi.cli._providers import (
        PROVIDER_EFFORT_KWARG,
        PROVIDERS_NO_EFFORT,
        build_chat_model,
        resolve_persisted_effort,
    )

    provider = "gemini"
    model = "gemini-3.1-pro"

    assert provider in PROVIDERS_NO_EFFORT
    assert provider not in PROVIDER_EFFORT_KWARG

    chat_model = build_chat_model(provider, model, False, False, None, "high", False)

    # gemini returns str (no extra flags → empty extra → str branch)
    assert isinstance(chat_model, str), (
        f"Expected str from build_chat_model for gemini with no extra flags, got {type(chat_model)}"
    )

    result = resolve_persisted_effort(provider, chat_model, "high")
    assert result is None, f"resolve_persisted_effort must return None for gemini, got {result!r}"


def test_resolve_persisted_effort_codex_max_clamps_to_xhigh():
    """codex + effort='max' resolves to 'xhigh' (build_chat_model clamps max→xhigh in the iModel kwargs)."""
    from lionagi import iModel
    from lionagi.cli._providers import (
        PROVIDER_EFFORT_KWARG,
        PROVIDERS_NO_EFFORT,
        build_chat_model,
        resolve_persisted_effort,
    )

    provider = "codex"
    model = "gpt-5.3-codex-spark"

    assert provider in PROVIDER_EFFORT_KWARG
    assert provider not in PROVIDERS_NO_EFFORT

    chat_model = build_chat_model(provider, model, False, False, None, "max", False)

    # codex returns iModel (reasoning_effort kwarg present → extra non-empty)
    assert isinstance(chat_model, iModel), (
        f"Expected iModel for codex with effort kwarg, got {type(chat_model)}"
    )

    result = resolve_persisted_effort(provider, chat_model, "max")
    assert result == "xhigh", (
        f"resolve_persisted_effort must return 'xhigh' for codex max, got {result!r}"
    )


def test_resolve_persisted_effort_no_effort_wins_over_imodel():
    """PROVIDERS_NO_EFFORT overrides even an iModel with an effort kwarg (defence-in-depth)."""
    from lionagi.cli._providers import resolve_persisted_effort

    class _FakeEndpointConfig:
        kwargs = {"reasoning_effort": "xhigh"}
        provider = "fake_no_effort_provider"

    class _FakeEndpoint:
        config = _FakeEndpointConfig()

    class _FakeIModel:
        endpoint = _FakeEndpoint()

    # Patch PROVIDERS_NO_EFFORT for the duration of this call only.
    import lionagi.cli._providers as pmod

    original = pmod.PROVIDERS_NO_EFFORT
    try:
        pmod.PROVIDERS_NO_EFFORT = frozenset({"fake_no_effort_provider"})
        result = resolve_persisted_effort("fake_no_effort_provider", _FakeIModel(), "xhigh")
    finally:
        pmod.PROVIDERS_NO_EFFORT = original

    assert result is None, (
        f"PROVIDERS_NO_EFFORT must override even an iModel with an effort kwarg, got {result!r}"
    )


def test_module_invariant_sets_are_disjoint():
    """PROVIDERS_NO_EFFORT and PROVIDER_EFFORT_KWARG must be disjoint (re-checked for a readable failure message)."""
    from lionagi.cli._providers import PROVIDER_EFFORT_KWARG, PROVIDERS_NO_EFFORT

    overlap = PROVIDERS_NO_EFFORT & PROVIDER_EFFORT_KWARG.keys()
    assert not overlap, (
        f"Provider(s) {overlap!r} appear in both PROVIDERS_NO_EFFORT and "
        f"PROVIDER_EFFORT_KWARG — this is a classification conflict"
    )
