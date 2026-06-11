# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for guidance strings and recovery-hint error messages in reader/editor/bash tools."""

import inspect

import pytest

from lionagi.tools.code.bash import BashRequest, BashTool
from lionagi.tools.file.editor import EditorRequest, EditorTool, _edit_sync
from lionagi.tools.file.reader import ReaderRequest, ReaderTool

# ---------------------------------------------------------------------------
# Reader: field descriptions contain the line-prefix trap warning
# ---------------------------------------------------------------------------


def test_reader_action_description_warns_line_prefix():
    schema = ReaderRequest.model_json_schema()
    action_desc = schema["properties"]["action"]["description"]
    assert (
        "\\t" in action_desc or "tab" in action_desc.lower() or "line-number" in action_desc.lower()
    )


def test_reader_action_description_mentions_offset_limit():
    schema = ReaderRequest.model_json_schema()
    action_desc = schema["properties"]["action"]["description"]
    assert "offset" in action_desc or "limit" in action_desc


def test_reader_path_description_mentions_workspace():
    schema = ReaderRequest.model_json_schema()
    path_desc = schema["properties"]["path"]["description"]
    assert "workspace" in path_desc.lower()


def test_reader_offset_description_has_example():
    schema = ReaderRequest.model_json_schema()
    offset_desc = schema["properties"]["offset"]["description"]
    # Should give a concrete example of windowed reads
    assert "200" in offset_desc or "offset" in offset_desc.lower()


# ---------------------------------------------------------------------------
# Reader: tool docstring mentions line-prefix
# ---------------------------------------------------------------------------


def test_reader_tool_docstring_mentions_line_prefix():
    rt = ReaderTool()
    tool = rt.to_tool()
    doc = inspect.getdoc(tool.func_callable) or ""
    assert "\\t" in doc or "prefix" in doc.lower() or "number" in doc.lower()


# ---------------------------------------------------------------------------
# Reader: error messages contain recovery hints
# ---------------------------------------------------------------------------


async def test_reader_not_found_error_has_hint(tmp_path):
    rt = ReaderTool(workspace_root=str(tmp_path))
    resp = await rt.handle_request(ReaderRequest(action="read", path=str(tmp_path / "missing.py")))
    assert not resp.success
    assert "workspace" in resp.error.lower() or "path" in resp.error.lower()


async def test_reader_not_a_file_suggests_list_dir(tmp_path):
    rt = ReaderTool(workspace_root=str(tmp_path))
    resp = await rt.handle_request(ReaderRequest(action="read", path=str(tmp_path)))
    assert not resp.success
    assert "list_dir" in resp.error


async def test_reader_binary_suggests_bash(tmp_path):
    f = tmp_path / "bin.bin"
    f.write_bytes(b"\x00\x01\x02\x03")
    rt = ReaderTool(workspace_root=str(tmp_path))
    resp = await rt.handle_request(ReaderRequest(action="read", path=str(f)))
    assert not resp.success
    assert "bash" in resp.error.lower()


