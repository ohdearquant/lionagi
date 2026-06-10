# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Attack-driven regression tests for the coding preset security gate.

The AgentConfig.coding() and AgentSpec.coding() presets advertise
"guard hooks + strict path policy" but previously wired NO default security
hook. An agent using the coding preset could execute destructive shell commands
(rm -rf, git reset --hard, etc.) without any barrier.

These tests prove that:
1. The preset's default bash preprocessor blocks known-destructive commands.
2. A benign command passes through the guard unchanged.
3. Callers who opt out via secure=False receive no preprocessor from the preset.
"""

from __future__ import annotations

import pytest

from lionagi.agent.config import AgentConfig
from lionagi.agent.spec import AgentSpec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_branch(config: AgentConfig):
    from lionagi.agent.factory import create_agent

    return await create_agent(config, load_settings=False)


async def _make_branch_from_spec(spec: AgentSpec):
    from lionagi.agent.factory import create_agent

    return await create_agent(spec, load_settings=False)


# ---------------------------------------------------------------------------
# AgentConfig.coding() — attack-driven tests
# ---------------------------------------------------------------------------


async def test_coding_preset_blocks_rm_rf():
    """Destructive 'rm -rf' must be refused by the default coding preset.

    A coding agent should never silently execute a recursive forced removal
    command without explicit human confirmation; the guard must intercept it
    before the bash tool executes.
    """
    config = AgentConfig.coding()
    branch = await _make_branch(config)
    bash_tool = branch.acts.registry["bash"]

    assert bash_tool.preprocessor is not None, "coding preset must wire a bash preprocessor"

    with pytest.raises(PermissionError, match="Blocked destructive command"):
        await bash_tool.preprocessor({"action": "run", "command": "rm -rf /tmp/project"})


async def test_coding_preset_blocks_git_reset_hard():
    """'git reset --hard' is a history-destroying operation and must be blocked."""
    config = AgentConfig.coding()
    branch = await _make_branch(config)
    bash_tool = branch.acts.registry["bash"]

    with pytest.raises(PermissionError, match="Blocked destructive command"):
        await bash_tool.preprocessor({"action": "run", "command": "git reset --hard HEAD~3"})


async def test_coding_preset_blocks_git_push_force():
    """Force-push can rewrite shared history; the preset must refuse it."""
    config = AgentConfig.coding()
    branch = await _make_branch(config)
    bash_tool = branch.acts.registry["bash"]

    with pytest.raises(PermissionError, match="Blocked destructive command"):
        await bash_tool.preprocessor({"action": "run", "command": "git push --force origin main"})


async def test_coding_preset_allows_benign_command():
    """A safe read-only command must pass through the guard without error."""
    config = AgentConfig.coding()
    branch = await _make_branch(config)
    bash_tool = branch.acts.registry["bash"]

    # Must not raise; return value is None (pass-through) or a dict.
    result = await bash_tool.preprocessor({"action": "run", "command": "git status"})
    # The guard returns None on success; a modified dict is also acceptable.
    assert result is None or isinstance(result, dict)


async def test_coding_preset_allows_uv_run():
    """'uv run pytest' is a common safe command that must not be blocked."""
    config = AgentConfig.coding()
    branch = await _make_branch(config)
    bash_tool = branch.acts.registry["bash"]

    result = await bash_tool.preprocessor({"action": "run", "command": "uv run pytest -q"})
    assert result is None or isinstance(result, dict)


async def test_coding_preset_secure_false_has_no_default_guard():
    """secure=False must disable the default guard hook wired by the preset.

    Callers who opt out must be able to manage hooks themselves without the
    preset silently injecting a guard they did not request.
    """
    config = AgentConfig.coding(secure=False)
    branch = await _make_branch(config)
    bash_tool = branch.acts.registry["bash"]

    # No preprocessor at all — the preset contributed nothing.
    assert bash_tool.preprocessor is None


async def test_coding_preset_guard_destructive_in_hook_handlers():
    """The default guard hook must appear in hook_handlers before create_agent."""
    from lionagi.agent.hooks import guard_destructive

    config = AgentConfig.coding()
    handlers = config.hook_handlers.get("pre:bash", [])
    assert guard_destructive in handlers, (
        "guard_destructive must be in pre:bash hook_handlers for the coding preset"
    )


# ---------------------------------------------------------------------------
# AgentSpec.coding() — same attack surface via the modern API
# ---------------------------------------------------------------------------


async def test_spec_coding_preset_blocks_rm_rf():
    """AgentSpec.coding() must wire the same guard as AgentConfig.coding()."""
    spec = AgentSpec.coding()
    branch = await _make_branch_from_spec(spec)
    bash_tool = branch.acts.registry["bash"]

    assert bash_tool.preprocessor is not None, "AgentSpec.coding() must wire a bash preprocessor"

    with pytest.raises(PermissionError, match="Blocked destructive command"):
        await bash_tool.preprocessor({"action": "run", "command": "rm -rf /"})


async def test_spec_coding_preset_secure_false_no_guard():
    """AgentSpec.coding(secure=False) must not inject any default guard."""
    from lionagi.agent.hooks import guard_destructive

    spec = AgentSpec.coding(secure=False)
    handlers = spec.hook_handlers.get("pre:bash", [])
    assert guard_destructive not in handlers


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
    """reader called with a path OUTSIDE the workspace root must be BLOCKED.

    An agent with the default coding preset must not be able to exfiltrate
    secrets from outside its workspace (e.g. /etc/passwd, ~/.ssh/id_rsa,
    or a parent-directory traversal).
    """
    config = AgentConfig.coding(cwd=str(tmp_path))
    branch = await _make_branch(config)

    with pytest.raises(PermissionError):
        await _invoke_pre_hooks(branch, "reader", {"action": "read", "path": "/etc/passwd"})


async def test_coding_preset_editor_blocks_outside_workspace(tmp_path):
    """editor called with a file_path OUTSIDE the workspace root must be BLOCKED.

    Writing to arbitrary paths would let an agent corrupt system files or
    overwrite SSH keys; the preset path guard must refuse it.
    """
    config = AgentConfig.coding(cwd=str(tmp_path))
    branch = await _make_branch(config)

    with pytest.raises(PermissionError):
        await _invoke_pre_hooks(
            branch,
            "editor",
            {"action": "write", "file_path": "/etc/cron.d/evil", "content": "bad"},
        )


async def test_coding_preset_reader_allows_inside_workspace(tmp_path):
    """reader called with a path INSIDE the workspace root must be allowed."""
    config = AgentConfig.coding(cwd=str(tmp_path))
    branch = await _make_branch(config)

    inside = str(tmp_path / "src" / "main.py")
    # Must not raise — path is within the allowed root.
    result = await branch.acts.registry["reader"].preprocessor({"action": "read", "path": inside})
    assert result is None or isinstance(result, dict)


async def test_coding_preset_editor_allows_inside_workspace(tmp_path):
    """editor called with a file_path INSIDE the workspace root must be allowed."""
    config = AgentConfig.coding(cwd=str(tmp_path))
    branch = await _make_branch(config)

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
    config = AgentConfig.coding(cwd=str(tmp_path))
    branch = await _make_branch(config)

    # One level above tmp_path — clearly outside the workspace.
    outside = str(tmp_path.parent / "secret.txt")
    with pytest.raises(PermissionError):
        await _invoke_pre_hooks(branch, "reader", {"action": "read", "path": outside})


async def test_coding_preset_reader_guard_in_hook_handlers(tmp_path):
    """guard_paths hook must appear in pre:reader hook_handlers for coding preset."""
    config = AgentConfig.coding(cwd=str(tmp_path))
    handlers = config.hook_handlers.get("pre:reader", [])
    assert len(handlers) >= 1, "guard_paths must be wired into pre:reader for the coding preset"


async def test_coding_preset_editor_guard_in_hook_handlers(tmp_path):
    """guard_paths hook must appear in pre:editor hook_handlers for coding preset."""
    config = AgentConfig.coding(cwd=str(tmp_path))
    handlers = config.hook_handlers.get("pre:editor", [])
    assert len(handlers) >= 1, "guard_paths must be wired into pre:editor for the coding preset"


async def test_coding_preset_secure_false_no_path_guard():
    """secure=False must not wire any path guard on reader or editor."""
    config = AgentConfig.coding(secure=False)
    assert not config.hook_handlers.get("pre:reader"), (
        "secure=False must not wire any pre:reader hook"
    )
    assert not config.hook_handlers.get("pre:editor"), (
        "secure=False must not wire any pre:editor hook"
    )


async def test_spec_coding_preset_reader_blocks_outside_workspace(tmp_path):
    """AgentSpec.coding() — reader outside workspace must be blocked."""
    spec = AgentSpec.coding(cwd=str(tmp_path))
    branch = await _make_branch_from_spec(spec)

    with pytest.raises(PermissionError):
        await _invoke_pre_hooks(branch, "reader", {"action": "read", "path": "/etc/passwd"})


async def test_spec_coding_preset_editor_blocks_outside_workspace(tmp_path):
    """AgentSpec.coding() — editor outside workspace must be blocked."""
    spec = AgentSpec.coding(cwd=str(tmp_path))
    branch = await _make_branch_from_spec(spec)

    with pytest.raises(PermissionError):
        await _invoke_pre_hooks(
            branch,
            "editor",
            {"action": "write", "file_path": "/etc/passwd", "content": "bad"},
        )


# ---------------------------------------------------------------------------
# Relative-path regression tests (guard_paths must resolve against workspace)
# ---------------------------------------------------------------------------
# These guard against a regression where relative paths like "src/foo.py" were
# resolved against the process cwd instead of the configured workspace root,
# causing valid in-workspace relative paths to be wrongly blocked.


async def test_coding_preset_reader_allows_relative_in_workspace(tmp_path):
    """A workspace-relative reader path ("src/foo.py") must be allowed.

    The guard must resolve the relative path against the workspace root, not
    the process cwd, so agents can use natural relative paths inside their
    workspace.
    """
    config = AgentConfig.coding(cwd=str(tmp_path))
    branch = await _make_branch(config)

    result = await branch.acts.registry["reader"].preprocessor(
        {"action": "read", "path": "src/foo.py"}
    )
    assert result is None or isinstance(result, dict)


async def test_coding_preset_editor_allows_relative_in_workspace(tmp_path):
    """A workspace-relative editor path ("output.txt") must be allowed."""
    config = AgentConfig.coding(cwd=str(tmp_path))
    branch = await _make_branch(config)

    result = await branch.acts.registry["editor"].preprocessor(
        {"action": "write", "file_path": "output.txt", "content": "hello"}
    )
    assert result is None or isinstance(result, dict)


async def test_coding_preset_reader_blocks_relative_traversal(tmp_path):
    """A relative traversal ("../../etc/passwd") must be blocked.

    Even with a relative path, a ../ escape that lands outside the workspace
    root must be rejected — resolving relative against the workspace root
    must not weaken the escape check.
    """
    config = AgentConfig.coding(cwd=str(tmp_path))
    branch = await _make_branch(config)

    with pytest.raises(PermissionError):
        await _invoke_pre_hooks(branch, "reader", {"action": "read", "path": "../../etc/passwd"})


async def test_coding_preset_editor_blocks_relative_traversal(tmp_path):
    """A relative traversal via editor ("../../etc/cron.d/evil") must be blocked."""
    config = AgentConfig.coding(cwd=str(tmp_path))
    branch = await _make_branch(config)

    with pytest.raises(PermissionError):
        await _invoke_pre_hooks(
            branch,
            "editor",
            {"action": "write", "file_path": "../../etc/cron.d/evil", "content": "bad"},
        )


async def test_spec_coding_preset_reader_allows_relative_in_workspace(tmp_path):
    """AgentSpec.coding() — workspace-relative reader path must be allowed."""
    spec = AgentSpec.coding(cwd=str(tmp_path))
    branch = await _make_branch_from_spec(spec)

    result = await branch.acts.registry["reader"].preprocessor(
        {"action": "read", "path": "src/main.py"}
    )
    assert result is None or isinstance(result, dict)


async def test_spec_coding_preset_editor_allows_relative_in_workspace(tmp_path):
    """AgentSpec.coding() — workspace-relative editor path must be allowed."""
    spec = AgentSpec.coding(cwd=str(tmp_path))
    branch = await _make_branch_from_spec(spec)

    result = await branch.acts.registry["editor"].preprocessor(
        {"action": "write", "file_path": "lib/util.py", "content": "# util"}
    )
    assert result is None or isinstance(result, dict)


async def test_spec_coding_preset_reader_blocks_relative_traversal(tmp_path):
    """AgentSpec.coding() — relative traversal on reader must be blocked."""
    spec = AgentSpec.coding(cwd=str(tmp_path))
    branch = await _make_branch_from_spec(spec)

    with pytest.raises(PermissionError):
        await _invoke_pre_hooks(branch, "reader", {"action": "read", "path": "../../etc/passwd"})


async def test_spec_coding_preset_editor_blocks_relative_traversal(tmp_path):
    """AgentSpec.coding() — relative traversal on editor must be blocked."""
    spec = AgentSpec.coding(cwd=str(tmp_path))
    branch = await _make_branch_from_spec(spec)

    with pytest.raises(PermissionError):
        await _invoke_pre_hooks(
            branch,
            "editor",
            {"action": "write", "file_path": "../../etc/passwd", "content": "bad"},
        )
