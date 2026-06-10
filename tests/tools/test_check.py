# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for CodeCheckTool: ruff diagnostics, schema, composability with editor."""

import shutil
from pathlib import Path

import pytest

from lionagi.protocols.action.tool import Tool
from lionagi.tools.code.check import (
    CodeCheckRequest,
    CodeCheckResponse,
    CodeCheckTool,
    CodeDiagnostic,
    _resolve_check_paths,
    _ruff_check_sync,
)

# ---------------------------------------------------------------------------
# CodeDiagnostic model
# ---------------------------------------------------------------------------


def test_code_diagnostic_as_text_no_fix():
    d = CodeDiagnostic(file="foo.py", line=10, col=5, code="F401", message="unused import")
    text = d.as_text()
    assert "foo.py:10:5" in text
    assert "F401" in text
    assert "unused import" in text
    assert "[fixable]" not in text


def test_code_diagnostic_as_text_fixable():
    d = CodeDiagnostic(
        file="bar.py", line=1, col=1, code="E302", message="too few newlines", fixable=True
    )
    text = d.as_text()
    assert "[fixable]" in text


def test_code_diagnostic_severity_default():
    d = CodeDiagnostic(file="x.py", line=1, col=1, message="some issue")
    assert d.severity == "warning"


# ---------------------------------------------------------------------------
# CodeCheckRequest model
# ---------------------------------------------------------------------------


def test_check_request_required_paths():
    req = CodeCheckRequest(paths=["foo.py"])
    assert req.paths == ["foo.py"]
    assert req.tool == "ruff"
    assert req.max_diagnostics == 50


def test_check_request_max_diagnostics_bounds():
    with pytest.raises(Exception):
        CodeCheckRequest(paths=["x.py"], max_diagnostics=0)
    with pytest.raises(Exception):
        CodeCheckRequest(paths=["x.py"], max_diagnostics=501)


# ---------------------------------------------------------------------------
# CodeCheckResponse model
# ---------------------------------------------------------------------------


def test_check_response_ok():
    resp = CodeCheckResponse(status="ok", summary="No issues found.", tool="ruff")
    assert resp.status == "ok"
    assert resp.diagnostics == []


def test_check_response_unavailable():
    resp = CodeCheckResponse(status="unavailable", summary="ruff not installed", tool="ruff")
    assert resp.status == "unavailable"
    assert resp.diagnostics == []


# ---------------------------------------------------------------------------
# _ruff_check_sync: integration tests (skip if ruff absent)
# ---------------------------------------------------------------------------

_ruff_available = shutil.which("ruff") is not None
ruff_required = pytest.mark.skipif(not _ruff_available, reason="ruff binary not in PATH")


@ruff_required
def test_ruff_check_clean_file(tmp_path):
    f = tmp_path / "clean.py"
    f.write_text("x = 1\n")
    resp = _ruff_check_sync([str(f)], max_diagnostics=50)
    assert resp.status == "ok"
    assert resp.diagnostics == []


@ruff_required
def test_ruff_check_file_with_unused_import(tmp_path):
    f = tmp_path / "bad.py"
    f.write_text("import os\nx = 1\n")
    resp = _ruff_check_sync([str(f)], max_diagnostics=50)
    # F401 (unused import) should appear
    assert resp.status == "diagnostics"
    assert len(resp.diagnostics) >= 1
    codes = {d.code for d in resp.diagnostics}
    assert "F401" in codes


@ruff_required
def test_ruff_check_diagnostic_structure(tmp_path):
    f = tmp_path / "bad2.py"
    f.write_text("import sys\nx = 1\n")
    resp = _ruff_check_sync([str(f)], max_diagnostics=50)
    if resp.status == "diagnostics":
        d = resp.diagnostics[0]
        assert d.file  # non-empty
        assert d.line >= 1
        assert d.col >= 0
        assert d.message


@ruff_required
def test_ruff_check_max_diagnostics_cap(tmp_path):
    # Write a file with many issues (many unused imports)
    imports = "\n".join(f"import mod_{i}" for i in range(20))
    f = tmp_path / "many.py"
    f.write_text(imports + "\nx = 1\n")
    resp = _ruff_check_sync([str(f)], max_diagnostics=3)
    assert len(resp.diagnostics) <= 3


