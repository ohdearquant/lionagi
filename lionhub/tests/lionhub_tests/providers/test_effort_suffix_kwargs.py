# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The kwargs an effort-suffixed model name gives each CLI provider that takes effort.

Claude Code takes it as ``effort``, clamped to the model's tiers, codex as ``reasoning_effort``
and pi as ``thinking``, both as written. An effort passed as its own kwarg wins over the
suffix, a name without a suffix sets no effort, and a provider that takes no effort keeps the
name whole. Every case goes through ``iModel`` alone, so the table holds for any release.
"""

from __future__ import annotations

import pytest

from lionagi.service.imodel import iModel

_NO_EFFORT = object()

# (provider, model, extra kwargs, expected model, effort kwarg, expected effort)
_CASES = {
    "claude-high": ("claude_code", "sonnet-high", {}, "sonnet", "effort", "high"),
    "claude-xhigh-tier": ("claude_code", "sonnet-xhigh", {}, "sonnet", "effort", "xhigh"),
    "claude-xhigh-clamped": (
        "claude_code",
        "claude-opus-4-6-xhigh",
        {},
        "claude-opus-4-6",
        "effort",
        "high",
    ),
    "claude-ultra-clamped": ("claude_code", "opus-ultra", {}, "opus", "effort", "max"),
    "claude-alias-folded": ("claude", "opus-MAX", {}, "opus", "effort", "max"),
    "claude-no-suffix": ("claude_code", "sonnet", {}, "sonnet", "effort", _NO_EFFORT),
    "claude-explicit-wins": (
        "claude_code",
        "sonnet-xhigh",
        {"effort": "low"},
        "sonnet",
        "effort",
        "low",
    ),
    "codex-max": ("codex", "gpt-5.4-max", {}, "gpt-5.4", "reasoning_effort", "max"),
    "codex-alias-unclamped": (
        "openai-codex",
        "gpt-5.3-codex-spark-ultra",
        {},
        "gpt-5.3-codex-spark",
        "reasoning_effort",
        "ultra",
    ),
    "codex-no-suffix": ("codex", "gpt-5.4", {}, "gpt-5.4", "reasoning_effort", _NO_EFFORT),
    "codex-explicit-wins": (
        "codex",
        "gpt-5.4-max",
        {"reasoning_effort": "low"},
        "gpt-5.4",
        "reasoning_effort",
        "low",
    ),
    "pi-low": ("pi", "gemini-2.5-flash-low", {}, "gemini-2.5-flash", "thinking", "low"),
    "pi-xhigh-unclamped": (
        "pi",
        "gemini-2.5-flash-xhigh",
        {},
        "gemini-2.5-flash",
        "thinking",
        "xhigh",
    ),
    "pi-explicit-wins": (
        "pi",
        "gemini-2.5-flash-high",
        {"thinking": "medium"},
        "gemini-2.5-flash",
        "thinking",
        "medium",
    ),
}


@pytest.mark.parametrize(
    ("provider", "model", "extra", "expected_model", "kwarg", "expected"),
    list(_CASES.values()),
    ids=list(_CASES),
)
def test_an_effort_suffix_gives_the_provider_its_effort_kwarg(
    provider, model, extra, expected_model, kwarg, expected
):
    built = iModel(provider=provider, model=model, **extra)
    kwargs = built.endpoint.config.kwargs

    assert kwargs["model"] == expected_model
    if expected is _NO_EFFORT:
        assert kwarg not in kwargs
    else:
        assert kwargs[kwarg] == expected


def test_a_provider_named_in_the_model_string_routes_the_same_way():
    built = iModel(model="claude_code/claude-opus-4-6-xhigh")
    kwargs = built.endpoint.config.kwargs

    assert (kwargs["model"], kwargs["effort"]) == ("claude-opus-4-6", "high")


def test_a_provider_that_takes_no_effort_keeps_the_name_whole():
    built = iModel(provider="openai", model="gpt-4.1-mini-high", api_key="test-key")
    kwargs = built.endpoint.config.kwargs

    assert kwargs["model"] == "gpt-4.1-mini-high"
    assert not {"effort", "reasoning_effort", "thinking"} & set(kwargs)
