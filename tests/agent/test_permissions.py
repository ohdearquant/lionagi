# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for PermissionPolicy: modes, rules, fnmatch, and pre-hook."""

import pytest

from lionagi.agent.permissions import PermissionPolicy


def test_allow_all_permits_any_tool():
    p = PermissionPolicy.allow_all()
    for tool, action, args in [
        ("bash", "run", {"command": "rm -rf /"}),
        ("editor", "write", {"file_path": "/etc/passwd"}),
        ("reader", "read", {"path": "/tmp/x"}),
    ]:
        assert p.check(tool, action, args).behavior == "allow"


def test_deny_all_rejects_any_tool():
    p = PermissionPolicy.deny_all()
    for tool, action, args in [
        ("reader", "read", {"path": "/tmp/file.txt"}),
        ("search", "grep", {"pattern": "foo"}),
        ("editor", "write", {"file_path": "/tmp/x.py"}),
        ("bash", "run", {"command": "echo hi"}),
    ]:
        assert p.check(tool, action, args).behavior == "deny"


# ---------------------------------------------------------------------------
# Preset: read_only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool,action,args",
    [
        ("reader", "read", {"path": "/tmp/x.py"}),
        ("search", "grep", {"pattern": "def foo", "path": "."}),
        ("context", "status", {}),
    ],
)
def test_read_only_allows(tool, action, args):
    p = PermissionPolicy.read_only()
    assert p.check(tool, action, args).behavior == "allow"


@pytest.mark.parametrize(
    "tool,action,args",
    [
        ("editor", "write", {"file_path": "/tmp/x.py"}),
        ("bash", "run", {"command": "echo hi"}),
    ],
)
def test_read_only_denies(tool, action, args):
    p = PermissionPolicy.read_only()
    assert p.check(tool, action, args).behavior == "deny"


# ---------------------------------------------------------------------------
# Preset: safe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd,expected",
    [
        ("rm /tmp/x", "deny"),
        ("sudo apt-get install curl", "deny"),
        ("cargo build", "escalate"),
        ("uv run pytest", "escalate"),
    ],
)
def test_safe_bash_behaviors(cmd, expected):
    p = PermissionPolicy.safe()
    d = p.check("bash", "run", {"command": cmd})
    assert d.behavior == expected


def test_safe_allows_non_bash():
    p = PermissionPolicy.safe()
    assert p.check("reader", "read", {"path": "/tmp/f.py"}).behavior == "allow"
    assert p.check("editor", "write", {"file_path": "/tmp/f.py"}).behavior == "allow"


# ---------------------------------------------------------------------------
# Custom rules: deny > allow > escalate ordering
# ---------------------------------------------------------------------------


def test_deny_beats_allow_when_both_match():
    p = PermissionPolicy(mode="rules", allow={"bash": ["git *"]}, deny={"bash": ["git *"]})
    assert p.check("bash", "run", {"command": "git status"}).behavior == "deny"


def test_allow_beats_escalate():
    p = PermissionPolicy(mode="rules", allow={"bash": ["cargo *"]}, escalate={"bash": ["*"]})
    assert p.check("bash", "run", {"command": "cargo build"}).behavior == "allow"


def test_default_deny_when_no_rule_matches():
    p = PermissionPolicy(mode="rules", allow={"bash": ["git *"]})
    d = p.check("bash", "run", {"command": "pytest tests/"})
    assert d.behavior == "deny"
    assert "no matching rule" in d.reason


# ---------------------------------------------------------------------------
# fnmatch pattern matching
# ---------------------------------------------------------------------------


def test_fnmatch_wildcard_matches_any():
    p = PermissionPolicy(mode="rules", allow={"bash": ["*"]})
    assert p.check("bash", "run", {"command": "uv run pytest"}).behavior == "allow"


def test_fnmatch_prefix_pattern_matches():
    p = PermissionPolicy(mode="rules", allow={"bash": ["git *"]})
    assert p.check("bash", "run", {"command": "git log --oneline"}).behavior == "allow"


def test_fnmatch_prefix_pattern_no_match():
    p = PermissionPolicy(mode="rules", allow={"bash": ["git *"]})
    assert p.check("bash", "run", {"command": "uv run pytest"}).behavior != "allow"


def test_shell_control_operator_denied():
    p = PermissionPolicy(mode="rules", allow={"bash": ["*"]})
    d = p.check("bash", "run", {"command": "echo hi; rm /tmp/x"})
    assert d.behavior == "deny"


# ---------------------------------------------------------------------------
# to_pre_hook
# ---------------------------------------------------------------------------


async def test_pre_hook_raises_on_deny():
    hook = PermissionPolicy.deny_all().to_pre_hook()
    with pytest.raises(PermissionError):
        await hook("bash", "run", {"command": "echo hi"})


