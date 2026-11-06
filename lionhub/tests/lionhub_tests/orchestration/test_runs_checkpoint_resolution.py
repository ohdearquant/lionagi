# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"Run branch and checkpoint-directory identity resolution."

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from lionhub.orchestration.job import AmbiguousIdError
from tests._cli_logging_test_support import _restore_cli_logging as _restore_cli_logging
from tests._id_resolution_test_support import FIRST, SECOND, SHARED
from tests._id_resolution_test_support import (
    db_path as db_path,  # noqa: F401
)
from tests._runs_checkpoint_resolution_test_support import runs_root as runs_root


def _write_branch(runs_root: Path, run_id: str, branch_id: str) -> Path:
    branches = runs_root / run_id / "branches"
    branches.mkdir(parents=True, exist_ok=True)
    path = branches / f"{branch_id}.json"
    path.write_text(json.dumps({"id": branch_id}))
    return path


def test_find_branch_refuses_a_prefix_that_fits_two_branches(runs_root: Path):
    """Resuming acts, so the wrong branch is a new leg on someone else's work."""
    from lionhub.orchestration.runs import find_branch

    _write_branch(runs_root, "run-a", FIRST)
    _write_branch(runs_root, "run-b", SECOND)

    with pytest.raises(AmbiguousIdError) as caught:
        find_branch(SHARED)

    assert FIRST in str(caught.value) and SECOND in str(caught.value)


def test_find_branch_takes_an_exact_id_from_an_older_run(runs_root: Path):
    """An exact id is a complete answer and must win wherever it lives.

    Directories are walked newest first, which is a reasonable place to start
    looking and a bad reason to prefer one complete answer over another.
    """
    from lionhub.orchestration.runs import find_branch

    older = _write_branch(runs_root, "run-old", FIRST)
    _write_branch(runs_root, "run-new", SECOND)
    newer_dir = runs_root / "run-new"
    older_dir = runs_root / "run-old"
    now = time.time()
    import os

    os.utime(older_dir, (now - 600, now - 600))
    os.utime(newer_dir, (now, now))

    run_id, path = find_branch(FIRST)

    assert (run_id, path) == ("run-old", older)


def test_run_dir_lookup_refuses_a_prefix_that_fits_two_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Taking the newest match answers "the newest run starting with this".

    That is not the question the caller asked, and the commands built on this
    resolver replay and inspect whatever comes back.
    """
    from lionhub.orchestration.flow import checkpoint as _checkpoint

    root = tmp_path / "runs"
    (root / f"{SHARED}-aaaaaa").mkdir(parents=True)
    (root / f"{SHARED}-bbbbbb").mkdir(parents=True)
    monkeypatch.setattr(_checkpoint, "RUNS_ROOT", root)

    with pytest.raises(AmbiguousIdError) as caught:
        _checkpoint._find_run_dir_by_id(SHARED)

    assert f"{SHARED}-aaaaaa" in str(caught.value)


def test_run_dir_lookup_still_resolves_an_exact_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from lionhub.orchestration.flow import checkpoint as _checkpoint

    root = tmp_path / "runs"
    exact = root / f"{SHARED}-aaaaaa"
    exact.mkdir(parents=True)
    (root / f"{SHARED}-aaaaaa-extended").mkdir(parents=True)
    monkeypatch.setattr(_checkpoint, "RUNS_ROOT", root)

    run_dir = _checkpoint._find_run_dir_by_id(f"{SHARED}-aaaaaa")

    assert run_dir is not None
    assert run_dir.state_root == exact
