# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Sandbox execution via git worktrees — isolated branch per session, merge or discard."""

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
    base_sha: str = ""


# Branch names a merge refuses to target unless the caller opts in explicitly.
_PROTECTED_BRANCH_NAMES = {"main", "master"}


def _is_protected_branch(name: str) -> bool:
    return name in _PROTECTED_BRANCH_NAMES or name.startswith("release")


def _run_git(args: list[str], cwd: str | None = None) -> tuple[str, str, int]:
    result = _subprocess_sync(["git"] + args, False, 30.0, cwd)  # noqa: S603  # argv is always ["git"] + validated git sub-commands; no shell interpolation
    return result["stdout"].strip(), result["stderr"].strip(), result["returncode"]


def _create_worktree_sync(repo_root: str, branch_name: str, base_branch: str) -> SandboxSession:
    """Create a git worktree for isolated work."""
    root = Path(repo_root)
    worktree_dir = root / ".worktrees" / branch_name
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)

    base_sha, sha_err, sha_rc = _run_git(["rev-parse", base_branch], cwd=repo_root)
    if sha_rc != 0:
        raise RuntimeError(f"Failed to resolve base branch {base_branch!r}: {sha_err}")

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
        base_sha=base_sha,
    )


def _diff_untracked_file(wt: str, rel_path: str) -> tuple[str, str]:
    """Diff a single untracked file against /dev/null without touching the index."""
    patch, _, _ = _run_git(["diff", "--no-index", "--", "/dev/null", rel_path], cwd=wt)
    stat, _, _ = _run_git(["diff", "--no-index", "--stat", "--", "/dev/null", rel_path], cwd=wt)
    return patch, stat


def _list_untracked_files(wt: str) -> list[str]:
    """List untracked (non-ignored) files, recursing into untracked directories.

    Uses ``git ls-files --others --exclude-standard -z``: NUL-delimited raw
    paths, unlike ``git status --porcelain`` which quotes/escapes paths with
    spaces or special characters (breaking naive ``line[3:]`` slicing) and
    reports an untracked directory as a single ``?? dir/`` entry instead of
    the files inside it.
    """
    result = _subprocess_sync(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], False, 30.0, wt
    )
    raw = result["stdout"]
    return [p for p in raw.split("\0") if p]


def _get_diff_sync(session: SandboxSession) -> dict:
    """Get diff of all changes in the worktree vs base branch.

    Reads the diff without staging anything — the worktree's index is left
    exactly as the caller left it.
    """
    wt = session.worktree_path
    if not Path(wt).is_dir():
        # Without this guard the git calls below fail silently and the
        # result looks like a clean, empty diff.
        raise RuntimeError(
            f"Sandbox worktree {wt} no longer exists; cleanup was partially "
            "completed. Retry discard to finish cleanup."
        )

    tracked_patch, _, _ = _run_git(["diff", "HEAD"], cwd=wt)
    tracked_stat, _, _ = _run_git(["diff", "HEAD", "--stat"], cwd=wt)
    tracked_changed, _, _ = _run_git(["diff", "HEAD", "--name-only"], cwd=wt)
    files = [f for f in tracked_changed.split("\n") if f] if tracked_changed else []

    untracked = _list_untracked_files(wt)

    untracked_patches = []
    untracked_stats = []
    for rel in untracked:
        patch, stat = _diff_untracked_file(wt, rel)
        if patch:
            untracked_patches.append(patch)
        if stat:
            untracked_stats.append(stat)
        files.append(rel)

    diff_patch = tracked_patch
    if untracked_patches:
        diff_patch = "\n".join([p for p in [tracked_patch, *untracked_patches] if p])

    diff_stat = tracked_stat
    if untracked_stats:
        diff_stat = "\n".join([s for s in [tracked_stat, *untracked_stats] if s])

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
    if not Path(wt).is_dir():
        return {
            "success": False,
            "error": (
                f"Sandbox worktree {wt} no longer exists; cleanup was "
                "partially completed. Retry discard to finish cleanup."
            ),
        }
    _run_git(["add", "-A"], cwd=wt)

    stdout, stderr, rc = _run_git(["commit", "-m", message], cwd=wt)
    if rc != 0:
        if "nothing to commit" in stdout + stderr:
            return {"success": True, "message": "Nothing to commit"}
        return {"success": False, "error": stderr}

    sha, _, _ = _run_git(["rev-parse", "HEAD"], cwd=wt)
    return {"success": True, "commit": sha, "message": message}


