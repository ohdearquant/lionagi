# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for CodexCodeRequest.skip_git_repo_check default and CLI serialization.

Codex CLI refuses to run outside a git repository unless --skip-git-repo-check
is passed. Agents routinely execute in per-task artifact dirs and worktrees that
are not git repos, so the field must default to True to avoid silent failures.
"""

from __future__ import annotations

import pytest

from lionagi.providers.openai.codex.models import CodexCodeRequest

# ── 1. Default is True ──────────────────────────────────────────────────────


def test_skip_git_repo_check_default_true():
    """skip_git_repo_check defaults to True without any explicit kwarg."""
    req = CodexCodeRequest(prompt="hello")
    assert req.skip_git_repo_check is True


# ── 2. Default True emits --skip-git-repo-check in CLI args ─────────────────


def test_skip_git_repo_check_default_emits_flag():
    """Default True produces --skip-git-repo-check in the assembled arg list."""
    req = CodexCodeRequest(prompt="hello")
    args = req.as_cmd_args()
    assert "--skip-git-repo-check" in args


# ── 3. Explicit False omits the flag ────────────────────────────────────────


def test_skip_git_repo_check_false_omits_flag():
    """Explicit False does not emit --skip-git-repo-check."""
    req = CodexCodeRequest(prompt="hello", skip_git_repo_check=False)
    assert req.skip_git_repo_check is False
    args = req.as_cmd_args()
    assert "--skip-git-repo-check" not in args


# ── 4. Explicit True still emits the flag ───────────────────────────────────


def test_skip_git_repo_check_explicit_true_emits_flag():
    """Explicit True still emits --skip-git-repo-check."""
    req = CodexCodeRequest(prompt="hello", skip_git_repo_check=True)
    args = req.as_cmd_args()
    assert "--skip-git-repo-check" in args
