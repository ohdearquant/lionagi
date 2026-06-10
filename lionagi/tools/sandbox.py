# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Sandbox execution via git worktrees.

Agent works in an isolated worktree branch. Changes are tracked as git diff.
If approved, merge the branch back. If rejected, delete the worktree.

Why worktrees over tempdir:
- Based on real repo state (same files, same history)
- Changes are a proper git branch (reviewable, mergeable)
- No need to copy files — worktree shares the git objects
- Agent sees the real codebase, not a synthetic environment
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from lionagi.ln.concurrency import run_sync

from ._subprocess import _subprocess_sync


@dataclass
class SandboxSession:
    worktree_path: str
    branch_name: str
    base_branch: str
    repo_root: str
    is_active: bool = True


def _run_git(args: list[str], cwd: str | None = None) -> tuple[str, str, int]:
    result = _subprocess_sync(["git"] + args, False, 30.0, cwd)  # noqa: S603  # argv is always ["git"] + validated git sub-commands; no shell interpolation
    return result["stdout"].strip(), result["stderr"].strip(), result["returncode"]


def _create_worktree_sync(repo_root: str, branch_name: str, base_branch: str) -> SandboxSession:
    """Create a git worktree for isolated work."""
    root = Path(repo_root)
    worktree_dir = root / ".worktrees" / branch_name
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)

    _, err, rc = _run_git(
        ["worktree", "add", "-b", branch_name, str(worktree_dir), base_branch],
        cwd=repo_root,
    )
    if rc != 0:
        raise RuntimeError(f"Failed to create worktree: {err}")

    return SandboxSession(
        worktree_path=str(worktree_dir),
        branch_name=branch_name,
        base_branch=base_branch,
        repo_root=repo_root,
    )


def _get_diff_sync(session: SandboxSession) -> dict:
    """Get diff of all changes in the worktree vs base branch."""
    wt = session.worktree_path

    _run_git(["add", "-A"], cwd=wt)

    diff_stat, _, _ = _run_git(["diff", "--cached", "--stat"], cwd=wt)
    diff_patch, _, _ = _run_git(["diff", "--cached"], cwd=wt)

    changed, _, _ = _run_git(["diff", "--cached", "--name-only"], cwd=wt)
    files = [f for f in changed.split("\n") if f] if changed else []

    return {
        "files_changed": files,
        "stat": diff_stat,
        "patch": diff_patch[:10000] if len(diff_patch) > 10000 else diff_patch,
        "patch_truncated": len(diff_patch) > 10000,
        "full_patch_chars": len(diff_patch),
    }


def _commit_sync(session: SandboxSession, message: str) -> dict:
    """Commit staged changes in the worktree."""
    wt = session.worktree_path
    _run_git(["add", "-A"], cwd=wt)

    stdout, stderr, rc = _run_git(["commit", "-m", message], cwd=wt)
    if rc != 0:
        if "nothing to commit" in stdout + stderr:
            return {"success": True, "message": "Nothing to commit"}
        return {"success": False, "error": stderr}

    sha, _, _ = _run_git(["rev-parse", "HEAD"], cwd=wt)
    return {"success": True, "commit": sha, "message": message}


def _cleanup_worktree_sync(session: SandboxSession) -> dict:
    """Remove worktree and delete branch."""
    _, err1, rc1 = _run_git(
        ["worktree", "remove", session.worktree_path, "--force"],
        cwd=session.repo_root,
    )
    _, err2, rc2 = _run_git(
        ["branch", "-D", session.branch_name],
        cwd=session.repo_root,
    )
    session.is_active = False
    return {
        "worktree_removed": rc1 == 0,
        "branch_deleted": rc2 == 0,
        "errors": [e for e in [err1, err2] if e and "error" in e.lower()],
    }


def _merge_sync(session: SandboxSession) -> dict:
    """Merge worktree branch back into base branch."""
    _run_git(["add", "-A"], cwd=session.worktree_path)
    _run_git(["commit", "-m", f"sandbox: {session.branch_name}"], cwd=session.worktree_path)

    stdout, stderr, rc = _run_git(
        [
            "merge",
            "--no-ff",
            session.branch_name,
            "-m",
            f"Merge sandbox {session.branch_name}",
        ],
        cwd=session.repo_root,
    )
    if rc != 0:
        return {"success": False, "error": stderr}

    cleanup = _cleanup_worktree_sync(session)
    return {"success": True, "merged": True, **cleanup}


async def create_sandbox(
    repo_root: str,
    base_branch: str | None = None,
    name: str | None = None,
) -> SandboxSession:
    """Create an isolated sandbox (git worktree) for safe code changes."""
    if base_branch is None:
        stdout, _, _ = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root)
        base_branch = stdout or "main"

    branch_name = name or f"sandbox-{uuid.uuid4().hex[:8]}"
    return await run_sync(_create_worktree_sync, repo_root, branch_name, base_branch)


async def sandbox_diff(session: SandboxSession) -> dict:
    """Get diff of changes made in the sandbox."""
    return await run_sync(_get_diff_sync, session)


async def sandbox_commit(session: SandboxSession, message: str) -> dict:
    """Commit changes in the sandbox."""
    return await run_sync(_commit_sync, session, message)


async def sandbox_merge(session: SandboxSession) -> dict:
    """Merge sandbox changes back and clean up."""
    return await run_sync(_merge_sync, session)


async def sandbox_discard(session: SandboxSession) -> dict:
    """Discard sandbox and all changes."""
    return await run_sync(_cleanup_worktree_sync, session)
