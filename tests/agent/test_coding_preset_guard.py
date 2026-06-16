# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Attack-driven regression tests for the coding preset security gate."""

from __future__ import annotations

import pytest

from lionagi.agent.spec import AgentSpec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_branch(spec: AgentSpec):
    from lionagi.agent.factory import create_agent

    return await create_agent(spec, load_settings=False)


# ---------------------------------------------------------------------------
# AgentSpec.coding() — destructive-command guard
# ---------------------------------------------------------------------------


async def test_coding_preset_blocks_rm_rf():
    """The default coding preset must block 'rm -rf'."""
    spec = AgentSpec.coding()
    branch = await _make_branch(spec)
    bash_tool = branch.acts.registry["bash"]

    assert bash_tool.preprocessor is not None, "coding preset must wire a bash preprocessor"

    with pytest.raises(PermissionError, match="Blocked destructive command"):
        await bash_tool.preprocessor({"action": "run", "command": "rm -rf /tmp/project"})


async def test_coding_preset_blocks_git_reset_hard():
    """'git reset --hard' is a history-destroying operation and must be blocked."""
    spec = AgentSpec.coding()
    branch = await _make_branch(spec)
    bash_tool = branch.acts.registry["bash"]

    with pytest.raises(PermissionError, match="Blocked destructive command"):
        await bash_tool.preprocessor({"action": "run", "command": "git reset --hard HEAD~3"})


async def test_coding_preset_blocks_git_push_force():
    """Force-push can rewrite shared history; the preset must refuse it."""
    spec = AgentSpec.coding()
    branch = await _make_branch(spec)
    bash_tool = branch.acts.registry["bash"]

    with pytest.raises(PermissionError, match="Blocked destructive command"):
        await bash_tool.preprocessor({"action": "run", "command": "git push --force origin main"})


async def test_coding_preset_allows_benign_command():
    """A safe read-only command must pass through the guard without error."""
    spec = AgentSpec.coding()
    branch = await _make_branch(spec)
    bash_tool = branch.acts.registry["bash"]

    # Must not raise; return value is None (pass-through) or a dict.
    result = await bash_tool.preprocessor({"action": "run", "command": "git status"})
    # The guard returns None on success; a modified dict is also acceptable.
    assert result is None or isinstance(result, dict)


async def test_coding_preset_allows_uv_run():
    """'uv run pytest' is a common safe command that must not be blocked."""
    spec = AgentSpec.coding()
    branch = await _make_branch(spec)
    bash_tool = branch.acts.registry["bash"]

    result = await bash_tool.preprocessor({"action": "run", "command": "uv run pytest -q"})
    assert result is None or isinstance(result, dict)


async def test_coding_preset_secure_false_has_no_default_guard():
    """secure=False must disable the default guard hook wired by the preset."""
    spec = AgentSpec.coding(secure=False)
    branch = await _make_branch(spec)
    bash_tool = branch.acts.registry["bash"]

    # No preprocessor at all — the preset contributed nothing.
    assert bash_tool.preprocessor is None


async def test_coding_preset_guard_destructive_in_hook_handlers():
    """The default guard hook must appear in hook_handlers before create_agent."""
    from lionagi.agent.hooks import guard_destructive

    spec = AgentSpec.coding()
    handlers = spec.hook_handlers.get("pre:bash", [])
    assert guard_destructive in handlers, (
        "guard_destructive must be in pre:bash hook_handlers for the coding preset"
    )


# ---------------------------------------------------------------------------
# Path-policy tests — the "strict path policy" claim must be functionally true
# ---------------------------------------------------------------------------


async def _invoke_pre_hooks(branch, tool_name: str, args: dict) -> None:
    """Drive pre-hooks directly via the tool's preprocessor (same path as factory)."""
    tool = branch.acts.registry[tool_name]
    if tool.preprocessor is None:
        raise AssertionError(f"No preprocessor wired on {tool_name!r}")
    await tool.preprocessor(args)


async def test_coding_preset_reader_blocks_outside_workspace(tmp_path):
    """reader with a path outside the workspace root must be blocked."""
    spec = AgentSpec.coding(cwd=str(tmp_path))
    branch = await _make_branch(spec)

    with pytest.raises(PermissionError):
        await _invoke_pre_hooks(branch, "reader", {"action": "read", "path": "/etc/passwd"})


async def test_coding_preset_editor_blocks_outside_workspace(tmp_path):
    """editor with a file_path outside the workspace root must be blocked."""
    spec = AgentSpec.coding(cwd=str(tmp_path))
    branch = await _make_branch(spec)

    with pytest.raises(PermissionError):
        await _invoke_pre_hooks(
            branch,
            "editor",
            {"action": "write", "file_path": "/etc/cron.d/evil", "content": "bad"},
        )


