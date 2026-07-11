# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for sandbox.py: create, diff, commit, merge, discard."""

import os
import subprocess
from pathlib import Path

import pytest

import lionagi.tools.sandbox as sandbox_module
from lionagi.tools.sandbox import (
    SandboxSession,
    create_sandbox,
    sandbox_commit,
    sandbox_diff,
    sandbox_discard,
    sandbox_merge,
)

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


# ---------------------------------------------------------------------------
# SandboxSession dataclass
# ---------------------------------------------------------------------------


def test_sandbox_session_fields():
    s = SandboxSession(
        worktree_path="/tmp/wt",
        branch_name="sandbox-abc",
        base_branch="main",
        repo_root="/tmp/repo",
    )
    assert s.worktree_path == "/tmp/wt"
    assert s.branch_name == "sandbox-abc"
    assert s.base_branch == "main"
    assert s.repo_root == "/tmp/repo"
    assert s.is_active is True


def test_sandbox_session_is_active_false():
    s = SandboxSession(
        worktree_path="/tmp/wt",
        branch_name="b",
        base_branch="main",
        repo_root="/tmp/r",
        is_active=False,
    )
    assert s.is_active is False


def test_sandbox_session_base_sha_defaults_empty():
    """base_sha is additive — old call sites that don't pass it still work."""
    s = SandboxSession(
        worktree_path="/tmp/wt",
        branch_name="b",
        base_branch="main",
        repo_root="/tmp/r",
    )
    assert s.base_sha == ""


# ---------------------------------------------------------------------------
# create_sandbox
# ---------------------------------------------------------------------------


async def test_create_sandbox_creates_worktree(git_repo):
    session = await create_sandbox(str(git_repo))
    assert isinstance(session, SandboxSession)
    assert session.is_active is True
    assert os.path.isdir(session.worktree_path)
    assert session.branch_name.startswith("sandbox-")
    await sandbox_discard(session)


async def test_create_sandbox_custom_name(git_repo):
    session = await create_sandbox(str(git_repo), name="my-experiment")
    assert session.branch_name == "my-experiment"
    assert os.path.isdir(session.worktree_path)
    await sandbox_discard(session)


async def test_create_sandbox_explicit_fields(git_repo):
    session = await create_sandbox(str(git_repo), name="chk-fields")
    assert session.repo_root == str(git_repo)
    assert session.base_branch != ""
    worktree_path = Path(session.worktree_path)
    assert worktree_path.is_dir()
    assert worktree_path.parent.name == ".worktrees"
    await sandbox_discard(session)


async def test_create_sandbox_non_git_dir_raises(tmp_path):
    with pytest.raises(RuntimeError):
        await create_sandbox(str(tmp_path))


