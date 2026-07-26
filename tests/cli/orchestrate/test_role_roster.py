# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The planner roster line for each role: description plus model."""

from __future__ import annotations

import pytest

from lionagi.cli._providers import AgentProfile
from lionagi.cli.orchestrate import _orchestration as orch


@pytest.fixture
def stub_profiles(monkeypatch):
    """Serve profiles from a dict instead of the user's agents directories."""
    table: dict[str, AgentProfile] = {}

    def fake_load(name: str) -> AgentProfile:
        try:
            return table[name]
        except KeyError:
            raise FileNotFoundError(name) from None

    monkeypatch.setattr(orch, "load_agent_profile", fake_load)
    return table


class TestRoleBlurb:
    def test_profile_body_describes_the_role_it_replaces(self, stub_profiles):
        """A profile that defines a body replaces the built-in role body, so the
        roster must describe the profile rather than the role it shadowed."""
        from lionagi.casts.pattern import Role

        stub_profiles["critic"] = AgentProfile(
            name="critic",
            raw_body="# Critic\n\n**Mission**: gate the release.",
            system_prompt="# Critic\n\n**Mission**: gate the release.",
            model="codex/gpt-5",
        )
        blurb = orch._role_blurb("critic", "openai/gpt-4.1-mini")

        assert "gate the release" in blurb
        assert "(model: codex/gpt-5)" in blurb
        opening = Role.load("critic").description.split(". ", 1)[0]
        assert opening[:60] not in blurb

    def test_profile_that_only_sets_a_model_keeps_the_builtin_description(self, stub_profiles):
        """Such a profile leaves the built-in body composing, so the built-in
        description is the accurate one; the profile still names the model."""
        from lionagi.casts.pattern import Role

        stub_profiles["critic"] = AgentProfile(name="critic", model="codex/gpt-5")
        blurb = orch._role_blurb("critic", "openai/gpt-4.1-mini")

        opening = Role.load("critic").description.split(". ", 1)[0]
        assert opening[:60] in blurb
        assert "(model: codex/gpt-5)" in blurb

    def test_builtin_without_profile_is_description_only(self, stub_profiles):
        blurb = orch._role_blurb("contrarian", "openai/gpt-4.1-mini")
        assert "minority" in blurb
        assert "model:" not in blurb

    def test_profile_only_role_gets_a_non_empty_blurb(self, stub_profiles):
        stub_profiles["deckhand"] = AgentProfile(
            name="deckhand",
            raw_body=(
                "# alpha[Deckhand]\n\n"
                "`deckhand -> LION`\n\n"
                "**Mission**: keep the rigging sound and the decks clear.\n\n"
                "## Identity\n\nYou are the deckhand.\n"
            ),
            model="codex/gpt-5",
        )
        blurb = orch._role_blurb("deckhand", "openai/gpt-4.1-mini")

        assert "keep the rigging sound and the decks clear" in blurb
        assert "(model: codex/gpt-5)" in blurb
        assert "user profile" not in blurb

    def test_profile_without_mission_uses_its_first_prose(self, stub_profiles):
        stub_profiles["deckhand"] = AgentProfile(
            name="deckhand",
            raw_body=("# Deckhand\n\nYou keep the rigging sound. Second sentence is dropped.\n"),
            model="codex/gpt-5",
        )
        blurb = orch._role_blurb("deckhand", "openai/gpt-4.1-mini")

        assert blurb.startswith("You keep the rigging sound")
        assert "Second sentence" not in blurb

    def test_unusable_profile_body_falls_back_to_the_plain_line(self, stub_profiles):
        stub_profiles["deckhand"] = AgentProfile(
            name="deckhand", raw_body="# Deckhand\n\n---\n\n| a | b |\n", model=None
        )
        blurb = orch._role_blurb("deckhand", "openai/gpt-4.1-mini")

        assert blurb == "user profile (model: openai/gpt-4.1-mini)"

    def test_long_description_is_capped(self, stub_profiles):
        stub_profiles["deckhand"] = AgentProfile(
            name="deckhand", raw_body="# Deckhand\n\n**Mission**: " + "rope " * 200, model=None
        )
        blurb = orch._role_blurb("deckhand", "openai/gpt-4.1-mini")

        assert "…" in blurb
        assert len(blurb.split(" (model:")[0]) <= 161


class TestRoleRoster:
    def test_one_line_per_role(self):
        roster = orch.role_roster("openai/gpt-4.1-mini")
        lines = roster.split("\n")[1:]
        assert len(lines) == len(orch.available_roles())
        assert all(line.startswith("- ") for line in lines)

    def test_every_line_carries_a_description(self):
        roster = orch.role_roster("openai/gpt-4.1-mini")
        bare = [
            line
            for line in roster.split("\n")[1:]
            if line.split(": ", 1)[1].startswith("user profile (model:")
        ]
        assert bare == []
