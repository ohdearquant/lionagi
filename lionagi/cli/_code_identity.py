# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""What code is actually running, as a value a caller with no shell can read.

A long-lived process loads its code once, at startup. A commit landing on disk
does not change it, and a restart command that exits 0 says nothing about which
tree the new process imported. So the running process reports its own identity —
version, the resolved path it imported itself from, that tree's git position,
and how many verbs it registered — and every answer is derived here, in one
place, so the handshake and the server's own info cannot disagree.

The git position is the part that can go stale under a running process, because
someone can move the checkout the process imported from and the loaded module
objects will not notice. So it is captured once, as early as the process can
manage, and that capture is what the identity calls the running code. The tree's
current position is read too, and reported beside it: when the two disagree the
checkout moved after this process loaded, and that divergence is itself the fact
an operator needs, so it is named in the payload rather than left to be inferred.

A tree can also part from the running code without its commit changing at all,
because an uncommitted edit moves the files and leaves the commit id alone. Two
things follow. A commit id does not describe a dirty tree in the first place, so
a snapshot taken over uncommitted changes can only ever be a partial identity and
says so. And to notice an edit made afterwards, the snapshot carries a digest of
the tree's uncommitted state — its shape, and the size, modification time, mode
and inode of every path git names, never its content — which is compared live
exactly as the commit is. The two kinds of movement stay separate in the payload: moving the
checkout and editing files under it are different operator actions with different
remedies, and one boolean cannot carry both.

What that digest can miss is enumerated where it is computed, as a list rather
than as a count: an edit is invisible to it only if it moves none of a path's
size, modification time, mode or inode, or if it happens somewhere git never
names, which is what ``.gitignore`` puts out of scope. Everything else an operator
can do to a file under this process — including to an untracked file, to a file
inside an untracked directory, to a file git renders only as a modified binary,
and a permission change that rewrites no bytes at all — moves the digest.

