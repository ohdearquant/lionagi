# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Attack-driven path-validation tests for agentic-CLI providers (Codex, Claude Code, Gemini, Pi).

Traversal paths are always rejected; absolute paths are rejected in write-target fields but allowed in read-grant fields (add_dir); relative paths accepted; symlink-escape caught by repo-containment.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Shared helper unit tests
# ---------------------------------------------------------------------------


class TestCliPathsHelper:
    """Direct unit tests for the shared helper functions."""

    def test_check_path_safe_rejects_absolute(self):
        from lionagi.libs.path_safety import check_path_safe

        with pytest.raises(ValueError, match="absolute path"):
            check_path_safe("/etc/passwd", "field")

    def test_check_path_safe_rejects_traversal(self):
        from lionagi.libs.path_safety import check_path_safe

        with pytest.raises(ValueError, match="traversal"):
            check_path_safe("../../etc/passwd", "field")

    def test_check_path_safe_accepts_relative(self):
        from lionagi.libs.path_safety import check_path_safe

        assert check_path_safe("src/main.py", "field") == "src/main.py"

    def test_check_paths_safe_rejects_on_first_bad(self):
        from lionagi.libs.path_safety import check_paths_safe

        with pytest.raises(ValueError):
            check_paths_safe(["ok.txt", "/etc/passwd", "also_ok.txt"], "field")

    def test_contain_path_in_repo_rejects_symlink_escape(self, tmp_path):
        from lionagi.libs.path_safety import contain_path_in_root as contain_path_in_repo

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
        from lionagi.libs.path_safety import contain_path_in_root as contain_path_in_repo

        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "main.py").write_text("x = 1")
        repo_root = repo.resolve()
        # Must not raise
        contain_path_in_repo("src/main.py", repo_root, "field")

    def test_check_add_dir_entry_safe_allows_absolute(self):
        """Absolute paths are legitimate read grants and must not be rejected."""
        from lionagi.libs.path_safety import check_add_dir_safe as check_add_dir_entry_safe

        # Must not raise — this is the core of the orchestration regression fix
        result = check_add_dir_entry_safe("/home/user/projects/myproject", "add_dir")
        assert result == "/home/user/projects/myproject"

    def test_check_add_dir_entry_safe_rejects_traversal(self):
        """Traversal sequences are still rejected even in read-grant fields."""
        from lionagi.libs.path_safety import check_add_dir_safe as check_add_dir_entry_safe

        with pytest.raises(ValueError, match="traversal"):
            check_add_dir_entry_safe("../../etc", "add_dir")

    def test_check_add_dir_entry_safe_accepts_relative(self):
        from lionagi.libs.path_safety import check_add_dir_safe as check_add_dir_entry_safe

        result = check_add_dir_entry_safe("subdir/work", "add_dir")
        assert result == "subdir/work"

    def test_check_add_dir_entries_safe_rejects_traversal_in_list(self):
        from lionagi.libs.path_safety import check_add_dirs_safe as check_add_dir_entries_safe

        with pytest.raises(ValueError, match="traversal"):
            check_add_dir_entries_safe(["ok", "../../bad", "/abs/ok"], "add_dir")


# ---------------------------------------------------------------------------
# Codex provider — CodexCodeRequest
# ---------------------------------------------------------------------------


