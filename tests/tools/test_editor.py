# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for EditorTool: write, edit, and security checks (symlink, path-escape, denied names)."""

from lionagi.tools.file.editor import (
    EditorAction,
    EditorRequest,
    EditorResponse,
    EditorTool,
)

# ---------------------------------------------------------------------------
# EditorAction enum
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Write: basic
# ---------------------------------------------------------------------------


async def test_write_new_file(tmp_path):
    tool = EditorTool(workspace_root=str(tmp_path))
    target = tmp_path / "new.py"
    resp = await tool.handle_request(
        EditorRequest(action="write", file_path=str(target), content="print('hello')\n")
    )
    assert resp.success is True
    assert target.read_text() == "print('hello')\n"


async def test_write_creates_parent_dirs(tmp_path):
    tool = EditorTool(workspace_root=str(tmp_path))
    target = tmp_path / "sub" / "deep" / "file.py"
    resp = await tool.handle_request(
        EditorRequest(action="write", file_path=str(target), content="x = 1\n")
    )
    assert resp.success is True
    assert target.exists()
    assert target.read_text() == "x = 1\n"


async def test_write_missing_content_fails(tmp_path):
    tool = EditorTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(
        EditorRequest(action="write", file_path=str(tmp_path / "f.py"))
    )
    assert resp.success is False
    assert "content" in resp.error.lower()


async def test_write_dict_input_accepted(tmp_path):
    tool = EditorTool(workspace_root=str(tmp_path))
    target = tmp_path / "dict.py"
    resp = await tool.handle_request(
        {
            "action": "write",
            "file_path": str(target),
            "content": "pass\n",
        }
    )
    assert resp.success is True
    assert target.read_text() == "pass\n"


# ---------------------------------------------------------------------------
# Edit: basic
# ---------------------------------------------------------------------------


async def test_edit_replaces_string(tmp_path):
    target = tmp_path / "code.py"
    target.write_text("hello world\n")
    tool = EditorTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(
        EditorRequest(
            action="edit",
            file_path=str(target),
            old_string="hello",
            new_string="goodbye",
        )
    )
    assert resp.success is True
    assert "goodbye" in target.read_text()
    assert "hello" not in target.read_text()


async def test_edit_old_string_not_found(tmp_path):
    target = tmp_path / "code.py"
    target.write_text("nothing here\n")
    tool = EditorTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(
        EditorRequest(
            action="edit",
            file_path=str(target),
            old_string="NOTPRESENT",
            new_string="x",
        )
    )
    assert resp.success is False
    assert "not found" in resp.error.lower()


async def test_edit_ambiguous_without_replace_all(tmp_path):
    target = tmp_path / "dup.py"
    target.write_text("foo\nfoo\nbar\n")
    tool = EditorTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(
        EditorRequest(
            action="edit",
            file_path=str(target),
            old_string="foo",
            new_string="baz",
            replace_all=False,
        )
    )
    assert resp.success is False
    assert "2" in resp.error or "times" in resp.error


async def test_edit_replace_all_replaces_all_occurrences(tmp_path):
    target = tmp_path / "dup2.py"
    target.write_text("foo\nfoo\nbar\n")
    tool = EditorTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(
        EditorRequest(
            action="edit",
            file_path=str(target),
            old_string="foo",
            new_string="baz",
            replace_all=True,
        )
    )
    assert resp.success is True
    assert target.read_text().count("baz") == 2
    assert "foo" not in target.read_text()


async def test_edit_missing_file_fails(tmp_path):
    tool = EditorTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(
        EditorRequest(
            action="edit",
            file_path=str(tmp_path / "nonexistent.py"),
            old_string="x",
            new_string="y",
        )
    )
    assert resp.success is False
    assert "not found" in resp.error.lower() or "file" in resp.error.lower()


async def test_edit_missing_old_string_field_fails(tmp_path):
    target = tmp_path / "f.py"
    target.write_text("x\n")
    tool = EditorTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(
        EditorRequest(action="edit", file_path=str(target), new_string="y")
    )
    assert resp.success is False
    assert "old_string" in resp.error


async def test_edit_missing_new_string_field_fails(tmp_path):
    target = tmp_path / "f.py"
    target.write_text("x\n")
    tool = EditorTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(
        EditorRequest(action="edit", file_path=str(target), old_string="x")
    )
    assert resp.success is False
    assert "new_string" in resp.error


