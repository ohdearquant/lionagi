# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Attack-driven path-validation tests for agentic-CLI providers.

Each provider (Codex, Claude Code, Gemini, Pi) accepts path-grant fields
that flow into subprocess argv.  A traversal or absolute path in any of
these fields could let the spawned CLI read arbitrary host files.

These tests verify that the shared containment helper is wired correctly:
  - traversal paths (``../../etc/passwd``) are rejected at model-construction
    time, before any subprocess is launched;
  - absolute paths (``/etc/passwd``) are likewise rejected;
  - legitimate in-repo relative paths are accepted.

Tests are designed to FAIL on the old behaviour (no validation on
Codex / Claude Code / Gemini).
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Shared helper unit tests
# ---------------------------------------------------------------------------


class TestCliPathsHelper:
    """Direct unit tests for the shared helper functions."""

    def test_check_path_safe_rejects_absolute(self):
        from lionagi.providers._cli_paths import check_path_safe

        with pytest.raises(ValueError, match="absolute path"):
            check_path_safe("/etc/passwd", "field")

    def test_check_path_safe_rejects_traversal(self):
        from lionagi.providers._cli_paths import check_path_safe

        with pytest.raises(ValueError, match="traversal"):
            check_path_safe("../../etc/passwd", "field")

    def test_check_path_safe_accepts_relative(self):
        from lionagi.providers._cli_paths import check_path_safe

        assert check_path_safe("src/main.py", "field") == "src/main.py"

    def test_check_paths_safe_rejects_on_first_bad(self):
        from lionagi.providers._cli_paths import check_paths_safe

        with pytest.raises(ValueError):
            check_paths_safe(["ok.txt", "/etc/passwd", "also_ok.txt"], "field")

    def test_contain_path_in_repo_rejects_symlink_escape(self, tmp_path):
        from lionagi.providers._cli_paths import contain_path_in_repo

        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "link").symlink_to(outside, target_is_directory=True)

        repo_root = repo.resolve()
        with pytest.raises(ValueError, match="outside the repository"):
            contain_path_in_repo("link/secret.txt", repo_root, "field")

    def test_contain_path_in_repo_accepts_valid(self, tmp_path):
        from lionagi.providers._cli_paths import contain_path_in_repo

        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "main.py").write_text("x = 1")
        repo_root = repo.resolve()
        # Must not raise
        contain_path_in_repo("src/main.py", repo_root, "field")


# ---------------------------------------------------------------------------
# Codex provider — CodexCodeRequest
# ---------------------------------------------------------------------------


class TestCodexPathValidation:
    """Path-grant fields in CodexCodeRequest must be validated before argv."""

    # add_dir attacks
    def test_add_dir_absolute_rejected(self):
        from lionagi.providers.openai.codex.models import CodexCodeRequest

        with pytest.raises(Exception, match="absolute path"):
            CodexCodeRequest(prompt="hi", add_dir=["/etc"])

    def test_add_dir_traversal_rejected(self):
        from lionagi.providers.openai.codex.models import CodexCodeRequest

        with pytest.raises(Exception, match="traversal"):
            CodexCodeRequest(prompt="hi", add_dir=["../../etc"])

    def test_add_dir_valid_accepted(self):
        from lionagi.providers.openai.codex.models import CodexCodeRequest

        req = CodexCodeRequest(prompt="hi", add_dir=["subdir/work"])
        assert req.add_dir == ["subdir/work"]

    # images attacks
    def test_images_absolute_rejected(self):
        from lionagi.providers.openai.codex.models import CodexCodeRequest

        with pytest.raises(Exception, match="absolute path"):
            CodexCodeRequest(prompt="hi", images=["/etc/passwd"])

    def test_images_traversal_rejected(self):
        from lionagi.providers.openai.codex.models import CodexCodeRequest

        with pytest.raises(Exception, match="traversal"):
            CodexCodeRequest(prompt="hi", images=["../../secret.png"])

    def test_images_valid_accepted(self):
        from lionagi.providers.openai.codex.models import CodexCodeRequest

        req = CodexCodeRequest(prompt="hi", images=["assets/screenshot.png"])
        assert req.images == ["assets/screenshot.png"]

    # output_schema attacks
    def test_output_schema_absolute_rejected(self):
        from lionagi.providers.openai.codex.models import CodexCodeRequest

        with pytest.raises(Exception, match="absolute path"):
            CodexCodeRequest(prompt="hi", output_schema="/tmp/schema.json")

    def test_output_schema_traversal_rejected(self):
        from lionagi.providers.openai.codex.models import CodexCodeRequest

        with pytest.raises(Exception, match="traversal"):
            CodexCodeRequest(prompt="hi", output_schema="../../schema.json")

    def test_output_schema_valid_accepted(self):
        from lionagi.providers.openai.codex.models import CodexCodeRequest

        req = CodexCodeRequest(prompt="hi", output_schema="schemas/output.json")
        assert str(req.output_schema) == "schemas/output.json"

    # output_last_message attacks
    def test_output_last_message_absolute_rejected(self):
        from lionagi.providers.openai.codex.models import CodexCodeRequest

        with pytest.raises(Exception, match="absolute path"):
            CodexCodeRequest(prompt="hi", output_last_message="/tmp/out.txt")

    def test_output_last_message_traversal_rejected(self):
        from lionagi.providers.openai.codex.models import CodexCodeRequest

        with pytest.raises(Exception, match="traversal"):
            CodexCodeRequest(prompt="hi", output_last_message="../../out.txt")

    def test_output_last_message_valid_accepted(self):
        from lionagi.providers.openai.codex.models import CodexCodeRequest

        req = CodexCodeRequest(prompt="hi", output_last_message="results/last.txt")
        assert str(req.output_last_message) == "results/last.txt"

    def test_add_dir_symlink_escape_rejected(self, tmp_path):
        """A symlinked add_dir that resolves outside the repo must be rejected."""
        from lionagi.providers.openai.codex.models import CodexCodeRequest

        outside = tmp_path / "outside"
        outside.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "escape").symlink_to(outside, target_is_directory=True)

        with pytest.raises(Exception, match="outside the repository"):
            CodexCodeRequest(prompt="hi", repo=repo, add_dir=["escape"])


