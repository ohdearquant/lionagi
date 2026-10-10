# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The Claude effort clamp downgrades silently by design: an unsupported
tier becomes a supported one and the request succeeds, so a model missing
from the xhigh allow-list runs a tier lower with nothing in the result
saying so. That makes the membership itself the thing worth pinning --
these tests fail when a documented xhigh identifier is dropped, which is the
only signal the downgrade produces."""

from __future__ import annotations

import pytest

from lionagi.service.providers import _clamp_claude_effort

# Every spelling callers pass for a Claude model with a documented xhigh tier.
# Callers pass the bare
# alias, the claude- prefixed id, or a provider-qualified spec, and the clamp
# has to answer the same way for all three.
XHIGH_IDENTIFIERS = [
    "opus",
    "opus-4-7",
    "claude-opus-4-7",
    "opus-4-8",
    "claude-opus-4-8",
    "opus-5",
    "claude-opus-5",
    "claude/claude-opus-5",
    "claude_code/opus",
    "opus-5-5",
    "claude-opus-5-5",
    "claude/claude-opus-5-5",
    "sonnet",
    "sonnet-5",
    "claude-sonnet-5",
    "sonnet-5-5",
    "claude-sonnet-5-5",
    "claude/claude-sonnet-5-5",
    "haiku",
    "haiku-5-5",
    "claude-haiku-5-5",
    "fable",
    "fable-5",
    "claude-fable-5",
    "fable-5-1",
    "claude-fable-5-1",
    "claude/claude-fable-5-1",
]

# Older Claude models without a documented xhigh tier guard against granting
# xhigh to every model in the provider family.
NON_XHIGH_CLAUDE_MODELS = [
    "claude-sonnet-4-6",
    "claude/claude-sonnet-4-6",
    "claude-haiku-4-5",
]


@pytest.mark.parametrize("model", XHIGH_IDENTIFIERS)
def test_documented_models_keep_xhigh(model):
    assert _clamp_claude_effort("xhigh", model) == "xhigh"


@pytest.mark.parametrize("model", NON_XHIGH_CLAUDE_MODELS)
def test_models_without_an_xhigh_tier_fall_back_to_high(model):
    assert _clamp_claude_effort("xhigh", model) == "high"


@pytest.mark.parametrize("model", XHIGH_IDENTIFIERS + NON_XHIGH_CLAUDE_MODELS)
def test_max_is_a_real_claude_tier_and_is_never_clamped(model):
    """Claude has max on every model, so the clamp must leave it alone. It is
    the tier above xhigh, so a table that gates xhigh must not be read as
    gating max as well."""
    assert _clamp_claude_effort("max", model) == "max"


@pytest.mark.parametrize("model", XHIGH_IDENTIFIERS + NON_XHIGH_CLAUDE_MODELS)
def test_ultra_is_not_a_claude_tier_and_becomes_max(model):
    assert _clamp_claude_effort("ultra", model) == "max"


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
@pytest.mark.parametrize("model", ["opus", "claude-opus-5", "claude-sonnet-5"])
def test_ordinary_tiers_pass_through_untouched(effort, model):
    assert _clamp_claude_effort(effort, model) == effort
