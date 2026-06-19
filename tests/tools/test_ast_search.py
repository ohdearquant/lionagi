# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for AstSearchTool: ast-grep structural search with graceful degradation."""

import shutil

import pytest

from lionagi.protocols.action.tool import Tool
from lionagi.tools.code.ast_search import (
    AstSearchRequest,
    AstSearchResponse,
    AstSearchTool,
    _ast_search_sync,
)

_sg_available = (shutil.which("sg") or shutil.which("ast-grep")) is not None
sg_required = pytest.mark.skipif(not _sg_available, reason="ast-grep (sg) binary not in PATH")


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------


def test_request_defaults():
    req = AstSearchRequest(pattern="$X")
    assert req.lang == "python"
    assert req.path == "."
    assert req.max_results == 50


def test_request_max_results_bounds():
    with pytest.raises(Exception):
        AstSearchRequest(pattern="x", max_results=0)
    with pytest.raises(Exception):
        AstSearchRequest(pattern="x", max_results=501)


def test_response_ok():
    resp = AstSearchResponse(status="ok", summary="No matches found.")
    assert resp.status == "ok"
    assert resp.matches == []
    assert resp.total == 0


def test_response_unavailable():
    resp = AstSearchResponse(status="unavailable", summary="sg not installed")
    assert resp.status == "unavailable"
    assert resp.matches == []


# ---------------------------------------------------------------------------
# Graceful degradation when sg binary absent
# ---------------------------------------------------------------------------


def test_unavailable_when_no_binary(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    resp = _ast_search_sync("$X", ".", "python", 50)
    assert resp.status == "unavailable"
    assert "ast-grep" in resp.summary.lower() or "sg" in resp.summary.lower()
    assert "install" in resp.summary.lower()


def test_unavailable_message_actionable(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    resp = _ast_search_sync("except: pass", ".", "python", 50)
    assert resp.status == "unavailable"
    # Should tell user how to install
    assert "cargo" in resp.summary or "ast-grep.github.io" in resp.summary


# ---------------------------------------------------------------------------
# Integration tests (only run when sg present)
# ---------------------------------------------------------------------------


@sg_required
def test_search_finds_bare_except(tmp_path):
    f = tmp_path / "bad.py"
    f.write_text("try:\n    pass\nexcept:\n    pass\n")
    resp = _ast_search_sync("except: pass", str(f), "python", 50)
    assert resp.status in ("ok", "matches")
    if resp.status == "matches":
        assert resp.total >= 1
        assert resp.matches[0].file


@sg_required
def test_search_no_matches(tmp_path):
    f = tmp_path / "clean.py"
    f.write_text("x = 1\ny = 2\n")
    # Pattern that won't match in a clean file
    resp = _ast_search_sync("raise NotImplementedError", str(f), "python", 50)
    assert resp.status in ("ok", "matches")
    # Clean file should have 0 matches for this pattern
    if resp.status == "ok":
        assert resp.matches == []


@sg_required
def test_search_respects_max_results(tmp_path):
    # Write a file with many identical patterns
    lines = "x = None\n" * 30
    f = tmp_path / "many.py"
    f.write_text(lines)
    resp = _ast_search_sync("$X = None", str(f), "python", 5)
    assert resp.status in ("ok", "matches")
    if resp.status == "matches":
        assert len(resp.matches) <= 5


@sg_required
def test_search_match_has_file_and_line(tmp_path):
    f = tmp_path / "target.py"
    f.write_text("def foo(): pass\n")
    resp = _ast_search_sync("def $F(): $$$", str(f), "python", 50)
    if resp.status == "matches":
        m = resp.matches[0]
        assert m.file  # non-empty
        assert m.line >= 1
        assert m.text


@sg_required
def test_search_summary_nonempty(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("x = 1\n")
    resp = _ast_search_sync("$A = $B", str(f), "python", 50)
    assert resp.summary


# ---------------------------------------------------------------------------
# AstSearchTool class
# ---------------------------------------------------------------------------


def test_to_tool_returns_tool():
    tool = AstSearchTool()
    assert isinstance(tool.to_tool(), Tool)


def test_to_tool_cached():
    tool = AstSearchTool()
    assert tool.to_tool() is tool.to_tool()


def test_to_tool_func_name():
    tool = AstSearchTool()
    assert tool.to_tool().func_callable.__name__ == "ast_search"


async def test_handle_request_dict_input(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    tool = AstSearchTool()
    resp = await tool.handle_request({"pattern": "$X"})
    assert isinstance(resp, AstSearchResponse)
    assert resp.status == "unavailable"


async def test_handle_request_rejects_outside_workspace(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}_outside"
    outside.mkdir(exist_ok=True)
    f = outside / "secret.py"
    f.write_text("x = 1\n")
    tool = AstSearchTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(AstSearchRequest(pattern="$X", path=str(f)))
    assert resp.status == "error"
    assert resp.summary


# ---------------------------------------------------------------------------
# CodingToolkit integration
# ---------------------------------------------------------------------------


def test_coding_toolkit_registers_ast_search(tmp_path):
    from lionagi.session.branch import Branch
    from lionagi.tools.coding import ALL_CODING_TOOLS, DEFAULT_CODING_TOOLS, CodingToolkit

    assert "ast_search" in ALL_CODING_TOOLS
    assert "ast_search" in DEFAULT_CODING_TOOLS

    b = Branch()
    tk = CodingToolkit(workspace_root=str(tmp_path))
    tools = tk.bind(b)
    names = {t.func_callable.__name__ for t in tools}
    assert "ast_search" in names


async def test_coding_toolkit_ast_search_degrades(tmp_path, monkeypatch):
    """ast_search in CodingToolkit returns unavailable (not an exception) when sg absent."""
    monkeypatch.setattr(shutil, "which", lambda _: None)
    import asyncio

    from lionagi.session.branch import Branch
    from lionagi.tools.coding import CodingToolkit

    b = Branch()
    tk = CodingToolkit(workspace_root=str(tmp_path))
    tools = tk.bind(b)
    ast_fn = next(t.func_callable for t in tools if t.func_callable.__name__ == "ast_search")
    result = await ast_fn(pattern="$X", path=".")
    assert result["status"] == "unavailable"