def _worktree_registered(repo_root: str, worktree_path: str) -> bool:
    """Whether the path is still registered as a worktree of this repo."""
    out, _, rc = _run_git(["worktree", "list", "--porcelain"], cwd=repo_root)
    if rc != 0:
        # Cannot verify — assume it is still present so a caller never
        # treats an unverified worktree as cleaned up.
        return True
    target = str(Path(worktree_path).resolve())
    for line in out.splitlines():
        if line.startswith("worktree ") and str(Path(line[9:]).resolve()) == target:
            return True
    return False


def _branch_exists(repo_root: str, branch_name: str) -> bool:
    _, _, rc = _run_git(
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        cwd=repo_root,
    )
    return rc == 0


def _cleanup_worktree_sync(session: SandboxSession) -> dict:
    """Remove worktree and delete branch.

    Retry-safe: a resource that is already absent counts as cleaned up, so a
    partial failure (e.g. worktree removed but branch deletion blocked by
    another checkout) can be completed by a later retry instead of failing
    forever on the step that already succeeded. ``is_active`` is only flipped
    to ``False`` once both resources are actually gone — a partial failure
    keeps the session marked active so a caller cannot mistake it for
    cleaned up.
    """
    _, err1, rc1 = _run_git(
        ["worktree", "remove", session.worktree_path, "--force"],
        cwd=session.repo_root,
    )
    _, err2, rc2 = _run_git(
        ["branch", "-D", session.branch_name],
        cwd=session.repo_root,
    )
    worktree_removed = rc1 == 0 or not _worktree_registered(
        session.repo_root, session.worktree_path
    )
    branch_deleted = rc2 == 0 or not _branch_exists(session.repo_root, session.branch_name)
    if worktree_removed and branch_deleted:
        session.is_active = False
    return {
        "worktree_removed": worktree_removed,
        "branch_deleted": branch_deleted,
        "errors": [e for e in [err1, err2] if e and "error" in e.lower()],
    }


def _merge_sync(session: SandboxSession, allow_protected: bool = False) -> dict:
    """Merge worktree branch back into base branch.

    Refuses to run when ``repo_root`` is in a detached HEAD state, when it is
    not actually checked out on the session's recorded base branch (no
    auto-checkout), and refuses to merge into a protected branch name
    (``main``, ``master``, ``release*``) unless the caller explicitly opts
    in via ``allow_protected``.
    """
    current_branch, err, rc = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=session.repo_root)
    if rc != 0:
        return {
            "success": False,
            "error": f"Could not determine the branch checked out at repo_root: {err}",
        }
    if current_branch == "HEAD":
        # `--abbrev-ref HEAD` returns the literal string "HEAD" when detached
        # rather than a branch name — merging here would move a detached
        # HEAD forward with no branch ref pointing at the result.
        return {
            "success": False,
            "error": "repo_root is in a detached HEAD state; refusing to merge into an unverified target.",
        }
    if current_branch != session.base_branch:
        return {
            "success": False,
            "error": (
                f"repo_root is on {current_branch!r}, not the sandbox's recorded "
                f"base branch {session.base_branch!r}; refusing to merge into an "
                "unverified target."
            ),
        }
    if _is_protected_branch(session.base_branch) and not allow_protected:
        return {
            "success": False,
            "error": (
                f"base branch {session.base_branch!r} is protected; pass "
                "allow_protected=True to merge into it explicitly."
            ),
        }

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
        if stdout == "HEAD":
            # `--abbrev-ref HEAD` returns the literal string "HEAD" when
            # repo_root is in a detached HEAD state — there is no branch to
            # record as the sandbox's merge target.
            raise RuntimeError(
                "repo_root is in a detached HEAD state; pass an explicit base_branch."
            )
        base_branch = stdout or "main"

    branch_name = name or f"sandbox-{uuid.uuid4().hex[:8]}"
    return await run_sync(_create_worktree_sync, repo_root, branch_name, base_branch)


async def sandbox_diff(session: SandboxSession) -> dict:
    """Get diff of changes made in the sandbox."""
    return await run_sync(_get_diff_sync, session)


async def sandbox_commit(session: SandboxSession, message: str) -> dict:
    """Commit changes in the sandbox."""
    return await run_sync(_commit_sync, session, message)


async def sandbox_merge(session: SandboxSession, *, allow_protected: bool = False) -> dict:
    """Merge sandbox changes back and clean up.

    Refuses when ``repo_root`` is in a detached HEAD state or isn't checked
    out on the sandbox's recorded base branch, and refuses to merge into a
    protected branch name (``main``, ``master``, ``release*``) unless
    ``allow_protected=True``.
    """
    return await run_sync(_merge_sync, session, allow_protected)


async def sandbox_discard(session: SandboxSession) -> dict:
    """Discard sandbox and all changes."""
    return await run_sync(_cleanup_worktree_sync, session)
