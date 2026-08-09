# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for lionagi.state.session_naming — display-name derivation."""

from __future__ import annotations

import pytest

from lionagi.state.session_naming import (
    DISPLAY_NAME_MAX_LEN,
    agent_role_label,
    resolve_display_name,
    sanitize_prompt_name,
)

# ── sanitize_prompt_name ──────────────────────────────────────────────────


def test_sanitize_strips_leading_system_message_banner() -> None:
    raw = "LION_SYSTEM_MESSAGE\n\n---\n\n# Welcome to LIONAGI\n\nWe are an intelligence OS."
    out = sanitize_prompt_name(raw)
    assert not out.startswith("LION_SYSTEM_MESSAGE")
    assert not out.startswith("#")
    assert not out.startswith("-")
    assert out.startswith("Welcome to LIONAGI")


def test_sanitize_strips_a_guidance_label_wrapping_a_banner() -> None:
    raw = "Guidance: LION_SYSTEM_MESSAGE\n\n---\n\nSome real instruction text here."
    out = sanitize_prompt_name(raw)
    assert not out.startswith("Guidance:")
    assert not out.startswith("LION_SYSTEM_MESSAGE")
    assert out.startswith("Some real instruction text")


def test_sanitize_never_leaves_a_colond_label_prefix() -> None:
    out = sanitize_prompt_name("Instruction: fix the null pointer bug in the parser")
    assert not out.startswith("Instruction:")
    assert out.startswith("fix the null pointer bug")


def test_sanitize_collapses_internal_whitespace() -> None:
    out = sanitize_prompt_name("fix   the\n\nbug   please")
    assert out == "fix the bug please"


def test_sanitize_caps_length_with_ellipsis() -> None:
    raw = "x" * 200
    out = sanitize_prompt_name(raw)
    assert len(out) == DISPLAY_NAME_MAX_LEN
    assert out.endswith("…")


def test_sanitize_respects_custom_max_len() -> None:
    out = sanitize_prompt_name("x" * 50, max_len=10)
    assert len(out) == 10
    assert out.endswith("…")


def test_sanitize_is_idempotent_on_already_clean_short_text() -> None:
    assert sanitize_prompt_name("a normal short prompt") == "a normal short prompt"


def test_sanitize_empty_or_none_returns_none() -> None:
    assert sanitize_prompt_name(None) is None
    assert sanitize_prompt_name("") is None
    assert sanitize_prompt_name("   ") is None


def test_sanitize_banner_only_input_returns_none_not_empty_string() -> None:
    # Nothing survives stripping -- this is the "had content, produced
    # nothing usable" case, and must be distinguishable (via None) from the
    # "had no content to begin with" case above.
    assert sanitize_prompt_name("LION_SYSTEM_MESSAGE") is None
    assert sanitize_prompt_name("Guidance: LION_SYSTEM_MESSAGE") is None


# ── agent_role_label ──────────────────────────────────────────────────────


def test_agent_role_label_appends_utc_time_disambiguator() -> None:
    # 2026-01-01T14:22:00Z
    started_at = 1767277320.0
    assert agent_role_label("implementer", started_at) == "implementer · 14:22"


def test_agent_role_label_is_deterministic_across_calls() -> None:
    started_at = 1767277320.0
    first = agent_role_label("implementer", started_at)
    second = agent_role_label("implementer", started_at)
    assert first == second


def test_agent_role_label_disambiguates_concurrent_same_agent_runs() -> None:
    # Two "implementer" runs starting at different times must not collide.
    a = agent_role_label("implementer", 1767277320.0)  # 14:22 UTC
    b = agent_role_label("implementer", 1767278040.0)  # 14:34 UTC
    assert a != b
    assert a.startswith("implementer")
    assert b.startswith("implementer")


def test_agent_role_label_falls_back_to_bare_name_without_started_at() -> None:
    assert agent_role_label("implementer", None) == "implementer"


def test_agent_role_label_carries_a_short_slice_of_the_row_id() -> None:
    started_at = 1767277320.0  # 14:22 UTC
    label = agent_role_label("claude-code", started_at, "1167beca-3f2a-4d51-9c88-0e21a4b7d900")
    assert label == "claude-code · 1167 · 14:22"


def test_agent_role_label_separates_same_agent_runs_from_the_same_minute() -> None:
    # The case the surface actually produces: several sessions of one engine,
    # concurrent, so name and minute are identical across all of them. Without
    # the id slice these three labels are one string repeated three times.
    started_at = 1767277320.0
    labels = {
        agent_role_label("claude-code", started_at, run_id)
        for run_id in (
            "f3d1c5d8-1111-4444-8888-000000000001",
            "74c89f95-2222-4444-8888-000000000002",
            "0cd37251-3333-4444-8888-000000000003",
        )
    }
    assert len(labels) == 3


def test_agent_role_label_drops_missing_parts_rather_than_rendering_them_blank() -> None:
    assert agent_role_label("implementer", None, "abc12345") == "implementer · abc1"
    assert agent_role_label("implementer", 1767277320.0, None) == "implementer · 14:22"
    assert agent_role_label("implementer", None, "") == "implementer"


