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
