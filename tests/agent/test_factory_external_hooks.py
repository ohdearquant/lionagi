# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for `_wire_external_hooks` (lionagi.agent.factory): attaching parsed
`hooks_external` entries to the seam their event maps to -- ActionManager's
tool pre/post hook chain for PreToolUse/PostToolUse, HookBus for the rest."""

from __future__ import annotations

from lionagi.agent.factory import _wire_external_hooks
from lionagi.agent.spec import AgentSpec
from lionagi.hooks.bus import HookBus, HookPoint
from lionagi.session.branch import Branch


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