def test_resolve_display_name_feeds_the_row_id_into_the_agent_tier() -> None:
    row = {
        "agent_name": "explorer",
        "started_at": 1767277320.0,
        "id": "1167beca-3f2a-4d51-9c88-0e21a4b7d900",
    }
    assert resolve_display_name(row) == "explorer · 1167 · 14:22"


def test_resolve_display_name_agent_tier_accepts_run_id_when_there_is_no_id() -> None:
    row = {
        "agent_name": "explorer",
        "started_at": 1767277320.0,
        "run_id": "9f0e1d2c-4444-4444-8888-000000000004",
    }
    assert resolve_display_name(row) == "explorer · 9f0e · 14:22"


# ── resolve_display_name: priority chain ──────────────────────────────────


def test_priority_user_label_wins_over_everything() -> None:
    row = {
        "user_label": "my renamed run",
        "show_play_name": "show",
        "playbook_name": "pb",
        "agent_name": "implementer",
        "name": "raw",
        "id": "abc123",
    }
    assert resolve_display_name(row) == "my renamed run"


def test_priority_show_play_name_wins_over_playbook_and_agent() -> None:
    row = {
        "show_play_name": "ADR-0099 rollout",
        "playbook_name": "pb",
        "agent_name": "implementer",
        "name": "raw",
        "id": "abc123",
    }
    assert resolve_display_name(row) == "ADR-0099 rollout"


def test_priority_playbook_name_wins_over_agent_and_raw_name() -> None:
    row = {
        "playbook_name": "pr-merge-review",
        "agent_name": "implementer",
        "name": "raw",
        "id": "abc123",
    }
    assert resolve_display_name(row) == "pr-merge-review"


def test_priority_agent_role_descriptor_wins_over_raw_name() -> None:
    row = {
        "agent_name": "implementer",
        "name": "some raw prompt-derived name",
        "started_at": 1767277320.0,
        "id": "abc123",
    }
    assert resolve_display_name(row) == "implementer · abc1 · 14:22"


def test_agent_only_collision_two_concurrent_runs_resolve_to_distinct_names() -> None:
    row_a = {"agent_name": "implementer", "started_at": 1767277320.0, "id": "a"}
    row_b = {"agent_name": "implementer", "started_at": 1767278040.0, "id": "b"}
    name_a = resolve_display_name(row_a)
    name_b = resolve_display_name(row_b)
    assert name_a != name_b


def test_priority_sanitized_prompt_fallback_when_no_structured_identity() -> None:
    row = {
        "name": "LION_SYSTEM_MESSAGE\n\n---\n\n# Welcome to LIONAGI\n\nfix the bug",
        "id": "abc123",
    }
    out = resolve_display_name(row)
    assert not out.startswith("LION_SYSTEM_MESSAGE")
    assert "fix the bug" in out


def test_over_long_prompt_fallback_is_capped() -> None:
    row = {"name": "x" * 300, "id": "abc123"}
    out = resolve_display_name(row)
    assert len(out) <= DISPLAY_NAME_MAX_LEN


def test_priority_short_id_is_the_last_resort() -> None:
    row = {"id": "0123456789abcdef"}
    assert resolve_display_name(row) == "456789abcdef"


def test_falls_back_to_run_id_key_when_id_absent() -> None:
    row = {"run_id": "0123456789abcdef"}
    assert resolve_display_name(row) == "456789abcdef"


def test_blank_fields_are_treated_as_absent() -> None:
    row = {
        "show_play_name": "  ",
        "playbook_name": "",
        "agent_name": None,
        "name": "reviewer's genuine short name",
        "id": "abc123",
    }
    assert resolve_display_name(row) == "reviewer's genuine short name"


def test_a_yaml_block_indicator_does_not_shield_the_banner() -> None:
    """A prompt routed through a YAML document keeps the block-scalar indicator
    that introduced it, and it lands ahead of the banner. Every strip pattern is
    anchored at the start, so one stray "|-" left the entire system-message
    banner in the display name -- on most of the rows on this machine, not a
    rare shape.
    """
    out = sanitize_prompt_name("|- LION_SYSTEM_MESSAGE --- # Welcome to LIONAGI We are")
    assert out is not None
    assert not out.startswith("|"), out
    assert "LION_SYSTEM_MESSAGE" not in out, out
    assert "Welcome to LIONAGI" in out, out


def test_a_bare_pipe_indicator_is_stripped_too() -> None:
    out = sanitize_prompt_name("| # TASK - grade two answers to one question")
    assert out == "TASK - grade two answers to one question", out


@pytest.mark.parametrize(
    "raw",
    [
        "fix the graph fold so a long chain renders",
        "a || b is the guard we want here",
        "use the | operator to union the two sets",
    ],
    ids=["no-pipe", "pipe-inside", "pipe-as-subject"],
)
def test_prose_is_left_alone(raw: str) -> None:
    """The arm that keeps the strip from over-reaching. Only a leading indicator
    is noise; a pipe anywhere else is the text the reader asked to see."""
    assert sanitize_prompt_name(raw) == raw