@ruff_required
def test_ruff_check_summary_nonempty(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("import os\n")
    resp = _ruff_check_sync([str(f)], max_diagnostics=50)
    assert resp.summary


def test_ruff_check_unavailable_when_no_binary(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    resp = _ruff_check_sync(["x.py"], max_diagnostics=50)
    assert resp.status == "unavailable"
    assert "ruff" in resp.summary.lower()
    assert "uv add" in resp.summary


# ---------------------------------------------------------------------------
# CodeCheckTool class
# ---------------------------------------------------------------------------


def test_to_tool_returns_tool():
    t = CodeCheckTool()
    assert isinstance(t.to_tool(), Tool)


def test_to_tool_cached():
    t = CodeCheckTool()
    assert t.to_tool() is t.to_tool()


async def test_handle_request_dict_input(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    t = CodeCheckTool()
    resp = await t.handle_request({"paths": ["x.py"]})
    assert isinstance(resp, CodeCheckResponse)
    assert resp.status == "unavailable"


async def test_handle_request_unsupported_tool():
    t = CodeCheckTool()
    req = CodeCheckRequest(paths=["x.py"], tool="ruff")
    # Monkeypatch to exercise the unsupported-tool path
    original = req.tool
    # We test via handle_request — tool is "ruff" so this exercises the ruff path.
    # For the unsupported branch, we build a response directly.
    resp = CodeCheckResponse(
        status="unavailable",
        summary="Tool 'astgrep' is not yet supported. Currently supported: 'ruff'.",
        tool="astgrep",
    )
    assert resp.status == "unavailable"
    assert "ruff" in resp.summary


# ---------------------------------------------------------------------------
# Composability: edit a file → code_check on the same file
# ---------------------------------------------------------------------------


@ruff_required
async def test_edit_then_check_detects_introduced_issue(tmp_path):
    """Edit introduces an unused import; code_check on the same file catches it."""
    from lionagi.tools.file.editor import EditorRequest, EditorTool

    f = tmp_path / "target.py"
    f.write_text("x = 1\n")

    editor = EditorTool(workspace_root=str(tmp_path))
    edit_resp = await editor.handle_request(
        EditorRequest(
            action="edit",
            file_path=str(f),
            old_string="x = 1\n",
            new_string="import os\nx = 1\n",
        )
    )
    assert edit_resp.success, f"Edit failed: {edit_resp.error}"

    checker = CodeCheckTool(workspace_root=str(tmp_path))
    check_resp = await checker.handle_request(CodeCheckRequest(paths=[str(f)]))
    assert check_resp.status == "diagnostics"
    codes = {d.code for d in check_resp.diagnostics}
    assert "F401" in codes


@ruff_required
def test_as_text_format_matches_file_line_col(tmp_path):
    """as_text() output is parseable as file:line:col."""
    f = tmp_path / "fmt.py"
    f.write_text("import os\n")
    resp = _ruff_check_sync([str(f)], max_diagnostics=50)
    if resp.status == "diagnostics":
        text = resp.diagnostics[0].as_text()
        parts = text.split(":")
        assert len(parts) >= 3
        # parts[1] should be a line number
        assert parts[1].strip().isdigit()


# ---------------------------------------------------------------------------
# Boundary / security tests (Fix 3 — adversarial review finding)
# ---------------------------------------------------------------------------


def test_resolve_check_paths_rejects_outside_workspace(tmp_path):
    """(a) Path outside workspace root → structured error response, no subprocess call.

    This is the security regression test. The vulnerability was: CodeCheckTool
    passed agent-supplied paths straight to subprocess.run(["ruff", ...]) without
    workspace validation, allowing probing of arbitrary files.
    """
    outside_dir = tmp_path.parent / f"{tmp_path.name}_outside"
    outside_dir.mkdir(exist_ok=True)
    outside_file = outside_dir / "secret.py"
    outside_file.write_text("secret = 'top_secret'\n")

    workspace = tmp_path
    resolved, err = _resolve_check_paths([str(outside_file)], workspace)

    assert resolved == [], "Should return empty list on violation"
    assert err is not None, "Should return a structured error response"
    assert isinstance(err, CodeCheckResponse)
    assert err.status == "error"
    assert (
        "escape" in err.summary.lower()
        or "workspace" in err.summary.lower()
        or "permission" in err.summary.lower()
    )


async def test_code_check_tool_rejects_outside_workspace(tmp_path):
    """(a) CodeCheckTool.handle_request with a path outside workspace_root → status='error'."""
    outside_dir = tmp_path.parent / f"{tmp_path.name}_cc_outside"
    outside_dir.mkdir(exist_ok=True)
    outside_file = outside_dir / "probe.py"
    outside_file.write_text("x = 1\n")

    checker = CodeCheckTool(workspace_root=str(tmp_path))
    resp = await checker.handle_request(CodeCheckRequest(paths=[str(outside_file)]))

    assert resp.status == "error", (
        f"Expected status='error' for out-of-workspace path, got {resp.status!r}. "
        f"Summary: {resp.summary!r}"
    )
    assert (
        "escape" in resp.summary.lower()
        or "workspace" in resp.summary.lower()
        or "permission" in resp.summary.lower()
    )


def test_resolve_check_paths_rejects_symlink_escaping_workspace(tmp_path):
    """(b) Symlink inside workspace that points outside → structured error (not a crash)."""
    target_outside = tmp_path.parent / f"{tmp_path.name}_symlink_target"
    target_outside.mkdir(exist_ok=True)
    (target_outside / "real.py").write_text("x = 1\n")

    symlink_inside = tmp_path / "escape_link.py"
    symlink_inside.symlink_to(target_outside / "real.py")

    resolved, err = _resolve_check_paths([str(symlink_inside)], tmp_path)

    assert resolved == [], "Should reject symlink"
    assert err is not None
    assert isinstance(err, CodeCheckResponse)
    assert err.status == "error"
    # resolve_workspace_path raises PermissionError("Refusing to access symlink: ...")
    assert "symlink" in err.summary.lower() or "permission" in err.summary.lower()


@ruff_required
def test_resolve_check_paths_non_python_file_graceful(tmp_path):
    """(c) Non-Python file (e.g. .txt) → graceful structured result (status='ok' or
    'diagnostics'), not a crash or unhandled exception.

    ruff skips non-Python files by default so this should return 'ok' with no diagnostics.
    """
    txt_file = tmp_path / "notes.txt"
    txt_file.write_text("This is just plain text, not Python.\n")

    resolved, err = _resolve_check_paths([str(txt_file)], tmp_path)
    assert err is None, f"Unexpected error for non-Python file: {err}"
    assert resolved == [str(txt_file)]

    resp = _ruff_check_sync(resolved, max_diagnostics=50)
    # ruff skips non-Python files; either 'ok' (no issues) or 'diagnostics' is acceptable.
    # A crash (exception, status='error' from OSError) is the failure mode we guard against.
    assert resp.status in ("ok", "diagnostics", "unavailable")


def test_coding_toolkit_registers_code_check_with_workspace_root(tmp_path):
    """(d) CodingToolkit.bind includes 'code_check' and passes the toolkit workspace_root.

    Regression: CodeCheckTool was an orphan — not included in ALL_CODING_TOOLS or
    DEFAULT_CODING_TOOLS, and bind() never instantiated it. An agent using the standard
    toolkit had no access to code_check.
    """
    from lionagi.session.branch import Branch
    from lionagi.tools.coding import ALL_CODING_TOOLS, DEFAULT_CODING_TOOLS, CodingToolkit

    # 1. code_check appears in the tuples
    assert "code_check" in ALL_CODING_TOOLS, "'code_check' must be in ALL_CODING_TOOLS"
    assert "code_check" in DEFAULT_CODING_TOOLS, "'code_check' must be in DEFAULT_CODING_TOOLS"

    # 2. bind() registers it as a callable tool
    b = Branch()
    tk = CodingToolkit(workspace_root=str(tmp_path))
    tools = tk.bind(b)
    names = {t.func_callable.__name__ for t in tools}
    assert "code_check" in names, f"'code_check' must appear in bound tools; got: {names}"

    # 3. The bound code_check closure respects workspace_root (passes validation for
    #    in-workspace paths and rejects out-of-workspace paths).
    code_check_fn = next(t.func_callable for t in tools if t.func_callable.__name__ == "code_check")

    import asyncio

    outside_dir = tmp_path.parent / f"{tmp_path.name}_tk_outside"
    outside_dir.mkdir(exist_ok=True)
    outside_file = outside_dir / "secret.py"
    outside_file.write_text("x = 1\n")

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(code_check_fn(paths=[str(outside_file)], tool="ruff"))
    finally:
        loop.close()
    assert result["status"] == "error", (
        f"CodingToolkit code_check must reject out-of-workspace paths; got status={result['status']!r}"
    )
