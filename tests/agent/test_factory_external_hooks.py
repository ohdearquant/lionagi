# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for `_wire_external_hooks` (lionagi.agent.factory): attaching parsed
`hooks_external` entries to the seam their event maps to -- ActionManager's
tool pre/post hook chain for PreToolUse/PostToolUse, HookBus for the rest."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from lionagi.agent.factory import _wire_external_hooks, create_agent
from lionagi.agent.spec import AgentSpec
from lionagi.hooks.bus import HookBus, HookPoint
from lionagi.session.branch import Branch
from lionagi.session.session import Session


def _spec_with(entries: list[dict]) -> AgentSpec:
    spec = AgentSpec.compose("implementer")
    spec.external_hooks = entries
    return spec


def test_no_external_hooks_is_a_noop():
    branch = Branch()
    spec = _spec_with([])
    _wire_external_hooks(branch, spec)
    assert branch.acts._tool_pre_hooks == []
    assert branch.acts._tool_post_hooks == []


def test_pre_tool_use_attaches_to_action_manager():
    branch = Branch()
    spec = _spec_with(
        [
            {
                "event": "PreToolUse",
                "matcher": None,
                "command": ["guard"],
                "timeout": 30.0,
                "source": None,
            }
        ]
    )
    _wire_external_hooks(branch, spec)
    assert len(branch.acts._tool_pre_hooks) == 1
    assert branch.acts._tool_post_hooks == []


def test_post_tool_use_attaches_to_action_manager():
    branch = Branch()
    spec = _spec_with(
        [
            {
                "event": "PostToolUse",
                "matcher": None,
                "command": ["notify"],
                "timeout": 30.0,
                "source": None,
            }
        ]
    )
    _wire_external_hooks(branch, spec)
    assert len(branch.acts._tool_post_hooks) == 1
    assert branch.acts._tool_pre_hooks == []


def test_user_prompt_submit_attaches_to_hook_bus_when_present():
    branch = Branch()
    branch._hooks = HookBus()
    spec = _spec_with(
        [
            {
                "event": "UserPromptSubmit",
                "matcher": None,
                "command": ["hygiene"],
                "timeout": 30.0,
                "source": None,
            }
        ]
    )
    _wire_external_hooks(branch, spec)
    assert len(branch._hooks.handlers_for(HookPoint.USER_PROMPT_SUBMIT)) == 1


def test_user_prompt_submit_skipped_without_hook_bus(caplog):
    branch = Branch()
    assert branch._hooks is None
    spec = _spec_with(
        [
            {
                "event": "UserPromptSubmit",
                "matcher": None,
                "command": ["hygiene"],
                "timeout": 30.0,
                "source": None,
            }
        ]
    )
    _wire_external_hooks(branch, spec)  # must not raise
    assert branch._hooks is None


def test_user_prompt_submit_without_hook_bus_is_queued_not_dropped():
    """A HookBus-only external hook configured before the branch has a bus
    must not be lost: it queues onto `_pending_hook_bus_entries` so it can
    still attach once the branch acquires one (`Branch.attach_hook_bus`)."""
    branch = Branch()
    spec = _spec_with(
        [
            {
                "event": "UserPromptSubmit",
                "matcher": None,
                "command": ["hygiene"],
                "timeout": 30.0,
                "source": None,
            }
        ]
    )
    _wire_external_hooks(branch, spec)
    assert branch._hooks is None
    assert len(branch._pending_hook_bus_entries) == 1

    bus = HookBus()
    branch.attach_hook_bus(bus)
    assert len(branch._hooks.handlers_for(HookPoint.USER_PROMPT_SUBMIT)) == 1
    # Retained (not cleared) so the branch can re-register onto a later bus
    # if reparented; re-attaching the same bus must not double-register.
    assert len(branch._pending_hook_bus_entries) == 1
    branch.attach_hook_bus(bus)
    assert len(branch._hooks.handlers_for(HookPoint.USER_PROMPT_SUBMIT)) == 1


