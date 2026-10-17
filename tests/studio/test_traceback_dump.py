# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The periodic stack dump is off unless asked for, and says why when it refuses.

The disarmed arm is the load-bearing one. A diagnostic hook that costs anything
when nobody asked for it is a worse problem than the hang it exists to
diagnose, so "no timer, no file, no behaviour change" is asserted directly
rather than inferred from the code path not being reached.

Every refusal is asserted through the channel that carries it. The hook prints
to stderr, so a test that only checked "it did not arm" would pass on a version
that refuses silently, which is the shape the flag-says-whether-log-says-why
rule exists to prevent.
"""

from __future__ import annotations

import faulthandler
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lionagi.studio import _traceback_dump as td


@pytest.fixture(autouse=True)
def _never_leave_a_timer_armed():
    """A leaked timer would dump stacks into a closed file for the rest of the run."""
    yield
    td.disarm_traceback_dump()


@pytest.fixture
def spy(monkeypatch):
    """Record calls to the stdlib arming call instead of making them."""
    calls: list[dict] = []

    def fake(timeout, repeat=False, file=None, exit=False):  # noqa: A002
        calls.append({"timeout": timeout, "repeat": repeat, "file": file, "exit": exit})

    # td holds the module, not the function, so one patch covers both names.
    monkeypatch.setattr(faulthandler, "dump_traceback_later", fake)
    return calls


def _outside_any_checkout(tmp_path: Path) -> Path:
    """pytest's tmp_path is outside the repo, but assert it rather than assume."""
    assert td._inside_a_git_tree(tmp_path) is None, (
        f"{tmp_path} is inside a checkout, so it cannot stand in for an external path"
    )
    return tmp_path


def test_it_does_not_arm_when_the_variable_is_absent(tmp_path, monkeypatch, spy, capsys):
    monkeypatch.delenv(td._ENV_PATH, raising=False)
    target = _outside_any_checkout(tmp_path) / "dump.txt"

    assert td.arm_traceback_dump() is False
    assert spy == [], "a timer was armed with nothing asking for one"
    assert not target.exists()
    assert capsys.readouterr().err == "", "the disarmed path must be silent as well as inert"


def test_it_arms_on_the_variable_and_appends_to_the_named_path(tmp_path, monkeypatch, spy):
    target = _outside_any_checkout(tmp_path) / "nested" / "dump.txt"
    monkeypatch.setenv(td._ENV_PATH, str(target))

    assert td.arm_traceback_dump() is True

    assert len(spy) == 1
    call = spy[0]
    assert call["repeat"] is True, "a single dump cannot show whether a frame is stuck"
    assert call["timeout"] == td._DEFAULT_INTERVAL_SECONDS
    assert call["file"] is not None
    assert target.exists(), "the file is opened at arm time, not at the first dump"
    assert call["file"].mode == "a", "an overwriting handle loses every dump but the last"


def test_the_interval_is_configurable(tmp_path, monkeypatch, spy):
    target = _outside_any_checkout(tmp_path) / "dump.txt"
    monkeypatch.setenv(td._ENV_PATH, str(target))
    monkeypatch.setenv(td._ENV_INTERVAL, "3.5")

    assert td.arm_traceback_dump() is True
    assert spy[0]["timeout"] == 3.5


@pytest.mark.parametrize("bad", ["not-a-number", "0", "-5"])
def test_a_bad_interval_refuses_out_loud_rather_than_arming_a_default(
    tmp_path, monkeypatch, spy, capsys, bad
):
    target = _outside_any_checkout(tmp_path) / "dump.txt"
    monkeypatch.setenv(td._ENV_PATH, str(target))
    monkeypatch.setenv(td._ENV_INTERVAL, bad)

    assert td.arm_traceback_dump() is False
    assert spy == []
    err = capsys.readouterr().err
    assert "not armed" in err, err
    assert td._ENV_INTERVAL in err, err


