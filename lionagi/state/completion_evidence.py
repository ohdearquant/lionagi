# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Completion-trust gate: cheap, local, no-network evidence that a run produced something.

A session can exit its loop cleanly with nothing to show for it — no commits,
no artifacts, no diff. Historically that still got stamped ``completed``, so
operators stopped trusting the status and re-verified by hand. This module
gives the teardown path a lightweight git-based signal to fall back on when
no artifact contract caught the emptiness: is HEAD ahead of the base ref, or
does the working tree carry uncommitted changes?
"""

from __future__ import annotations

import subprocess
from typing import TypedDict

_GIT_TIMEOUT = 5  # seconds; local-only ops, never touches the network


class CompletionEvidence(TypedDict):
    checked: bool
    ahead_of_base: bool | None
    commits_ahead: int | None
    dirty: bool | None
    base_ref: str | None
    reason: str


def _run_git(cwd: str, args: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(  # noqa: S603 — fixed "git" argv, no shell, cwd is caller-controlled
            ["git", *args],  # noqa: S607 — relies on PATH resolution for "git", intentional
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
        return proc.returncode, proc.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


def _resolve_base_ref(cwd: str, base_ref: str | None) -> str | None:
    if base_ref:
        rc, _ = _run_git(cwd, ["rev-parse", "--verify", "--quiet", base_ref])
        if rc == 0:
            return base_ref
    rc, out = _run_git(cwd, ["symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD"])
    if rc == 0 and out:
        return out
    for candidate in ("origin/main", "origin/master", "main", "master"):
        rc, _ = _run_git(cwd, ["rev-parse", "--verify", "--quiet", candidate])
        if rc == 0:
            return candidate
    return None


def _no_evidence(reason: str) -> CompletionEvidence:
    return {
        "checked": False,
        "ahead_of_base": None,
        "commits_ahead": None,
        "dirty": None,
        "base_ref": None,
        "reason": reason,
    }


def check_completion_evidence(
    cwd: str | None,
    *,
    base_ref: str | None = None,
) -> CompletionEvidence:
    """Check whether *cwd* shows local evidence of work: commits ahead of a
    base ref, or an uncommitted (dirty) working tree.

    Returns ``checked=False`` when *cwd* is missing or not a git working
    tree — callers must treat that as "no opinion", not "no evidence found".
    """
    if not cwd:
        return _no_evidence("no cwd provided")

    rc, _ = _run_git(cwd, ["rev-parse", "--is-inside-work-tree"])
    if rc != 0:
        return _no_evidence("cwd is not a git working tree")

    # A probe that actually runs and fails (transient error, timeout, git
    # hiccup) must never be read as "ran and found nothing" — that silently
    # turns a git error into a false completed_empty on real work. Only a
    # probe that *succeeds* is allowed to report an absence of evidence;
    # any decisive failure bails the whole check out as unchecked so the
    # caller keeps trusting "completed".
    rc, status_out = _run_git(cwd, ["status", "--porcelain"])
    if rc != 0:
        return _no_evidence("git status probe failed")
    dirty = bool(status_out)

    resolved_base = _resolve_base_ref(cwd, base_ref)
    commits_ahead: int | None = None
    ahead: bool | None = None
    if resolved_base is not None:
        rc, out = _run_git(cwd, ["rev-list", "--count", f"{resolved_base}..HEAD"])
        if rc != 0 or not out.isdigit():
            return _no_evidence("git rev-list probe failed")
        commits_ahead = int(out)
        ahead = commits_ahead > 0

    return {
        "checked": True,
        "ahead_of_base": ahead,
        "commits_ahead": commits_ahead,
        "dirty": dirty,
        "base_ref": resolved_base,
        "reason": "",
    }


def has_completion_evidence(evidence: CompletionEvidence) -> bool:
    """True if the check ran and found commits ahead of base or a dirty tree."""
    if not evidence.get("checked"):
        return False
    return bool(evidence.get("ahead_of_base")) or bool(evidence.get("dirty"))