async def test_create_sandbox_records_base_sha(git_repo):
    """base_sha is captured at creation time and matches the base branch tip."""
    session = await create_sandbox(str(git_repo))
    expected_sha = subprocess.run(
        ["git", "rev-parse", session.base_branch],
        cwd=str(git_repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert session.base_sha == expected_sha
    assert len(session.base_sha) == 40
    await sandbox_discard(session)


async def test_create_sandbox_base_sha_matches_worktree_head(git_repo):
    session = await create_sandbox(str(git_repo))
    worktree_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=session.worktree_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert session.base_sha == worktree_head
    await sandbox_discard(session)


# ---------------------------------------------------------------------------
# sandbox_diff
# ---------------------------------------------------------------------------


async def test_sandbox_diff_empty_for_no_changes(git_repo):
    session = await create_sandbox(str(git_repo))
    diff = await sandbox_diff(session)
    assert diff["files_changed"] == [] and diff["patch"] == ""
    await sandbox_discard(session)


async def test_sandbox_diff_shows_new_file(git_repo):
    session = await create_sandbox(str(git_repo))
    (Path(session.worktree_path) / "new.txt").write_text("hello sandbox\n")
    diff = await sandbox_diff(session)
    assert "new.txt" in diff["files_changed"]
    assert "hello sandbox" in diff["patch"]
    await sandbox_discard(session)


async def test_sandbox_diff_stat_populated(git_repo):
    session = await create_sandbox(str(git_repo))
    (Path(session.worktree_path) / "stats.txt").write_text("content\n")
    diff = await sandbox_diff(session)
    assert "files_changed" in diff
    assert "stat" in diff
    assert "patch" in diff
    assert "patch_truncated" in diff
    assert "full_patch_chars" in diff
    assert diff["stat"] != ""
    assert isinstance(diff["patch_truncated"], bool)
    await sandbox_discard(session)


async def test_sandbox_diff_patch_truncation(git_repo):
    session = await create_sandbox(str(git_repo))
    (Path(session.worktree_path) / "big.txt").write_text("X" * 11000)
    diff = await sandbox_diff(session)
    assert diff["patch_truncated"] is True
    assert diff["full_patch_chars"] > 10000
    assert len(diff["patch"]) <= 10000
    await sandbox_discard(session)


async def test_sandbox_diff_untracked_filename_with_space(git_repo):
    """`git status --porcelain` line[3:] slicing is fine for a plain space in
    the middle of a filename too, but this pins the ls-files-based enumeration
    so a future regression back to porcelain parsing is caught here first."""
    session = await create_sandbox(str(git_repo))
    (Path(session.worktree_path) / "space name.txt").write_text("has a space\n")
    diff = await sandbox_diff(session)
    assert "space name.txt" in diff["files_changed"]
    assert "has a space" in diff["patch"]
    await sandbox_discard(session)


async def test_sandbox_diff_untracked_filename_with_quote(git_repo):
    """A filename containing a double quote is quoted/escaped by porcelain
    output (core.quotepath) — naive `?? ` line slicing mangles it and the
    file silently disappears from files_changed/patch."""
    session = await create_sandbox(str(git_repo))
    (Path(session.worktree_path) / 'quote"name.txt').write_text("has a quote\n")
    diff = await sandbox_diff(session)
    assert 'quote"name.txt' in diff["files_changed"]
    assert "has a quote" in diff["patch"]
    await sandbox_discard(session)


async def test_sandbox_diff_untracked_nested_directory(git_repo):
    """`?? dir/` reports the directory as one entry — files inside it must
    still show up individually in files_changed and in the patch."""
    session = await create_sandbox(str(git_repo))
    nested = Path(session.worktree_path) / "nested"
    nested.mkdir()
    (nested / "child.txt").write_text("nested content\n")
    diff = await sandbox_diff(session)
    assert "nested/child.txt" in diff["files_changed"]
    assert "nested content" in diff["patch"]
    await sandbox_discard(session)


async def test_sandbox_diff_untracked_binary_file(git_repo):
    session = await create_sandbox(str(git_repo))
    (Path(session.worktree_path) / "blob.bin").write_bytes(bytes(range(256)))
    diff = await sandbox_diff(session)
    assert "blob.bin" in diff["files_changed"]
    assert "Binary files" in diff["patch"] or "blob.bin" in diff["patch"]
    await sandbox_discard(session)


async def test_sandbox_diff_does_not_mutate_index_new_file(git_repo):
    """diff must be read-only — status before/after must be identical."""
    session = await create_sandbox(str(git_repo))
    wt = session.worktree_path
    (Path(wt) / "untouched.txt").write_text("content\n")

    before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=wt, capture_output=True, text=True, check=True
    ).stdout
    await sandbox_diff(session)
    after = subprocess.run(
        ["git", "status", "--porcelain"], cwd=wt, capture_output=True, text=True, check=True
    ).stdout

    assert before == after
    # untracked, never staged by the diff read
    assert "?? untouched.txt" in after
    await sandbox_discard(session)


async def test_sandbox_diff_does_not_mutate_index_modified_tracked_file(git_repo):
    session = await create_sandbox(str(git_repo))
    wt = session.worktree_path
    (Path(wt) / "README.md").write_text("changed\n")

    before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=wt, capture_output=True, text=True, check=True
    ).stdout
    await sandbox_diff(session)
    after = subprocess.run(
        ["git", "status", "--porcelain"], cwd=wt, capture_output=True, text=True, check=True
    ).stdout

    assert before == after
    await sandbox_discard(session)


# ---------------------------------------------------------------------------
# sandbox_commit
# ---------------------------------------------------------------------------


async def test_sandbox_commit_records_change(git_repo):
    session = await create_sandbox(str(git_repo))
    (Path(session.worktree_path) / "work.py").write_text("x = 1\n")
    result = await sandbox_commit(session, "add work.py")
    assert result["success"] is True
    assert "commit" in result
    assert result["message"] == "add work.py"
    await sandbox_discard(session)


async def test_sandbox_commit_nothing_to_commit(git_repo):
    session = await create_sandbox(str(git_repo))
    result = await sandbox_commit(session, "empty commit")
    assert result["success"] is True
    assert "Nothing to commit" in result.get("message", "")
    await sandbox_discard(session)


# ---------------------------------------------------------------------------
# sandbox_merge
# ---------------------------------------------------------------------------


async def test_sandbox_merge_applies_changes(git_repo):
    session = await create_sandbox(str(git_repo))
    (Path(session.worktree_path) / "merged.txt").write_text("from sandbox\n")
    result = await sandbox_merge(session, allow_protected=True)
    assert result["success"] is True and result["merged"] is True
    assert (git_repo / "merged.txt").read_text() == "from sandbox\n"


