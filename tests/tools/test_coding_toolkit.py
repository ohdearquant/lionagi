# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for CodingToolkit: bind, reader, editor, bash, search."""

import asyncio
import inspect

import pytest
from pydantic import ValidationError

from lionagi.session.branch import Branch
from lionagi.tools.coding import CodingToolkit, SubagentRequest


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


def test_bind_returns_lean_default(tmp_path):
    _, _, tools = _make_toolkit(tmp_path)
    assert (
        len(tools) == 8
    )  # reader/editor/bash/search/code_check/code_nav/ast_search/context; sandbox/subagent are opt-in


def test_bind_all_tools_async(tmp_path):
    _, _, tools = _make_toolkit(tmp_path)
    non_async = [
        t.func_callable.__name__ for t in tools if not asyncio.iscoroutinefunction(t.func_callable)
    ]
    assert non_async == [], f"Non-async tools: {non_async}"


def test_bind_tool_names(tmp_path):
    """Default registers the lean core plus context — sandbox/subagent are opt-in."""
    _, _, tools = _make_toolkit(tmp_path)
    assert {t.func_callable.__name__ for t in tools} == {
        "reader",
        "editor",
        "bash",
        "search",
        "code_check",
        "code_nav",
        "ast_search",
        "context",
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


def test_subagent_request_exposes_only_resolvable_permission_presets():
    permissions = SubagentRequest.model_json_schema()["properties"]["permissions"]

    assert permissions["enum"] == ["read_only", "safe", "allow_all", "deny_all"]
    assert permissions["default"] == "read_only"


@pytest.mark.parametrize("preset", ["read_only", "safe", "allow_all", "deny_all"])
def test_subagent_request_accepts_each_resolvable_permission_preset(preset):
    request = SubagentRequest(instruction="inspect the repository", permissions=preset)

    assert request.permissions == preset


def test_subagent_request_rejects_unimplemented_permission_inheritance():
    with pytest.raises(ValidationError, match="permissions"):
        SubagentRequest(instruction="inspect the repository", permissions="inherit")


def test_bound_subagent_rejects_inheritance_before_callable_execution(tmp_path):
    branch = Branch()
    [tool] = CodingToolkit(workspace_root=str(tmp_path), tools=["subagent"]).bind(branch)
    branch.acts.register_tool(tool)

    assert inspect.signature(tool.func_callable).parameters["permissions"].default == "read_only"
    with pytest.raises(ValidationError, match="permissions"):
        branch.acts.match_tool(
            {
                "function": "subagent",
                "arguments": {
                    "instruction": "inspect the repository",
                    "permissions": "inherit",
                },
            }
        )


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


async def test_bash_runs_in_the_workspace_root_not_the_process_cwd(tmp_path, monkeypatch):
    """A command with no cwd= runs where the other tools read and write.

    The host process can be started from any directory; a command that ran
    there would test, build or inspect a different tree from the one the
    editor just changed.
    """
    workspace = tmp_path / "workspace"
    elsewhere = tmp_path / "elsewhere"
    workspace.mkdir()
    elsewhere.mkdir()
    _, _, tools = _make_toolkit(workspace)
    monkeypatch.chdir(elsewhere)

    result = await _tool_fn(tools, "bash")(command="/bin/pwd -P")

    assert result["return_code"] == 0
    assert result["stdout"].strip() == str(workspace.resolve())


async def test_bash_takes_a_relative_cwd_from_the_workspace_root(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "pkg").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "pkg").mkdir(parents=True)
    _, _, tools = _make_toolkit(workspace)
    monkeypatch.chdir(elsewhere)

    result = await _tool_fn(tools, "bash")(command="/bin/pwd -P", cwd="pkg")

    assert result["return_code"] == 0
    assert result["stdout"].strip() == str((workspace / "pkg").resolve())


async def test_bash_keeps_an_absolute_cwd(tmp_path):
    workspace = tmp_path / "workspace"
    other = tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    _, _, tools = _make_toolkit(workspace)

    result = await _tool_fn(tools, "bash")(command="/bin/pwd -P", cwd=str(other))

    assert result["return_code"] == 0
    assert result["stdout"].strip() == str(other.resolve())


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


async def test_search_reads_a_leading_dash_as_a_pattern_not_an_option(tmp_path):
    # As an option, "-f<file>" would read the patterns from a file outside the workspace.
    patterns = tmp_path.parent / f"{tmp_path.name}_patterns"
    patterns.write_text("SECRET\n")
    (tmp_path / "notes.txt").write_text("SECRET\n")
    _, _, tools = _make_toolkit(tmp_path)

    result = await _tool_fn(tools, "search")(action="grep", pattern=f"-f{patterns}", path=".")

    assert result["success"] is True
    assert result["total_matches"] == 0


