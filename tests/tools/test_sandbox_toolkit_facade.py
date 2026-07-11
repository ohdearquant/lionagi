# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for CodingToolkit's sandbox tool facade: protected-branch merge
gating and truthful discard cleanup reporting."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import lionagi.tools.sandbox as sandbox_module
from lionagi.session.branch import Branch
from lionagi.tools.coding import CodingToolkit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path) -> None:
    cmds = [
        ["git", "init"],
        ["git", "config", "user.email", "test@test.com"],
        ["git", "config", "user.name", "Test"],
    ]
    for cmd in cmds:
        subprocess.run(cmd, cwd=str(path), capture_output=True, check=True)
    (path / "README.md").write_text("initial\n")
    subprocess.run(["git", "add", "."], cwd=str(path), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path), capture_output=True, check=True)


@pytest.fixture
def git_repo(tmp_path):
    _init_git_repo(tmp_path)
    return tmp_path


def _make_sandbox_tool(git_repo, **toolkit_kwargs):
    branch = Branch()
    tk = CodingToolkit(
        notify=False, workspace_root=str(git_repo), tools=["sandbox"], **toolkit_kwargs
    )
    tools = tk.bind(branch)
    for t in tools:
        if t.func_callable.__name__ == "sandbox":
            return tk, t.func_callable
    raise KeyError("sandbox tool not found")


# ---------------------------------------------------------------------------
# Constructor-level sandbox_allow_protected — operator trust decision
# ---------------------------------------------------------------------------


def test_sandbox_allow_protected_defaults_false(git_repo):
    tk, _ = _make_sandbox_tool(git_repo)
    assert tk.sandbox_allow_protected is False


def test_sandbox_request_schema_has_no_allow_protected_field():
    """An in-band agent must not be able to self-approve a protected merge —
    allow_protected is deliberately absent from the LLM-facing schema."""
    from lionagi.tools.coding import SandboxRequest

    assert "allow_protected" not in SandboxRequest.model_fields


async def test_facade_merge_refuses_protected_branch_by_default(git_repo):
    """git init defaults to a protected branch name (master); the facade's
    merge action must refuse it when the toolkit wasn't given the operator
    flag, even though the agent-facing SandboxRequest has no such field."""
    _, sandbox = _make_sandbox_tool(git_repo)

    created = await sandbox(action="create")
    assert created["success"] is True

    result = await sandbox(action="merge")

    assert result["success"] is False
    assert "protected" in result["error"]
    assert not (git_repo / "merged.txt").exists()


async def test_facade_merge_succeeds_with_operator_level_flag(git_repo):
    """Only a constructor-level (operator-composed) flag can unlock merging
    into a protected branch — never a per-call agent argument."""
    _, sandbox = _make_sandbox_tool(git_repo, sandbox_allow_protected=True)

    created = await sandbox(action="create")
    assert created["success"] is True
    worktree = Path(created["worktree"])
    (worktree / "merged.txt").write_text("from sandbox\n")

    result = await sandbox(action="merge")

    assert result["success"] is True
    assert (git_repo / "merged.txt").read_text() == "from sandbox\n"


# ---------------------------------------------------------------------------
# Truthful discard cleanup reporting
# ---------------------------------------------------------------------------


async def test_facade_discard_reports_failure_and_keeps_session(git_repo, monkeypatch):
    """A partial cleanup failure (e.g. locked worktree) must not be reported
    as success:True, and the session must be retained so a caller can retry
    (or at least still inspect the sandbox) instead of losing the handle."""
    _, sandbox = _make_sandbox_tool(git_repo)

    created = await sandbox(action="create")
    assert created["success"] is True

    async def fake_discard(session):
        return {
            "worktree_removed": False,
            "branch_deleted": True,
            "errors": ["worktree is locked"],
        }

    monkeypatch.setattr(sandbox_module, "sandbox_discard", fake_discard)

    result = await sandbox(action="discard")

    assert result["success"] is False
    assert result["worktree_removed"] is False

    # Session retained: a subsequent action must still see an active sandbox
    # rather than "No active sandbox. Create one first."
    diff_result = await sandbox(action="diff")
    assert diff_result["success"] is True
    assert "error" not in diff_result or diff_result.get("success") is True


async def test_facade_discard_branch_delete_only_failure_reported(git_repo, monkeypatch):
    """The mirror case: worktree removed cleanly but branch deletion fails
    (branch checked out elsewhere) — also success:False, session retained."""
    _, sandbox = _make_sandbox_tool(git_repo)

    created = await sandbox(action="create")
    assert created["success"] is True

    async def fake_discard(session):
        return {
            "worktree_removed": True,
            "branch_deleted": False,
            "errors": ["branch checked out elsewhere"],
        }

    monkeypatch.setattr(sandbox_module, "sandbox_discard", fake_discard)

    result = await sandbox(action="discard")

    assert result["success"] is False
    assert result["branch_deleted"] is False

    diff_result = await sandbox(action="diff")
    assert diff_result["success"] is True


async def test_facade_discard_reports_success_only_when_both_steps_succeed(git_repo):
    """Real (non-mocked) discard through the facade on a healthy sandbox
    still reports success:True and clears the session — the fix only
    changes behavior on partial failure."""
    _, sandbox = _make_sandbox_tool(git_repo)

    created = await sandbox(action="create")
    assert created["success"] is True

    result = await sandbox(action="discard")
    assert result["success"] is True
    assert result["worktree_removed"] is True
    assert result["branch_deleted"] is True

    # session cleared: a further action reports "no active sandbox"
    after = await sandbox(action="diff")
    assert after["success"] is False
    assert "No active sandbox" in after["error"]
