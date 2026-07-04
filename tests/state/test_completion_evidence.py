# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the completion-trust gate's git-based evidence check."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lionagi.state.completion_evidence import (
    check_completion_evidence,
    has_completion_evidence,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(path), capture_output=True, check=True)


def _init_git_repo(path: Path) -> None:
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("initial\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "init")


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    _init_git_repo(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# checked=False cases — the check must have "no opinion", not "no evidence"
# ---------------------------------------------------------------------------


def test_no_cwd_is_unchecked():
    evidence = check_completion_evidence(None)
    assert evidence["checked"] is False
    assert has_completion_evidence(evidence) is False


def test_non_git_dir_is_unchecked(tmp_path: Path):
    evidence = check_completion_evidence(str(tmp_path))
    assert evidence["checked"] is False
    assert has_completion_evidence(evidence) is False


# ---------------------------------------------------------------------------
# checked=True cases
# ---------------------------------------------------------------------------


def test_clean_tree_no_commits_ahead_has_no_evidence(git_repo: Path):
    _git(git_repo, "checkout", "-b", "feature")
    evidence = check_completion_evidence(str(git_repo), base_ref="main")
    assert evidence["checked"] is True
    assert evidence["ahead_of_base"] is False
    assert evidence["commits_ahead"] == 0
    assert evidence["dirty"] is False
    assert has_completion_evidence(evidence) is False


def test_commit_ahead_of_base_has_evidence(git_repo: Path):
    _git(git_repo, "checkout", "-b", "feature")
    (git_repo / "fix.py").write_text("print('fixed')\n")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-m", "the fix")
    evidence = check_completion_evidence(str(git_repo), base_ref="main")
    assert evidence["checked"] is True
    assert evidence["ahead_of_base"] is True
    assert evidence["commits_ahead"] == 1
    assert has_completion_evidence(evidence) is True


def test_dirty_working_tree_has_evidence_even_without_commits(git_repo: Path):
    """Reproduces the reported incident: a substantive fix sitting uncommitted
    in the working tree must count as evidence, not be lost as `completed`."""
    _git(git_repo, "checkout", "-b", "feature")
    (git_repo / "fix.py").write_text("print('uncommitted fix')\n")
    evidence = check_completion_evidence(str(git_repo), base_ref="main")
    assert evidence["checked"] is True
    assert evidence["ahead_of_base"] is False
    assert evidence["dirty"] is True
    assert has_completion_evidence(evidence) is True


def test_base_ref_auto_resolves_to_main(git_repo: Path):
    _git(git_repo, "checkout", "-b", "feature")
    evidence = check_completion_evidence(str(git_repo))
    assert evidence["checked"] is True
    assert evidence["base_ref"] == "main"


def test_status_probe_failure_is_unchecked_not_clean(git_repo: Path, monkeypatch):
    """A `git status` probe that errors (transient failure, timeout) must
    never be read as "ran and found nothing" — that would silently turn a
    git error into a false completed_empty on real, uncommitted work."""
    import lionagi.state.completion_evidence as ce_mod

    real_run_git = ce_mod._run_git

    def _flaky(cwd: str, args: list[str]) -> tuple[int, str]:
        if args and args[0] == "status":
            return 128, ""
        return real_run_git(cwd, args)

    monkeypatch.setattr(ce_mod, "_run_git", _flaky)
    evidence = check_completion_evidence(str(git_repo), base_ref="main")
    assert evidence["checked"] is False
    assert has_completion_evidence(evidence) is False


def test_rev_list_probe_failure_is_unchecked_not_clean(git_repo: Path, monkeypatch):
    """A `git rev-list` probe that errors after a resolvable base ref must
    also bail out unchecked rather than silently reporting ahead=None,
    which combined with a clean tree would read as "no evidence"."""
    import lionagi.state.completion_evidence as ce_mod

    real_run_git = ce_mod._run_git

    def _flaky(cwd: str, args: list[str]) -> tuple[int, str]:
        if args and args[0] == "rev-list":
            return 128, ""
        return real_run_git(cwd, args)

    monkeypatch.setattr(ce_mod, "_run_git", _flaky)
    evidence = check_completion_evidence(str(git_repo), base_ref="main")
    assert evidence["checked"] is False
    assert has_completion_evidence(evidence) is False


def test_unresolvable_base_ref_still_checks_dirty(tmp_path: Path):
    """A repo with no resolvable base (e.g. a fresh repo with no commits at
    all) still reports dirty/clean — the base-ref comparison just no-ops."""
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@test.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "untracked.txt").write_text("data\n")
    evidence = check_completion_evidence(str(tmp_path))
    assert evidence["checked"] is True
    assert evidence["base_ref"] is None
    assert evidence["commits_ahead"] is None
    assert evidence["dirty"] is True
    assert has_completion_evidence(evidence) is True
