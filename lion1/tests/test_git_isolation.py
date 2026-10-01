"""The bench tests' git (`tests/_git.py`) leaves out the git variables of the process running the tests."""

import os
import subprocess
import sys
from pathlib import Path

from tests._git import git

ROOT = Path(__file__).resolve().parents[1]


def test_a_fixture_commit_ignores_the_git_variables_and_ignore_file_the_test_process_carries(tmp_path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    ran = tmp_path / "hook-ran"
    (hooks / "pre-commit").write_text(f"#!/bin/sh\ntouch '{ran}'\n")
    (hooks / "pre-commit").chmod(0o755)
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    fixture = tmp_path / "fixture"
    xdg = tmp_path / "xdg"
    (xdg / "git").mkdir(parents=True)
    (xdg / "git" / "ignore").write_text("a.txt\n")
    # what a test process run from a git hook, or under `git -c`, carries
    env = {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": str(hooks),
        "GIT_DIR": str(other / ".git"),
        "GIT_INDEX_FILE": str(tmp_path / "other-index"),
        # and a user whose own ignore file lists the fixture's file
        "XDG_CONFIG_HOME": str(xdg),
    }
    script = (
        "from pathlib import Path\nfrom tests._git import repo\n"
        f"print(repo(Path({str(fixture)!r}), {{'a.txt': 'a'}}))"
    )

    done = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, capture_output=True, text=True)

    assert done.returncode == 0, done.stderr
    assert not ran.exists()
    assert git(fixture, "rev-parse", "HEAD").strip() == done.stdout.strip()
    assert git(fixture, "show", "HEAD:a.txt") == "a"
    assert not git(other, "rev-list", "--all").strip()
