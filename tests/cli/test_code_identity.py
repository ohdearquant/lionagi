# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The running code's self-report, and the checks that call it out when it drifts."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lionagi.cli import _code_identity, doctor
from lionagi.cli._code_identity import code_identity, git_identity


class _Args:
    """Stand-in for the argparse namespace `run_doctor` reads."""

    json = False


def _git(tree: Path, *argv: str) -> str:
    completed = subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=test",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(tree),
            *argv,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _commit(tree: Path, name: str) -> None:
    (tree / name).write_text(name)
    _git(tree, "add", name)
    _git(tree, "commit", "-m", name)


@pytest.fixture
def checkout_behind(tmp_path: Path) -> Path:
    """A working checkout whose HEAD is one commit behind its own upstream."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "main", ".")

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main", ".")
    _commit(work, "first")
    _commit(work, "second")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-u", "origin", "main")
    _git(work, "reset", "--hard", "HEAD~1")
    return work


def test_checkout_behind_upstream_reads_as_drift(checkout_behind: Path) -> None:
    git = git_identity(checkout_behind)
    assert git["status"] == "ok"
    assert git["comparison_ref"] == "origin/main"
    assert git["comparison_ref_source"] == "upstream"
    assert git["behind"] == 1
    assert git["ahead"] == 0


def test_detached_checkout_falls_back_to_the_remote_default_branch(
    checkout_behind: Path,
) -> None:
    """A pinned deployment has no upstream — the remote's HEAD still answers for it."""
    _git(checkout_behind, "remote", "set-head", "origin", "-a")
    _git(checkout_behind, "checkout", "--detach", "HEAD")

    git = git_identity(checkout_behind)
    assert git["status"] == "ok"
    assert git["detached"] is True
    assert git["branch"] is None
    assert git["comparison_ref_source"] == "remote_head"
    assert git["behind"] == 1


def test_up_to_date_checkout_is_not_behind(checkout_behind: Path) -> None:
    """The guard fires on drift, not on every checkout that has a remote."""
    _git(checkout_behind, "merge", "--ff-only", "origin/main")

    git = git_identity(checkout_behind)
    assert git["status"] == "ok"
    assert git["behind"] == 0


def test_directory_outside_any_checkout_says_so_plainly(tmp_path: Path) -> None:
    git = git_identity(tmp_path)
    assert git["status"] == "not_a_git_checkout"
    assert str(tmp_path) in git["detail"]


def test_unreadable_head_is_unknown_not_ok(tmp_path: Path) -> None:
    """An initialized tree with no commits: git answers, HEAD does not resolve."""
    _git(tmp_path, "init", "-b", "main", ".")

    git = git_identity(tmp_path)
    assert git["status"] == "unknown"
    assert "HEAD" in git["detail"]


