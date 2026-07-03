# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.cli._providers — parse_model_spec, build_imodel_from_spec,
and resolve_persisted_effort."""

import pytest

from lionagi.cli._providers import build_imodel_from_spec, parse_model_spec


def test_parse_model_spec_folds_effort_suffix_for_gemini_code():
    """gemini-code now supports effort levels — folded into the agy model-name
    suffix downstream via resolve_agy_model — so an embedded '-high' suffix
    parses instead of raising."""
    ms = parse_model_spec("gemini-code/gemini-3.1-pro-high")
    assert ms.model == "gemini-code/gemini-3.1-pro"
    assert ms.effort == "high"


def test_parse_model_spec_rejects_effort_for_bare_gemini_provider():
    """The bare 'gemini' provider (direct Google API, not the agy CLI) still
    does not support effort levels — ValueError is raised."""
    with pytest.raises(ValueError, match="does not support effort"):
        parse_model_spec("gemini/gemini-3.1-pro-high")


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


def test_module_invariant_three_way_disjoint_including_effort_via_model_name():
    """PROVIDERS_NO_EFFORT, PROVIDER_EFFORT_KWARG, and PROVIDERS_EFFORT_VIA_MODEL_NAME
    must be pairwise disjoint (providers.py raises RuntimeError at import time if
    not; this re-checks for a readable failure message at the test layer)."""
    from lionagi.cli._providers import (
        PROVIDER_EFFORT_KWARG,
        PROVIDERS_EFFORT_VIA_MODEL_NAME,
        PROVIDERS_NO_EFFORT,
    )

    assert not (PROVIDERS_NO_EFFORT & PROVIDER_EFFORT_KWARG.keys())
    assert not (PROVIDERS_NO_EFFORT & PROVIDERS_EFFORT_VIA_MODEL_NAME)
    assert not (PROVIDER_EFFORT_KWARG.keys() & PROVIDERS_EFFORT_VIA_MODEL_NAME)


# ── gemini-code / agy effort folding ───────────────────────────────────────
# agy (Antigravity CLI) has no effort flag/kwarg — --effort must fold into
# the resolved --model name suffix instead (see resolve_agy_model). These
# cover the CLI-integration layer (build_chat_model / resolve_persisted_effort);
# tests/providers/google/gemini_code/test_ndjson_mapping.py covers
# resolve_agy_model itself directly.


def test_gemini_code_no_longer_in_no_effort_but_bare_gemini_still_is():
    """The four agy-backed aliases moved out of PROVIDERS_NO_EFFORT into
    PROVIDERS_EFFORT_VIA_MODEL_NAME; bare 'gemini' (the API provider) stays."""
    from lionagi.cli._providers import PROVIDERS_EFFORT_VIA_MODEL_NAME, PROVIDERS_NO_EFFORT

    for alias in ("gemini_code", "gemini-code", "gemini_cli", "gemini-cli"):
        assert alias not in PROVIDERS_NO_EFFORT
        assert alias in PROVIDERS_EFFORT_VIA_MODEL_NAME
    assert "gemini" in PROVIDERS_NO_EFFORT
    assert "gemini" not in PROVIDERS_EFFORT_VIA_MODEL_NAME


def test_build_chat_model_folds_effort_into_gemini_code_model_name():
    """--effort high for gemini-code folds into the agy model-name suffix
    (agy has no effort kwarg) instead of being silently dropped."""
    from lionagi.cli._providers import build_chat_model

    chat_model = build_chat_model(
        "gemini-code", "gemini-3.5-flash", False, False, None, "high", False
    )
    assert isinstance(chat_model, str), f"expected bare spec string, got {type(chat_model)}"
    assert chat_model == "gemini-code/Gemini 3.5 Flash (High)"


def test_build_chat_model_folds_max_effort_to_high_for_gemini_code():
    """--effort max clamps to agy's High tier (agy has no 'max')."""
    from lionagi.cli._providers import build_chat_model

    chat_model = build_chat_model(
        "gemini-code", "gemini-3.5-flash", False, False, None, "max", False
    )
    assert chat_model == "gemini-code/Gemini 3.5 Flash (High)"


def test_build_chat_model_folds_medium_effort_to_high_for_gemini_pro():
    """Gemini 3.1 Pro has no Medium tier — medium clamps to High."""
    from lionagi.cli._providers import build_chat_model

    chat_model = build_chat_model(
        "gemini-code", "gemini-3.1-pro", False, False, None, "medium", False
    )
    assert chat_model == "gemini-code/Gemini 3.1 Pro (High)"


