# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Profile frontmatter `bypass` field: parsing and precedence over the CLI flag."""

from __future__ import annotations

from lionagi.cli._providers import _parse_profile


def _profile_text(frontmatter: str) -> str:
    return f"---\n{frontmatter}\n---\nbody prompt\n"


class TestProfileBypassParsing:
    def test_bypass_true_parsed(self):
        profile = _parse_profile("p", _profile_text("bypass: true"))
        assert profile.bypass is True

    def test_bypass_absent_defaults_false(self):
        profile = _parse_profile("p", _profile_text("model: codex/gpt-5.5"))
        assert profile.bypass is False

    def test_bypass_false_parsed(self):
        profile = _parse_profile("p", _profile_text("bypass: false"))
        assert profile.bypass is False

    def test_bypass_not_leaked_into_extra(self):
        profile = _parse_profile("p", _profile_text("bypass: true"))
        assert "bypass" not in profile.extra

    def test_bypass_independent_of_yolo(self):
        profile = _parse_profile("p", _profile_text("bypass: true"))
        assert profile.yolo is False
        profile = _parse_profile("p", _profile_text("yolo: true"))
        assert profile.bypass is False
