"""git as the bench tests run it: the repository's own configuration alone and a fixed identity, so a
commit a test makes does not depend on this machine's global or system config (signing, hooks), on the
ignore file git reads by default from the user's config directory, nor on git variables the process
running the tests carries: settings passed down as GIT_CONFIG_COUNT or GIT_CONFIG_PARAMETERS, or the
GIT_DIR and GIT_INDEX_FILE a hook runs under, which would send a test's commands to another
repository."""

import os
import subprocess
from pathlib import Path

__all__ = ("ENV", "git", "repo")

ENV = {
    **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    # core.excludesFile defaults to $XDG_CONFIG_HOME/git/ignore even when no config file is read
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "core.excludesFile",
    "GIT_CONFIG_VALUE_0": os.devnull,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(cwd: Path, *argv: str) -> str:
    """git's stdout; a nonzero exit fails the test with git's stderr."""
    r = subprocess.run(["git", *argv], cwd=cwd, env=ENV, capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(argv)} exited {r.returncode}: {r.stderr}"
    return r.stdout


def repo(path: Path, files: dict[str, str]) -> str:
    """A repository at `path` whose one commit holds `files`; returns that commit's sha."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    for name, text in files.items():
        (path / name).write_text(text)
    git(path, "add", *files)
    git(path, "commit", "-q", "-m", "base")
    return git(path, "rev-parse", "HEAD").strip()