# ---------------------------------------------------------------------------
# Security: path escape
# ---------------------------------------------------------------------------


async def test_write_relative_path_escape_rejected(tmp_path):
    tool = EditorTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(
        EditorRequest(action="write", file_path="../escape.txt", content="bad")
    )
    assert resp.success is False
    assert "escape" in resp.error.lower() or "workspace" in resp.error.lower()


async def test_write_absolute_path_outside_workspace_rejected(tmp_path):
    tool = EditorTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(
        EditorRequest(action="write", file_path="/etc/passwd", content="bad")
    )
    assert resp.success is False
    assert "escape" in resp.error.lower() or "workspace" in resp.error.lower()


async def test_edit_path_escape_rejected(tmp_path):
    tool = EditorTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(
        EditorRequest(
            action="edit",
            file_path="../outside.py",
            old_string="x",
            new_string="y",
        )
    )
    assert resp.success is False
    assert "escape" in resp.error.lower() or "workspace" in resp.error.lower()


# ---------------------------------------------------------------------------
# Security: symlink rejection
# ---------------------------------------------------------------------------


async def test_write_symlink_rejected(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("target content\n")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    tool = EditorTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(
        EditorRequest(action="write", file_path=str(link), content="overwrite attempt")
    )
    assert resp.success is False
    assert "symlink" in resp.error.lower()
    # Original target must be unchanged
    assert real.read_text() == "target content\n"


async def test_edit_symlink_rejected(tmp_path):
    real = tmp_path / "real_edit.txt"
    real.write_text("hello world\n")
    link = tmp_path / "link_edit.txt"
    link.symlink_to(real)
    tool = EditorTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(
        EditorRequest(
            action="edit",
            file_path=str(link),
            old_string="hello",
            new_string="goodbye",
        )
    )
    assert resp.success is False
    assert "symlink" in resp.error.lower()
    # Original target must be unchanged
    assert real.read_text() == "hello world\n"


# ---------------------------------------------------------------------------
# Security: denied filenames
# ---------------------------------------------------------------------------


async def test_write_denied_env_file(tmp_path):
    tool = EditorTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(
        EditorRequest(
            action="write",
            file_path=str(tmp_path / ".env"),
            content="SECRET=123\n",
        )
    )
    assert resp.success is False
    assert "protected" in resp.error.lower() or ".env" in resp.error


async def test_write_denied_id_rsa(tmp_path):
    tool = EditorTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(
        EditorRequest(
            action="write",
            file_path=str(tmp_path / "id_rsa"),
            content="-----BEGIN RSA PRIVATE KEY-----\n",
        )
    )
    assert resp.success is False
    assert "protected" in resp.error.lower() or "id_rsa" in resp.error


async def test_write_denied_netrc(tmp_path):
    tool = EditorTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(
        EditorRequest(
            action="write",
            file_path=str(tmp_path / ".netrc"),
            content="machine example.com\n",
        )
    )
    assert resp.success is False
    assert "protected" in resp.error.lower() or ".netrc" in resp.error


async def test_write_denied_htpasswd(tmp_path):
    tool = EditorTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(
        EditorRequest(
            action="write",
            file_path=str(tmp_path / ".htpasswd"),
            content="user:hash\n",
        )
    )
    assert resp.success is False
    assert "protected" in resp.error.lower() or ".htpasswd" in resp.error


async def test_edit_denied_filename_blocked(tmp_path):
    # Create the file directly (bypassing the tool) to test edit path
    target = tmp_path / ".env"
    target.write_text("SECRET=old\n")
    tool = EditorTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(
        EditorRequest(
            action="edit",
            file_path=str(target),
            old_string="old",
            new_string="new",
        )
    )
    assert resp.success is False
    assert "protected" in resp.error.lower() or ".env" in resp.error


# ---------------------------------------------------------------------------
# to_tool
# ---------------------------------------------------------------------------


async def test_to_tool_callable_executes(tmp_path):
    tool = EditorTool(workspace_root=str(tmp_path))
    target = tmp_path / "via_tool.py"
    result = await tool.to_tool().func_callable(
        action="write", file_path=str(target), content="via tool\n"
    )
    assert result["success"] is True
    assert target.read_text() == "via tool\n"
