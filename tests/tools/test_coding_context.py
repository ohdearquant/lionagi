# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for CodingToolkit: search, context management, and file_state tracking."""

from lionagi.session.branch import Branch
from lionagi.tools.coding import ALL_CODING_TOOLS, CodingToolkit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_toolkit(tmp_path, notify=False):
    b = Branch()
    tk = CodingToolkit(notify=notify, workspace_root=str(tmp_path), tools=ALL_CODING_TOOLS)
    tools = tk.bind(b)
    return b, tk, tools


def _tool_fn(tools, name):
    for t in tools:
        if t.func_callable.__name__ == name:
            return t.func_callable
    raise KeyError(f"tool '{name}' not found")


# ---------------------------------------------------------------------------
# Search: grep and find
# ---------------------------------------------------------------------------


async def test_search_grep_finds_pattern(tmp_path):
    (tmp_path / "source.py").write_text("def hello():\n    pass\n")
    _, _, tools = _make_toolkit(tmp_path)
    result = await _tool_fn(tools, "search")(action="grep", pattern="def hello", path=str(tmp_path))
    assert result["success"] is True and "hello" in result["content"]
    assert result["total_matches"] >= 1


async def test_search_find_finds_files(tmp_path):
    (tmp_path / "alpha.py").write_text("")
    _, _, tools = _make_toolkit(tmp_path)
    result = await _tool_fn(tools, "search")(action="find", pattern="*.py", path=str(tmp_path))
    assert result["success"] is True and "alpha.py" in result["content"]


async def test_search_grep_no_matches(tmp_path):
    (tmp_path / "empty.py").write_text("nothing here\n")
    _, _, tools = _make_toolkit(tmp_path)
    result = await _tool_fn(tools, "search")(
        action="grep", pattern="XYZNOTFOUND", path=str(tmp_path)
    )
    assert result["success"] is True and result["total_matches"] == 0


# ---------------------------------------------------------------------------
# Context: status reports message count
# ---------------------------------------------------------------------------


async def test_context_status_empty_branch(tmp_path):
    _, _, tools = _make_toolkit(tmp_path)
    context = _tool_fn(tools, "context")
    result = await context(action="status")
    assert result["success"] is True
    assert result["active_messages"] == 0
    assert result["total_messages"] == 0
    assert result["evicted"] == 0
    assert result["files_tracked"] == 0


async def test_context_status_with_system_message(tmp_path):
    b = Branch()
    tk = CodingToolkit(notify=False, workspace_root=str(tmp_path), tools=ALL_CODING_TOOLS)
    tools = tk.bind(b)
    context = _tool_fn(tools, "context")

    sys_msg = b.msgs.create_system(system="You are a coder.")
    b.msgs.set_system(sys_msg)

    result = await context(action="status")
    assert result["active_messages"] == 1
    assert result["files_tracked"] == 0


async def test_context_status_tracks_files_after_read(tmp_path):
    f = tmp_path / "tracked.py"
    f.write_text("x = 1\n")
    b = Branch()
    tk = CodingToolkit(notify=False, workspace_root=str(tmp_path), tools=ALL_CODING_TOOLS)
    tools = tk.bind(b)
    reader = _tool_fn(tools, "reader")
    context = _tool_fn(tools, "context")

    await reader(action="read", path=str(f))
    result = await context(action="status")
    assert result["files_tracked"] == 1


# ---------------------------------------------------------------------------
# Context: evict removes from progression, not pile
# ---------------------------------------------------------------------------


async def test_context_evict_reduces_active_not_total(tmp_path):
    b = Branch()
    tk = CodingToolkit(notify=False, workspace_root=str(tmp_path), tools=ALL_CODING_TOOLS)
    tools = tk.bind(b)
    context = _tool_fn(tools, "context")

    sys_msg = b.msgs.create_system(system="sys")
    b.msgs.set_system(sys_msg)
    b.msgs.add_message(instruction="do something")

    before = await context(action="status")
    assert before["active_messages"] == 2

    evict_result = await context(action="evict", start=1, end=2)
    assert evict_result["success"] is True
    assert evict_result["removed"] == 1

    after = await context(action="status")
    assert after["active_messages"] == 1
    assert after["total_messages"] == 2  # pile unchanged


async def test_context_evict_invalid_range(tmp_path):
    _, _, tools = _make_toolkit(tmp_path)
    context = _tool_fn(tools, "context")
    result = await context(action="evict", start=5, end=3)
    assert result["success"] is False
    assert "Invalid range" in result["error"]


# ---------------------------------------------------------------------------
# file_state: mtime tracked after read, checked before edit
# ---------------------------------------------------------------------------