def test_it_refuses_a_path_inside_a_checkout(monkeypatch, spy, capsys):
    """A dump under a tracked tree is one `git add -A` away from being committed.

    The candidate path is a real one inside this checkout, because a fake one
    would not exercise the check. Cleaned up unconditionally: a version that
    creates the file before refusing would otherwise leave it in the working
    tree, where the next run of this same test reads it as the earlier failure
    rather than its own.
    """
    inside = Path(__file__).resolve().parent / "refused-dump.txt"
    inside.unlink(missing_ok=True)
    assert td._inside_a_git_tree(inside.parent) is not None, (
        "this test's own directory is not in a checkout, so it proves nothing"
    )
    monkeypatch.setenv(td._ENV_PATH, str(inside))

    assert td.arm_traceback_dump() is False
    assert spy == []
    assert not inside.exists(), "the refused path must not be created on the way to refusing"
    err = capsys.readouterr().err
    assert "not armed" in err and "git tree" in err, err
    inside.unlink(missing_ok=True)


def test_an_unwritable_path_refuses_out_loud_rather_than_arming_nothing(
    tmp_path, monkeypatch, spy, capsys
):
    """The failure the operator is most likely to hit, and the one silence hides best."""
    blocker = _outside_any_checkout(tmp_path) / "not-a-directory"
    blocker.write_text("")
    target = blocker / "dump.txt"  # parent is a regular file, so mkdir and open both fail
    monkeypatch.setenv(td._ENV_PATH, str(target))

    assert td.arm_traceback_dump() is False
    assert spy == []
    err = capsys.readouterr().err
    assert "not armed" in err, err
    assert str(target) in err, "the refusal must name the path the operator set"


def test_disarm_is_safe_when_nothing_was_ever_armed(monkeypatch, spy):
    monkeypatch.delenv(td._ENV_PATH, raising=False)

    td.disarm_traceback_dump()
    td.disarm_traceback_dump()


_REAL_ARM = """
import sys, time
from pathlib import Path
from lionagi.studio import _traceback_dump as td

target = Path(sys.argv[1])
if not td.arm_traceback_dump():
    sys.exit(3)
try:
    deadline = time.time() + 5.0
    while target.stat().st_size == 0 and time.time() < deadline:
        time.sleep(0.05)
finally:
    td.disarm_traceback_dump()
"""


