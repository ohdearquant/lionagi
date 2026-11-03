# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""CLI workspace errors name the rule without retaining caller paths."""

from pathlib import Path

import pytest

from lionhub.providers._cli_subprocess import resolve_cli_workspace


@pytest.mark.parametrize("kind", ["missing-repo", "absolute-workspace", "traversal", "symlink"])
def test_workspace_refusal_does_not_retain_path_or_exception_chain(tmp_path, kind):
    marker = "sensitive fixture content"
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = None
    if kind == "missing-repo":
        repo = tmp_path / marker
    elif kind == "absolute-workspace":
        workspace = "/" + marker
    elif kind == "traversal":
        workspace = "../" + marker
    else:
        outside = tmp_path / marker
        outside.mkdir()
        (repo / "alias").symlink_to(outside, target_is_directory=True)
        workspace = "alias"
    error = None
    try:
        resolve_cli_workspace(repo, workspace)
    except Exception as caught:
        error = caught
    if not isinstance(error, ValueError):
        pytest.fail("workspace was not refused with its existing error category", pytrace=False)
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if marker in str(current) or marker in repr(current):
            pytest.fail("workspace refusal retained caller input", pytrace=False)
        pending.extend(x for x in (current.__cause__, current.__context__) if x is not None)


def test_uninspectable_repo_omits_path_and_original_error(monkeypatch, tmp_path):
    marker = "sensitive fixture content"
    repo = tmp_path / marker

    def fail_stat(_path):
        raise PermissionError(marker)

    monkeypatch.setattr(Path, "is_dir", fail_stat)
    caught = None
    try:
        resolve_cli_workspace(repo, None)
    except Exception as error:
        caught = error
    if not isinstance(caught, PermissionError):
        pytest.fail("workspace inspection changed error type", pytrace=False)
    if marker in str(caught) or caught.__context__ is not None or caught.__cause__ is not None:
        pytest.fail("workspace inspection retained private input or original error", pytrace=False)
