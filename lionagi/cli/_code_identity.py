# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""What code is actually running, as a value a caller with no shell can read.

A long-lived process loads its code once, at startup. A commit landing on disk
does not change it, and a restart command that exits 0 says nothing about which
tree the new process imported. So the running process reports its own identity —
version, the resolved path it imported itself from, that tree's git position,
and how many verbs it registered — and every answer is derived here, in one
place, so the handshake and the server's own info cannot disagree.

Failure is closed: a tree whose git state cannot be read is ``unknown``, never
``ok``. "Cannot tell" and "nothing wrong" are different answers.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

__all__ = (
    "code_identity",
    "git_identity",
    "GIT_TIMEOUT_SECONDS",
)

# Bounded because this runs inside a handshake a client is waiting on, and a git
# call against an unhealthy tree (a stale index lock, a dead network remote) can
# otherwise hang the answer instead of failing it.
GIT_TIMEOUT_SECONDS = 5.0

_SHORT_SHA = 12


def _git(tree: Path, *argv: str) -> tuple[int, str, str]:
    """Run one git command against *tree*; returns (rc, stdout, stderr), stripped.

    An rc of -1 means git itself could not be run, which is a different fact
    from git running and refusing.
    """
    try:
        completed = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "-C", str(tree), *argv],  # noqa: S607 — resolved from PATH by design
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, "", f"{type(exc).__name__}: {exc}"
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


def _comparison_ref(tree: Path) -> tuple[str | None, str, str | None]:
    """The ref this checkout should be measured against, and where it came from.

    The configured upstream is preferred. A detached checkout has none — which is
    exactly the state a pinned deployment sits in — so the remote's default
    branch is the fallback, because "behind the branch this remote publishes" is
    the question a detached tree still has an answer to.
    """
    rc, out, _ = _git(tree, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if rc == 0 and out:
        return out, "upstream", None

    rc, out, err = _git(tree, "rev-parse", "--abbrev-ref", "origin/HEAD")
    if rc == 0 and out:
        return out, "remote_head", None
    return None, "none", err or "no upstream configured and origin/HEAD does not resolve"


def git_identity(tree: Path) -> dict[str, Any]:
    """Where *tree* sits in git history, or why that could not be established."""
    rc, top, err = _git(tree, "rev-parse", "--show-toplevel")
    if rc != 0:
        if "not a git repository" in err.lower():
            return {
                "status": "not_a_git_checkout",
                "detail": f"{tree} is not inside a git checkout",
            }
        return {"status": "unknown", "detail": f"git rev-parse failed: {err or 'no output'}"}

    identity: dict[str, Any] = {"status": "ok", "toplevel": top}

    rc, commit, err = _git(tree, "rev-parse", "HEAD")
    if rc != 0 or not commit:
        return {"status": "unknown", "detail": f"could not read HEAD: {err or 'no output'}"}
    identity["commit"] = commit
    identity["commit_short"] = commit[:_SHORT_SHA]

    rc, branch, _ = _git(tree, "rev-parse", "--abbrev-ref", "HEAD")
    detached = rc != 0 or branch == "HEAD"
    identity["detached"] = detached
    identity["branch"] = None if detached else branch

    rc, porcelain, err = _git(tree, "status", "--porcelain")
    if rc != 0:
        return {"status": "unknown", "detail": f"could not read working tree: {err}"}
    identity["dirty"] = bool(porcelain)

    ref, source, why = _comparison_ref(tree)
    identity["comparison_ref"] = ref
    identity["comparison_ref_source"] = source
    if ref is None:
        identity["ahead"] = None
        identity["behind"] = None
        identity["comparison_detail"] = why
        return identity

    rc, counts, err = _git(tree, "rev-list", "--left-right", "--count", f"HEAD...{ref}")
    if rc != 0:
        return {
            "status": "unknown",
            "detail": f"could not compare HEAD against {ref}: {err or 'no output'}",
            "commit": commit,
        }
    try:
        ahead_text, behind_text = counts.split()
        identity["ahead"] = int(ahead_text)
        identity["behind"] = int(behind_text)
    except ValueError:
        return {
            "status": "unknown",
            "detail": f"unreadable rev-list output comparing HEAD against {ref}: {counts!r}",
            "commit": commit,
        }
    return identity


def _distribution_version() -> tuple[str | None, str | None]:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as dist_version

    try:
        return dist_version("lionagi"), None
    except PackageNotFoundError:
        return None, "no installed lionagi distribution metadata (running from source?)"
    except Exception as exc:  # noqa: BLE001 — metadata readers raise broadly
        return None, f"{type(exc).__name__}: {exc}"


def _verb_count() -> tuple[int | None, str | None]:
    try:
        from lionagi.mcp.verbs import VERBS
    except Exception as exc:  # noqa: BLE001 — a broken verb table must not break the handshake
        return None, f"could not import the verb table: {type(exc).__name__}: {exc}"
    return len(VERBS), None


def _drift(
    git: dict[str, Any],
    version: str,
    distribution_version: str | None,
) -> dict[str, Any]:
    reasons: list[str] = []
    unknowns: list[str] = []

    if distribution_version is not None and distribution_version != version:
        reasons.append(
            f"the loaded package reports version {version} but the installed "
            f"distribution declares {distribution_version} — the import path is not "
            "serving the installed build"
        )

    status = git.get("status")
    if status == "unknown":
        unknowns.append(f"git state unreadable: {git.get('detail', 'no detail')}")
    elif status == "ok":
        behind = git.get("behind")
        if behind is None:
            unknowns.append(
                "nothing to compare this checkout against: "
                f"{git.get('comparison_detail', 'no comparison ref')}"
            )
        elif behind > 0:
            reasons.append(
                f"the loaded checkout is {behind} commit(s) behind "
                f"{git['comparison_ref']} — it is serving code older than that ref"
            )

    if reasons:
        return {"status": "drift", "reasons": reasons, "unknown": unknowns}
    if unknowns:
        return {"status": "unknown", "reasons": [], "unknown": unknowns}
    return {"status": "ok", "reasons": [], "unknown": []}


def code_identity() -> dict[str, Any]:
    """The running process's own code identity, drift verdict included.

    Everything here describes the module objects this process actually imported,
    never the environment that was supposed to supply them.
    """
    import lionagi
    from lionagi.version import __version__

    module_file = getattr(lionagi, "__file__", None)
    package_path = str(Path(module_file).resolve().parent) if module_file else None

    git = (
        git_identity(Path(package_path))
        if package_path
        else {"status": "unknown", "detail": "the loaded lionagi package has no __file__"}
    )
    distribution_version, distribution_detail = _distribution_version()
    verb_count, verb_detail = _verb_count()

    identity: dict[str, Any] = {
        "version": __version__,
        "package_path": package_path,
        "distribution_version": distribution_version,
        "verb_count": verb_count,
        "git": git,
        "drift": _drift(git, __version__, distribution_version),
    }
    if distribution_detail is not None:
        identity["distribution_detail"] = distribution_detail
    if verb_detail is not None:
        identity["verb_count_detail"] = verb_detail
    return identity