def test_session_start_and_error_route_to_hook_bus():
    branch = Branch()
    branch._hooks = HookBus()
    spec = _spec_with(
        [
            {
                "event": "SessionStart",
                "matcher": None,
                "command": ["a"],
                "timeout": 30.0,
                "source": None,
            },
            {
                "event": "PostToolUseFailure",
                "matcher": None,
                "command": ["b"],
                "timeout": 30.0,
                "source": None,
            },
        ]
    )
    _wire_external_hooks(branch, spec)
    assert len(branch._hooks.handlers_for(HookPoint.SESSION_START)) == 1
    assert len(branch._hooks.handlers_for(HookPoint.TOOL_ERROR)) == 1


def test_multiple_entries_wire_independently():
    branch = Branch()
    branch._hooks = HookBus()
    spec = _spec_with(
        [
            {
                "event": "PreToolUse",
                "matcher": "bash",
                "command": ["g1"],
                "timeout": 30.0,
                "source": None,
            },
            {
                "event": "PreToolUse",
                "matcher": "reader",
                "command": ["g2"],
                "timeout": 30.0,
                "source": None,
            },
            {
                "event": "UserPromptSubmit",
                "matcher": None,
                "command": ["hyg"],
                "timeout": 30.0,
                "source": None,
            },
        ]
    )
    _wire_external_hooks(branch, spec)
    assert len(branch.acts._tool_pre_hooks) == 2
    assert len(branch._hooks.handlers_for(HookPoint.USER_PROMPT_SUBMIT)) == 1


# ---------------------------------------------------------------------------
# Session-bus wiring: create_agent() always builds a standalone branch with
# no HookBus yet; a configured UserPromptSubmit hook must still fire once
# that branch joins a Session (the only thing that ever attaches a bus).
# ---------------------------------------------------------------------------


class _FakeStream:
    """Minimal async-read stand-in for a StreamReader: returns *data* on the
    first ``read()`` call, then EOF -- matches how ``_read_capped``'s
    read-until-empty loop drains a real pipe (see ``lionagi.hooks.external``)."""

    def __init__(self, data: bytes = b""):
        self._data = data
        self._sent = False

    async def read(self, n: int = -1) -> bytes:
        if self._sent:
            return b""
        self._sent = True
        return self._data


class _FakeStdin:
    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


def _mock_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.pid = 4242
    proc.stdin = _FakeStdin()
    proc.stdout = _FakeStream(stdout)
    proc.stderr = _FakeStream(stderr)
    proc.wait = AsyncMock(return_value=returncode)
    return proc


async def test_user_prompt_submit_hook_fires_after_create_agent_and_session_inclusion(
    monkeypatch, tmp_path
):
    # Isolate HOME so `create_agent`'s MCP auto-load (it always checks
    # ~/.lionagi/.mcp.json, see `_resolve_mcp_path`) can't pick up a real
    # operator config and consume the single mocked subprocess below before
    # the external hook itself gets to invoke it.
    monkeypatch.setenv("HOME", str(tmp_path))
    stdout = json.dumps({"decision": "block", "reason": "hygiene check failed"}).encode()
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=_mock_proc(0, stdout=stdout))
    )

    spec = _spec_with(
        [
            {
                "event": "UserPromptSubmit",
                "matcher": None,
                "command": ["hygiene"],
                "timeout": 30.0,
                "source": None,
            }
        ]
    )
    branch = await create_agent(spec, load_settings=False)
    assert branch._hooks is None, "standalone branch: no bus exists yet"

    session = Session()
    session.include_branches(branch)
    # Session.hooks lazily creates the bus and must flush the queued
    # UserPromptSubmit handler onto every branch it already owns.
    assert len(session.hooks.handlers_for(HookPoint.USER_PROMPT_SUBMIT)) == 1
    assert branch._hooks is session.hooks

    with pytest.raises(PermissionError, match="hygiene check failed"):
        await branch._hooks.blocking_emit(
            HookPoint.USER_PROMPT_SUBMIT, session_id=str(session.id), prompt="do a thing"
        )


