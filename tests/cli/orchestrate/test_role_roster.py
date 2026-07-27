# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The body a worker runs, and the roster line that advertises it.

Both are decided by the same signal — whether the profile authored a body — so
they are tested together: a drift between them is the roster describing one
thing while the worker runs another.
"""

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

    def test_frontmatter_only_profile_falls_back_to_the_builtin_description(self, stub_profiles):
        """A profile with no authored body has no summary of its own, and the
        built-in role is what such a worker runs, so the built-in description is
        the accurate line. Built from the parser rather than by hand: the parser
        gives every profile a system prompt, so a hand-built one with an empty
        system prompt would test a shape that never occurs."""
        from lionagi.casts.pattern import Role
        from lionagi.cli._providers import _parse_profile

        profile = _parse_profile("critic", "---\nmodel: codex/gpt-5\n---\n")
        assert not profile.raw_body
        assert profile.system_prompt, "the shared preamble must not be read as a summary"
        stub_profiles["critic"] = profile
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


@pytest.fixture
def stub_roster(stub_profiles, monkeypatch):
    """A roster built from a fixed profile set rather than from the machine's.

    `role_roster` reaches the agents directories twice — once to list the names
    and once per name to load them — so stubbing the loader alone still lets the
    host's own profiles decide the result.
    """
    stub_profiles["deckhand"] = AgentProfile(
        name="deckhand",
        raw_body="# Deckhand\n\n**Mission**: keep the decks clear.",
        model="codex/gpt-5",
    )
    stub_profiles["critic"] = AgentProfile(
        name="critic",
        raw_body="# Critic\n\n**Mission**: gate the release.",
        system_prompt="# Critic\n\n**Mission**: gate the release.",
        model="codex/gpt-5",
    )
    monkeypatch.setattr(orch, "list_agents", lambda: list(stub_profiles))
    monkeypatch.setattr("lionagi.casts.pattern.list_roles", lambda: ["critic", "contrarian"])
    return stub_profiles


@pytest.fixture
def worker_env(tmp_path, monkeypatch):
    """Enough of an orchestration environment to build one worker branch."""
    from types import SimpleNamespace

    from lionagi import iModel

    class _Session:
        def __init__(self):
            self.branches = []

        def include_branches(self, branch):
            self.branches.append(branch)

    monkeypatch.setattr(
        orch,
        "build_imodel_from_spec",
        lambda *_a, **_kw: iModel(provider="openai", model="gpt-4o-mini", api_key="dummy-key"),
    )
    return SimpleNamespace(
        run=SimpleNamespace(agent_artifact_dir=lambda name: tmp_path / name),
        session=_Session(),
        default_model_spec="openai/gpt-4o-mini",
        bare=False,
        effort=None,
        theme=None,
        yolo=False,
        bypass=False,
        verbose=False,
        fast=False,
        cwd=str(tmp_path),
        team_data=None,
        exchange=None,
        messenger=None,
        roster=None,
        messenger_names=None,
        pack=None,
        _live_persist=None,
        register_name=lambda _name: None,
    )


CRITIC_BODY_MARKER = "The null hypothesis is failure"


class TestAuthoredBodySelection:
    """Which body a worker actually runs — the claim the roster line makes."""

    @pytest.mark.asyncio
    async def test_frontmatter_only_profile_leaves_the_role_composing(
        self, worker_env, stub_profiles
    ):
        from lionagi.cli._providers import _parse_profile

        stub_profiles["critic"] = _parse_profile("critic", "---\neffort: high\n---\n")
        branch, *_ = await orch.build_worker_branch(
            worker_env, agent_id="critic", role="critic", explicit_name="critic"
        )

        assert CRITIC_BODY_MARKER in branch.system.rendered

    @pytest.mark.asyncio
    async def test_authored_body_replaces_the_role(self, worker_env, stub_profiles):
        from lionagi.cli._providers import _parse_profile

        stub_profiles["critic"] = _parse_profile(
            "critic", "---\neffort: high\n---\n# Critic\n\n**Mission**: gate the release.\n"
        )
        branch, *_ = await orch.build_worker_branch(
            worker_env, agent_id="critic", role="critic", explicit_name="critic"
        )

        assert "gate the release" in branch.system.rendered
        assert CRITIC_BODY_MARKER not in branch.system.rendered


class TestRoleRoster:
    def test_one_line_per_role(self, stub_roster):
        roster = orch.role_roster("openai/gpt-4.1-mini")
        lines = roster.split("\n")[1:]
        assert len(lines) == len(orch.available_roles())
        assert all(line.startswith("- ") for line in lines)

    def test_every_line_carries_a_description(self, stub_roster):
        roster = orch.role_roster("openai/gpt-4.1-mini")
        bare = [
            line
            for line in roster.split("\n")[1:]
            if line.split(": ", 1)[1].startswith("user profile (model:")
        ]
        assert bare == []
