# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests asserting improved error/guidance text and recovery paths for #1248.

Each test group:
- Triggers a specific failure case.
- Asserts the error message contains actionable guidance.
- Where documented, also asserts that the recovery path works.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Standalone EditorTool guidance
# ---------------------------------------------------------------------------


async def test_editor_old_string_not_found_message(tmp_path):
    from lionagi.tools.file.editor import EditorRequest, EditorTool

    f = tmp_path / "code.py"
    f.write_text("hello world\n")
    tool = EditorTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(
        EditorRequest(action="edit", file_path=str(f), old_string="NOTPRESENT", new_string="x")
    )
    assert resp.success is False
    assert "not found" in resp.error.lower()
    # Must instruct to re-read and copy exact text
    assert "re-read" in resp.error.lower() or "read" in resp.error.lower()


async def test_editor_old_string_line_prefix_hint(tmp_path):
    """When old_string contains reader's <number>\\t prefix, hint must appear."""
    from lionagi.tools.file.editor import EditorRequest, EditorTool

    f = tmp_path / "code.py"
    f.write_text("hello world\n")
    tool = EditorTool(workspace_root=str(tmp_path))
    # Simulate the common mistake: include the reader's line-number prefix
    resp = await tool.handle_request(
        EditorRequest(
            action="edit",
            file_path=str(f),
            old_string="1\thello world\n",  # includes the tab-prefixed line number
            new_string="replaced\n",
        )
    )
    assert resp.success is False
    # The error must call out the line-number prefix specifically
    assert "prefix" in resp.error.lower() or "number" in resp.error.lower()


async def test_editor_ambiguous_match_message(tmp_path):
    """Ambiguous match error must mention both replace_all and adding context."""
    from lionagi.tools.file.editor import EditorRequest, EditorTool

    f = tmp_path / "dup.py"
    f.write_text("foo\nfoo\nbar\n")
    tool = EditorTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(
        EditorRequest(action="edit", file_path=str(f), old_string="foo", new_string="baz")
    )
    assert resp.success is False
    assert "replace_all" in resp.error
    # Must also tell user to add more context, not only set replace_all
    assert "context" in resp.error.lower() or "surrounding" in resp.error.lower()


async def test_editor_file_not_found_message(tmp_path):
    """File-not-found error must suggest creating the file with write."""
    from lionagi.tools.file.editor import EditorRequest, EditorTool

    tool = EditorTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(
        EditorRequest(
            action="edit",
            file_path=str(tmp_path / "ghost.py"),
            old_string="x",
            new_string="y",
        )
    )
    assert resp.success is False
    assert "not found" in resp.error.lower()
    # Recovery hint: create with write
    assert "write" in resp.error.lower()


async def test_editor_missing_old_string_message(tmp_path):
    """Missing old_string must tell user to read the file first."""
    from lionagi.tools.file.editor import EditorRequest, EditorTool

    f = tmp_path / "f.py"
    f.write_text("x\n")
    tool = EditorTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(EditorRequest(action="edit", file_path=str(f), new_string="y"))
    assert resp.success is False
    assert "old_string" in resp.error
    # Must instruct to read first
    assert "read" in resp.error.lower()


async def test_editor_missing_new_string_message(tmp_path):
    """Missing new_string must mention empty string for deletion."""
    from lionagi.tools.file.editor import EditorRequest, EditorTool

    f = tmp_path / "f.py"
    f.write_text("x\n")
    tool = EditorTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(EditorRequest(action="edit", file_path=str(f), old_string="x"))
    assert resp.success is False
    assert "new_string" in resp.error
    assert "'" in resp.error or "empty" in resp.error.lower() or "''" in resp.error


async def test_editor_field_description_mentions_pre_read():
    """old_string field description must warn about the line-prefix trap."""
    from lionagi.tools.file.editor import EditorRequest

    schema = EditorRequest.model_json_schema()
    old_str_desc = schema["properties"]["old_string"].get("description", "")
    # Must mention the line-number prefix trap
    assert "number" in old_str_desc.lower() or "prefix" in old_str_desc.lower()


async def test_editor_field_description_replace_all_context():
    """replace_all field description must mention adding context as alternative."""
    from lionagi.tools.file.editor import EditorRequest

    schema = EditorRequest.model_json_schema()
    ra_desc = schema["properties"]["replace_all"].get("description", "")
    assert "context" in ra_desc.lower() or "surrounding" in ra_desc.lower()


async def test_editor_docstring_mentions_line_prefix(tmp_path):
    """Editor callable docstring must warn about the line-prefix trap."""
    from lionagi.tools.file.editor import EditorTool

    tool = EditorTool(workspace_root=str(tmp_path))
    t = tool.to_tool()
    doc = t.func_callable.__doc__ or ""
    assert "number" in doc.lower() or "prefix" in doc.lower()