Failure is closed: a tree whose git state cannot be read is ``unknown``, never
``ok``. "Cannot tell" and "nothing wrong" are different answers, and a git call
that could not run at all is a third thing again — it is not evidence that the
tree lacks an upstream, so it never falls through to the fallback comparison.
"""

from __future__ import annotations

import hashlib
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = (
    "code_identity",
    "git_identity",
    "snapshot_git_position",
    "GIT_TIMEOUT_SECONDS",
    "IDENTITY_BUDGET_SECONDS",
)

# Bounded because this runs inside a handshake a client is waiting on, and a git
# call against an unhealthy tree (a stale index lock, a dead network remote) can
# otherwise hang the answer instead of failing it.
GIT_TIMEOUT_SECONDS = 5.0

# One allowance for the whole computation, not per call — several git calls
# each free to hit the per-call timeout would outlast the handshake. Once
# exhausted the answer is `unknown` with the reason, not a false pass.
IDENTITY_BUDGET_SECONDS = 6.0

_SHORT_SHA = 12

# The digest is only ever compared against another digest of the same tree, so it
# needs to be long enough that an accidental collision is not a concern and short
# enough to read in a payload.
_FINGERPRINT_CHARS = 16

# Return codes for calls that never produced one: git could not be run at all,
# or the allowance was already spent before this call's turn came.
_COULD_NOT_RUN = -1
_BUDGET_SPENT = -2

_REF_CAVEATS = {
    "upstream": (
        "this is a remote-tracking ref, updated only by a fetch in this tree, so the "
        "comparison is as current as the last fetch and no more"
    ),
    "remote_head": (
        "origin/HEAD is a local symbolic ref, written when this clone was made and "
        "refreshed only on request; if the remote's default branch has changed since, "
        "this measures against the wrong branch and understates how far behind the "
        "checkout is. It is also a remote-tracking ref, so it is only as current as "
        "the last fetch in this tree"
    ),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _Budget:
    """One wall-clock allowance shared by every git call in a single computation."""

    def __init__(self, seconds: float) -> None:
        self.total = seconds
        self._deadline = time.monotonic() + seconds

    def remaining(self) -> float:
        return self._deadline - time.monotonic()


def _ran(rc: int) -> bool:
    """Whether git actually ran and produced this return code."""
    return rc >= 0


def _git(tree: Path, *argv: str, budget: _Budget) -> tuple[int, str, str]:
    """Run one git command against *tree*; returns (rc, stdout, stderr), stripped.

    A negative rc means no git process produced it — either git could not be run,
    or the shared allowance was gone before this call. Both are a different fact
    from git running and refusing.
    """
    left = budget.remaining()
    if left <= 0:
        return (
            _BUDGET_SPENT,
            "",
            f"the {budget.total}s allowance for reading git state was spent before "
            f"`git {' '.join(argv)}` could run",
        )
    try:
        completed = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "-C", str(tree), *argv],  # noqa: S607 — resolved from PATH by design
            capture_output=True,
            text=True,
            timeout=min(GIT_TIMEOUT_SECONDS, left),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _COULD_NOT_RUN, "", f"{type(exc).__name__}: {exc}"
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


def _comparison_ref(tree: Path, budget: _Budget) -> tuple[str | None, str, str | None, bool]:
    """The ref this checkout should be measured against, where it came from, and
    whether the question could be asked at all.

    The configured upstream is preferred. A detached checkout has none — which is
    exactly the state a pinned deployment sits in — so the remote's default
    branch is the fallback, because "behind the branch this remote publishes" is
    the question a detached tree still has an answer to.

    The fallback is only reached when git ran and told us there is no upstream. A
    call that never ran — a timeout, a missing binary, a spent allowance — says
    nothing about upstreams, so it stops here instead of being read as one.
    """
    rc, out, err = _git(
        tree, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", budget=budget
    )
    if rc == 0 and out:
        return out, "upstream", None, True
    if not _ran(rc):
        return None, "unreadable", err or "the upstream lookup did not run", False

    rc, out, err = _git(tree, "rev-parse", "--abbrev-ref", "origin/HEAD", budget=budget)
    if rc == 0 and out:
        return out, "remote_head", None, True
    if not _ran(rc):
        return None, "unreadable", err or "the origin/HEAD lookup did not run", False
    return None, "none", err or "no upstream configured and origin/HEAD does not resolve", True


def _status_paths(porcelain: str) -> list[str]:
    """The worktree paths a NUL-delimited status listing names, relative to the root.

    Records are ``XY<space><path>``. A rename or copy carries the name it came
    from as an extra field, which is dropped: that name is gone from the disk by
    definition, so asking the filesystem about it would measure nothing.
    """
    fields = [field for field in porcelain.split("\x00") if field]
    paths: list[str] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if "R" in entry[:2] or "C" in entry[:2]:
            index += 1
    return paths


def _worktree_fingerprint(
    tree: Path, root: Path, porcelain: str, budget: _Budget
) -> tuple[str | None, str | None]:
    """A digest of what is uncommitted in *tree*, or why one could not be taken.

    Metadata-only (status listing + diff against HEAD + each named path's
    size/mtime/inode, never file content), so a timeout yields no digest at all
    rather than a partial one that would compare unequal to a full digest taken
    later. See docs/internals/cli.md for known blind spots and why a false
    "unchanged" is the error this guards against harder than a false "changed".
    """
    digest = hashlib.sha256(porcelain.encode(errors="replace"))
    if porcelain:
        rc, diff, err = _git(tree, "diff", "HEAD", budget=budget)
        if rc != 0:
            return None, f"could not read the uncommitted changes: {err or 'no output'}"
        digest.update(b"\x00")
        digest.update(diff.encode(errors="replace"))
        paths = _status_paths(porcelain)
        for measured, relative in enumerate(paths):
            if budget.remaining() <= 0:
                return None, (
                    f"the {budget.total}s allowance for reading git state was spent "
                    f"after measuring {measured} of the {len(paths)} paths the status "
                    "listing names, so no digest of the uncommitted state was taken"
                )
            try:
                stat = (root / relative).lstat()
            except FileNotFoundError:
                mark = "absent"
            except OSError as exc:
                return None, (
                    f"could not read the state of {relative} under {root}: "
                    f"{type(exc).__name__}: {exc}"
                )
            else:
                mark = f"{stat.st_size}:{stat.st_mtime_ns}:{stat.st_mode}:{stat.st_ino}"
            digest.update(f"\x00{relative}\x00{mark}".encode(errors="replace"))
    return digest.hexdigest()[:_FINGERPRINT_CHARS], None


def git_identity(tree: Path, budget: _Budget | None = None) -> dict[str, Any]:
    """Where *tree* sits in git history now, or why that could not be established.

    The reading is stamped with when it was taken, because a git position is only
    true of the instant it was read.
    """
    identity = _read_git_identity(tree, budget or _Budget(IDENTITY_BUDGET_SECONDS))
    identity["observed_at"] = _now()
    return identity


def _read_git_identity(tree: Path, budget: _Budget) -> dict[str, Any]:
    rc, top, err = _git(tree, "rev-parse", "--show-toplevel", budget=budget)
    if rc != 0:
        if _ran(rc) and "not a git repository" in err.lower():
            return {
                "status": "not_a_git_checkout",
                "detail": f"{tree} is not inside a git checkout",
            }
        return {"status": "unknown", "detail": f"git rev-parse failed: {err or 'no output'}"}

    identity: dict[str, Any] = {"status": "ok", "toplevel": top}

    rc, commit, err = _git(tree, "rev-parse", "HEAD", budget=budget)
    if rc != 0 or not commit:
        return {"status": "unknown", "detail": f"could not read HEAD: {err or 'no output'}"}
    identity["commit"] = commit
    identity["commit_short"] = commit[:_SHORT_SHA]

    rc, branch, _ = _git(tree, "rev-parse", "--abbrev-ref", "HEAD", budget=budget)
    detached = rc != 0 or branch == "HEAD"
    identity["detached"] = detached
    identity["branch"] = None if detached else branch

    # `-z` so a path with a newline or a quote in it stays one field, and
    # `--untracked-files=all` so an untracked directory is listed as the files
    # inside it rather than as a single line that an edit within cannot move.
    rc, porcelain, err = _git(
        tree, "status", "--porcelain", "-z", "--untracked-files=all", budget=budget
    )
    if rc != 0:
        return {"status": "unknown", "detail": f"could not read working tree: {err}"}
    identity["dirty"] = bool(porcelain)

    fingerprint, fingerprint_detail = _worktree_fingerprint(tree, Path(top), porcelain, budget)
    identity["worktree_fingerprint"] = fingerprint
    if fingerprint_detail is not None:
        identity["worktree_fingerprint_detail"] = fingerprint_detail

    ref, source, why, asked = _comparison_ref(tree, budget)
    if not asked:
        return {
            "status": "unknown",
            "detail": f"could not establish a comparison ref: {why}",
            "commit": commit,
        }
    identity["comparison_ref"] = ref
    identity["comparison_ref_source"] = source
    if source in _REF_CAVEATS:
        identity["comparison_ref_caveat"] = _REF_CAVEATS[source]
    if ref is None:
        identity["ahead"] = None
        identity["behind"] = None
        identity["comparison_detail"] = why
        return identity

    rc, counts, err = _git(
        tree, "rev-list", "--left-right", "--count", f"HEAD...{ref}", budget=budget
    )
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


def loaded_package_path() -> str | None:
    """The directory this process actually imported ``lionagi`` from."""
    import lionagi

    module_file = getattr(lionagi, "__file__", None)
    return str(Path(module_file).resolve().parent) if module_file else None


# The tree position this process loaded from, read once and kept `None` until
# asked for — take it at startup for an honest answer later, before anything
# can move the tree underneath it.
_SNAPSHOT: dict[str, Any] | None = None


def snapshot_git_position(budget: _Budget | None = None) -> dict[str, Any]:
    """Read the loaded tree's git position once and keep it for every later call.

    Call this as early in the process as possible. The module objects this
    process holds were fixed at import; the tree they came from is a directory
    anyone can move afterwards. Capturing the position at startup is what makes a
    later divergence visible as a divergence rather than silently replacing the
    answer with a commit that was never loaded.
    """
    global _SNAPSHOT
    if _SNAPSHOT is None:
        _SNAPSHOT = _read_loaded_tree(budget or _Budget(IDENTITY_BUDGET_SECONDS))
    return _SNAPSHOT


def _read_loaded_tree(budget: _Budget) -> dict[str, Any]:
    try:
        package_path = loaded_package_path()
        if package_path is None:
            return {
                "status": "unknown",
                "detail": "the loaded lionagi package has no __file__",
                "observed_at": _now(),
            }
        return git_identity(Path(package_path), budget)
    except Exception as exc:  # noqa: BLE001 — startup must not fail on an unreadable tree
        return {
            "status": "unknown",
            "detail": f"could not read the loaded tree: {type(exc).__name__}: {exc}",
            "observed_at": _now(),
        }


def _checkout_movement(
    snapshot: dict[str, Any], live: dict[str, Any]
) -> tuple[bool | None, str | None]:
    """Whether the tree this process loaded from has moved since it loaded.

    ``True`` and ``False`` are answers; ``None`` means the comparison could not be
    made, which is not the same as the checkout having stayed put.
    """
    if live is snapshot:
        return False, None
    if snapshot.get("status") != "ok":
        return None, (
            "the position this process started from was never established: "
            f"{snapshot.get('detail', 'no detail')}"
        )
    if live.get("status") != "ok":
        return None, (f"the tree's position cannot be read now: {live.get('detail', 'no detail')}")
    if snapshot.get("commit") != live.get("commit"):
        return True, (
            f"the checkout at {snapshot.get('toplevel')} moved from "
            f"{snapshot.get('commit_short')} to {live.get('commit_short')} after this "
            "process loaded its code — the code answering here is the earlier commit, "
            "and reading that tree now describes something this process never imported"
        )
    return False, None


def _worktree_movement(
    snapshot: dict[str, Any], live: dict[str, Any], commit_moved: bool | None
) -> tuple[bool | None, str | None]:
    """Whether the files under this process were edited since it loaded its code.

    This is the movement that leaves the commit alone, so nothing about the commit
    can answer it — only the two digests can. When either is missing the answer is
    ``None``, because a digest that was never taken cannot show a tree standing
    still.

    A digest is taken against HEAD, so once the checkout has moved the two are
    measured from different commits and are not comparable. That case is already
    reported as a moved checkout; claiming anything about the files on top of it
    would be claiming more than was measured.
    """
    if live is snapshot:
        return False, None
    if commit_moved is not False:
        return None, (
            "the uncommitted state cannot be compared across a checkout that moved or "
            "whose position is unknown — the two readings are measured from different "
            "commits"
        )

    before = snapshot.get("worktree_fingerprint")
    after = live.get("worktree_fingerprint")
    if before is None or after is None:
        missing = snapshot if before is None else live
        which = "when this process loaded" if before is None else "now"
        return None, (
            f"the uncommitted state of the tree could not be read {which}: "
            f"{missing.get('worktree_fingerprint_detail', 'no detail')}"
        )
    if before != after:
        return True, (
            f"the working tree at {snapshot.get('toplevel')} was edited after this "
            f"process loaded its code — commit {snapshot.get('commit_short')} is "
            "unchanged, so the edit is invisible to the commit id, and the modules "
            "answering here are the ones read before it"
        )
    return False, None


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
    *,
    live: dict[str, Any] | None = None,
    moved: bool | None = False,
    movement_detail: str | None = None,
    worktree_edited: bool | None = False,
    worktree_detail: str | None = None,
) -> dict[str, Any]:
    """The verdict on *git*, the position of the code this process actually loaded.

    When the checkout has not moved, the live reading measures that same commit
    against a fresher view of the remote, so it is the better source for how far
    behind the loaded code is. When it has moved, the live reading is about some
    other commit and only the snapshot speaks for what is running.

    Uncommitted changes present when the snapshot was taken make the verdict
    ``unknown`` rather than ``ok``. Not because something is known to be wrong —
    the edits may be exactly what the operator intended to run — but because the
    commit id, which is the whole of what this surface can report, is then not a
    description of the loaded code. Saying ``ok`` there would be answering a
    question ("does the reported identity match what is running?") that the
    reading cannot reach.
    """
    reasons: list[str] = []
    unknowns: list[str] = []

    if distribution_version is not None and distribution_version != version:
        reasons.append(
            f"the loaded package reports version {version} but the installed "
            f"distribution declares {distribution_version} — the import path is not "
            "serving the installed build"
        )

    if moved is True and movement_detail:
        reasons.append(movement_detail)
    elif moved is None and movement_detail:
        unknowns.append(movement_detail)

    if worktree_edited is True and worktree_detail:
        reasons.append(worktree_detail)
    elif worktree_edited is None and worktree_detail:
        unknowns.append(worktree_detail)

    if git.get("dirty") is True:
        unknowns.append(
            f"the tree this process loaded from had uncommitted changes at "
            f"{git.get('commit_short', 'the captured commit')}, so that commit does not "
            "describe the code being run — some of it was only ever on disk"
        )

    position = git
    if moved is False and live is not None and live.get("status") == "ok":
        position = live

    status = position.get("status")
    if status == "unknown":
        unknowns.append(f"git state unreadable: {position.get('detail', 'no detail')}")
    elif status == "ok":
        behind = position.get("behind")
        if behind is None:
            unknowns.append(
                "nothing to compare this checkout against: "
                f"{position.get('comparison_detail', 'no comparison ref')}"
            )

        elif behind > 0:
            reasons.append(
                f"the loaded checkout is {behind} commit(s) behind "
                f"{position['comparison_ref']} — it is serving code older than that ref"
            )

    if reasons:
        return {"status": "drift", "reasons": reasons, "unknown": unknowns}
    if unknowns:
        return {"status": "unknown", "reasons": [], "unknown": unknowns}
    return {"status": "ok", "reasons": [], "unknown": []}


def code_identity() -> dict[str, Any]:
    """The running process's own code identity, drift verdict included.

    Everything here describes the module objects this process actually imported,
    never the environment that was supposed to supply them. ``git`` is the
    position captured when this process first looked, which is what it loaded;
    ``git_live`` is the tree as it stands now. On the call that takes the
    snapshot the two are one reading and are reported as such.

    ``checkout_moved`` and ``worktree_edited`` are separate answers to separate
    questions — the checkout was moved to another commit, and the files were
    edited where they stand — because the operator who caused one did not do the
    other, and undoing them is not the same act.
    """
    from lionagi.version import __version__

    budget = _Budget(IDENTITY_BUDGET_SECONDS)
    package_path = loaded_package_path()

    already_snapshotted = _SNAPSHOT is not None
    snapshot = snapshot_git_position(budget)
    if already_snapshotted and package_path is not None:
        live = git_identity(Path(package_path), budget)
    else:
        live = snapshot

    moved, movement_detail = _checkout_movement(snapshot, live)
    worktree_edited, worktree_detail = _worktree_movement(snapshot, live, moved)
    distribution_version, distribution_detail = _distribution_version()
    verb_count, verb_detail = _verb_count()

    identity: dict[str, Any] = {
        "version": __version__,
        "package_path": package_path,
        "distribution_version": distribution_version,
        "verb_count": verb_count,
        "git": snapshot,
        "git_snapshot_taken_at": snapshot.get("observed_at"),
        "git_live": live,
        "checkout_moved": moved,
        "worktree_edited": worktree_edited,
        "drift": _drift(
            snapshot,
            __version__,
            distribution_version,
            live=live,
            moved=moved,
            movement_detail=movement_detail,
            worktree_edited=worktree_edited,
            worktree_detail=worktree_detail,
        ),
    }
    if movement_detail is not None:
        identity["checkout_moved_detail"] = movement_detail
    if worktree_detail is not None:
        identity["worktree_edited_detail"] = worktree_detail
    if distribution_detail is not None:
        identity["distribution_detail"] = distribution_detail
    if verb_detail is not None:
        identity["verb_count_detail"] = verb_detail
    return identity
