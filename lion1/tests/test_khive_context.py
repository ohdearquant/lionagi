"""An actor directory's checkpoint folder, `.khive/context/` (ADR-0019/C6): `context_dir` names it and
`assert_ignored` refuses one a git work tree would commit, before anything is written; outside a
repository it passes, and a git that cannot answer refuses."""

import subprocess
import types
from pathlib import Path

import pytest

from hub import khive as module
from hub.khive import NotIgnored, assert_ignored, context_dir
from lion1.checkpoint import CheckpointError, save, take
from tests.test_checkpoint import ACTOR, AT, ID


def isolated_git(monkeypatch, root: Path) -> None:
    """No global or system git config: a global excludes file would answer the ignore question."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(root / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "xdg"))


def scripted_git(monkeypatch, *outcomes) -> list:
    """A git that answers each call from `outcomes`: (rc, stdout, stderr), or an exception to raise."""
    calls: list = []

    def run(argv, **kw):
        calls.append(argv)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        rc, out, err = outcome
        return subprocess.CompletedProcess(argv, rc, out, err)

    fake = types.SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)
    monkeypatch.setattr(module, "subprocess", fake)
    return calls


TRUE = (0, "true\n", "")


@pytest.mark.parametrize(
    ("outcomes", "raises", "said"),
    [
        (
            (OSError("no git here"),),
            CheckpointError,
            "git could not be asked about .*: no git here; nothing was written",
        ),
        ((subprocess.TimeoutExpired("git", 30),), CheckpointError, "git could not be asked about"),
        (((128, "", "fatal: not a git repository (or any of the parent directories): .git\n"),), None, ""),
        (((1, "", ""),), CheckpointError, "git could not say whether .* is in a repository: rc 1; nothing"),
        (((1, "", "fatal: odd\n"),), CheckpointError, "is in a repository: fatal: odd; nothing was written"),
        (((0, "false\n", ""),), None, ""),
        (
            (TRUE, OSError("gone")),
            CheckpointError,
            "git check-ignore could not be asked about .*: gone; nothing",
        ),
        ((TRUE, (0, "", "")), None, ""),
        ((TRUE, (1, "", "")), NotIgnored, "does not ignore it, so a checkpoint could be committed"),
        (
            (TRUE, (128, "", "fatal: bad pattern\n")),
            CheckpointError,
            "could not answer for .*: fatal: bad pattern",
        ),
        ((TRUE, (2, "", "")), CheckpointError, "could not answer for .*: rc 2; nothing was written"),
    ],
)
def test_assert_ignored_reads_gits_two_answers_and_refuses_when_git_cannot_answer(
    tmp_path, monkeypatch, outcomes, raises, said
):
    calls = scripted_git(monkeypatch, *outcomes)
    if raises is None:
        assert assert_ignored(tmp_path) is None
    else:
        with pytest.raises(raises, match=said):
            assert_ignored(tmp_path)
    assert len(calls) == len(outcomes)
    assert calls[0][1:] == ["-C", str(tmp_path), "rev-parse", "--is-inside-work-tree"]
    if len(calls) == 2:
        assert calls[1][1:] == ["-C", str(tmp_path), "check-ignore", "-q", ".khive/context/"]


def test_the_guard_refuses_a_folder_git_would_commit_and_a_checkpoint_saved_past_it_stays_untracked(
    tmp_path, monkeypatch
):
    isolated_git(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    below = repo / "sub" / "x"
    below.mkdir(parents=True)
    assert issubclass(NotIgnored, CheckpointError) and context_dir(repo) == repo / ".khive" / "context"
    for d in (repo, below):
        with pytest.raises(NotIgnored, match="does not ignore it"):
            assert_ignored(d)
        assert not (d / ".khive").exists()
    # the root form reaches the root only; a directory below needs the `**/` form the refusal names
    (repo / ".gitignore").write_text(".khive/context/\n")
    assert assert_ignored(repo) is None
    with pytest.raises(NotIgnored, match=r"`\*\*/.khive/context/` when it sits below it"):
        assert_ignored(below)
    (repo / ".gitignore").write_text("**/.khive/context/\n")
    assert assert_ignored(below) is None
    c = take({}, [], actor=ACTOR, why="manual", budget=100, now=AT)
    assert save(c, context_dir(repo)) == repo / ".khive" / "context" / f"{ID}.json"
    assert save(c, context_dir(below)).is_file()
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == "?? .gitignore\n"  # the checkpoints are not there to commit
    # the .git directory is in the repository but not its work tree; outside any repository it passes
    assert assert_ignored(repo / ".git") is None
    probe = subprocess.run(["git", "-C", str(tmp_path), "rev-parse"], capture_output=True, text=True)
    assert "not a git repository" in probe.stderr
    (tmp_path / "plain").mkdir()
    assert assert_ignored(tmp_path / "plain") is None
    # a directory that does not exist is not "outside a repository": git cannot answer, so it refuses
    with pytest.raises(CheckpointError, match="git could not say whether .* is in a repository"):
        assert_ignored(tmp_path / "absent")