async def test_sandbox_merge_cleans_up_worktree(git_repo):
    session = await create_sandbox(str(git_repo))
    worktree_path = session.worktree_path
    await sandbox_merge(session, allow_protected=True)
    assert not os.path.exists(worktree_path)


async def test_sandbox_merge_sets_is_active_false(git_repo):
    session = await create_sandbox(str(git_repo))
    assert session.is_active is True
    await sandbox_merge(session, allow_protected=True)
    assert session.is_active is False


async def test_sandbox_merge_refuses_protected_base_without_override(git_repo):
    """git init defaults to a protected branch name (master); merge must refuse it."""
    session = await create_sandbox(str(git_repo))
    assert session.base_branch in ("main", "master")
    (Path(session.worktree_path) / "merged.txt").write_text("from sandbox\n")

    result = await sandbox_merge(session)

    assert result["success"] is False
    assert "protected" in result["error"]
    assert not (git_repo / "merged.txt").exists()
    assert session.is_active is True
    assert os.path.isdir(session.worktree_path)
    await sandbox_discard(session)


async def test_sandbox_merge_allows_protected_base_with_override(git_repo):
    session = await create_sandbox(str(git_repo))
    (Path(session.worktree_path) / "merged.txt").write_text("from sandbox\n")

    result = await sandbox_merge(session, allow_protected=True)

    assert result["success"] is True
    assert (git_repo / "merged.txt").read_text() == "from sandbox\n"


async def test_sandbox_merge_refuses_when_repo_root_on_different_branch(git_repo):
    """A non-protected feature base still refuses if repo_root moved off it."""
    subprocess.run(
        ["git", "checkout", "-b", "feature-base"],
        cwd=str(git_repo),
        capture_output=True,
        check=True,
    )
    session = await create_sandbox(str(git_repo), base_branch="feature-base")
    assert session.base_branch == "feature-base"
    (Path(session.worktree_path) / "merged.txt").write_text("from sandbox\n")

    # repo_root moves off the recorded base branch before merge is attempted.
    subprocess.run(
        ["git", "checkout", "master"],
        cwd=str(git_repo),
        capture_output=True,
        check=True,
    )

    result = await sandbox_merge(session)

    assert result["success"] is False
    assert "feature-base" in result["error"]
    assert not (git_repo / "merged.txt").exists()
    assert session.is_active is True
    await sandbox_discard(session)


async def test_sandbox_merge_succeeds_on_unprotected_matching_base(git_repo):
    """Merging into a normal (non-protected) checked-out base needs no override."""
    subprocess.run(
        ["git", "checkout", "-b", "feature-base"],
        cwd=str(git_repo),
        capture_output=True,
        check=True,
    )
    session = await create_sandbox(str(git_repo), base_branch="feature-base")
    (Path(session.worktree_path) / "merged.txt").write_text("from sandbox\n")

    result = await sandbox_merge(session)

    assert result["success"] is True and result["merged"] is True
    assert (git_repo / "merged.txt").read_text() == "from sandbox\n"


# ---------------------------------------------------------------------------
# sandbox_discard
# ---------------------------------------------------------------------------


async def test_sandbox_discard_sets_is_active_false(git_repo):
    session = await create_sandbox(str(git_repo))
    assert session.is_active is True
    await sandbox_discard(session)
    assert session.is_active is False


async def test_sandbox_discard_removes_worktree(git_repo):
    session = await create_sandbox(str(git_repo))
    worktree_path = session.worktree_path
    result = await sandbox_discard(session)
    assert result["worktree_removed"] is True
    assert not os.path.exists(worktree_path)


async def test_sandbox_discard_deletes_branch(git_repo):
    session = await create_sandbox(str(git_repo))
    branch_name = session.branch_name
    await sandbox_discard(session)
    out = subprocess.run(["git", "branch"], cwd=str(git_repo), capture_output=True, text=True)
    assert branch_name not in out.stdout


async def test_sandbox_discard_changes_not_in_base(git_repo):
    session = await create_sandbox(str(git_repo))
    (Path(session.worktree_path) / "ephemeral.txt").write_text("gone\n")
    await sandbox_discard(session)
    assert not (git_repo / "ephemeral.txt").exists()