async def test_search_leaves_out_files_the_reader_refuses_by_name(tmp_path):
    (tmp_path / ".env").write_text("TOKEN=one\n")
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys" / "ID_RSA").write_text("TOKEN=two\n")
    (tmp_path / "notes.txt").write_text("TOKEN=three\n")
    _, _, tools = _make_toolkit(tmp_path)

    result = await _tool_fn(tools, "search")(action="grep", pattern="TOKEN", path=".")

    assert result["success"] is True
    assert result["total_matches"] == 1
    assert result["content"] == f"{tmp_path / 'notes.txt'}:1:TOKEN=three"


async def test_search_does_not_follow_a_symlink_out_of_the_workspace(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "outside.txt").write_text("TOKEN=file\n")
    (tmp_path / "outdir").mkdir()
    (tmp_path / "outdir" / "f.txt").write_text("TOKEN=dir\n")
    (workspace / "link.txt").symlink_to(tmp_path / "outside.txt")
    (workspace / "linkdir").symlink_to(tmp_path / "outdir")
    (workspace / "in.txt").write_text("TOKEN=in\n")
    _, _, tools = _make_toolkit(workspace)

    result = await _tool_fn(tools, "search")(action="grep", pattern="TOKEN", path=".")

    assert result["success"] is True
    assert result["content"] == f"{workspace / 'in.txt'}:1:TOKEN=in"


async def test_search_skips_binary_files_and_reads_names_across_a_newline(tmp_path):
    (tmp_path / ".env").write_bytes(b"TOKEN=one\x00\n")
    (tmp_path / "blob.bin").write_bytes(b"TOKEN=two\x00\n")
    (tmp_path / "sub\ndir").mkdir()
    (tmp_path / "sub\ndir" / ".env").write_text("TOKEN=three\n")
    odd = tmp_path / "odd\nname.txt"
    odd.write_text("TOKEN=four\n")
    _, _, tools = _make_toolkit(tmp_path)

    result = await _tool_fn(tools, "search")(action="grep", pattern="TOKEN", path=".")

    assert result["success"] is True
    assert result["total_matches"] == 1
    assert result["content"] == f"{odd}:1:TOKEN=four"


async def test_search_asks_grep_to_skip_binary_files(tmp_path, monkeypatch):
    import lionagi.tools.coding as coding_mod

    seen = []

    def run(cmd, *args, **kwargs):
        seen.append(cmd)
        return {"returncode": 1, "stdout": "", "stderr": ""}

    monkeypatch.setattr(coding_mod, "_subprocess_sync", run)
    _, _, tools = _make_toolkit(tmp_path)

    await _tool_fn(tools, "search")(action="grep", pattern="TOKEN", path=".")

    flags = seen[0][1 : seen[0].index("-e")]
    assert any(f.startswith("-") and not f.startswith("--") and "I" in f for f in flags)


@pytest.mark.parametrize(
    "partial",
    ["", "a.py\x001:TOKEN\n"],
    ids=["nothing-read", "some-rows-read"],
)
async def test_search_reports_a_timed_out_grep_as_a_failure(tmp_path, monkeypatch, partial):
    import lionagi.tools.coding as coding_mod

    def run(cmd, *args, **kwargs):
        # The shape _subprocess_sync returns on a timeout.
        return {
            "stdout": partial + "\n\n[... output truncated at 100000 bytes ...]\n",
            "stderr": "Command timed out after 30.0s",
            "returncode": -1,
            "timed_out": True,
        }

    monkeypatch.setattr(coding_mod, "_subprocess_sync", run)
    _, _, tools = _make_toolkit(tmp_path)

    result = await _tool_fn(tools, "search")(action="grep", pattern="TOKEN", path=".")

    assert result == {"success": False, "error": "grep timed out"}


async def test_coding_toolkit_reader_rejects_workspace_escape(tmp_path):
    """ReaderTool rejects paths that escape the workspace root."""
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("TOP SECRET\n")

    _, _, tools = _make_toolkit(tmp_path)
    result = await _tool_fn(tools, "reader")(action="read", path=str(secret))

    assert result["success"] is False
    assert "escape" in result["error"].lower() or "workspace" in result["error"].lower()


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


def test_reader_request_schema_is_canonical():
    """reader tool_def must use canonical ReaderRequest from tools/file/reader.py."""
    from lionagi.tools.coding import CodingToolkit
    from lionagi.tools.file.reader import ReaderRequest as CanonicalReaderRequest

    b = Branch()
    tk = CodingToolkit(notify=False, workspace_root="/tmp", tools=["reader"])
    tools = tk.bind(b)
    reader_tool = tools[0]
    assert reader_tool.request_options is CanonicalReaderRequest, (
        "CodingToolkit reader must use the canonical ReaderRequest from tools/file/reader.py"
    )