class TestCodexPathValidation:
    """Path-grant fields in CodexCodeRequest must be validated before argv."""

    # add_dir — read-grant field: absolute paths allowed, traversal rejected
    def test_add_dir_absolute_allowed(self):
        """Absolute add_dir paths are deliberate read grants and must be accepted."""
        from lionagi.providers.openai.codex import CodexCodeRequest

        req = CodexCodeRequest(prompt="hi", add_dir=["/home/user/projects"])
        assert req.add_dir == ["/home/user/projects"]

    def test_add_dir_traversal_rejected(self):
        from lionagi.providers.openai.codex import CodexCodeRequest

        with pytest.raises(Exception, match="traversal"):
            CodexCodeRequest(prompt="hi", add_dir=["../../etc"])

    def test_add_dir_valid_relative_accepted(self):
        from lionagi.providers.openai.codex import CodexCodeRequest

        req = CodexCodeRequest(prompt="hi", add_dir=["subdir/work"])
        assert req.add_dir == ["subdir/work"]

    # images attacks
    def test_images_absolute_rejected(self):
        from lionagi.providers.openai.codex import CodexCodeRequest

        with pytest.raises(Exception, match="absolute path"):
            CodexCodeRequest(prompt="hi", images=["/etc/passwd"])

    def test_images_traversal_rejected(self):
        from lionagi.providers.openai.codex import CodexCodeRequest

        with pytest.raises(Exception, match="traversal"):
            CodexCodeRequest(prompt="hi", images=["../../secret.png"])

    def test_images_valid_accepted(self):
        from lionagi.providers.openai.codex import CodexCodeRequest

        req = CodexCodeRequest(prompt="hi", images=["assets/screenshot.png"])
        assert req.images == ["assets/screenshot.png"]

    # output_schema attacks
    def test_output_schema_absolute_rejected(self):
        from lionagi.providers.openai.codex import CodexCodeRequest

        with pytest.raises(Exception, match="absolute path"):
            CodexCodeRequest(prompt="hi", output_schema="/tmp/schema.json")

    def test_output_schema_traversal_rejected(self):
        from lionagi.providers.openai.codex import CodexCodeRequest

        with pytest.raises(Exception, match="traversal"):
            CodexCodeRequest(prompt="hi", output_schema="../../schema.json")

    def test_output_schema_valid_accepted(self):
        from lionagi.providers.openai.codex import CodexCodeRequest

        req = CodexCodeRequest(prompt="hi", output_schema="schemas/output.json")
        assert str(req.output_schema) == "schemas/output.json"

    # output_last_message attacks
    def test_output_last_message_absolute_rejected(self):
        from lionagi.providers.openai.codex import CodexCodeRequest

        with pytest.raises(Exception, match="absolute path"):
            CodexCodeRequest(prompt="hi", output_last_message="/tmp/out.txt")

    def test_output_last_message_traversal_rejected(self):
        from lionagi.providers.openai.codex import CodexCodeRequest

        with pytest.raises(Exception, match="traversal"):
            CodexCodeRequest(prompt="hi", output_last_message="../../out.txt")

    def test_output_last_message_valid_accepted(self):
        from lionagi.providers.openai.codex import CodexCodeRequest

        req = CodexCodeRequest(prompt="hi", output_last_message="results/last.txt")
        assert str(req.output_last_message) == "results/last.txt"

    def test_add_dir_orchestration_regression(self, tmp_path):
        """Regression: repo=artifact_dir with add_dir=[project_root] must succeed; orchestration sets add_dir to the project root outside the per-agent artifact dir."""
        from lionagi.providers.openai.codex import CodexCodeRequest

        project_root = tmp_path / "project"
        project_root.mkdir()
        artifact_dir = (
            tmp_path
            / "project"
            / ".lionagi"
            / "runs"
            / "20260607T120000-abc123"
            / "agents"
            / "agent-1"
        )
        artifact_dir.mkdir(parents=True)

        # artifact_dir is inside project_root, but project_root is "outside"
        # artifact_dir — this is the orchestration scenario
        req = CodexCodeRequest(
            prompt="implement the feature",
            repo=artifact_dir,
            add_dir=[str(project_root)],
        )
        assert str(project_root) in req.add_dir


# ---------------------------------------------------------------------------
# Claude Code provider — ClaudeCodeRequest
# ---------------------------------------------------------------------------