async def test_sandbox_discard_leaves_is_active_true_on_failure(git_repo):
    """If worktree removal fails, is_active must stay True — no false-clean signal.

    A locked worktree refuses plain ``--force`` removal (git requires the
    force flag twice for a locked worktree), which is used here to force a
    real git-level failure without hand-rolling a fake git binary.
    """
    session = await create_sandbox(str(git_repo))
    subprocess.run(
        ["git", "worktree", "lock", session.worktree_path],
        cwd=str(git_repo),
        capture_output=True,
        check=True,
    )

    result = await sandbox_discard(session)

    assert result["worktree_removed"] is False
    assert session.is_active is True
    assert os.path.isdir(session.worktree_path)

    # Manual cleanup: unlock then remove for real.
    subprocess.run(
        ["git", "worktree", "unlock", session.worktree_path],
        cwd=str(git_repo),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "worktree", "remove", "--force", session.worktree_path],
        cwd=str(git_repo),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "branch", "-D", session.branch_name],
        cwd=str(git_repo),
        capture_output=True,
        check=True,
    )


# ---------------------------------------------------------------------------
# Full lifecycle
# ---------------------------------------------------------------------------


async def test_sandbox_full_lifecycle(git_repo):
    # create
    session = await create_sandbox(str(git_repo), name="lifecycle-test")
    assert isinstance(session, SandboxSession)
    assert os.path.isdir(session.worktree_path)

    # edit
    wt = Path(session.worktree_path)
    (wt / "feature.py").write_text("def hello(): return 'world'\n")

    # diff
    diff = await sandbox_diff(session)
    assert "feature.py" in diff["files_changed"]
    assert "hello" in diff["patch"]
    assert diff["stat"] != ""

    # commit
    commit_result = await sandbox_commit(session, "add feature.py")
    assert commit_result["success"] is True
    assert "commit" in commit_result
    sha = commit_result["commit"]
    assert len(sha) == 40

    # merge back into base
    merge_result = await sandbox_merge(session, allow_protected=True)
    assert merge_result["success"] is True
    assert merge_result["merged"] is True

    # verify file landed in the base repo
    assert (git_repo / "feature.py").read_text() == "def hello(): return 'world'\n"

    # verify worktree cleaned up
    assert not os.path.exists(session.worktree_path)


# ---------------------------------------------------------------------------
# Detached HEAD
# ---------------------------------------------------------------------------


async def test_create_sandbox_detached_head_raises(git_repo):
    """A defaulted base_branch must never resolve to the literal "HEAD" —
    that name doesn't refer to any branch and can't be a merge target."""
    subprocess.run(
        ["git", "checkout", "--detach", "master"],
        cwd=str(git_repo),
        capture_output=True,
        check=True,
    )
    with pytest.raises(RuntimeError, match="detached"):
        await create_sandbox(str(git_repo))


async def test_sandbox_merge_refuses_detached_head_target(git_repo):
    """Even if a session ends up recording base_branch="HEAD" (e.g. passed
    explicitly), merge must refuse when repo_root is actually detached at
    merge time rather than comparing HEAD == HEAD and merging blind."""
    subprocess.run(
        ["git", "checkout", "--detach", "master"],
        cwd=str(git_repo),
        capture_output=True,
        check=True,
    )
    session = await create_sandbox(str(git_repo), base_branch="HEAD")
    assert session.base_branch == "HEAD"
    (Path(session.worktree_path) / "merged.txt").write_text("from sandbox\n")

    result = await sandbox_merge(session, allow_protected=True)

    assert result["success"] is False
    assert "detached" in result["error"].lower()
    assert not (git_repo / "merged.txt").exists()
    assert session.is_active is True
    await sandbox_discard(session)


# ---------------------------------------------------------------------------
# Cleanup: branch-deletion-only failure
# ---------------------------------------------------------------------------


async def test_sandbox_discard_branch_delete_failure_keeps_session(git_repo):
    """Worktree removal can succeed while branch deletion still fails (the
    branch gets checked out elsewhere the instant it frees up) — is_active
    must stay True and branch_deleted must be reported False, mirroring the
    existing worktree-removal-failure case."""
    session = await create_sandbox(str(git_repo))
    branch = session.branch_name
    other_wt = git_repo / "other-wt"

    real_run_git = sandbox_module._run_git

    def fake_run_git(args, cwd=None):
        if args[:2] == ["branch", "-D"] and args[-1] == branch:
            # Simulate a concurrent checkout grabbing the branch the moment
            # the worktree that held it is removed, before deletion runs.
            subprocess.run(
                ["git", "worktree", "add", str(other_wt), branch],
                cwd=str(git_repo),
                capture_output=True,
                check=True,
            )
        return real_run_git(args, cwd)

    original = sandbox_module._run_git
    sandbox_module._run_git = fake_run_git
    try:
        result = await sandbox_discard(session)
    finally:
        sandbox_module._run_git = original

    assert result["worktree_removed"] is True
    assert result["branch_deleted"] is False
    assert session.is_active is True

    subprocess.run(
        ["git", "worktree", "remove", str(other_wt), "--force"],
        cwd=str(git_repo),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "branch", "-D", branch], cwd=str(git_repo), capture_output=True, check=True
    )