# ---------------------------------------------------------------------------
# Claude Code provider — ClaudeCodeRequest
# ---------------------------------------------------------------------------


class TestClaudeCodePathValidation:
    """Path-grant fields in ClaudeCodeRequest must be validated before argv."""

    # system_prompt_file attacks
    def test_system_prompt_file_absolute_rejected(self):
        from lionagi.providers.anthropic.claude_code.models import ClaudeCodeRequest

        with pytest.raises(Exception, match="absolute path"):
            ClaudeCodeRequest(prompt="hi", system_prompt_file="/etc/passwd")

    def test_system_prompt_file_traversal_rejected(self):
        from lionagi.providers.anthropic.claude_code.models import ClaudeCodeRequest

        with pytest.raises(Exception, match="traversal"):
            ClaudeCodeRequest(prompt="hi", system_prompt_file="../../secret.txt")

    def test_system_prompt_file_valid_accepted(self):
        from lionagi.providers.anthropic.claude_code.models import ClaudeCodeRequest

        req = ClaudeCodeRequest(prompt="hi", system_prompt_file="prompts/system.md")
        assert str(req.system_prompt_file) == "prompts/system.md"

    # append_system_prompt_file attacks
    def test_append_system_prompt_file_absolute_rejected(self):
        from lionagi.providers.anthropic.claude_code.models import ClaudeCodeRequest

        with pytest.raises(Exception, match="absolute path"):
            ClaudeCodeRequest(prompt="hi", append_system_prompt_file="/tmp/extra.md")

    def test_append_system_prompt_file_traversal_rejected(self):
        from lionagi.providers.anthropic.claude_code.models import ClaudeCodeRequest

        with pytest.raises(Exception, match="traversal"):
            ClaudeCodeRequest(prompt="hi", append_system_prompt_file="../../extra.md")

    def test_append_system_prompt_file_valid_accepted(self):
        from lionagi.providers.anthropic.claude_code.models import ClaudeCodeRequest

        req = ClaudeCodeRequest(prompt="hi", append_system_prompt_file="prompts/extra.md")
        assert str(req.append_system_prompt_file) == "prompts/extra.md"

    # mcp_config attacks
    def test_mcp_config_absolute_rejected(self):
        from lionagi.providers.anthropic.claude_code.models import ClaudeCodeRequest

        with pytest.raises(Exception, match="absolute path"):
            ClaudeCodeRequest(prompt="hi", mcp_config="/home/user/.config/mcp.json")

    def test_mcp_config_traversal_rejected(self):
        from lionagi.providers.anthropic.claude_code.models import ClaudeCodeRequest

        with pytest.raises(Exception, match="traversal"):
            ClaudeCodeRequest(prompt="hi", mcp_config="../../mcp.json")

    def test_mcp_config_valid_accepted(self):
        from lionagi.providers.anthropic.claude_code.models import ClaudeCodeRequest

        req = ClaudeCodeRequest(prompt="hi", mcp_config=".mcp/config.json")
        assert str(req.mcp_config) == ".mcp/config.json"

    # settings attacks
    def test_settings_absolute_rejected(self):
        from lionagi.providers.anthropic.claude_code.models import ClaudeCodeRequest

        with pytest.raises(Exception, match="absolute path"):
            ClaudeCodeRequest(prompt="hi", settings="/etc/claude-settings.json")

    def test_settings_traversal_rejected(self):
        from lionagi.providers.anthropic.claude_code.models import ClaudeCodeRequest

        with pytest.raises(Exception, match="traversal"):
            ClaudeCodeRequest(prompt="hi", settings="../../settings.json")

    def test_settings_valid_accepted(self):
        from lionagi.providers.anthropic.claude_code.models import ClaudeCodeRequest

        req = ClaudeCodeRequest(prompt="hi", settings=".claude/settings.json")
        assert str(req.settings) == ".claude/settings.json"

    # add_dir attacks
    def test_add_dir_absolute_rejected(self):
        from lionagi.providers.anthropic.claude_code.models import ClaudeCodeRequest

        with pytest.raises(Exception, match="absolute path"):
            ClaudeCodeRequest(prompt="hi", add_dir=["/tmp/work"])

    def test_add_dir_traversal_rejected(self):
        from lionagi.providers.anthropic.claude_code.models import ClaudeCodeRequest

        with pytest.raises(Exception, match="traversal"):
            ClaudeCodeRequest(prompt="hi", add_dir=["../../tmp/work"])

    def test_add_dir_valid_accepted(self):
        from lionagi.providers.anthropic.claude_code.models import ClaudeCodeRequest

        req = ClaudeCodeRequest(prompt="hi", add_dir=["workspace/subdir"])
        assert req.add_dir == ["workspace/subdir"]

    def test_mcp_config_symlink_escape_rejected(self, tmp_path):
        """A symlinked mcp_config that resolves outside the repo must be rejected."""
        from lionagi.providers.anthropic.claude_code.models import ClaudeCodeRequest

        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "evil.json").write_text("{}")
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "linked.json").symlink_to(outside / "evil.json")

        with pytest.raises(Exception, match="outside the repository"):
            ClaudeCodeRequest(prompt="hi", repo=repo, mcp_config="linked.json")


