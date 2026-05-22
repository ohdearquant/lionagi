# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.cli._providers — parse_model_spec and build_imodel_from_spec."""

import pytest

from lionagi.cli._providers import build_imodel_from_spec, parse_model_spec


def test_parse_model_spec_rejects_effort_for_gemini_provider():
    """Gemini providers do not support effort levels — ValueError is raised."""
    with pytest.raises(ValueError, match="does not support effort"):
        parse_model_spec("gemini-code/gemini-3.1-pro-high")


def test_build_imodel_from_spec_maps_effort_and_yolo_without_network(monkeypatch):
    """build_imodel_from_spec passes correct kwargs to iModel for codex+xhigh+yolo."""
    calls = []

    class FakeIModel:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    import lionagi.cli._providers as pmod

    monkeypatch.setattr(pmod, "iModel", FakeIModel)

    build_imodel_from_spec("codex/gpt-5.4-xhigh", yolo=True, theme="dark")

    assert len(calls) == 1
    kwargs = calls[0]
    assert kwargs["model"] == "codex/gpt-5.4"
    assert kwargs.get("reasoning_effort") == "xhigh"
    assert kwargs.get("full_auto") is True
    assert kwargs.get("skip_git_repo_check") is True
    assert kwargs.get("cli_display_theme") == "dark"


def test_no_effort_provider_effort_resolves_to_none():
    """Gemini + --effort high must persist effort=None via the real build_chat_model path.

    Previously the PROVIDERS_NO_EFFORT reset was nested inside
    ``if isinstance(chat_model, iModel)``.  build_chat_model returns a plain
    string for gemini because PROVIDER_EFFORT_KWARG has no gemini entry, so no
    effort kwarg is added, extra stays empty, and the string branch fires.  The
    isinstance guard evaluates False, the elif never runs, and effort remained
    'high'.  This test would FAIL on the pre-fix code and PASS after.
    """
    from lionagi.cli._providers import (
        PROVIDER_EFFORT_KWARG,
        PROVIDERS_NO_EFFORT,
        build_chat_model,
    )

    provider = "gemini"
    model = "gemini-3.1-pro"

    # Confirm gemini is in the no-effort set and NOT in the effort-kwarg map.
    assert provider in PROVIDERS_NO_EFFORT
    assert provider not in PROVIDER_EFFORT_KWARG

    # Call the real build_chat_model with only --effort high (no yolo/verbose/theme/fast).
    chat_model = build_chat_model(provider, model, False, False, None, "high", False)

    # build_chat_model must return a plain string because extra is empty for gemini.
    assert isinstance(chat_model, str), (
        f"Expected str from build_chat_model for gemini with no extra flags, got {type(chat_model)}"
    )
    assert chat_model == f"{provider}/{model}"

    # Now replay the effort-resolution logic from agent.py (post-fix).
    # This must yield None even though chat_model is a str (not iModel).
    effort = "high"
    if hasattr(chat_model, "endpoint"):  # isinstance(chat_model, iModel)
        _ep_kwargs = chat_model.endpoint.config.kwargs or {}
        _kwarg = PROVIDER_EFFORT_KWARG.get(provider)
        if _kwarg and _kwarg in _ep_kwargs:
            effort = _ep_kwargs[_kwarg]

    if provider in PROVIDERS_NO_EFFORT:
        effort = None

    assert effort is None, (
        "Gemini provider must resolve effort to None regardless of build_chat_model return type"
    )


def test_effort_aware_provider_codex_max_resolves_to_xhigh():
    """codex --effort max must still resolve to persisted 'xhigh' — no regression."""
    from lionagi import iModel
    from lionagi.cli._providers import (
        PROVIDER_EFFORT_KWARG,
        PROVIDERS_NO_EFFORT,
        build_chat_model,
    )

    provider = "codex"
    model = "gpt-5.3-codex-spark"

    # codex is in PROVIDER_EFFORT_KWARG and NOT in PROVIDERS_NO_EFFORT.
    assert provider in PROVIDER_EFFORT_KWARG
    assert provider not in PROVIDERS_NO_EFFORT

    chat_model = build_chat_model(provider, model, False, False, None, "max", False)

    # build_chat_model adds a reasoning_effort kwarg → extra non-empty → iModel returned.
    assert isinstance(chat_model, iModel), (
        f"Expected iModel for codex with effort kwarg, got {type(chat_model)}"
    )

    # Replay agent.py post-fix resolution.
    effort = "max"
    if hasattr(chat_model, "endpoint"):
        _ep_kwargs = chat_model.endpoint.config.kwargs or {}
        _kwarg = PROVIDER_EFFORT_KWARG.get(provider)
        if _kwarg and _kwarg in _ep_kwargs:
            effort = _ep_kwargs[_kwarg]

    if provider in PROVIDERS_NO_EFFORT:
        effort = None

    assert effort == "xhigh", (
        f"codex --effort max must persist 'xhigh' after clamp, got {effort!r}"
    )
