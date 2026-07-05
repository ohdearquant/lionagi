"""Isolated per-task workspaces — clone the bug's repo, never touch real files.

Safety model (never touch real files):
  - All clones live under ``~/.lionagi/swebench-work`` (scratch, outside any real
    project). The agent NEVER sees the user's actual repos.
  - One cached full clone per upstream repo (django, sphinx); each task gets its
    own git WORKTREE checked out at the bug's ``base_commit`` (worktrees share
    objects — cheap). Edits in one task can't affect another.
  - lionagi's coding tools are path-jailed to the worktree (editor/reader resolve
    under workspace_root; bash runs with cwd=worktree). Destructive commands are
    blocked by the guard hook the runner attaches.

The model_patch handed to the oracle is ``git diff`` of the worktree vs
``base_commit`` — exactly what SWE-bench applies on top of the held-out tests.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

WORK_ROOT = Path.home() / ".lionagi" / "swebench-work"
REPOS = WORK_ROOT / "repos"
WORKTREES = WORK_ROOT / "wt"

_REPO_URL = {
    "django/django": "https://github.com/django/django.git",
    "sphinx-doc/sphinx": "https://github.com/sphinx-doc/sphinx.git",
}


def _git(
    args: list[str], cwd: str | Path | None = None, timeout: int = 600
) -> tuple[str, str, int]:
    p = subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["git", *args],  # noqa: S607 — git resolved on PATH by design
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
    )
    return p.stdout.strip(), p.stderr.strip(), p.returncode


def repo_url(repo: str) -> str:
    return _REPO_URL.get(repo, f"https://github.com/{repo}.git")


def ensure_repo(repo: str) -> Path:
    """Cached full clone of an upstream repo under the scratch root."""
    REPOS.mkdir(parents=True, exist_ok=True)
    dest = REPOS / repo.replace("/", "__")
    if (dest / ".git").exists():
        return dest
    _, err, rc = _git(["clone", repo_url(repo), str(dest)], timeout=1800)
    if rc != 0:
        raise RuntimeError(f"clone {repo} failed: {err}")
    return dest


def make_worktree(repo: str, base_commit: str, instance_id: str) -> Path:
    """A fresh worktree of ``repo`` checked out at ``base_commit``."""
    base = ensure_repo(repo)
    WORKTREES.mkdir(parents=True, exist_ok=True)
    wt = WORKTREES / instance_id
    if wt.exists():
        remove_worktree(repo, instance_id)
    # The commit may not be present in a stale cache — fetch it by SHA.
    _git(["fetch", "origin", base_commit], cwd=base, timeout=900)
    _, err, rc = _git(["worktree", "add", "--detach", str(wt), base_commit], cwd=base)
    if rc != 0:
        raise RuntimeError(f"worktree add {instance_id}@{base_commit[:10]} failed: {err}")
    return wt


def extract_patch(repo: str, instance_id: str) -> str:
    """Unified diff of the worktree vs its base_commit — the model_patch.

    Excludes untracked noise; captures tracked edits + new files the agent added.
    """
    wt = WORKTREES / instance_id
    _git(["add", "-A"], cwd=wt)
    patch, _, _ = _git(["diff", "--cached"], cwd=wt, timeout=120)
    return patch


def remove_worktree(repo: str, instance_id: str) -> None:
    base = REPOS / repo.replace("/", "__")
    wt = WORKTREES / instance_id
    _git(["worktree", "remove", "--force", str(wt)], cwd=base)