# ---------------------------------------------------------------------------
# Gemini provider — GeminiCodeRequest
# ---------------------------------------------------------------------------


class TestGeminiPathValidation:
    """include_directories in GeminiCodeRequest must be validated before argv."""

    def test_include_directories_absolute_rejected(self):
        from lionagi.providers.google.gemini_code.models import GeminiCodeRequest

        with pytest.raises(Exception, match="absolute path"):
            GeminiCodeRequest(prompt="hi", include_directories=["/etc"])

    def test_include_directories_traversal_rejected(self):
        from lionagi.providers.google.gemini_code.models import GeminiCodeRequest

        with pytest.raises(Exception, match="traversal"):
            GeminiCodeRequest(prompt="hi", include_directories=["../../secrets"])

    def test_include_directories_valid_accepted(self):
        from lionagi.providers.google.gemini_code.models import GeminiCodeRequest

        req = GeminiCodeRequest(prompt="hi", include_directories=["src", "tests"])
        assert req.include_directories == ["src", "tests"]

    def test_include_directories_mixed_rejected(self):
        """Even one bad entry in the list must reject the whole request."""
        from lionagi.providers.google.gemini_code.models import GeminiCodeRequest

        with pytest.raises(Exception):
            GeminiCodeRequest(
                prompt="hi",
                include_directories=["src", "../../etc"],
            )

    def test_include_directories_symlink_escape_rejected(self, tmp_path):
        """A symlinked directory that resolves outside the repo must be rejected."""
        from lionagi.providers.google.gemini_code.models import GeminiCodeRequest

        outside = tmp_path / "outside"
        outside.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "escape").symlink_to(outside, target_is_directory=True)

        with pytest.raises(Exception, match="outside the repository"):
            GeminiCodeRequest(prompt="hi", repo=repo, include_directories=["escape"])


# ---------------------------------------------------------------------------
# Pi provider — PiCodeRequest (extension and skill fields now also validated)
# ---------------------------------------------------------------------------


class TestPiExtensionSkillValidation:
    """extension and skill path fields in PiCodeRequest must be validated."""

    def test_extension_absolute_rejected(self):
        from lionagi.providers.pi.cli.models import PiCodeRequest

        with pytest.raises(Exception, match="absolute path"):
            PiCodeRequest(prompt="hi", extension=["/home/user/.config/ext.js"])

    def test_extension_traversal_rejected(self):
        from lionagi.providers.pi.cli.models import PiCodeRequest

        with pytest.raises(Exception, match="traversal"):
            PiCodeRequest(prompt="hi", extension=["../../evil-ext.js"])

    def test_extension_valid_accepted(self):
        from lionagi.providers.pi.cli.models import PiCodeRequest

        req = PiCodeRequest(prompt="hi", extension=["extensions/my-ext.js"])
        assert req.extension == ["extensions/my-ext.js"]

    def test_skill_absolute_rejected(self):
        from lionagi.providers.pi.cli.models import PiCodeRequest

        with pytest.raises(Exception, match="absolute path"):
            PiCodeRequest(prompt="hi", skill=["/etc/passwd"])

    def test_skill_traversal_rejected(self):
        from lionagi.providers.pi.cli.models import PiCodeRequest

        with pytest.raises(Exception, match="traversal"):
            PiCodeRequest(prompt="hi", skill=["../../evil-skill"])

    def test_skill_valid_accepted(self):
        from lionagi.providers.pi.cli.models import PiCodeRequest

        req = PiCodeRequest(prompt="hi", skill=["skills/my-skill"])
        assert req.skill == ["skills/my-skill"]
