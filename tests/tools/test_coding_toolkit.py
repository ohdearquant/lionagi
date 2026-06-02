# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for CodingToolkit: bind, reader, editor, bash, search."""

import asyncio

import pytest

from lionagi.session.branch import Branch
from lionagi.tools.coding import CodingToolkit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_toolkit(tmp_path, notify=False):
    b = Branch()
    tk = CodingToolkit(notify=notify, workspace_root=str(tmp_path))
    tools = tk.bind(b)
    return b, tk, tools


def _tool_fn(tools, name):
    for t in tools:
        if t.func_callable.__name__ == name:
            return t.func_callable
    raise KeyError(f"tool '{name}' not found")


# ---------------------------------------------------------------------------
# Bind
# ---------------------------------------------------------------------------


def test_bind_returns_lean_default(tmp_path):
    _, _, tools = _make_toolkit(tmp_path)
    assert len(tools) == 4  # reader/editor/bash/search; extras are opt-in


def test_bind_all_tools_async(tmp_path):
    _, _, tools = _make_toolkit(tmp_path)
    non_async = [
        t.func_callable.__name__ for t in tools if not asyncio.iscoroutinefunction(t.func_callable)
    ]
    assert non_async == [], f"Non-async tools: {non_async}"


def test_bind_tool_names(tmp_path):
    """Default registers the lean core only — context/sandbox/subagent are opt-in."""
    _, _, tools = _make_toolkit(tmp_path)
    assert {t.func_callable.__name__ for t in tools} == {
        "reader",
        "editor",
        "bash",
        "search",
    }


def test_bind_tool_names_opt_in_extras(tmp_path):
    """Passing tools= opts into the extra capabilities (and validates names)."""
    from lionagi.tools.coding import ALL_CODING_TOOLS

    tk = CodingToolkit(notify=False, workspace_root=str(tmp_path), tools=ALL_CODING_TOOLS)
    assert {t.func_callable.__name__ for t in tk.bind(Branch())} == set(ALL_CODING_TOOLS)

    only = CodingToolkit(workspace_root=str(tmp_path), tools=["reader", "subagent"])
    assert {t.func_callable.__name__ for t in only.bind(Branch())} == {"reader", "subagent"}

    with pytest.raises(ValueError, match="unknown coding tool"):
        CodingToolkit(workspace_root=str(tmp_path), tools=["reader", "nope"])


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


async def test_reader_read_returns_numbered_lines(tmp_path):
    (tmp_path / "f.py").write_text("alpha\nbeta\ngamma\n")
    _, _, tools = _make_toolkit(tmp_path)
    result = await _tool_fn(tools, "reader")(action="read", path=str(tmp_path / "f.py"))
    assert result["success"] is True
    assert "1\talpha" in result["content"]
    assert "2\tbeta" in result["content"]


async def test_reader_list_dir(tmp_path):
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.py").write_text("y")
    _, _, tools = _make_toolkit(tmp_path)
    result = await _tool_fn(tools, "reader")(action="list_dir", path=str(tmp_path))
    assert result["success"] is True
    assert "a.py" in result["content"] or "b.py" in result["content"]


async def test_reader_binary_file_rejected(tmp_path):
    (tmp_path / "data.bin").write_bytes(b"\x00\x01\x02\x03")
    _, _, tools = _make_toolkit(tmp_path)
    result = await _tool_fn(tools, "reader")(action="read", path=str(tmp_path / "data.bin"))
    assert result["success"] is False
    assert "inary" in result["error"]


# ---------------------------------------------------------------------------
# Editor: write
# ---------------------------------------------------------------------------


async def test_editor_write_new_file(tmp_path):
    target = tmp_path / "new.py"
    _, _, tools = _make_toolkit(tmp_path)
    result = await _tool_fn(tools, "editor")(
        action="write", file_path=str(target), content="print('hi')\n"
    )
    assert result["success"] is True
    assert target.read_text() == "print('hi')\n"


async def test_editor_write_creates_parent_dirs(tmp_path):
    target = tmp_path / "sub" / "deep" / "file.py"
    _, _, tools = _make_toolkit(tmp_path)
    result = await _tool_fn(tools, "editor")(action="write", file_path=str(target), content="x=1\n")
    assert result["success"] is True and target.exists()


# ---------------------------------------------------------------------------
# Editor: read-before-write guard
# ---------------------------------------------------------------------------


async def test_editor_read_guard_blocks_unread_existing_file(tmp_path):
    (tmp_path / "existing.py").write_text("original\n")
    _, _, tools = _make_toolkit(tmp_path)
    result = await _tool_fn(tools, "editor")(
        action="edit",
        file_path=str(tmp_path / "existing.py"),
        old_string="original",
        new_string="replaced",
    )
    assert result["success"] is False
    assert "read" in result["error"].lower()


