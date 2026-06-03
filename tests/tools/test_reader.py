# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for standalone ReaderTool: read, list_dir, error messages, and guidance text."""

from __future__ import annotations

import asyncio
import time

import pytest

from lionagi.protocols.action.tool import Tool
from lionagi.tools.file.reader import (
    ReaderAction,
    ReaderRequest,
    ReaderResponse,
    ReaderTool,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def test_reader_request_action_values():
    assert ReaderAction.read == "read"
    assert ReaderAction.list_dir == "list_dir"


def test_reader_request_read_construction():
    req = ReaderRequest(action="read", path="/tmp/f.py")
    assert req.action == ReaderAction.read
    assert req.path == "/tmp/f.py"
    assert req.offset is None
    assert req.limit is None


def test_reader_request_list_dir_construction():
    req = ReaderRequest(action="list_dir", path="/tmp", recursive=True)
    assert req.action == ReaderAction.list_dir
    assert req.recursive is True


def test_reader_response_construction():
    r = ReaderResponse(success=True, content="hello")
    assert r.success is True
    assert r.content == "hello"
    assert r.error is None

    e = ReaderResponse(success=False, error="oops")
    assert e.success is False
    assert e.error == "oops"


# ---------------------------------------------------------------------------
# Schema / Tool registration
# ---------------------------------------------------------------------------


def test_to_tool_returns_tool(tmp_path):
    tool = ReaderTool(workspace_root=str(tmp_path))
    t = tool.to_tool()
    assert isinstance(t, Tool)


def test_to_tool_cached(tmp_path):
    tool = ReaderTool(workspace_root=str(tmp_path))
    t1 = tool.to_tool()
    t2 = tool.to_tool()
    assert t1 is t2


def test_tool_schema_has_line_prefix_guidance(tmp_path):
    """Schema description for 'action' must mention the line-number prefix."""
    tool = ReaderTool(workspace_root=str(tmp_path))
    t = tool.to_tool()
    schema = t.tool_schema
    schema_str = str(schema)
    assert "number" in schema_str.lower() or "prefix" in schema_str.lower()


def test_tool_schema_has_partial_read_guidance(tmp_path):
    """Schema description must mention offset+limit for large files."""
    tool = ReaderTool(workspace_root=str(tmp_path))
    t = tool.to_tool()
    schema_str = str(t.tool_schema)
    assert "offset" in schema_str.lower() or "limit" in schema_str.lower()


# ---------------------------------------------------------------------------
# Read: success paths
# ---------------------------------------------------------------------------


async def test_read_file_returns_line_numbers(tmp_path):
    f = tmp_path / "hello.py"
    f.write_text("a\nb\nc\n")
    tool = ReaderTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(ReaderRequest(action="read", path=str(f)))
    assert resp.success is True
    assert "1\t" in resp.content
    assert "2\t" in resp.content
    assert "3\t" in resp.content


async def test_read_file_offset_and_limit(tmp_path):
    f = tmp_path / "lines.py"
    f.write_text("".join(f"line{i}\n" for i in range(10)))
    tool = ReaderTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(ReaderRequest(action="read", path=str(f), offset=3, limit=2))
    assert resp.success is True
    assert "line3" in resp.content
    assert "line4" in resp.content
    assert "line0" not in resp.content


async def test_read_accepts_dict_input(tmp_path):
    f = tmp_path / "d.py"
    f.write_text("ok\n")
    tool = ReaderTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request({"action": "read", "path": str(f)})
    assert isinstance(resp, ReaderResponse)
    assert resp.success is True


# ---------------------------------------------------------------------------
# Read: error messages with recovery guidance
# ---------------------------------------------------------------------------


async def test_read_file_not_found_message(tmp_path):
    tool = ReaderTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(ReaderRequest(action="read", path=str(tmp_path / "ghost.py")))
    assert resp.success is False
    assert "not found" in resp.error.lower()
    # Recovery hint: mention workspace or path spelling
    assert "workspace" in resp.error.lower() or "path" in resp.error.lower()


async def test_read_directory_error_message(tmp_path):
    """Passing a directory to action='read' should suggest list_dir."""
    tool = ReaderTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(ReaderRequest(action="read", path=str(tmp_path)))
    assert resp.success is False
    assert "not a file" in resp.error.lower()
    assert "list_dir" in resp.error


async def test_read_binary_file_error_message(tmp_path):
    """Binary files should include a recovery hint."""
    b = tmp_path / "data.bin"
    b.write_bytes(b"\x00\x01\x02\x03")
    tool = ReaderTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(ReaderRequest(action="read", path=str(b)))
    assert resp.success is False
    assert "binary" in resp.error.lower()
    # Should suggest bash for binary inspection
    assert "bash" in resp.error.lower()


async def test_read_missing_path_message(tmp_path):
    tool = ReaderTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(ReaderRequest(action="read", path=None))
    assert resp.success is False
    assert "path" in resp.error.lower()
    # Should name which actions need a path
    assert "read" in resp.error.lower() or "list_dir" in resp.error.lower()


async def test_read_symlink_error_message(tmp_path):
    real = tmp_path / "real.py"
    real.write_text("real\n")
    link = tmp_path / "link.py"
    link.symlink_to(real)
    tool = ReaderTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(ReaderRequest(action="read", path=str(link)))
    assert resp.success is False
    assert "symlink" in resp.error.lower()
    # Recovery hint: use the real file
    assert "workspace" in resp.error.lower() or "real" in resp.error.lower()


# ---------------------------------------------------------------------------
# Read: recovery paths work
# ---------------------------------------------------------------------------


async def test_read_recovery_after_not_found(tmp_path):
    """After 'file not found', creating the file and retrying succeeds."""
    tool = ReaderTool(workspace_root=str(tmp_path))
    missing = tmp_path / "later.py"
    resp1 = await tool.handle_request(ReaderRequest(action="read", path=str(missing)))
    assert resp1.success is False
    assert "not found" in resp1.error.lower()

    missing.write_text("content\n")
    resp2 = await tool.handle_request(ReaderRequest(action="read", path=str(missing)))
    assert resp2.success is True
    assert "content" in resp2.content


# ---------------------------------------------------------------------------
# list_dir: success and error messages
# ---------------------------------------------------------------------------


async def test_list_dir_basic(tmp_path):
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    tool = ReaderTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(ReaderRequest(action="list_dir", path=str(tmp_path)))
    assert resp.success is True
    assert "a.py" in resp.content
    assert "b.py" in resp.content


async def test_list_dir_file_path_error_message(tmp_path):
    """Passing a file path to list_dir should suggest read."""
    f = tmp_path / "file.py"
    f.write_text("")
    tool = ReaderTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(ReaderRequest(action="list_dir", path=str(f)))
    assert resp.success is False
    assert "not a directory" in resp.error.lower()
    # Recovery hint: use read instead
    assert "read" in resp.error.lower()


# ---------------------------------------------------------------------------
# Docstring includes guidance
# ---------------------------------------------------------------------------


def test_reader_tool_docstring_mentions_line_prefix(tmp_path):
    """The callable docstring should mention stripping the number\\t prefix."""
    tool = ReaderTool(workspace_root=str(tmp_path))
    t = tool.to_tool()
    doc = t.func_callable.__doc__ or ""
    assert "number" in doc.lower() or "prefix" in doc.lower()


def test_reader_tool_docstring_mentions_partial_read(tmp_path):
    """The callable docstring should mention offset+limit for large files."""
    tool = ReaderTool(workspace_root=str(tmp_path))
    t = tool.to_tool()
    doc = t.func_callable.__doc__ or ""
    assert "offset" in doc.lower() or "limit" in doc.lower()


# ---------------------------------------------------------------------------
# Field description content
# ---------------------------------------------------------------------------


def test_action_field_description_mentions_line_prefix():
    schema = ReaderRequest.model_json_schema()
    desc = schema["properties"]["action"].get("description", "")
    assert "number" in desc.lower() or "prefix" in desc.lower()


def test_action_field_description_mentions_partial_read():
    schema = ReaderRequest.model_json_schema()
    desc = schema["properties"]["action"].get("description", "")
    assert "offset" in desc.lower() or "limit" in desc.lower()
