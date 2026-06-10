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


# ---------------------------------------------------------------------------
# Schema equivalence: CodingToolkit uses canonical file/ schemas (anti-divergence)
# ---------------------------------------------------------------------------


def test_reader_request_schema_is_canonical():
    """CodingToolkit's reader tool_def must reference the canonical ReaderRequest."""
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
    """CodingToolkit's editor tool_def must reference the canonical EditorRequest."""
    from lionagi.tools.coding import CodingToolkit
    from lionagi.tools.file.editor import EditorRequest as CanonicalEditorRequest

    b = Branch()
    tk = CodingToolkit(notify=False, workspace_root="/tmp", tools=["editor"])
    tools = tk.bind(b)
    editor_tool = tools[0]
    assert editor_tool.request_options is CanonicalEditorRequest, (
        "CodingToolkit editor must use the canonical EditorRequest from tools/file/editor.py"
    )


def test_canonical_reader_request_has_open_action():
    """Canonical ReaderRequest must enumerate the 'open' action so CodingToolkit exposes it."""
    from lionagi.tools.file.reader import ReaderAction

    assert hasattr(ReaderAction, "open"), "ReaderAction must include the 'open' member"
    assert ReaderAction.open.value == "open"


# ---------------------------------------------------------------------------
# Reader: open action (docling not required — validate error path without it)
# ---------------------------------------------------------------------------


async def test_reader_open_unsupported_extension_fails(tmp_path):
    """open action on a non-document file returns a descriptive failure."""
    f = tmp_path / "code.py"
    f.write_text("x = 1\n")
    _, _, tools = _make_toolkit(tmp_path)
    result = await _tool_fn(tools, "reader")(action="open", path=str(f))
    # Either unsupported extension or docling-not-installed — both are informative failures
    assert result["success"] is False
    assert result.get("error")


async def test_reader_open_missing_path_fails(tmp_path):
    """open action with empty/None path returns failure immediately."""
    _, _, tools = _make_toolkit(tmp_path)
    result = await _tool_fn(tools, "reader")(action="open", path="")
    assert result["success"] is False


async def test_reader_open_caches_and_read_serves_from_cache(tmp_path, monkeypatch):
    """After a successful open, subsequent read on the same path uses the cache."""
    import time

    import lionagi.tools.coding as _coding_mod
    from lionagi.tools.file.reader import ReaderResponse

    cached_text = "line one\nline two\nline three\n"

    # Patch _open_sync in the coding module namespace (that's where the closure
    # captures it) so we don't need docling installed in CI.
    def _fake_open_sync(path, cache, workspace_root, allowed_url_hosts):
        cache[path] = (cached_text, time.time())
        lines = cached_text.split("\n")
        return ReaderResponse(
            success=True,
            content=f"Opened: {path} ({len(lines)} lines). Use read to view.",
        )

    monkeypatch.setattr(_coding_mod, "_open_sync", _fake_open_sync)

    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.4 fake")

    _, _, tools = _make_toolkit(tmp_path)
    reader_fn = _tool_fn(tools, "reader")

    open_result = await reader_fn(action="open", path=str(f))
    assert open_result["success"] is True, open_result.get("error")

    # Now read should serve from cache (not from disk bytes)
    read_result = await reader_fn(action="read", path=str(f), offset=0, limit=2)
    assert read_result["success"] is True
    assert "line one" in read_result["content"]
    assert "line two" in read_result["content"]


# ---------------------------------------------------------------------------
# Schema validation: path is required in LLM-facing schema (anti-drift)
# ---------------------------------------------------------------------------


def test_coding_toolkit_reader_schema_requires_path(tmp_path):
    """CodingToolkit reader bind — LLM-facing schema must mark 'path' as required.

    This guards against schema drift where path reverts to Optional: an LLM
    that omits path would pass JSON schema validation but fail at runtime.
    """
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