async def test_editor_edit_after_read_succeeds(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("hello world\n")
    _, _, tools = _make_toolkit(tmp_path)
    await _tool_fn(tools, "reader")(action="read", path=str(f))
    result = await _tool_fn(tools, "editor")(
        action="edit", file_path=str(f), old_string="hello", new_string="goodbye"
    )
    assert result["success"] is True
    assert "goodbye" in f.read_text()


async def test_editor_relative_write_existing_file_requires_prior_read(tmp_path):
    f = tmp_path / "existing.py"
    f.write_text("original\n")
    _, _, tools = _make_toolkit(tmp_path)

    result = await _tool_fn(tools, "editor")(
        action="write", file_path="existing.py", content="replaced\n"
    )

    assert result["success"] is False
    assert "read" in result["error"].lower()
    assert f.read_text() == "original\n"


async def test_editor_relative_edit_after_read_succeeds(tmp_path):
    f = tmp_path / "relative.py"
    f.write_text("hello world\n")
    _, _, tools = _make_toolkit(tmp_path)
    await _tool_fn(tools, "reader")(action="read", path="relative.py")

    result = await _tool_fn(tools, "editor")(
        action="edit", file_path="relative.py", old_string="hello", new_string="goodbye"
    )

    assert result["success"] is True
    assert f.read_text() == "goodbye world\n"


# ---------------------------------------------------------------------------
# Editor: multiple matches
# ---------------------------------------------------------------------------


async def test_editor_multiple_matches_fails_without_replace_all(tmp_path):
    f = tmp_path / "dup.py"
    f.write_text("foo\nfoo\nbar\n")
    _, _, tools = _make_toolkit(tmp_path)
    await _tool_fn(tools, "reader")(action="read", path=str(f))
    result = await _tool_fn(tools, "editor")(
        action="edit",
        file_path=str(f),
        old_string="foo",
        new_string="baz",
        replace_all=False,
    )
    assert result["success"] is False
    assert "2" in result["error"] or "times" in result["error"]


async def test_editor_multiple_matches_succeeds_with_replace_all(tmp_path):
    f = tmp_path / "dup2.py"
    f.write_text("foo\nfoo\nbar\n")
    _, _, tools = _make_toolkit(tmp_path)
    await _tool_fn(tools, "reader")(action="read", path=str(f))
    result = await _tool_fn(tools, "editor")(
        action="edit",
        file_path=str(f),
        old_string="foo",
        new_string="baz",
        replace_all=True,
    )
    assert result["success"] is True
    assert f.read_text().count("baz") == 2


# ---------------------------------------------------------------------------
# Bash
# ---------------------------------------------------------------------------


async def test_bash_echo_returns_stdout(tmp_path):
    _, _, tools = _make_toolkit(tmp_path)
    result = await _tool_fn(tools, "bash")(command="/bin/echo hello")
    assert result["return_code"] == 0 and "hello" in result["stdout"]


async def test_bash_timeout_handling(tmp_path):
    _, _, tools = _make_toolkit(tmp_path)
    result = await _tool_fn(tools, "bash")(command="sleep 10", timeout=100)
    assert result["timed_out"] is True and result["return_code"] == -1


async def test_bash_shell_control_rejected(tmp_path):
    _, _, tools = _make_toolkit(tmp_path)
    result = await _tool_fn(tools, "bash")(command="echo hi; echo there")
    assert result["return_code"] == -1
    assert "Shell control" in result["stderr"] or "rejected" in result["stderr"]


# ---------------------------------------------------------------------------
# Search: workspace containment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action,pattern",
    [
        ("grep", "SECRET"),
        ("find", "*.txt"),
    ],
)
async def test_search_rejects_path_outside_workspace(tmp_path, action, pattern):
    outside = tmp_path.parent / f"{tmp_path.name}_outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET\n")
    _, _, tools = _make_toolkit(tmp_path)

    result = await _tool_fn(tools, "search")(action=action, pattern=pattern, path=str(outside))

    assert result["success"] is False
    assert "escapes workspace" in result["error"]


# ---------------------------------------------------------------------------
# C5: reader rejects workspace escape
# ---------------------------------------------------------------------------


async def test_coding_toolkit_reader_rejects_workspace_escape(tmp_path):
    """ReaderTool rejects paths that escape the workspace root."""
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("TOP SECRET\n")

    _, _, tools = _make_toolkit(tmp_path)
    result = await _tool_fn(tools, "reader")(action="read", path=str(secret))

    assert result["success"] is False
    assert "escape" in result["error"].lower() or "workspace" in result["error"].lower()


# ---------------------------------------------------------------------------
# C6: editor reports ambiguous replacement without writing
# ---------------------------------------------------------------------------


async def test_coding_toolkit_editor_reports_ambiguous_replacement_without_writing(
    tmp_path,
):
    """edit with replace_all=False on a file with duplicate old_string returns failure."""
    target = tmp_path / "dup.py"
    target.write_text("a\na\n")

    _, _, tools = _make_toolkit(tmp_path)
    result = await _tool_fn(tools, "editor")(
        action="edit",
        file_path=str(target),
        old_string="a",
        new_string="b",
        replace_all=False,
    )

    assert result["success"] is False
    # File must be unchanged
    assert target.read_text() == "a\na\n"
