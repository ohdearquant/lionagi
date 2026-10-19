# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Shared root normalization keeps every existing tool's constructor behavior."""

from __future__ import annotations

import os
from functools import partial
from pathlib import Path

import pytest

from lionagi.tools.code.ast_search import AstSearchTool
from lionagi.tools.code.check import CodeCheckTool
from lionagi.tools.code.nav import NavTool
from lionagi.tools.coding import CodingToolkit
from lionagi.tools.file.editor import EditorTool
from lionagi.tools.file.reader import ReaderTool

_FACTORIES = (
    pytest.param(partial(CodingToolkit, notify=False, tools=()), id="coding"),
    pytest.param(ReaderTool, id="reader"),
    pytest.param(EditorTool, id="editor"),
    pytest.param(CodeCheckTool, id="check"),
    pytest.param(AstSearchTool, id="ast-search"),
    pytest.param(NavTool, id="nav"),
)


@pytest.mark.parametrize("make_tool", _FACTORIES)
@pytest.mark.parametrize(
    "root_kind",
    ("none", "empty", "relative", "absolute", "tilde", "missing", "parent", "path"),
)
def test_tool_workspace_root_normalizes_without_creating_the_target(
    make_tool, root_kind, monkeypatch, tmp_path
):
    project = (tmp_path / "project").resolve()
    home = (tmp_path / "home").resolve()
    project.mkdir()
    home.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    root, expected = {
        "none": (None, project),
        "empty": ("", project),
        "relative": ("child/..", project),
        "absolute": (str(home), home),
        "tilde": ("~/workspace", home / "workspace"),
        "missing": ("missing/nested", project / "missing" / "nested"),
        "parent": ("..", project.parent),
        "path": (Path("path-value"), project / "path-value"),
    }[root_kind]
    existed = expected.exists()

    tool = make_tool(workspace_root=root)

    assert isinstance(tool.workspace_root, Path)
    assert tool.workspace_root == expected
    assert expected.exists() is existed


@pytest.mark.parametrize("make_tool", _FACTORIES)
def test_tool_workspace_root_follows_an_explicit_directory_link(make_tool, tmp_path):
    target = (tmp_path / "target").resolve()
    target.mkdir()
    link = tmp_path / "root-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("this Windows test environment lacks symlink creation privilege")
        raise

    tool = make_tool(workspace_root=link)

    assert tool.workspace_root == target
    assert link.is_symlink()
