# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Completion-trust gate: cheap, local, no-network git signal (commits ahead of
base / dirty tree) for the teardown path when no artifact contract caught an empty run."""

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
    """Check whether *cwd* shows local evidence of work: commits ahead of a base
    ref, or a dirty tree. ``checked=False`` means "no opinion", not "no evidence"."""
    if not cwd:
        return _no_evidence("no cwd provided")

    rc, _ = _run_git(cwd, ["rev-parse", "--is-inside-work-tree"])
    if rc != 0:
        return _no_evidence("cwd is not a git working tree")

    # A probe failure (transient error, timeout) must never read as "ran and found
    # nothing" — any decisive failure bails out as unchecked, not false-empty.
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