def test_missing_git_binary_is_unknown_not_a_missing_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _no_git(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(_code_identity.subprocess, "run", _no_git)

    git = git_identity(tmp_path)
    assert git["status"] == "unknown"
    assert "FileNotFoundError" in git["detail"]


def test_a_git_call_that_never_ran_does_not_read_as_a_missing_upstream(
    checkout_behind: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lookup that could not run is not evidence the tree has no upstream.

    origin/HEAD resolves here, so falling through to it would produce a confident
    `ok` built on a question that was never actually asked.
    """
    _git(checkout_behind, "remote", "set-head", "origin", "-a")
    real_run = subprocess.run

    def _upstream_probe_times_out(argv: list[str], **kwargs: object):
        if "@{upstream}" in argv:
            raise subprocess.TimeoutExpired(argv, 5.0)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(_code_identity.subprocess, "run", _upstream_probe_times_out)

    git = git_identity(checkout_behind)
    assert git["status"] == "unknown"
    assert "TimeoutExpired" in git["detail"]
    assert git.get("comparison_ref_source") != "remote_head"


def test_a_spent_allowance_is_unknown_not_ok(checkout_behind: Path) -> None:
    """The whole identity gets one allowance; running out of it is not a pass."""
    git = git_identity(checkout_behind, _code_identity._Budget(0.0))
    assert git["status"] == "unknown"
    assert "allowance" in git["detail"]


def test_the_remote_head_fallback_carries_its_own_staleness(checkout_behind: Path) -> None:
    """origin/HEAD is local and can name a branch the remote no longer defaults to."""
    _git(checkout_behind, "remote", "set-head", "origin", "-a")
    _git(checkout_behind, "checkout", "--detach", "HEAD")

    git = git_identity(checkout_behind)
    assert git["comparison_ref_source"] == "remote_head"
    assert "local symbolic ref" in git["comparison_ref_caveat"]


def test_a_reading_says_when_it_was_taken(checkout_behind: Path) -> None:
    assert git_identity(checkout_behind)["observed_at"]


def test_no_comparison_ref_is_unknown_not_ok(tmp_path: Path) -> None:
    """A checkout with no remote cannot be measured, so it is not declared current."""
    _git(tmp_path, "init", "-b", "main", ".")
    _commit(tmp_path, "only")

    git = git_identity(tmp_path)
    assert git["status"] == "ok"
    assert git["comparison_ref"] is None
    assert git["behind"] is None

    drift = _code_identity._drift(git, "1.0.0", "1.0.0")
    assert drift["status"] == "unknown"
    assert drift["unknown"]


# the drift verdict


def test_version_mismatch_against_installed_distribution_is_drift() -> None:
    git = {"status": "not_a_git_checkout", "detail": "wheel install"}
    drift = _code_identity._drift(git, "0.1.0", "9.9.9")
    assert drift["status"] == "drift"
    assert any("9.9.9" in reason for reason in drift["reasons"])


def test_wheel_install_with_matching_version_is_ok() -> None:
    git = {"status": "not_a_git_checkout", "detail": "wheel install"}
    assert _code_identity._drift(git, "0.1.0", "0.1.0")["status"] == "ok"


def test_behind_outranks_unknown_in_the_verdict() -> None:
    git = {"status": "ok", "behind": 24, "comparison_ref": "origin/main"}
    drift = _code_identity._drift(git, "0.1.0", None)
    assert drift["status"] == "drift"
    assert any("24 commit(s) behind" in reason for reason in drift["reasons"])


# the snapshot, and the tree moving underneath it


@pytest.fixture
def loaded_from(checkout_behind: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pretend this process imported itself from *checkout_behind*, snapshot unset."""
    monkeypatch.setattr(_code_identity, "_SNAPSHOT", None)
    monkeypatch.setattr(_code_identity, "loaded_package_path", lambda: str(checkout_behind))
    return checkout_behind


def test_the_first_call_takes_the_snapshot_and_it_is_the_current_reading(
    loaded_from: Path,
) -> None:
    identity = code_identity()
    assert identity["git_snapshot_taken_at"]
    assert identity["checkout_moved"] is False
    assert identity["git"]["commit"] == identity["git_live"]["commit"]


def test_a_checkout_that_moves_under_the_process_is_reported_as_moved(
    loaded_from: Path,
) -> None:
    """The loaded code is the snapshot; the tree's new position is a separate fact."""
    loaded_commit = code_identity()["git"]["commit"]

    _git(loaded_from, "merge", "--ff-only", "origin/main")

    after = code_identity()
    assert after["git"]["commit"] == loaded_commit
    assert after["git_live"]["commit"] != loaded_commit
    assert after["checkout_moved"] is True
    assert "moved from" in after["checkout_moved_detail"]
    assert after["drift"]["status"] == "drift"
    assert any("moved from" in reason for reason in after["drift"]["reasons"])


def test_the_snapshot_is_read_once_not_once_per_call(loaded_from: Path) -> None:
    first = code_identity()["git_snapshot_taken_at"]
    _git(loaded_from, "merge", "--ff-only", "origin/main")
    assert code_identity()["git_snapshot_taken_at"] == first


def test_the_server_snapshots_its_position_before_it_starts_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lionagi.mcp import server

    monkeypatch.setattr(_code_identity, "_SNAPSHOT", None)
    seen: dict[str, object] = {}
    monkeypatch.setattr(server.mcp, "run", lambda: seen.update(snapshot=_code_identity._SNAPSHOT))

    server.main()
    assert seen["snapshot"] is not None


def test_an_unreadable_live_position_is_unknown_movement_not_no_movement() -> None:
    moved, detail = _code_identity._checkout_movement(
        {"status": "ok", "commit": "a" * 40},
        {"status": "unknown", "detail": "git went away"},
    )
    assert moved is None
    assert "cannot be read" in detail


def test_the_live_count_answers_when_the_checkout_has_not_moved() -> None:
    """Same commit, fresher view of the remote: the newer measurement is the true one."""
    drift = _code_identity._drift(
        {"status": "ok", "behind": 0, "comparison_ref": "origin/main"},
        "0.1.0",
        None,
        live={"status": "ok", "behind": 3, "comparison_ref": "origin/main"},
        moved=False,
    )
    assert drift["status"] == "drift"
    assert any("3 commit(s) behind" in reason for reason in drift["reasons"])


def test_the_snapshot_answers_when_the_checkout_has_moved() -> None:
    """Once the tree moves, the live count is about a commit that is not running."""
    drift = _code_identity._drift(
        {"status": "ok", "behind": 24, "comparison_ref": "origin/main"},
        "0.1.0",
        None,
        live={"status": "ok", "behind": 0, "comparison_ref": "origin/main"},
        moved=True,
        movement_detail="it moved",
    )
    assert drift["status"] == "drift"
    assert "it moved" in drift["reasons"]
    assert any("24 commit(s) behind" in reason for reason in drift["reasons"])


# the tree moving without the commit moving


def test_a_dirty_tree_is_fingerprinted_and_a_clean_one_is_too(checkout_behind: Path) -> None:
    """The digest is a value on every readable tree, so it can always be compared."""
    clean = git_identity(checkout_behind)["worktree_fingerprint"]
    assert clean

    (checkout_behind / "first").write_text("edited")
    dirty = git_identity(checkout_behind)["worktree_fingerprint"]
    assert dirty
    assert dirty != clean


def test_editing_an_already_modified_file_changes_the_fingerprint(
    checkout_behind: Path,
) -> None:
    """The status listing alone would not move here — the path and its code are the same."""
    (checkout_behind / "first").write_text("one")
    before = git_identity(checkout_behind)
    (checkout_behind / "first").write_text("two")
    after = git_identity(checkout_behind)

    assert before["dirty"] is True
    assert after["dirty"] is True
    assert before["worktree_fingerprint"] != after["worktree_fingerprint"]


def test_a_fingerprint_that_could_not_be_taken_is_none_with_a_reason(
    checkout_behind: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (checkout_behind / "first").write_text("edited")
    real_run = subprocess.run

    def _diff_fails(argv: list[str], **kwargs: object):
        if argv[3:5] == ["diff", "HEAD"]:
            raise subprocess.TimeoutExpired(argv, 5.0)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(_code_identity.subprocess, "run", _diff_fails)

    git = git_identity(checkout_behind)
    assert git["worktree_fingerprint"] is None
    assert "TimeoutExpired" in git["worktree_fingerprint_detail"]


def test_an_edit_under_the_process_is_reported_though_the_commit_never_moved(
    loaded_from: Path,
) -> None:
    """The case that used to read as a clean `ok`: same commit, different files."""
    before = code_identity()
    assert before["worktree_edited"] is False

    (loaded_from / "first").write_text("edited under the running process")

    after = code_identity()
    assert after["git"]["commit"] == after["git_live"]["commit"]
    assert after["checkout_moved"] is False
    assert after["worktree_edited"] is True
    assert "was edited after this process loaded" in after["worktree_edited_detail"]
    assert after["drift"]["status"] == "drift"
    assert any("was edited after this process loaded" in r for r in after["drift"]["reasons"])


def test_the_two_kinds_of_movement_are_reported_separately(loaded_from: Path) -> None:
    """Moving the checkout is one operator action; editing files is another."""
    code_identity()
    _git(loaded_from, "merge", "--ff-only", "origin/main")

    after = code_identity()
    assert after["checkout_moved"] is True
    assert after["worktree_edited"] is None, "not comparable across a commit change"
    assert "different commits" in after["worktree_edited_detail"]
    assert any("different commits" in u for u in after["drift"]["unknown"])


def test_a_missing_fingerprint_is_unknown_movement_not_a_still_tree() -> None:
    edited, detail = _code_identity._worktree_movement(
        {"status": "ok", "commit": "a" * 40, "worktree_fingerprint": None},
        {"status": "ok", "commit": "a" * 40, "worktree_fingerprint": "beef"},
        False,
    )
    assert edited is None
    assert "when this process loaded" in detail


def test_rewriting_an_untracked_file_that_already_existed_is_an_edit(
    loaded_from: Path,
) -> None:
    """Neither the status listing nor the diff carries an untracked file's contents."""
    scratch = loaded_from / "scratch.txt"
    scratch.write_text("present before the process started")

    before = code_identity()
    assert before["git"]["dirty"] is True
    assert before["worktree_edited"] is False

    scratch.write_text("rewritten while the process was running")

    after = code_identity()
    assert after["checkout_moved"] is False
    assert after["worktree_edited"] is True
    assert "was edited after this process loaded" in after["worktree_edited_detail"]


def test_an_edit_inside_an_untracked_directory_is_an_edit(loaded_from: Path) -> None:
    """git collapses an untracked directory to one line; the listing alone cannot move."""
    package = loaded_from / "plugins"
    package.mkdir()
    (package / "handler.py").write_text("def run(): return 1\n")

    before = code_identity()
    assert before["worktree_edited"] is False

    (package / "handler.py").write_text("def run(): return 2\n")

    after = code_identity()
    assert after["checkout_moved"] is False
    assert after["worktree_edited"] is True


def test_a_new_file_inside_an_untracked_directory_is_an_edit(loaded_from: Path) -> None:
    package = loaded_from / "plugins"
    package.mkdir()
    (package / "handler.py").write_text("def run(): return 1\n")

    assert code_identity()["worktree_edited"] is False

    (package / "extra.py").write_text("def also(): return 3\n")

    assert code_identity()["worktree_edited"] is True


def test_an_ignored_path_is_not_enumerated(checkout_behind: Path) -> None:
    """The listing stays bounded: build output and virtualenvs are still excluded."""
    (checkout_behind / ".gitignore").write_text("build/\n")
    _git(checkout_behind, "add", ".gitignore")
    _git(checkout_behind, "commit", "-m", "ignore build")
    build = checkout_behind / "build"
    build.mkdir()
    (build / "artifact.bin").write_bytes(b"\x00" * 32)

    before = git_identity(checkout_behind)
    assert before["dirty"] is False

    (build / "artifact.bin").write_bytes(b"\x01" * 64)

    assert git_identity(checkout_behind)["worktree_fingerprint"] == before["worktree_fingerprint"]


def test_a_path_that_cannot_be_stat_d_is_none_with_a_reason(
    checkout_behind: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable path is not evidence that the tree stood still."""
    (checkout_behind / "scratch.txt").write_text("untracked")
    real_lstat = Path.lstat

    def _refuse(self: Path):
        if self.name == "scratch.txt":
            raise PermissionError(13, "Permission denied")
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", _refuse)

    git = git_identity(checkout_behind)
    assert git["worktree_fingerprint"] is None
    assert "scratch.txt" in git["worktree_fingerprint_detail"]
    assert "PermissionError" in git["worktree_fingerprint_detail"]


def test_a_stat_failure_makes_the_edit_answer_unknown_not_false(
    loaded_from: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (loaded_from / "scratch.txt").write_text("untracked")
    code_identity()

    real_lstat = Path.lstat

    def _refuse(self: Path):
        if self.name == "scratch.txt":
            raise PermissionError(13, "Permission denied")
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", _refuse)

    after = code_identity()
    assert after["worktree_edited"] is None
    assert "scratch.txt" in after["worktree_edited_detail"]
    assert any("scratch.txt" in u for u in after["drift"]["unknown"])


def test_a_deleted_path_is_measured_rather_than_read_as_unreadable(
    loaded_from: Path,
) -> None:
    """A path git lists as deleted is absent by definition — absence is the reading."""
    (loaded_from / "first").unlink()

    before = code_identity()
    assert before["git"]["dirty"] is True
    assert before["worktree_edited"] is False

    after = code_identity()
    assert after["worktree_edited"] is False, "an absent path must not read as unknown"


def test_a_renamed_path_does_not_stat_the_name_it_came_from(checkout_behind: Path) -> None:
    """The origin path of a rename no longer exists; only the destination is on disk."""
    _git(checkout_behind, "mv", "first", "renamed")

    git = git_identity(checkout_behind)
    assert git["worktree_fingerprint"] is not None
    assert "worktree_fingerprint_detail" not in git


def test_a_permission_change_on_an_untracked_file_is_an_edit(loaded_from: Path) -> None:
    """chmod rewrites no bytes, so nothing but the mode moves — and it still counts.

    Making a file executable changes what an operator can run out of the tree
    while leaving the size, the modification time and the status listing exactly
    as they were.
    """
    scratch = loaded_from / "scratch.txt"
    scratch.write_text("plain")
    scratch.chmod(0o644)
    was = scratch.lstat()

    before = code_identity()
    assert before["git"]["dirty"] is True
    assert before["worktree_edited"] is False

    scratch.chmod(0o755)

    now = scratch.lstat()
    assert (now.st_size, now.st_mtime_ns) == (was.st_size, was.st_mtime_ns), (
        "the premise of this test is that size and mtime cannot see a chmod"
    )

    after = code_identity()
    assert after["checkout_moved"] is False
    assert after["worktree_edited"] is True
    assert "was edited after this process loaded" in after["worktree_edited_detail"]


class _SpentAfter:
    """A budget generous for a fixed number of readings, then exhausted.

    Deterministic where a wall-clock deadline is not: exhaustion lands on a chosen
    iteration of the per-path loop rather than wherever the machine happens to be
    slow that day.
    """

    total = 6.0

    def __init__(self, readings: int) -> None:
        self._left = readings

    def remaining(self) -> float:
        if self._left > 0:
            self._left -= 1
            return self.total
        return -1.0


def test_an_allowance_spent_inside_the_per_path_loop_yields_no_digest(
    checkout_behind: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop is inside the allowance, and running out of it is not a partial answer.

    A digest over some of the paths compares unequal to one over all of them, so a
    loop that stopped at the deadline and returned what it had would report an
    edit to a tree nobody touched. This pins the difference that truncation would
    manufacture, then shows the code declines to manufacture it.
    """
    for name in ("one.txt", "two.txt", "three.txt"):
        (checkout_behind / name).write_text(name)

    top = Path(_git(checkout_behind, "rev-parse", "--show-toplevel"))
    porcelain = _git(checkout_behind, "status", "--porcelain", "-z", "--untracked-files=all")
    paths = _code_identity._status_paths(porcelain)
    assert len(paths) == 3

    full, detail = _code_identity._worktree_fingerprint(
        checkout_behind, top, porcelain, _code_identity._Budget(60.0)
    )
    assert detail is None
    assert full

    with monkeypatch.context() as stop_after_one:
        stop_after_one.setattr(_code_identity, "_status_paths", lambda _: paths[:1])
        truncated, _ = _code_identity._worktree_fingerprint(
            checkout_behind, top, porcelain, _code_identity._Budget(60.0)
        )
    assert truncated != full, "truncation invents a difference in a tree that did not change"

    spent, reason = _code_identity._worktree_fingerprint(
        checkout_behind, top, porcelain, _SpentAfter(2)
    )
    assert spent is None, "a spent allowance must not return the digest built so far"
    assert spent != truncated
    assert "allowance" in reason
    assert f"measuring 1 of the {len(paths)} paths" in reason

    edited, movement = _code_identity._worktree_movement(
        {"status": "ok", "commit": "a" * 40, "worktree_fingerprint": full},
        {
            "status": "ok",
            "commit": "a" * 40,
            "worktree_fingerprint": spent,
            "worktree_fingerprint_detail": reason,
        },
        False,
    )
    assert edited is None, "a timeout is 'cannot tell', never 'the tree was edited'"
    assert "allowance" in movement


def test_a_dirty_snapshot_is_unknown_not_ok() -> None:
    """A commit id cannot describe a tree with changes that are not in it."""
    git = {
        "status": "ok",
        "behind": 0,
        "comparison_ref": "origin/main",
        "commit_short": "abc123def456",
        "dirty": True,
    }
    drift = _code_identity._drift(git, "0.1.0", "0.1.0")
    assert drift["status"] == "unknown"
    assert any("uncommitted changes" in u for u in drift["unknown"])


def test_a_clean_snapshot_at_its_ref_is_still_ok() -> None:
    """The dirty rule must not swallow the one state that is genuinely fine."""
    git = {"status": "ok", "behind": 0, "comparison_ref": "origin/main", "dirty": False}
    assert _code_identity._drift(git, "0.1.0", "0.1.0")["status"] == "ok"


def test_a_dirty_tree_read_end_to_end_does_not_claim_ok(loaded_from: Path) -> None:
    """Before anything moves at all: dirty at snapshot time is already not `ok`."""
    (loaded_from / "first").write_text("uncommitted when the process started")

    identity = code_identity()
    assert identity["git"]["dirty"] is True
    assert identity["checkout_moved"] is False
    assert identity["drift"]["status"] != "ok"
    assert any("uncommitted changes" in u for u in identity["drift"]["unknown"])


def test_doctor_says_the_tree_was_dirty_beside_the_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _code_identity,
        "code_identity",
        lambda: _identity(
            "unknown",
            git={
                "status": "ok",
                "commit_short": "abc123def456",
                "branch": None,
                "detached": True,
                "dirty": True,
            },
        ),
    )
    assert (
        "abc123def456 (detached) with uncommitted changes"
        in (doctor._check_code_identity()["detail"])
    )


def test_code_identity_reports_this_process() -> None:
    identity = code_identity()
    assert identity["version"]
    assert identity["package_path"].endswith("/lionagi")
    assert Path(identity["package_path"]).is_dir()
    assert identity["verb_count"] > 0
    assert identity["git"]["status"] in ("ok", "not_a_git_checkout", "unknown")
    assert identity["drift"]["status"] in ("ok", "drift", "unknown")
    assert identity["git_snapshot_taken_at"]
    assert identity["checkout_moved"] in (True, False, None)
    assert identity["git_live"]["status"] in ("ok", "not_a_git_checkout", "unknown")


# the doctor check


def _identity(drift_status: str, **overrides: object) -> dict[str, object]:
    identity: dict[str, object] = {
        "version": "0.1.0",
        "package_path": "/somewhere/lionagi",
        "distribution_version": "0.1.0",
        "verb_count": 40,
        "git": {
            "status": "ok",
            "commit_short": "abc123def456",
            "branch": None,
            "detached": True,
        },
        "drift": {
            "status": drift_status,
            "reasons": ["24 commit(s) behind origin/main"] if drift_status == "drift" else [],
            "unknown": ["git state unreadable"] if drift_status == "unknown" else [],
        },
    }
    identity.update(overrides)
    return identity


def test_doctor_fails_on_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_code_identity, "code_identity", lambda: _identity("drift"))
    result = doctor._check_code_identity()
    assert result["status"] == "fail"
    assert "24 commit(s) behind origin/main" in result["detail"]
    assert "40 verbs" in result["detail"]


def test_doctor_reports_unknown_rather_than_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_code_identity, "code_identity", lambda: _identity("unknown"))
    result = doctor._check_code_identity()
    assert result["status"] == "unknown"
    assert "git state unreadable" in result["detail"]


def test_doctor_ok_when_identity_is_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_code_identity, "code_identity", lambda: _identity("ok"))
    result = doctor._check_code_identity()
    assert result["status"] == "ok"
    assert "abc123def456 (detached)" in result["detail"]


