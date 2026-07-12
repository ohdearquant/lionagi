# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""`resolve_cli_workspace` is the one helper shared by every CLI-backed
provider (claude_code, codex, gemini_code) to resolve the subprocess working
directory before spawning. A nonexistent `repo` (the resolved value of a
user-supplied --cwd) must raise here — the single point that previously let
`li agent --cwd <bad path>` silently mkdir the directory (claude_code) or
spawn into a directory that was never validated (codex/gemini_code), instead
of failing fast.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lionagi.providers._cli_subprocess import resolve_cli_workspace


class TestResolveCliWorkspaceCwdValidation:
    def test_nonexistent_repo_raises(self, tmp_path):
        bad = tmp_path / "does-not-exist"
        with pytest.raises(ValueError) as exc_info:
            resolve_cli_workspace(bad, None)
        assert str(bad) in str(exc_info.value)

    def test_repo_that_is_a_file_raises(self, tmp_path):
        f = tmp_path / "im-a-file.txt"
        f.write_text("x")
        with pytest.raises(ValueError):
            resolve_cli_workspace(f, None)

    def test_existing_repo_no_workspace_returns_repo_unchanged(self, tmp_path):
        assert resolve_cli_workspace(tmp_path, None) == tmp_path

    def test_existing_repo_with_new_workspace_subdir_still_resolves(self, tmp_path):
        """Regression guard: a --ws sub-workspace that doesn't exist YET under
        an existing repo is a legitimate "create a fresh workspace" flow (the
        caller mkdir's it after resolution) — only the base repo must exist,
        not the final joined workspace path."""
        result = resolve_cli_workspace(tmp_path, "fresh-subdir")
        assert result == (tmp_path / "fresh-subdir").resolve()
        assert not result.exists()  # not created by resolve_cli_workspace itself

    def test_repo_none_defaults_to_process_cwd(self):
        # Path.cwd() always exists for a live process — sanity check that the
        # None-repo default path doesn't regress under the new validation.
        assert resolve_cli_workspace(None, None) == Path.cwd()

    def test_absolute_workspace_still_rejected(self, tmp_path):
        """Pre-existing validation (unrelated to this fix) must still work."""
        with pytest.raises(ValueError, match="must be relative"):
            resolve_cli_workspace(tmp_path, "/absolute/path")

    def test_workspace_traversal_still_rejected(self, tmp_path):
        """Pre-existing validation (unrelated to this fix) must still work."""
        with pytest.raises(ValueError, match="traversal"):
            resolve_cli_workspace(tmp_path, "../escape")


class TestProviderRequestsRejectNonexistentRepo:
    """End-to-end (still no subprocess spawn): each CLI-backed request's own
    .cwd() must surface the same fail-fast validation, since all three route
    through resolve_cli_workspace."""

    def test_claude_code_request_cwd_rejects_nonexistent_repo(self, tmp_path):
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

        bad = tmp_path / "nonexistent-repo"
        req = ClaudeCodeRequest(prompt="hi", repo=bad)
        with pytest.raises(ValueError):
            req.cwd()

    def test_codex_request_cwd_rejects_nonexistent_repo(self, tmp_path):
        from lionagi.providers.openai.codex import CodexCodeRequest

        bad = tmp_path / "nonexistent-repo"
        req = CodexCodeRequest(prompt="hi", repo=bad)
        with pytest.raises(ValueError):
            req.cwd()

    def test_gemini_code_request_cwd_rejects_nonexistent_repo(self, tmp_path):
        from lionagi.providers.google.gemini_code import GeminiCodeRequest

        bad = tmp_path / "nonexistent-repo"
        req = GeminiCodeRequest(prompt="hi", repo=bad)
        with pytest.raises(ValueError):
            req.cwd()