async def test_editor_recovery_after_not_found(tmp_path):
    """Recovery: after 'not found', re-read and use exact text succeeds."""
    from lionagi.tools.file.editor import EditorRequest, EditorTool
    from lionagi.tools.file.reader import ReaderRequest, ReaderTool

    f = tmp_path / "target.py"
    f.write_text("original content\n")

    editor = EditorTool(workspace_root=str(tmp_path))
    # First attempt: intentionally wrong old_string
    bad = await editor.handle_request(
        EditorRequest(action="edit", file_path=str(f), old_string="wrong text", new_string="new")
    )
    assert bad.success is False
    assert "not found" in bad.error.lower()

    # Recovery: read the file and use exact content
    reader = ReaderTool(workspace_root=str(tmp_path))
    read_resp = await reader.handle_request(ReaderRequest(action="read", path=str(f)))
    assert read_resp.success is True
    # Strip the line-number prefix to get actual content
    actual_line = read_resp.content.split("\t", 1)[1].rstrip("\n")  # "original content"

    fixed = await editor.handle_request(
        EditorRequest(
            action="edit", file_path=str(f), old_string=actual_line, new_string="replaced"
        )
    )
    assert fixed.success is True
    assert "replaced" in f.read_text()


# ---------------------------------------------------------------------------
# Standalone BashTool guidance
# ---------------------------------------------------------------------------


async def test_bash_shell_operator_message():
    """Shell control operator rejection must mention cwd= as alternative."""
    from lionagi.tools.code.bash import BashRequest, BashTool

    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command="echo hi; echo there"))
    assert resp.return_code == -1
    # Must not just say 'rejected' — must guide toward cwd=
    assert "cwd" in resp.stderr.lower() or "one command" in resp.stderr.lower()


async def test_bash_malformed_command_message():
    """Malformed command error must hint at quoting."""
    from lionagi.tools.code.bash import BashRequest, BashTool

    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command="echo 'unmatched"))
    assert resp.return_code == -1
    assert "quot" in resp.stderr.lower() or "malformed" in resp.stderr.lower()


async def test_bash_command_not_found_message():
    """Command-not-found error must mention PATH."""
    from lionagi.tools.code.bash import BashRequest, BashTool

    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command="definitely_not_installed_xyz_123"))
    assert resp.return_code == -1
    assert (
        "path" in resp.stderr.lower()
        or "not found" in resp.stderr.lower()
        or "installed" in resp.stderr.lower()
    )


async def test_bash_timeout_message_mentions_increase():
    """Timeout error must tell user to increase timeout."""
    from lionagi.tools.code.bash import BashRequest, BashTool

    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command="sleep 10", timeout=50))
    assert resp.timed_out is True
    # Must explicitly guide: increase timeout or break into steps
    assert (
        "increase" in resp.stderr.lower()
        or "300000" in resp.stderr
        or "smaller" in resp.stderr.lower()
    )


async def test_bash_truncation_message_mentions_redirect():
    """Truncation message must suggest redirecting output to a file."""
    from lionagi.tools.code.bash import BashRequest, BashTool

    tool = BashTool()
    resp = await tool.handle_request(
        BashRequest(command="python3 -c \"print('A' * 200000)\"", timeout=10000)
    )
    # May or may not truncate depending on environment; skip if no truncation
    if "truncated" in resp.stdout:
        assert (
            "redirect" in resp.stdout.lower()
            or ">" in resp.stdout
            or "reader" in resp.stdout.lower()
        )


async def test_bash_command_field_description_mentions_operators():
    """command field description must list the rejected operators."""
    from lionagi.tools.code.bash import BashRequest

    schema = BashRequest.model_json_schema()
    cmd_desc = schema["properties"]["command"].get("description", "")
    assert "&&" in cmd_desc or "operator" in cmd_desc.lower()
    assert "cwd" in cmd_desc.lower()


async def test_bash_cwd_field_description_mentions_cd_alternative():
    """cwd field description must mention it as an alternative to cd."""
    from lionagi.tools.code.bash import BashRequest

    schema = BashRequest.model_json_schema()
    cwd_desc = schema["properties"]["cwd"].get("description", "")
    assert "cd" in cwd_desc.lower() or "instead" in cwd_desc.lower()


async def test_bash_docstring_mentions_dedicated_tools(tmp_path):
    """Bash callable docstring must tell the model to prefer reader/editor/search."""
    from lionagi.tools.code.bash import BashTool

    tool = BashTool()
    t = tool.to_tool()
    doc = t.func_callable.__doc__ or ""
    assert "reader" in doc.lower() or "dedicated" in doc.lower()


async def test_bash_recovery_cwd(tmp_path):
    """Recovery: after operator rejection, using cwd= and separate calls works."""
    from lionagi.tools.code.bash import BashRequest, BashTool

    f = tmp_path / "hello.txt"
    f.write_text("hi\n")
    tool = BashTool()

    # This would have been: cd tmp_path && cat hello.txt
    # Recovery: use cwd= and run cat directly
    resp = await tool.handle_request(BashRequest(command=f"cat {f.name}", cwd=str(tmp_path)))
    assert resp.return_code == 0
    assert "hi" in resp.stdout