def test_editor_request_schema_is_canonical():
    """editor tool_def must use canonical EditorRequest from tools/file/editor.py."""
    from lionagi.tools.coding import CodingToolkit
    from lionagi.tools.file.editor import EditorRequest as CanonicalEditorRequest

    b = Branch()
    tk = CodingToolkit(notify=False, workspace_root="/tmp", tools=["editor"])
    tools = tk.bind(b)
    editor_tool = tools[0]
    assert editor_tool.request_options is CanonicalEditorRequest, (
        "CodingToolkit editor must use the canonical EditorRequest from tools/file/editor.py"
    )


def test_canonical_reader_request_has_read_and_list_dir_only():
    """The reader exposes text reads and directory listings; there is no document conversion."""
    from pydantic import ValidationError

    from lionagi.tools.file.reader import ReaderAction, ReaderRequest

    assert {a.value for a in ReaderAction} == {"read", "list_dir"}
    with pytest.raises(ValidationError):
        ReaderRequest(action="open", path="report.pdf")


async def test_reader_open_action_is_unknown(tmp_path):
    """The bound reader answers action='open' as an unknown action."""
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    _, _, tools = _make_toolkit(tmp_path)
    result = await _tool_fn(tools, "reader")(action="open", path=str(f))
    assert result == {"success": False, "error": "Unknown action: open"}


def test_coding_toolkit_reader_schema_requires_path(tmp_path):
    """LLM-facing reader schema must mark 'path' as required to guard against schema drift."""
    _, _, tools = _make_toolkit(tmp_path)
    reader_tool = next(t for t in tools if t.func_callable.__name__ == "reader")
    required = reader_tool.tool_schema["function"]["parameters"]["required"]
    assert "path" in required, (
        f"'path' must be in required fields of reader schema, got: {required}"
    )


def test_coding_toolkit_reader_request_options_raises_without_path(tmp_path):
    """request_options(action='read') without path must raise ValidationError."""
    from pydantic import ValidationError

    _, _, tools = _make_toolkit(tmp_path)
    reader_tool = next(t for t in tools if t.func_callable.__name__ == "reader")
    with pytest.raises(ValidationError):
        reader_tool.request_options(action="read")


@pytest.mark.parametrize("action", ["read", "list_dir"])
async def test_empty_path_exactly_matches_reader_tool(tmp_path, action):
    """Every reader action with empty path returns byte-identical output to ReaderTool's guard.

    Regression: the wrapper omitted 'content' and used a divergent error string for open,
    and list_dir fell through the guard entirely, listing the workspace root instead of erroring.
    """
    from lionagi.tools.file.reader import ReaderTool

    (tmp_path / "a.py").write_text("x")

    _, _, tools = _make_toolkit(tmp_path)
    reader_fn = _tool_fn(tools, "reader")
    actual = await reader_fn(action=action, path="")

    expected = (
        await ReaderTool(workspace_root=str(tmp_path)).handle_request(
            {"action": action, "path": ""}
        )
    ).model_dump()

    assert actual == expected


@pytest.mark.parametrize(
    "text, offset, total_lines, reached_eof, expected_content",
    [
        pytest.param("alpha\nbeta\ngamma\n", 0, 3, False, "1\talpha\n2\tbeta\n", id="before-end"),
        pytest.param(
            "alpha\nbeta\ngamma\n", 1, 3, True, "2\tbeta\n3\tgamma\n", id="ends-at-last-line"
        ),
        pytest.param("alpha\nbeta\ngamma\n", 3, 3, True, None, id="at-end"),
        pytest.param("alpha\nbeta\ngamma\n", 8, 3, True, None, id="past-end"),
        pytest.param("", 0, 0, True, "", id="empty"),
    ],
)
async def test_reader_passes_file_extent_through_the_bound_tool(
    tmp_path, text, offset, total_lines, reached_eof, expected_content
):
    path = tmp_path / "window.txt"
    path.write_text(text)
    _, _, tools = _make_toolkit(tmp_path)

    result = await _tool_fn(tools, "reader")(action="read", path=str(path), offset=offset, limit=2)

    assert result["success"] is True
    assert result.get("total_lines") == total_lines
    assert result.get("reached_eof") is reached_eof
    if expected_content is None:
        assert result["content"] == (
            f"Offset {offset} is at or beyond the end of the file ({total_lines} lines). "
            f"Use an offset below {total_lines} to read file content."
        )
    else:
        assert result["content"] == expected_content