def test_build_chat_model_explicit_qualified_model_wins_over_effort():
    """An explicit (...)-qualified model name is already a concrete agy
    display name — it wins over --effort rather than being reinterpreted."""
    from lionagi.cli._providers import build_chat_model

    chat_model = build_chat_model(
        "gemini-code", "Gemini 3.5 Flash (Low)", False, False, None, "high", False
    )
    assert chat_model == "gemini-code/Gemini 3.5 Flash (Low)"


def test_build_chat_model_no_effort_leaves_gemini_model_unresolved():
    """No --effort given: build_chat_model does not eagerly resolve through
    resolve_agy_model (unchanged from pre-effort-folding behavior) — the bare
    spec passes through and is resolved lazily in GeminiCodeRequest.as_cmd_args()."""
    from lionagi.cli._providers import build_chat_model

    chat_model = build_chat_model(
        "gemini-code", "gemini-3.5-flash", False, False, None, None, False
    )
    assert chat_model == "gemini-code/gemini-3.5-flash"


def test_resolve_persisted_effort_gemini_code_keeps_requested_effort():
    """Unlike bare 'gemini', gemini-code now persists the requested effort
    instead of forcing None — it consumes effort via model-name resolution,
    not a PROVIDER_EFFORT_KWARG entry (there still isn't one for gemini-code)."""
    from lionagi.cli._providers import (
        PROVIDER_EFFORT_KWARG,
        build_chat_model,
        resolve_persisted_effort,
    )

    provider = "gemini-code"
    assert provider not in PROVIDER_EFFORT_KWARG

    chat_model = build_chat_model(provider, "gemini-3.5-flash", False, False, None, "high", False)
    assert isinstance(chat_model, str)

    result = resolve_persisted_effort(provider, chat_model, "high")
    assert result == "high", f"expected requested effort to persist for gemini-code, got {result!r}"


# ── mixed-case --effort on effort-via-model-name paths ─
# All clamp tables (_CODEX_EFFORT_CLAMP, _clamp_claude_effort,
# _GEMINI_EFFORT_CLAMP) are lowercase-keyed. A mixed-case --effort silently
# misclamps instead of raising (worst on gemini: "High" -> "Medium" fallback).


def test_normalize_effort_lowercases_and_passes_through_none():
    from lionagi.service.providers import normalize_effort

    assert normalize_effort("High") == "high"
    assert normalize_effort("XHIGH") == "xhigh"
    assert normalize_effort("low") == "low"
    assert normalize_effort(None) is None


def test_build_chat_model_mixed_case_effort_folds_correct_gemini_tier():
    """--effort High (capitalized) must fold to the High agy tier, not
    silently misclamp to Medium via a lowercase-keyed dict miss."""
    from lionagi.cli._providers import build_chat_model

    chat_model = build_chat_model(
        "gemini-code", "gemini-3.5-flash", False, False, None, "High", False
    )
    assert chat_model == "gemini-code/Gemini 3.5 Flash (High)", (
        f"mixed-case --effort must not misclamp, got {chat_model!r}"
    )


def test_build_chat_model_mixed_case_effort_matches_lowercase_equivalent():
    from lionagi.cli._providers import build_chat_model

    mixed = build_chat_model("gemini-code", "gemini-3.5-flash", False, False, None, "HIGH", False)
    lower = build_chat_model("gemini-code", "gemini-3.5-flash", False, False, None, "high", False)
    assert mixed == lower


def test_build_imodel_from_spec_mixed_case_max_clamps_codex_to_xhigh(monkeypatch):
    """--effort Max (mixed case) must still clamp to xhigh for codex, not
    pass through raw as "Max"."""
    import lionagi.cli._providers as pmod
    from lionagi.testing import IModelKwargCaptor

    captor = IModelKwargCaptor.fresh()
    monkeypatch.setattr(pmod, "iModel", captor)

    build_imodel_from_spec("codex/gpt-5.4", effort_override="Max")

    assert captor.captures[0].get("reasoning_effort") == "xhigh"


def test_build_imodel_from_spec_mixed_case_xhigh_clamps_claude_to_high(monkeypatch):
    """--effort XHigh on a non-opus-4-7 Claude model must clamp to 'high',
    matching the lowercase 'xhigh' behavior (case must not bypass the clamp)."""
    import lionagi.cli._providers as pmod
    from lionagi.testing import IModelKwargCaptor

    captor = IModelKwargCaptor.fresh()
    monkeypatch.setattr(pmod, "iModel", captor)

    build_imodel_from_spec("claude/sonnet", effort_override="XHigh")

    assert captor.captures[0].get("effort") == "high"