def test_a_real_arm_writes_real_stacks(tmp_path):
    """One end-to-end pass with the stdlib call unmocked.

    Every other test here spies on ``dump_traceback_later``, so all of them
    would pass on a version that arms something which never writes anything.
    The armed timer's thread reads the stack of every thread in its process,
    so the pass runs in a child interpreter: in the test runner's own worker it
    would read the worker's other threads too, and a fault there ends the
    worker rather than this test.
    """
    target = _outside_any_checkout(tmp_path) / "real.txt"
    env = {**os.environ, td._ENV_PATH: str(target), td._ENV_INTERVAL: "0.2"}

    result = subprocess.run(
        [sys.executable, "-c", _REAL_ARM, str(target)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, f"the child exited {result.returncode}: {result.stderr[-2000:]}"
    assert "armed, every 0.2s" in result.stderr
    content = target.read_text()
    assert "Thread" in content or "File " in content, (
        f"the armed timer produced no stack text within the window: {content[:200]!r}"
    )


def _git_dir(root: Path) -> Path:
    """A `.git` carrying what git's own discovery looks for."""
    entry = root / ".git"
    (entry / "objects").mkdir(parents=True)
    (entry / "refs").mkdir()
    (entry / "HEAD").write_text("ref: refs/heads/main\n")
    return entry


def test_an_abandoned_git_init_is_not_a_checkout(tmp_path):
    """Incomplete `.git` directories do not identify a checkout.

    An interrupted initialization may leave `hooks` and `info` but omit the
    entries Git uses to recognize a repository. Paths below that directory
    must remain available for ordinary files.
    """
    entry = tmp_path / ".git"
    (entry / "hooks").mkdir(parents=True)
    (entry / "info").mkdir()

    assert td._inside_a_git_tree(tmp_path) is None
    assert td._inside_a_git_tree(tmp_path / "runs" / "deep") is None


def test_a_real_checkout_is_still_found(tmp_path):
    """The direction that must not regress: missing a tree is how dumps get committed.

    A real ``git init``, not the hand-built layout. Every other test here builds
    `.git` out of the same three names the predicate looks for, which can only
    show that the code accepts what the code expects. This one asks git.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)

    assert td._inside_a_git_tree(tmp_path) == tmp_path
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert td._inside_a_git_tree(nested) == tmp_path


def test_the_hand_built_layout_carries_what_git_init_carries(tmp_path):
    """Closes the circularity in the helper the negative tests are built on.

    `_git_dir` is what `test_a_partial_git_directory_is_not_a_checkout` removes
    entries from. If it ever drifts from what a real repository looks like,
    those tests keep passing while asserting things about a shape that does not
    occur. Checked against git's own output rather than restated here.
    """
    real = tmp_path / "real"
    subprocess.run(["git", "init", "-q", str(real)], check=True, capture_output=True)
    synthetic = _git_dir(tmp_path / "synthetic")

    for name in ("HEAD", "objects", "refs"):
        produced = real / ".git" / name
        assert produced.exists(), f"git init produced no {name}; the predicate is checking for it"
        assert (synthetic / name).exists(), f"the helper produced no {name}"
        assert produced.is_dir() == (synthetic / name).is_dir(), f"{name} differs in kind"


def test_a_git_file_is_a_checkout(tmp_path):
    """A linked worktree and a submodule both record their git dir in a `.git` FILE.

    Requiring a directory would read both as external, which is the expensive
    mistake: this suite runs from a linked worktree often enough that the
    positive control above depends on this branch.
    """
    (tmp_path / ".git").write_text(f"gitdir: {tmp_path}/elsewhere/.git/worktrees/wt\n")

    assert td._inside_a_git_tree(tmp_path) == tmp_path


def test_a_partial_git_directory_is_not_a_checkout(tmp_path):
    """Each required entry is load-bearing, not decoration on the first one."""
    for missing in ("HEAD", "objects", "refs"):
        root = tmp_path / f"without-{missing}"
        root.mkdir()
        entry = _git_dir(root)
        target = entry / missing
        if target.is_dir():
            target.rmdir()
        else:
            target.unlink()

        assert td._inside_a_git_tree(root) is None, (
            f"a .git missing {missing} was read as a checkout"
        )


@pytest.fixture
def unreadable_git(tmp_path):
    """A `.git` directory that can be seen but not entered.

    Restored in teardown unconditionally: a mode-000 directory left behind
    makes pytest's own tmp cleanup fail, which reports as an error in an
    unrelated test.

    The mode is asserted rather than assumed, because `chmod` does not bind a
    privileged runner: as root the entry is still readable, every test built on
    this fixture then measures an ordinary empty directory, and each one reports
    the wrong thing rather than nothing. Skipping says which case was not
    covered; a green run on a root runner does not.
    """
    entry = tmp_path / ".git"
    entry.mkdir()
    entry.chmod(0o000)
    try:
        try:
            (entry / "probe").stat()
        except PermissionError:
            pass
        except OSError:
            pytest.skip("chmod does not bind this runner, so nothing here is unreadable")
        else:
            pytest.skip("chmod does not bind this runner, so nothing here is unreadable")
        yield tmp_path
    finally:
        entry.chmod(0o755)


def test_an_unreadable_git_entry_resolves_as_a_checkout_rather_than_raising(unreadable_git):
    """Undecidable has to answer, and answer on the safe side.

    Descending into `.git` raises where noticing it did not, so the refusal is
    reached through an error rather than through a negative answer. Refusing a
    safe path is cheap; raising out of this function is not, since the daemon
    calls it while arming.

    Runs the interpreter's real behaviour, which is why it cannot stand alone:
    on 3.10 through 3.13 it exercises the PermissionError path, and on 3.14 it
    exercises the changed one. The test below covers whichever of those this
    runner is not.
    """
    assert td._inside_a_git_tree(unreadable_git) == unreadable_git


def _forbid_pathlib_probes(monkeypatch) -> None:
    """Make consulting `is_file`/`is_dir` a test failure rather than a no-op.

    An earlier version of these two tests patched both probes to return False,
    imitating 3.14, and asserted the refusal held. That patch was inert: the
    classification uses `stat` and `stat.S_IS*` and never calls either probe,
    so the tests passed identically with the patch removed and named a
    mechanism that was not running. A test that simulates a condition the code
    under test cannot observe reports on nothing.

    Raising instead of returning False inverts it into an assertion that holds
    its ground: the fix's whole content is that the classification asks `stat`,
    which raises for a path it cannot read on every supported version, rather
    than a probe that answers False on 3.14. Reintroducing such a probe fails
    here by name.
    """

    def _forbidden(self, *args, **kwargs):
        raise AssertionError(
            "the git-tree classification consulted a pathlib probe; those "
            "answer False for an unreadable path on 3.14 instead of raising, "
            "which is the fall-through this guard was rewritten to avoid"
        )

    monkeypatch.setattr(Path, "is_file", _forbidden)
    monkeypatch.setattr(Path, "is_dir", _forbidden)


def test_the_refusal_does_not_rest_on_probes_that_can_answer_false(unreadable_git, monkeypatch):
    """The version this runner is not on, made reachable on the one it is.

    Python 3.14 changed `Path.is_file`/`is_dir` to answer False for a path they
    cannot read rather than raise. Measured: 3.10 through 3.13 raise
    PermissionError, 3.14 returns False. `requires-python` carries no ceiling,
    so a 3.14 install is a supported one, and under it a guard written around
    the raising behaviour stops refusing precisely where it cannot tell -- a
    fall-through that looks exactly like a clean negative answer.

    Rather than imitate that answer, which this code cannot see, the probes are
    made fatal: the refusal below has to arrive without either of them, on
    whichever interpreter runs the suite.
    """
    _forbid_pathlib_probes(monkeypatch)

    assert td._inside_a_git_tree(unreadable_git) == unreadable_git


def test_a_real_checkout_is_still_found_without_those_probes(tmp_path, monkeypatch):
    """The must-match control for the test above.

    Refusing everything would satisfy it, so the same run has to still
    recognise a readable checkout, and still decline the directory above it.
    This is what keeps the fix from being "return the candidate more often".
    """
    entry = tmp_path / ".git"
    (entry / "objects").mkdir(parents=True)
    (entry / "refs").mkdir()
    (entry / "HEAD").write_text("ref: refs/heads/main\n")
    _forbid_pathlib_probes(monkeypatch)

    assert td._inside_a_git_tree(tmp_path / "sub" / "deep") == tmp_path
    assert td._inside_a_git_tree(tmp_path.parent) is None


def test_an_unreadable_ancestor_does_not_take_down_arming(unreadable_git, monkeypatch, spy, capsys):
    """The consequence the classification exists to prevent, asserted end to end."""
    monkeypatch.setenv(td._ENV_PATH, str(unreadable_git / "dump.txt"))

    assert td.arm_traceback_dump() is False
    assert spy == []
    err = capsys.readouterr().err
    assert "not armed" in err and "git tree" in err, err


def test_a_head_that_is_a_directory_is_not_a_checkout(tmp_path):
    """git requires HEAD to be a file; `exists()` cannot tell the two apart."""
    entry = tmp_path / ".git"
    (entry / "objects").mkdir(parents=True)
    (entry / "refs").mkdir()
    (entry / "HEAD").mkdir()

    assert td._inside_a_git_tree(tmp_path) is None


def test_a_non_directory_git_entry_is_fail_closed(tmp_path):
    make_fifo = getattr(os, "mkfifo", None)
    if make_fifo is None:
        pytest.skip("FIFO creation is unavailable on this platform")
    make_fifo(tmp_path / ".git")

    assert td._inside_a_git_tree(tmp_path) == tmp_path


def test_a_head_symlink_to_a_missing_ref_is_a_checkout(tmp_path):
    entry = _git_dir(tmp_path)
    head = entry / "HEAD"
    head.unlink()
    try:
        head.symlink_to("refs/heads/missing")
    except (NotImplementedError, OSError):
        pytest.skip("symbolic links are unavailable on this platform")

    assert td._inside_a_git_tree(tmp_path) == tmp_path