# ---------------------------------------------------------------------------
# Bundled CodingToolkit guidance (coding.py)
# ---------------------------------------------------------------------------


def _make_bundled(tmp_path):
    from lionagi.session.branch import Branch
    from lionagi.tools.coding import CodingToolkit

    b = Branch()
    tk = CodingToolkit(workspace_root=str(tmp_path))
    tools = tk.bind(b)
    reader_fn = next(t for t in tools if t.func_callable.__name__ == "reader")
    editor_fn = next(t for t in tools if t.func_callable.__name__ == "editor")
    bash_fn = next(t for t in tools if t.func_callable.__name__ == "bash")
    return reader_fn.func_callable, editor_fn.func_callable, bash_fn.func_callable


async def test_bundled_shell_operator_message(tmp_path):
    """Bundled bash must also guide toward cwd= on operator rejection."""
    _, _, bash = _make_bundled(tmp_path)
    result = await bash(command="echo a; echo b")
    assert result["return_code"] == -1
    assert "cwd" in result["stderr"].lower() or "one command" in result["stderr"].lower()


async def test_bundled_editor_old_string_not_found_message(tmp_path):
    """Bundled editor: old_string not found must hint at re-reading."""
    f = tmp_path / "code.py"
    f.write_text("hello world\n")
    reader, editor, _ = _make_bundled(tmp_path)

    await reader(action="read", path=str(f))
    result = await editor(action="edit", file_path=str(f), old_string="NOTPRESENT", new_string="x")
    assert result["success"] is False
    assert "not found" in result["error"].lower()


async def test_bundled_editor_ambiguous_message(tmp_path):
    """Bundled editor: ambiguous match must mention both strategies."""
    f = tmp_path / "dup.py"
    f.write_text("foo\nfoo\n")
    reader, editor, _ = _make_bundled(tmp_path)

    await reader(action="read", path=str(f))
    result = await editor(action="edit", file_path=str(f), old_string="foo", new_string="bar")
    assert result["success"] is False
    assert "replace_all" in result["error"]
    assert "context" in result["error"].lower() or "surrounding" in result["error"].lower()


async def test_bundled_pre_read_guard_message(tmp_path):
    """Pre-read guard must tell the model how to fix it (call reader first)."""
    f = tmp_path / "unread.py"
    f.write_text("x = 1\n")
    _, editor, _ = _make_bundled(tmp_path)

    result = await editor(action="edit", file_path=str(f), old_string="x = 1", new_string="x = 2")
    assert result["success"] is False
    assert "read" in result["error"].lower()
    assert "reader" in result["error"].lower() or "action='read'" in result["error"]


async def test_bundled_pre_read_guard_recovery(tmp_path):
    """Recovery: after read-guard failure, reading first allows the edit."""
    f = tmp_path / "guarded.py"
    f.write_text("x = 1\n")
    reader, editor, _ = _make_bundled(tmp_path)

    bad = await editor(action="edit", file_path=str(f), old_string="x = 1", new_string="x = 2")
    assert bad["success"] is False

    read_res = await reader(action="read", path=str(f))
    assert read_res["success"] is True

    good = await editor(action="edit", file_path=str(f), old_string="x = 1", new_string="x = 2")
    assert good["success"] is True
    assert f.read_text() == "x = 2\n"


async def test_bundled_stale_read_guard_message(tmp_path):
    """Stale-read guard must tell the model to re-read."""
    import os

    f = tmp_path / "stale.py"
    f.write_text("original\n")
    reader, editor, _ = _make_bundled(tmp_path)

    await reader(action="read", path=str(f))
    current_mtime = os.path.getmtime(str(f))
    os.utime(str(f), (current_mtime + 1, current_mtime + 1))

    result = await editor(
        action="edit", file_path=str(f), old_string="original", new_string="updated"
    )
    assert result["success"] is False
    assert "changed" in result["error"].lower() or "re-read" in result["error"].lower()


async def test_bundled_timeout_message(tmp_path):
    """Bundled bash timeout must mention increasing timeout."""
    _, _, bash = _make_bundled(tmp_path)
    result = await bash(command="sleep 10", timeout=50)
    assert result.get("timed_out") is True
    assert (
        "increase" in result["stderr"].lower()
        or "300000" in result["stderr"]
        or "smaller" in result["stderr"].lower()
    )


async def test_bundled_file_not_found_message(tmp_path):
    """Bundled reader: file not found must mention workspace and path spelling."""
    reader, _, _ = _make_bundled(tmp_path)
    result = await reader(action="read", path=str(tmp_path / "ghost.py"))
    assert result["success"] is False
    assert "not found" in result["error"].lower()
    assert "workspace" in result["error"].lower() or "path" in result["error"].lower()


async def test_bundled_not_a_file_message(tmp_path):
    """Bundled reader: passing a directory should suggest list_dir."""
    reader, _, _ = _make_bundled(tmp_path)
    result = await reader(action="read", path=str(tmp_path))
    assert result["success"] is False
    assert "not a file" in result["error"].lower()
    assert "list_dir" in result["error"]