class TestClaudeCodePathValidation:
    """Path-grant fields in ClaudeCodeRequest must be validated before argv."""

    # system_prompt_file attacks
    def test_system_prompt_file_absolute_rejected(self):
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

        with pytest.raises(Exception, match="absolute path"):
            ClaudeCodeRequest(prompt="hi", system_prompt_file="/etc/passwd")

    def test_system_prompt_file_traversal_rejected(self):
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

        with pytest.raises(Exception, match="traversal"):
            ClaudeCodeRequest(prompt="hi", system_prompt_file="../../secret.txt")

    def test_system_prompt_file_valid_accepted(self):
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

        req = ClaudeCodeRequest(prompt="hi", system_prompt_file="prompts/system.md")
        assert str(req.system_prompt_file) == "prompts/system.md"

    # append_system_prompt_file attacks
    def test_append_system_prompt_file_absolute_rejected(self):
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

        with pytest.raises(Exception, match="absolute path"):
            ClaudeCodeRequest(prompt="hi", append_system_prompt_file="/tmp/extra.md")

    def test_append_system_prompt_file_traversal_rejected(self):
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

        with pytest.raises(Exception, match="traversal"):
            ClaudeCodeRequest(prompt="hi", append_system_prompt_file="../../extra.md")

    def test_append_system_prompt_file_valid_accepted(self):
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

        req = ClaudeCodeRequest(prompt="hi", append_system_prompt_file="prompts/extra.md")
        assert str(req.append_system_prompt_file) == "prompts/extra.md"

    # mcp_config attacks
    def test_mcp_config_absolute_rejected(self):
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

        with pytest.raises(Exception, match="absolute path"):
            ClaudeCodeRequest(prompt="hi", mcp_config="/home/user/.config/mcp.json")

    def test_mcp_config_traversal_rejected(self):
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

        with pytest.raises(Exception, match="traversal"):
            ClaudeCodeRequest(prompt="hi", mcp_config="../../mcp.json")

    def test_mcp_config_valid_accepted(self):
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

        req = ClaudeCodeRequest(prompt="hi", mcp_config=".mcp/config.json")
        assert str(req.mcp_config) == ".mcp/config.json"

    # settings attacks
    def test_settings_absolute_rejected(self):
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

        with pytest.raises(Exception, match="absolute path"):
            ClaudeCodeRequest(prompt="hi", settings="/etc/claude-settings.json")

    def test_settings_traversal_rejected(self):
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

        with pytest.raises(Exception, match="traversal"):
            ClaudeCodeRequest(prompt="hi", settings="../../settings.json")

    def test_settings_valid_accepted(self):
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

        req = ClaudeCodeRequest(prompt="hi", settings=".claude/settings.json")
        assert str(req.settings) == ".claude/settings.json"

    # add_dir — read-grant field: absolute paths allowed, traversal rejected
    def test_add_dir_absolute_allowed(self):
        """Absolute add_dir paths are deliberate read grants and must be accepted."""
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

        req = ClaudeCodeRequest(prompt="hi", add_dir=["/home/user/projects"])
        assert req.add_dir == ["/home/user/projects"]

    def test_add_dir_traversal_rejected(self):
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

        with pytest.raises(Exception, match="traversal"):
            ClaudeCodeRequest(prompt="hi", add_dir=["../../tmp/work"])

    def test_add_dir_valid_relative_accepted(self):
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

        req = ClaudeCodeRequest(prompt="hi", add_dir=["workspace/subdir"])
        assert req.add_dir == ["workspace/subdir"]

    def test_add_dir_orchestration_regression(self, tmp_path):
        """Regression: repo=artifact_dir with add_dir=[project_root] must succeed; orchestration sets add_dir to the project root outside the per-agent artifact dir."""
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

        project_root = tmp_path / "project"
        project_root.mkdir()
        artifact_dir = (
            tmp_path
            / "project"
            / ".lionagi"
            / "runs"
            / "20260607T120000-abc123"
            / "agents"
            / "agent-1"
        )
        artifact_dir.mkdir(parents=True)

        req = ClaudeCodeRequest(
            prompt="implement the feature",
            repo=artifact_dir,
            add_dir=[str(project_root)],
        )
        assert str(project_root) in req.add_dir

    def test_mcp_config_symlink_escape_rejected(self, tmp_path):
        """A symlinked mcp_config that resolves outside the repo must be rejected."""
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

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
        from lionagi.providers.google.gemini_code import GeminiCodeRequest

        with pytest.raises(Exception, match="absolute path"):
            GeminiCodeRequest(prompt="hi", include_directories=["/etc"])

    def test_include_directories_traversal_rejected(self):
        from lionagi.providers.google.gemini_code import GeminiCodeRequest

        with pytest.raises(Exception, match="traversal"):
            GeminiCodeRequest(prompt="hi", include_directories=["../../secrets"])

    def test_include_directories_valid_accepted(self):
        from lionagi.providers.google.gemini_code import GeminiCodeRequest

        req = GeminiCodeRequest(prompt="hi", include_directories=["src", "tests"])
        assert req.include_directories == ["src", "tests"]

    def test_include_directories_mixed_rejected(self):
        """Even one bad entry in the list must reject the whole request."""
        from lionagi.providers.google.gemini_code import GeminiCodeRequest

        with pytest.raises(Exception):
            GeminiCodeRequest(
                prompt="hi",
                include_directories=["src", "../../etc"],
            )

    def test_include_directories_symlink_escape_rejected(self, tmp_path):
        """A symlinked directory that resolves outside the repo must be rejected."""
        from lionagi.providers.google.gemini_code import GeminiCodeRequest

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
        from lionagi.providers.pi.cli import PiCodeRequest

        with pytest.raises(Exception, match="absolute path"):
            PiCodeRequest(prompt="hi", extension=["/home/user/.config/ext.js"])

    def test_extension_traversal_rejected(self):
        from lionagi.providers.pi.cli import PiCodeRequest

        with pytest.raises(Exception, match="traversal"):
            PiCodeRequest(prompt="hi", extension=["../../evil-ext.js"])

    def test_extension_valid_accepted(self):
        from lionagi.providers.pi.cli import PiCodeRequest

        req = PiCodeRequest(prompt="hi", extension=["extensions/my-ext.js"])
        assert req.extension == ["extensions/my-ext.js"]

    def test_skill_absolute_rejected(self):
        from lionagi.providers.pi.cli import PiCodeRequest

        with pytest.raises(Exception, match="absolute path"):
            PiCodeRequest(prompt="hi", skill=["/etc/passwd"])

    def test_skill_traversal_rejected(self):
        from lionagi.providers.pi.cli import PiCodeRequest

        with pytest.raises(Exception, match="traversal"):
            PiCodeRequest(prompt="hi", skill=["../../evil-skill"])

    def test_skill_valid_accepted(self):
        from lionagi.providers.pi.cli import PiCodeRequest

        req = PiCodeRequest(prompt="hi", skill=["skills/my-skill"])
        assert req.skill == ["skills/my-skill"]
