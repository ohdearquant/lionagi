"""Tests for ADR-0029 artifact_defaults in AgentProfile frontmatter."""

from __future__ import annotations

import pytest

from lionagi.cli._providers import _parse_profile


class TestProfileArtifactDefaults:
    def test_no_artifact_defaults_is_none(self):
        text = "---\nmodel: codex/gpt-4o\n---\nSystem prompt."
        profile = _parse_profile("agent", text)
        assert profile.artifact_defaults is None

    def test_artifact_defaults_nested_dict(self):
        text = (
            "---\n"
            "artifact_defaults:\n"
            "  expected:\n"
            "    - id: report\n"
            "      path: report.md\n"
            "      required: true\n"
            "---\n"
            "System prompt."
        )
        profile = _parse_profile("agent", text)
        assert profile.artifact_defaults is not None
        expected = profile.artifact_defaults["expected"]
        assert isinstance(expected, list)
        assert len(expected) == 1
        assert expected[0]["id"] == "report"
        assert expected[0]["path"] == "report.md"
        assert expected[0]["required"] is True

    def test_artifact_defaults_multiple_entries(self):
        text = (
            "---\n"
            "artifact_defaults:\n"
            "  expected:\n"
            "    - id: brief\n"
            "      path: brief.md\n"
            "    - id: log\n"
            "      path: log.txt\n"
            "      required: false\n"
            "---\n"
        )
        profile = _parse_profile("agent", text)
        assert profile.artifact_defaults is not None
        assert len(profile.artifact_defaults["expected"]) == 2

    def test_scalar_booleans_still_parse(self):
        text = "---\nyolo: true\nfast_mode: false\nlion_system: true\n---\n"
        profile = _parse_profile("agent", text)
        assert profile.yolo is True
        assert profile.fast_mode is False
        assert profile.lion_system is True

    def test_artifact_defaults_not_in_extra(self):
        text = (
            "---\n"
            "artifact_defaults:\n"
            "  expected:\n"
            "    - id: x\n"
            "      path: x.md\n"
            "custom_key: custom_val\n"
            "---\n"
        )
        profile = _parse_profile("agent", text)
        assert "artifact_defaults" not in profile.extra
        assert "custom_key" in profile.extra

    def test_no_frontmatter_gives_no_artifact_defaults(self):
        text = "Just a plain system prompt without frontmatter."
        profile = _parse_profile("agent", text)
        assert profile.artifact_defaults is None