def test_doctor_quotes_when_the_position_was_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A commit with no time attached reads as current; this one says what it is."""
    monkeypatch.setattr(
        _code_identity,
        "code_identity",
        lambda: _identity("ok", git_snapshot_taken_at="2026-07-26T00:00:00+00:00"),
    )
    detail = doctor._check_code_identity()["detail"]
    assert "as read at 2026-07-26T00:00:00+00:00" in detail


def test_doctor_check_that_cannot_run_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> dict[str, object]:
        raise RuntimeError("no")

    monkeypatch.setattr(_code_identity, "code_identity", _boom)
    assert doctor._check_code_identity()["status"] == "unknown"


def test_run_doctor_exits_nonzero_on_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor,
        "collect_checks",
        lambda: {"code_identity": {"status": "unknown", "detail": "cannot tell"}},
    )
    assert doctor.run_doctor(_Args()) == 1


# the surfaces a client reads


def test_handshake_carries_code_identity() -> None:
    from lionagi.cli.machine import handshake_data

    identity = handshake_data()["code_identity"]
    assert identity["package_path"]
    assert identity["verb_count"] > 0
    assert "status" in identity["drift"]


def test_server_info_carries_code_identity() -> None:
    from lionagi.mcp.dispatch import _server_info

    info = _server_info()
    assert info["code_identity"]["version"] == info["lionagi_version"]
    assert info["code_identity"]["verb_count"] == info["verb_count"]


def test_doctor_machine_payload_separates_unknown_from_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lionagi.cli import machine

    monkeypatch.setattr(
        doctor,
        "collect_checks",
        lambda: {
            "a": {"status": "unknown", "detail": "cannot tell"},
            "b": {"status": "fail", "detail": "broken"},
            "c": {"status": "ok", "detail": "fine"},
        },
    )
    data = machine.doctor_data()
    assert data["failed"] == ["b"]
    assert data["unknown"] == ["a"]