async def test_file_state_mtime_tracked_after_read(tmp_path):
    f = tmp_path / "tracked.py"
    f.write_text("original\n")
    b = Branch()
    tk = CodingToolkit(notify=False, workspace_root=str(tmp_path), tools=ALL_CODING_TOOLS)
    tools = tk.bind(b)
    reader = _tool_fn(tools, "reader")
    editor = _tool_fn(tools, "editor")

    await reader(action="read", path=str(f))

    # Overwrite externally; advance mtime explicitly to guarantee it differs
    import os

    f.write_text("changed externally\n")
    os.utime(f, (f.stat().st_mtime + 1, f.stat().st_mtime + 1))

    # editor should detect stale mtime and reject
    result = await editor(
        action="edit",
        file_path=str(f),
        old_string="changed externally",
        new_string="nope",
    )
    assert result["success"] is False
    assert "changed" in result["error"].lower() or "read" in result["error"].lower()


async def test_file_state_allows_edit_when_mtime_matches(tmp_path):
    f = tmp_path / "stable.py"
    f.write_text("hello world\n")
    b = Branch()
    tk = CodingToolkit(notify=False, workspace_root=str(tmp_path), tools=ALL_CODING_TOOLS)
    tools = tk.bind(b)
    reader = _tool_fn(tools, "reader")
    editor = _tool_fn(tools, "editor")

    await reader(action="read", path=str(f))
    # No external change — mtime should match
    result = await editor(
        action="edit",
        file_path=str(f),
        old_string="hello",
        new_string="goodbye",
    )
    assert result["success"] is True


# ---------------------------------------------------------------------------
# Stale-read invalidation: evicting a reader-read result forces a re-read
# ---------------------------------------------------------------------------


async def test_stale_read_invalidation_forces_reread_after_evict(tmp_path):
    """Evicting the ActionResponse for a read must force a re-read before edit.

    Without this, the read-before-edit guard stays satisfied (file_state keeps
    the mtime) even though the model no longer has the file's contents in its
    active view — a silent unsoundness.
    """
    from lionagi.protocols.messages import ActionRequest, ActionResponse

    f = tmp_path / "x.py"
    f.write_text("original\n")

    b = Branch()
    tk = CodingToolkit(notify=False, workspace_root=str(tmp_path), tools=ALL_CODING_TOOLS)
    tools = tk.bind(b)
    reader = _tool_fn(tools, "reader")
    editor = _tool_fn(tools, "editor")
    context = _tool_fn(tools, "context")

    # Real read call — populates file_state + read_tracked with the resolved path.
    read_result = await reader(action="read", path=str(f))
    assert read_result["success"] is True

    # Simulate the agentic-loop bookkeeping the raw function call bypasses: the
    # ActionRequest/ActionResponse pair the framework would record for this call.
    req = ActionRequest(
        content={"function": "reader", "arguments": {"action": "read", "path": str(f)}}
    )
    b.msgs.messages.include(req)
    b.msgs.progression.include(req.id)
    resp = ActionResponse(
        content={
            "function": "reader",
            "arguments": {"action": "read", "path": str(f)},
            "output": read_result.get("content"),
        }
    )
    b.msgs.messages.include(resp)
    b.msgs.progression.include(resp.id)

    idx = list(b.progression).index(resp.id)
    evict_result = await context(action="evict", start=idx, end=idx + 1)
    assert evict_result["success"] is True

    result = await editor(
        action="edit", file_path=str(f), old_string="original", new_string="changed"
    )
    assert result["success"] is False
    assert "read" in result["error"].lower()


async def test_normal_read_then_edit_flow_unaffected_by_invalidation(tmp_path):
    """Reading then editing without any context eviction must still work."""
    f = tmp_path / "y.py"
    f.write_text("hello world\n")

    b = Branch()
    tk = CodingToolkit(notify=False, workspace_root=str(tmp_path), tools=ALL_CODING_TOOLS)
    tools = tk.bind(b)
    reader = _tool_fn(tools, "reader")
    editor = _tool_fn(tools, "editor")

    await reader(action="read", path=str(f))
    result = await editor(action="edit", file_path=str(f), old_string="hello", new_string="goodbye")
    assert result["success"] is True


async def test_write_tracked_files_survive_context_eviction(tmp_path):
    """A file known only via editor write (never read) must not be purged by the
    reader-focused invalidation — it was never added to `read_tracked`."""
    f = tmp_path / "new.py"

    b = Branch()
    tk = CodingToolkit(notify=False, workspace_root=str(tmp_path), tools=ALL_CODING_TOOLS)
    tools = tk.bind(b)
    editor = _tool_fn(tools, "editor")
    context = _tool_fn(tools, "context")

    write_result = await editor(action="write", file_path=str(f), content="x = 1\n")
    assert write_result["success"] is True

    b.msgs.add_message(instruction="noop")
    evict_result = await context(action="evict_action_results", keep_last=0)
    assert evict_result["success"] is True

    result = await editor(action="edit", file_path=str(f), old_string="x = 1", new_string="x = 2")
    assert result["success"] is True
