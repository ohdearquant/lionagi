# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for CodeCheckTool: structured diagnostics, binary-guard fallback, and
edit→check composition (issue #1247).

Test groups:
- Known-bad snippet: tool returns expected file:line:code diagnostic.
- Binary-guard fallback: graceful degradation when ruff is absent.
- Edit→check composition: editor introduces an issue; check surfaces it.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Known-bad snippet tests
# ---------------------------------------------------------------------------


async def test_check_unused_import_returns_f401(tmp_path):
    """CodeCheckTool must return an F401 diagnostic at line 1 for an unused import."""
    from lionagi.tools.code.check import CodeCheckRequest, CodeCheckTool

    bad_file = tmp_path / "bad.py"
    bad_file.write_text("import os\n")

    tool = CodeCheckTool()
    resp = await tool.handle_request(CodeCheckRequest(paths=[str(bad_file)]))

    assert resp.status == "diagnostics"
    assert len(resp.diagnostics) >= 1
    d = resp.diagnostics[0]
    assert d.file == str(bad_file)
    assert d.line == 1
    assert d.code == "F401"
    assert "os" in d.message
    assert d.fixable is True  # ruff can auto-remove unused imports
    # as_text must include file:line:col for actionable model output
    text = d.as_text()
    assert f"{bad_file}:1:" in text
    assert "F401" in text


async def test_check_clean_file_returns_ok(tmp_path):
    """CodeCheckTool returns status='ok' and empty diagnostics for a clean file."""
    from lionagi.tools.code.check import CodeCheckRequest, CodeCheckTool

    clean_file = tmp_path / "clean.py"
    clean_file.write_text('def hello() -> str:\n    return "world"\n')

    tool = CodeCheckTool()
    resp = await tool.handle_request(CodeCheckRequest(paths=[str(clean_file)]))

    assert resp.status == "ok"
    assert resp.diagnostics == []
    assert "no issues" in resp.summary.lower()


async def test_check_multiple_unused_imports(tmp_path):
    """Multiple issues in a file produce one diagnostic per issue."""
    from lionagi.tools.code.check import CodeCheckRequest, CodeCheckTool

    bad_file = tmp_path / "multi.py"
    bad_file.write_text("import os\nimport sys\n")  # two unused imports

    tool = CodeCheckTool()
    resp = await tool.handle_request(CodeCheckRequest(paths=[str(bad_file)]))

    assert resp.status == "diagnostics"
    assert len(resp.diagnostics) >= 2
    lines = {d.line for d in resp.diagnostics}
    assert 1 in lines
    assert 2 in lines


async def test_check_diagnostic_as_text_format(tmp_path):
    """as_text() must produce a colon-separated file:line:col: SEVERITY CODE message string."""
    from lionagi.tools.code.check import CodeCheckRequest, CodeCheckTool

    bad_file = tmp_path / "fmt.py"
    bad_file.write_text("import os\n")

    tool = CodeCheckTool()
    resp = await tool.handle_request(CodeCheckRequest(paths=[str(bad_file)]))

    assert resp.status == "diagnostics"
    text = resp.diagnostics[0].as_text()
    # Must be parseable as file:line:col: SEVERITY CODE message
    parts = text.split(":")
    assert len(parts) >= 4  # file, line, col, rest
    assert parts[1].strip().isdigit()  # line
    assert parts[2].strip().isdigit()  # col


async def test_check_max_diagnostics_caps_output(tmp_path):
    """max_diagnostics caps the returned diagnostics list."""
    from lionagi.tools.code.check import CodeCheckRequest, CodeCheckTool

    lines = "\n".join(f"import mod{i}" for i in range(20))
    bad_file = tmp_path / "many.py"
    bad_file.write_text(lines + "\n")

    tool = CodeCheckTool()
    resp = await tool.handle_request(CodeCheckRequest(paths=[str(bad_file)], max_diagnostics=5))

    assert resp.status == "diagnostics"
    assert len(resp.diagnostics) <= 5


# ---------------------------------------------------------------------------
# Binary-guard fallback tests
# ---------------------------------------------------------------------------


async def test_check_ruff_absent_returns_unavailable(tmp_path):
    """When ruff binary is absent, CodeCheckTool must return status='unavailable'."""
    from lionagi.tools.code.check import CodeCheckRequest, CodeCheckTool

    tool = CodeCheckTool()
    with patch("lionagi.tools.code.check.shutil.which", return_value=None):
        resp = await tool.handle_request(CodeCheckRequest(paths=["/tmp/any.py"]))

    assert resp.status == "unavailable"
    assert "ruff" in resp.summary.lower()
    # Message must guide the user to install ruff
    assert "install" in resp.summary.lower() or "not in path" in resp.summary.lower()
    assert resp.diagnostics == []


async def test_check_ruff_absent_no_exception(tmp_path):
    """Absent ruff binary must not raise; it returns a structured response."""
    from lionagi.tools.code.check import CodeCheckRequest, CodeCheckTool

    tool = CodeCheckTool()
    with patch("lionagi.tools.code.check.shutil.which", return_value=None):
        try:
            resp = await tool.handle_request(CodeCheckRequest(paths=["/nonexistent.py"]))
        except Exception as e:
            pytest.fail(f"Should not raise; got {e!r}")
    assert resp.status == "unavailable"


# ---------------------------------------------------------------------------
# Schema / tool registration tests
# ---------------------------------------------------------------------------


async def test_check_to_tool_returns_tool():
    """CodeCheckTool.to_tool() must return a properly registered Tool."""
    from lionagi.protocols.action.tool import Tool
    from lionagi.tools.code.check import CodeCheckTool

    tool = CodeCheckTool()
    t = tool.to_tool()
    assert isinstance(t, Tool)
    assert t.func_callable.__name__ == "code_check"


async def test_check_to_tool_idempotent():
    """to_tool() called twice must return the same object (cached)."""
    from lionagi.tools.code.check import CodeCheckTool

    tool = CodeCheckTool()
    assert tool.to_tool() is tool.to_tool()


async def test_check_accepts_dict_input(tmp_path):
    """handle_request must accept a plain dict (mimics FunctionCalling invocation)."""
    from lionagi.tools.code.check import CodeCheckTool

    bad_file = tmp_path / "dict.py"
    bad_file.write_text("import os\n")

    tool = CodeCheckTool()
    resp = await tool.handle_request({"paths": [str(bad_file)]})
    assert resp.status in ("diagnostics", "unavailable")


async def test_check_tool_schema_has_paths_field():
    """The tool schema must expose 'paths' as a required parameter."""
    from lionagi.tools.code.check import CodeCheckTool

    tool = CodeCheckTool()
    t = tool.to_tool()
    schema = t.tool_schema
    params = schema.get("function", {}).get("parameters", {})
    assert "paths" in params.get("properties", {})
    assert "paths" in params.get("required", [])


# ---------------------------------------------------------------------------
# edit → check composition test
# ---------------------------------------------------------------------------


async def test_edit_then_check_composition(tmp_path):
    """edit → code_check composition: editor introduces an issue; check surfaces it.

    This is the primary composability test for #1247.
    Workflow: editor(action='edit', ...) → code_check(paths=[same file]) → diagnostics.
    """
    from lionagi.tools.code.check import CodeCheckRequest, CodeCheckTool
    from lionagi.tools.file.editor import EditorRequest, EditorTool

    target = tmp_path / "compose.py"
    target.write_text('def greet() -> str:\n    return "hello"\n')

    editor = EditorTool(workspace_root=str(tmp_path))
    checker = CodeCheckTool()

    # Step 1: edit the file to introduce an unused import (F401)
    edit_resp = await editor.handle_request(
        EditorRequest(
            action="edit",
            file_path=str(target),
            old_string='def greet() -> str:\n    return "hello"\n',
            new_string='import os\n\n\ndef greet() -> str:\n    return "hello"\n',
        )
    )
    assert edit_resp.success, f"Edit failed unexpectedly: {edit_resp.error}"

    # Step 2: check the file immediately after the edit
    check_resp = await checker.handle_request(CodeCheckRequest(paths=[str(target)]))

    # Must surface the F401 introduced by the edit
    assert check_resp.status == "diagnostics"
    codes = {d.code for d in check_resp.diagnostics}
    assert "F401" in codes, f"Expected F401 in {codes!r}"

    # All diagnostics must point to the edited file
    for d in check_resp.diagnostics:
        assert d.file == str(target)

    # Summary must contain actionable file:line info
    assert ":" in check_resp.summary