async def test_user_prompt_submit_hook_survives_reparent_to_another_session(monkeypatch, tmp_path):
    """A blocking external hook must not silently vanish when its branch is
    moved between sessions (`Session.remove_branch` then `include_branches`
    on another session, a supported reparenting op). The handler was queued
    onto `_pending_hook_bus_entries` while the branch was standalone, then
    flushed onto session A's bus; it must also flush onto session B's bus."""
    # Isolate HOME -- see the sibling test above for why.
    monkeypatch.setenv("HOME", str(tmp_path))
    stdout = json.dumps({"decision": "block", "reason": "hygiene check failed"}).encode()
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=_mock_proc(0, stdout=stdout))
    )

    spec = _spec_with(
        [
            {
                "event": "UserPromptSubmit",
                "matcher": None,
                "command": ["hygiene"],
                "timeout": 30.0,
                "source": None,
            }
        ]
    )
    branch = await create_agent(spec, load_settings=False)

    session_a = Session()
    _ = session_a.hooks  # bus exists before the branch joins
    session_a.include_branches(branch)
    assert len(session_a.hooks.handlers_for(HookPoint.USER_PROMPT_SUBMIT)) == 1

    session_a.remove_branch(branch)
    session_b = Session()
    _ = session_b.hooks  # bus exists before the reparented branch joins
    session_b.include_branches(branch)

    assert branch._hooks is session_b.hooks
    assert len(session_b.hooks.handlers_for(HookPoint.USER_PROMPT_SUBMIT)) == 1

    with pytest.raises(PermissionError, match="hygiene check failed"):
        await branch._hooks.blocking_emit(
            HookPoint.USER_PROMPT_SUBMIT, session_id=str(session_b.id), prompt="do a thing"
        )


# ---------------------------------------------------------------------------
# Cross-branch isolation: a session's HookBus is shared by every branch it
# owns, so a branch-owned external handler must not fire for another
# branch's event, and a reparented/removed branch must not leave a stale
# registration behind on its old session's bus.
# ---------------------------------------------------------------------------


async def test_two_branches_with_different_hooks_do_not_observe_each_others_prompts(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    calls: list[str] = []

    async def fake_exec(*argv, **kwargs):
        calls.append(argv[0] if argv else kwargs.get("executable"))
        return _mock_proc(0, stdout=b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    def _spec_for(command: str) -> AgentSpec:
        return _spec_with(
            [
                {
                    "event": "UserPromptSubmit",
                    "matcher": None,
                    "command": [command],
                    "timeout": 30.0,
                    "source": None,
                }
            ]
        )

    branch1 = await create_agent(_spec_for("hygiene-1"), load_settings=False)
    branch2 = await create_agent(_spec_for("hygiene-2"), load_settings=False)

    session = Session()
    session.include_branches(branch1)
    session.include_branches(branch2)
    assert len(session.hooks.handlers_for(HookPoint.USER_PROMPT_SUBMIT)) == 2

    await branch1._hooks.blocking_emit(
        HookPoint.USER_PROMPT_SUBMIT,
        session_id=str(session.id),
        branch_id=str(branch1.id),
        prompt="from branch1",
    )
    assert calls == ["hygiene-1"], "branch2's handler must not fire for branch1's prompt"

    calls.clear()
    await branch2._hooks.blocking_emit(
        HookPoint.USER_PROMPT_SUBMIT,
        session_id=str(session.id),
        branch_id=str(branch2.id),
        prompt="from branch2",
    )
    assert calls == ["hygiene-2"], "branch1's handler must not fire for branch2's prompt"


async def test_reparented_branch_leaves_no_handler_on_the_old_sessions_bus(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    stdout = json.dumps({"decision": "block", "reason": "hygiene check failed"}).encode()
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=_mock_proc(0, stdout=stdout))
    )

    spec = _spec_with(
        [
            {
                "event": "UserPromptSubmit",
                "matcher": None,
                "command": ["hygiene"],
                "timeout": 30.0,
                "source": None,
            }
        ]
    )
    branch = await create_agent(spec, load_settings=False)

    session_a = Session()
    _ = session_a.hooks
    session_a.include_branches(branch)
    assert len(session_a.hooks.handlers_for(HookPoint.USER_PROMPT_SUBMIT)) == 1

    session_a.remove_branch(branch)
    assert len(session_a.hooks.handlers_for(HookPoint.USER_PROMPT_SUBMIT)) == 0, (
        "removing a branch must unregister its handlers from the old bus, "
        "not just clear the branch's own reference to it"
    )

    session_b = Session()
    _ = session_b.hooks
    session_b.include_branches(branch)
    assert len(session_b.hooks.handlers_for(HookPoint.USER_PROMPT_SUBMIT)) == 1
    assert len(session_a.hooks.handlers_for(HookPoint.USER_PROMPT_SUBMIT)) == 0