async def test_coding_preset_reader_allows_inside_workspace(tmp_path):
    """reader called with a path INSIDE the workspace root must be allowed."""
    spec = AgentSpec.coding(cwd=str(tmp_path))
    branch = await _make_branch(spec)

    inside = str(tmp_path / "src" / "main.py")
    # Must not raise — path is within the allowed root.
    result = await branch.acts.registry["reader"].preprocessor({"action": "read", "path": inside})
    assert result is None or isinstance(result, dict)


async def test_coding_preset_editor_allows_inside_workspace(tmp_path):
    """editor called with a file_path INSIDE the workspace root must be allowed."""
    spec = AgentSpec.coding(cwd=str(tmp_path))
    branch = await _make_branch(spec)

    inside = str(tmp_path / "output.txt")
    result = await branch.acts.registry["editor"].preprocessor(
        {"action": "write", "file_path": inside, "content": "hello"}
    )
    assert result is None or isinstance(result, dict)


async def test_coding_preset_parent_dir_traversal_blocked(tmp_path):
    """A parent-directory traversal path must not escape the workspace root.

    Even when an absolute path technically resolves to a parent directory,
    the path guard must detect and block it.
    """
    spec = AgentSpec.coding(cwd=str(tmp_path))
    branch = await _make_branch(spec)

    # One level above tmp_path — clearly outside the workspace.
    outside = str(tmp_path.parent / "secret.txt")
    with pytest.raises(PermissionError):
        await _invoke_pre_hooks(branch, "reader", {"action": "read", "path": outside})


async def test_coding_preset_reader_guard_in_hook_handlers(tmp_path):
    """guard_paths hook must appear in pre:reader hook_handlers for coding preset."""
    spec = AgentSpec.coding(cwd=str(tmp_path))
    handlers = spec.hook_handlers.get("pre:reader", [])
    assert len(handlers) >= 1, "guard_paths must be wired into pre:reader for the coding preset"


async def test_coding_preset_editor_guard_in_hook_handlers(tmp_path):
    """guard_paths hook must appear in pre:editor hook_handlers for coding preset."""
    spec = AgentSpec.coding(cwd=str(tmp_path))
    handlers = spec.hook_handlers.get("pre:editor", [])
    assert len(handlers) >= 1, "guard_paths must be wired into pre:editor for the coding preset"


async def test_coding_preset_secure_false_no_path_guard():
    """secure=False must not wire any path guard on reader or editor."""
    spec = AgentSpec.coding(secure=False)
    assert not spec.hook_handlers.get("pre:reader"), (
        "secure=False must not wire any pre:reader hook"
    )
    assert not spec.hook_handlers.get("pre:editor"), (
        "secure=False must not wire any pre:editor hook"
    )


# ---------------------------------------------------------------------------
# Relative-path tests (guard_paths resolves against workspace root, not cwd)
# ---------------------------------------------------------------------------


async def test_coding_preset_reader_allows_relative_in_workspace(tmp_path):
    """A workspace-relative reader path ("src/foo.py") must be allowed."""
    spec = AgentSpec.coding(cwd=str(tmp_path))
    branch = await _make_branch(spec)

    result = await branch.acts.registry["reader"].preprocessor(
        {"action": "read", "path": "src/foo.py"}
    )
    assert result is None or isinstance(result, dict)


async def test_coding_preset_editor_allows_relative_in_workspace(tmp_path):
    """A workspace-relative editor path ("output.txt") must be allowed."""
    spec = AgentSpec.coding(cwd=str(tmp_path))
    branch = await _make_branch(spec)

    result = await branch.acts.registry["editor"].preprocessor(
        {"action": "write", "file_path": "output.txt", "content": "hello"}
    )
    assert result is None or isinstance(result, dict)


async def test_coding_preset_reader_blocks_relative_traversal(tmp_path):
    """A relative traversal ("../../etc/passwd") must be blocked even when resolved against workspace."""
    spec = AgentSpec.coding(cwd=str(tmp_path))
    branch = await _make_branch(spec)

    with pytest.raises(PermissionError):
        await _invoke_pre_hooks(branch, "reader", {"action": "read", "path": "../../etc/passwd"})


async def test_coding_preset_editor_blocks_relative_traversal(tmp_path):
    """A relative traversal via editor ("../../etc/cron.d/evil") must be blocked."""
    spec = AgentSpec.coding(cwd=str(tmp_path))
    branch = await _make_branch(spec)

    with pytest.raises(PermissionError):
        await _invoke_pre_hooks(
            branch,
            "editor",
            {"action": "write", "file_path": "../../etc/cron.d/evil", "content": "bad"},
        )