async def test_reader_list_dir_not_dir_suggests_read(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("hello")
    rt = ReaderTool(workspace_root=str(tmp_path))
    resp = await rt.handle_request(ReaderRequest(action="list_dir", path=str(f)))
    assert not resp.success
    assert "read" in resp.error.lower()


# ---------------------------------------------------------------------------
# Editor: field descriptions contain line-prefix trap warning
# ---------------------------------------------------------------------------


def test_editor_old_string_description_warns_line_prefix():
    schema = EditorRequest.model_json_schema()
    desc = schema["properties"]["old_string"]["description"]
    assert "line-number" in desc.lower() or "\\t" in desc or "prefix" in desc.lower()


def test_editor_replace_all_description_mentions_context():
    schema = EditorRequest.model_json_schema()
    desc = schema["properties"]["replace_all"]["description"]
    assert "context" in desc.lower() or "surrounding" in desc.lower()


def test_editor_action_description_mentions_read_first():
    schema = EditorRequest.model_json_schema()
    desc = schema["properties"]["action"]["description"]
    assert "read" in desc.lower()


# ---------------------------------------------------------------------------
# Editor: tool docstring has recovery guidance
# ---------------------------------------------------------------------------


def test_editor_tool_docstring_has_not_found_hint():
    et = EditorTool()
    tool = et.to_tool()
    doc = inspect.getdoc(tool.func_callable) or ""
    assert "not found" in doc.lower() or "re-read" in doc.lower()


def test_editor_tool_docstring_mentions_line_prefix():
    et = EditorTool()
    tool = et.to_tool()
    doc = inspect.getdoc(tool.func_callable) or ""
    assert "prefix" in doc.lower() or "\\t" in doc or "number" in doc.lower()


# ---------------------------------------------------------------------------
# Editor: error messages contain recovery hints
# ---------------------------------------------------------------------------


async def test_editor_not_found_error_contains_reread(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("x = 1\n")
    tool = EditorTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(
        EditorRequest(action="edit", file_path=str(f), old_string="z = 99\n", new_string="")
    )
    assert not resp.success
    # Should tell agent to re-read
    assert "re-read" in resp.error.lower() or "read" in resp.error.lower()


async def test_editor_not_found_detects_line_prefix_hint(tmp_path):
    """old_string with line-number prefix should get a specific hint."""
    f = tmp_path / "code.py"
    f.write_text("x = 1\n")
    tool = EditorTool(workspace_root=str(tmp_path))
    # Include the line-number prefix the reader would produce
    resp = await tool.handle_request(
        EditorRequest(action="edit", file_path=str(f), old_string="1\tx = 1\n", new_string="")
    )
    assert not resp.success
    # Should detect that stripping the prefix would match
    assert (
        "line-number" in resp.error.lower() or "prefix" in resp.error.lower() or "\\t" in resp.error
    )


async def test_editor_ambiguous_error_suggests_context(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("x = 1\nx = 1\n")
    tool = EditorTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(
        EditorRequest(action="edit", file_path=str(f), old_string="x = 1\n", new_string="y = 2\n")
    )
    assert not resp.success
    assert "context" in resp.error.lower() or "replace_all" in resp.error.lower()


async def test_editor_file_not_found_suggests_write(tmp_path):
    tool = EditorTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(
        EditorRequest(
            action="edit",
            file_path=str(tmp_path / "nonexistent.py"),
            old_string="x",
            new_string="y",
        )
    )
    assert not resp.success
    assert "write" in resp.error.lower() or "create" in resp.error.lower()


async def test_editor_missing_old_string_error_guides(tmp_path):
    tool = EditorTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(EditorRequest(action="edit", file_path=str(tmp_path / "f.py")))
    assert not resp.success
    assert "old_string" in resp.error.lower() or "read" in resp.error.lower()


async def test_editor_missing_new_string_error_guides(tmp_path):
    f = tmp_path / "f.py"
    f.write_text("x = 1\n")
    tool = EditorTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(
        EditorRequest(action="edit", file_path=str(f), old_string="x = 1\n")
    )
    assert not resp.success
    assert "new_string" in resp.error.lower() or "replacement" in resp.error.lower()


# ---------------------------------------------------------------------------
# Bash: field descriptions have cwd guidance
# ---------------------------------------------------------------------------


def test_bash_command_description_warns_operators():
    schema = BashRequest.model_json_schema()
    desc = schema["properties"]["command"]["description"]
    assert "&&" in desc or "operator" in desc.lower()
    assert "cwd" in desc or "one command" in desc.lower()


def test_bash_cwd_description_mentions_cd_alternative():
    schema = BashRequest.model_json_schema()
    desc = schema["properties"]["cwd"]["description"]
    assert "cd" in desc.lower() or "instead" in desc.lower()


def test_bash_timeout_description_mentions_increase():
    schema = BashRequest.model_json_schema()
    desc = schema["properties"]["timeout"]["description"]
    assert "increase" in desc.lower() or "long" in desc.lower()


# ---------------------------------------------------------------------------
# Bash: tool docstring has recovery hints
# ---------------------------------------------------------------------------


def test_bash_tool_docstring_has_recovery_section():
    bt = BashTool()
    tool = bt.to_tool()
    doc = inspect.getdoc(tool.func_callable) or ""
    assert "recovery" in doc.lower() or "hint" in doc.lower() or "not found" in doc.lower()


def test_bash_tool_docstring_mentions_cwd_alternative():
    bt = BashTool()
    tool = bt.to_tool()
    doc = inspect.getdoc(tool.func_callable) or ""
    assert "cwd" in doc.lower()


# ---------------------------------------------------------------------------
# Bash: operator-rejection error mentions cwd
# ---------------------------------------------------------------------------


async def test_bash_operator_rejection_message_suggests_cwd():
    bt = BashTool()
    from lionagi.tools.code.bash import BashRequest

    resp = await bt.handle_request(BashRequest(command="cd /tmp && ls"))
    assert resp.return_code == -1
    assert "cwd" in resp.stderr.lower() or "one command" in resp.stderr.lower()
