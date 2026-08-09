# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The Claude effort clamp downgrades silently by design: an unsupported
tier becomes a supported one and the request succeeds, so a model missing
from the xhigh allow-list runs a tier lower with nothing in the result
saying so. That makes the membership itself the thing worth pinning --
these tests fail when an Opus identifier is dropped, which is the only
signal the downgrade produces."""

from __future__ import annotations

import pytest

from lionagi.service.providers import _clamp_claude_effort

# Every spelling a caller can reach the Opus line by. Callers pass the bare
# alias, the claude- prefixed id, or a provider-qualified spec, and the clamp
# has to answer the same way for all three.
OPUS_IDENTIFIERS = [
    "opus",
    "opus-4-7",
    "claude-opus-4-7",
    "opus-4-8",
    "claude-opus-4-8",
    "opus-5",
    "claude-opus-5",
    "claude/claude-opus-5",
    "claude_code/opus",
]

# Claude models with no xhigh tier. Sonnet and Fable are current releases, so
# they are not placeholders -- an implementation that granted xhigh to every
# Claude model would pass the Opus cases above and fail here.
NON_XHIGH_CLAUDE_MODELS = [
    "sonnet",
    "haiku",
    "claude-sonnet-5",
    "claude-fable-5",
    "claude/claude-sonnet-5",
]


@pytest.mark.parametrize("model", OPUS_IDENTIFIERS)
def test_opus_line_keeps_xhigh(model):
    assert _clamp_claude_effort("xhigh", model) == "xhigh"


@pytest.mark.parametrize("model", NON_XHIGH_CLAUDE_MODELS)
def test_models_without_an_xhigh_tier_fall_back_to_high(model):
    assert _clamp_claude_effort("xhigh", model) == "high"


@pytest.mark.parametrize("model", OPUS_IDENTIFIERS + NON_XHIGH_CLAUDE_MODELS)
def test_max_is_a_real_claude_tier_and_is_never_clamped(model):
    """Claude has max on every model, so the clamp must leave it alone. It is
    the tier above xhigh, so a table that gates xhigh must not be read as
    gating max as well."""
    assert _clamp_claude_effort("max", model) == "max"


@pytest.mark.parametrize("model", OPUS_IDENTIFIERS + NON_XHIGH_CLAUDE_MODELS)
def test_ultra_is_not_a_claude_tier_and_becomes_max(model):
    assert _clamp_claude_effort("ultra", model) == "max"


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
@pytest.mark.parametrize("model", ["opus", "claude-opus-5", "claude-sonnet-5"])
def test_ordinary_tiers_pass_through_untouched(effort, model):
    assert _clamp_claude_effort(effort, model) == effort