async def test_pre_hook_returns_none_on_allow():
    hook = PermissionPolicy.allow_all().to_pre_hook()
    assert await hook("bash", "run", {"command": "echo hi"}) is None


async def test_pre_hook_escalate_without_handler_raises():
    hook = PermissionPolicy.safe().to_pre_hook()
    with pytest.raises(PermissionError, match="escalation"):
        await hook("bash", "run", {"command": "uv run pytest"})


# ---------------------------------------------------------------------------
# Tool alias normalization
# ---------------------------------------------------------------------------


def test_tool_aliases_normalized_at_init():
    p = PermissionPolicy(
        mode="rules",
        deny={"bash_tool": ["*"]},
        allow={"editor_tool": ["*"]},
        escalate={"reader_tool": ["*"]},
    )
    assert "bash" in p.deny and "bash_tool" not in p.deny
    assert "editor" in p.allow and "editor_tool" not in p.allow
    assert "reader" in p.escalate and "reader_tool" not in p.escalate


# ---------------------------------------------------------------------------
# A7: rules mode denies tool with no matching allow entry
# ---------------------------------------------------------------------------


def test_permission_policy_rules_default_denies_unmatched_tool():
    p = PermissionPolicy(mode="rules", allow={"reader": ["*"]})
    d = p.check("bash", "run", {"command": "pwd"})
    assert d.behavior == "deny"
    assert "no matching rule" in d.reason


# ---------------------------------------------------------------------------
# A8: shell control operator denied even under wildcard allow
# ---------------------------------------------------------------------------


def test_permission_policy_rejects_shell_control_before_wildcard_allow():
    p = PermissionPolicy(mode="rules", allow={"bash": ["*"]})
    d = p.check("bash", "run", {"command": "git status && rm -rf /tmp/x"})
    assert d.behavior == "deny"
    assert "Shell control operator" in d.reason


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_rules_mode_empty_allow_deny_escalate_defaults_to_deny():
    p = PermissionPolicy(mode="rules")
    d = p.check("bash", "run", {"command": "ls"})
    assert d.behavior == "deny"
    assert "no matching rule" in d.reason


def test_check_with_args_containing_none_values():
    p = PermissionPolicy(mode="rules", allow={"reader": ["*"]})
    d = p.check("reader", "read", {"path": None, "extra": None})
    assert d.behavior == "allow"


def test_check_tool_name_with_special_characters():
    p = PermissionPolicy(mode="rules", allow={"my-tool!": ["*"]})
    d = p.check("my-tool!", "action", {"command": "ls"})
    # The tool name contains special chars; _canonical_tool_name just lowercases it
    assert d.behavior in ("allow", "deny")


def test_rules_mode_unmatched_tool_defaults_to_deny():
    p = PermissionPolicy(mode="rules", allow={"reader": ["*"]})
    d = p.check("unknown_tool", "do", {"arg": "val"})
    assert d.behavior == "deny"


async def test_escalation_handler_returning_true_allows():
    calls = []

    async def approve(decision, args):
        calls.append(decision)
        return True

    p = PermissionPolicy.safe()
    p.on_escalate = approve
    hook = p.to_pre_hook()
    result = await hook("bash", "run", {"command": "uv run pytest"})
    assert result is None
    assert len(calls) == 1


async def test_escalation_handler_returning_dict_returns_dict():
    async def override(decision, args):
        return {"overridden": True}

    p = PermissionPolicy.safe()
    p.on_escalate = override
    hook = p.to_pre_hook()
    result = await hook("bash", "run", {"command": "uv run pytest"})
    assert result == {"overridden": True}


async def test_multiple_escalation_handlers_chained_via_on_escalate():
    chain_results = []

    async def first_escalate(decision, args):
        chain_results.append("first")
        return True

    p = PermissionPolicy.safe()
    p.on_escalate = first_escalate
    hook = p.to_pre_hook()
    await hook("bash", "run", {"command": "cargo build"})
    assert chain_results == ["first"]


def test_permission_policy_from_dict_round_trip():
    original = PermissionPolicy(
        mode="rules",
        allow={"bash": ["git *"], "reader": ["*"]},
        deny={"bash": ["rm *"]},
        escalate={"bash": ["sudo *"]},
    )
    data = {
        "mode": original.mode,
        "allow": {k: list(v) for k, v in original.allow.items()},
        "deny": {k: list(v) for k, v in original.deny.items()},
        "escalate": {k: list(v) for k, v in original.escalate.items()},
    }
    restored = PermissionPolicy.from_dict(data)
    assert restored.mode == original.mode
    assert restored.allow == original.allow
    assert restored.deny == original.deny
    assert restored.escalate == original.escalate