# ---------------------------------------------------------------------------
# Error shape equivalence: CodingToolkit open vs ReaderTool for empty path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["open", "read", "list_dir"])
async def test_empty_path_exactly_matches_reader_tool(tmp_path, action):
    """Every reader action with an empty path returns byte-identical output to
    ReaderTool's canonical pre-dispatch guard:
    {'success': False, 'content': None, 'error': "'path' is required"}.

    Codex round-1/round-2 findings: the wrapper omitted 'content' and used a
    divergent error string for open, and list_dir fell through the guard
    entirely, listing the workspace root instead of erroring.
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


# ---------------------------------------------------------------------------
# Docling smoke: real import path for 'open' action is reachable
# ---------------------------------------------------------------------------


def test_docling_import_is_available():
    """Confirm docling is installed so the 'open' action's real code path is exercisable."""
    from docling.document_converter import DocumentConverter  # noqa: F401


async def test_reader_open_real_html_fixture(tmp_path):
    """Real docling open path: convert a minimal HTML file, no mocking."""
    html_file = tmp_path / "page.html"
    html_file.write_text("<html><body><p>Hello lion</p></body></html>", encoding="utf-8")

    _, _, tools = _make_toolkit(tmp_path)
    reader_fn = _tool_fn(tools, "reader")

    result = await reader_fn(action="open", path=str(html_file))
    assert result["success"] is True, f"open failed: {result.get('error')}"
    assert result["content"] is not None

    # Cache populated — subsequent read should serve from cache
    read_result = await reader_fn(action="read", path=str(html_file), offset=0, limit=10)
    assert read_result["success"] is True


# ---------------------------------------------------------------------------
# Equivalence: nonexistent PDF and offset-beyond-cache for CodingToolkit vs ReaderTool
# ---------------------------------------------------------------------------


async def test_open_nonexistent_pdf_equivalence(tmp_path):
    """nonexistent .pdf path: CodingToolkit output is byte-identical to ReaderTool."""
    from lionagi.tools.file.reader import ReaderRequest, ReaderTool

    fake_pdf = str(tmp_path / "nope.pdf")

    standalone = ReaderTool(workspace_root=str(tmp_path))
    rt_resp = await standalone.handle_request(ReaderRequest(action="open", path=fake_pdf))
    assert rt_resp.success is False

    _, _, tools = _make_toolkit(tmp_path)
    reader_fn = _tool_fn(tools, "reader")
    ct_result = await reader_fn(action="open", path=fake_pdf)
    assert ct_result == rt_resp.model_dump()


async def test_read_offset_beyond_cached_doc_equivalence(tmp_path, monkeypatch):
    """Offset beyond end of cached doc: both tools return success=True with empty/minimal content."""
    import time

    import lionagi.tools.coding as _coding_mod
    from lionagi.tools.file.reader import ReaderResponse, ReaderTool

    cached_text = "only one line"

    def _fake_open_sync(path, cache, workspace_root, allowed_url_hosts):
        cache[path] = (cached_text, time.time())
        return ReaderResponse(success=True, content=f"Opened: {path}")

    monkeypatch.setattr(_coding_mod, "_open_sync", _fake_open_sync)

    # Also patch the standalone ReaderTool's module-level _open_sync
    import lionagi.tools.file.reader as _reader_mod

    monkeypatch.setattr(_reader_mod, "_open_sync", _fake_open_sync)

    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.4 fake")

    # CodingToolkit: open then read with offset=9999
    _, _, tools = _make_toolkit(tmp_path)
    reader_fn = _tool_fn(tools, "reader")
    await reader_fn(action="open", path=str(f))
    ct_result = await reader_fn(action="read", path=str(f), offset=9999, limit=10)
    assert ct_result["success"] is True  # empty slice is still a success

    # ReaderTool standalone: same scenario must produce byte-identical output
    standalone = ReaderTool(workspace_root=str(tmp_path))
    from lionagi.tools.file.reader import ReaderRequest

    await standalone.handle_request(ReaderRequest(action="open", path=str(f)))
    rt_resp = await standalone.handle_request(
        ReaderRequest(action="read", path=str(f), offset=9999, limit=10)
    )
    assert ct_result == rt_resp.model_dump()
