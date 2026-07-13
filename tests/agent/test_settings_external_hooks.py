# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the `hooks_external:` settings block (D6): parsing, validation,
and that it lands on AgentSpec.external_hooks distinct from the legacy
hook_handlers dict."""

from __future__ import annotations

import pytest

from lionagi.agent.settings import apply_hooks_from_settings, parse_external_hooks
from lionagi.agent.spec import AgentSpec
from lionagi.hooks.external import ExternalHookConfigError


def test_parse_external_hooks_basic_entry():
    config = {
        "PreToolUse": [
            {
                "matcher": "bash|shell",
                "hooks": [{"type": "command", "command": ["uv", "run", "guard.py"], "timeout": 30}],
            }
        ]
    }
    entries = parse_external_hooks(config)
    assert entries == [
        {
            "event": "PreToolUse",
            "matcher": "bash|shell",
            "command": ["uv", "run", "guard.py"],
            "timeout": 30.0,
            "source": None,
        }
    ]


def test_parse_external_hooks_defaults_timeout_to_60():
    config = {"UserPromptSubmit": [{"hooks": [{"command": ["./hygiene"]}]}]}
    entries = parse_external_hooks(config)
    assert entries[0]["timeout"] == 60.0
    assert entries[0]["matcher"] is None


def test_parse_external_hooks_preserves_source_provenance():
    config = {"PreToolUse": [{"hooks": [{"command": ["guard"], "source": "imported:claude"}]}]}
    entries = parse_external_hooks(config)
    assert entries[0]["source"] == "imported:claude"


def test_parse_external_hooks_rejects_unmappable_event():
    config = {"Stop": [{"hooks": [{"command": ["guard"]}]}]}
    with pytest.raises(ExternalHookConfigError, match="no seam for event 'Stop'"):
        parse_external_hooks(config)


def test_parse_external_hooks_rejects_non_command_type():
    config = {"PreToolUse": [{"hooks": [{"type": "http", "command": ["guard"]}]}]}
    with pytest.raises(ExternalHookConfigError, match="unsupported hook type 'http'"):
        parse_external_hooks(config)


def test_parse_external_hooks_rejects_shell_string_command():
    config = {"PreToolUse": [{"hooks": [{"command": "echo unsafe"}]}]}
    with pytest.raises(ExternalHookConfigError, match="argv list"):
        parse_external_hooks(config)


def test_parse_external_hooks_rejects_empty_argv():
    config = {"PreToolUse": [{"hooks": [{"command": []}]}]}
    with pytest.raises(ExternalHookConfigError):
        parse_external_hooks(config)


def test_parse_external_hooks_multiple_events_and_matchers():
    config = {
        "PreToolUse": [
            {"matcher": "bash", "hooks": [{"command": ["guard1"]}]},
            {"matcher": "reader", "hooks": [{"command": ["guard2"]}]},
        ],
        "SessionStart": [{"hooks": [{"command": ["notify"]}]}],
    }
    entries = parse_external_hooks(config)
    assert len(entries) == 3
    events = {e["event"] for e in entries}
    assert events == {"PreToolUse", "SessionStart"}


# ---------------------------------------------------------------------------
# apply_hooks_from_settings: hooks_external lands on config.external_hooks,
# never on config.hook_handlers (the legacy shape's storage).
# ---------------------------------------------------------------------------


def test_apply_hooks_from_settings_populates_external_hooks_field():
    settings = {
        "hooks_external": {
            "PreToolUse": [{"hooks": [{"command": ["guard"]}]}],
        }
    }
    spec = AgentSpec.compose("implementer")
    apply_hooks_from_settings(spec, settings)

    assert len(spec.external_hooks) == 1
    assert spec.external_hooks[0]["event"] == "PreToolUse"
    # The legacy hook_handlers dict is untouched by the new block.
    assert spec.hook_handlers == {}


def test_apply_hooks_from_settings_legacy_and_external_coexist():
    settings = {
        "hooks": {"pre": {"bash": [{"command": ["legacy_guard"]}]}},
        "hooks_external": {"PreToolUse": [{"hooks": [{"command": ["new_guard"]}]}]},
    }
    spec = AgentSpec.compose("implementer")
    apply_hooks_from_settings(spec, settings)

    assert "pre:bash" in spec.hook_handlers
    assert len(spec.external_hooks) == 1


def test_apply_hooks_from_settings_no_hooks_external_key_is_a_noop():
    spec = AgentSpec.compose("implementer")
    apply_hooks_from_settings(spec, {})
    assert spec.external_hooks == []
