# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the external-hook exec adapter: envelope construction, the
exit-code/stdout contract, timeout handling, and the trust gate."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from lionagi.hooks.external import (
    ExternalHookConfigError,
    build_envelope,
    compute_command_hash,
    external_hook_adapter,
    is_command_trusted,
    match_hook,
    validate_argv,
)
from lionagi.protocols.action.tool_hooks import ToolPostDecision, ToolPreDecision

# ---------------------------------------------------------------------------
# validate_argv (D4: non-empty argv of non-empty/non-whitespace strings)
# ---------------------------------------------------------------------------


def test_validate_argv_accepts_nonempty_string_list():
    assert validate_argv(["uv", "run", "guard.py"]) == ["uv", "run", "guard.py"]


@pytest.mark.parametrize(
    "bad",
    [
        "echo unsafe",  # shell string, not a list
        [],  # empty list
        ["ok", ""],  # blank entry
        ["ok", "   "],  # whitespace-only entry
        ["ok", 1],  # non-string entry
        None,
    ],
)
def test_validate_argv_rejects(bad):
    with pytest.raises(ExternalHookConfigError):
        validate_argv(bad)


# ---------------------------------------------------------------------------
# build_envelope: D1 field guarantees per event
# ---------------------------------------------------------------------------


def test_envelope_common_fields_always_present():
    env = build_envelope(
        hook_event_name="PreToolUse",
        session_id="s-1",
        cwd="/tmp/proj",
        tool_name="bash",
        tool_input={"command": ["git", "status"]},
    )
    assert env["session_id"] == "s-1"
    assert env["cwd"] == "/tmp/proj"
    assert env["hook_event_name"] == "PreToolUse"
    assert env["harness"] == "lionagi"


def test_envelope_pre_tool_use_guarantees_tool_name_and_input():
    env = build_envelope(
        hook_event_name="PreToolUse",
        session_id="s",
        cwd="/",
        tool_name="bash",
        tool_input={"command": ["ls"]},
    )
    assert env["tool_name"] == "bash"
    assert env["tool_input"] == {"command": ["ls"]}
    assert "tool_response" not in env


def test_envelope_post_tool_use_guarantees_tool_response():
    env = build_envelope(
        hook_event_name="PostToolUse",
        session_id="s",
        cwd="/",
        tool_name="bash",
        tool_input={"command": ["ls"]},
        tool_response={"stdout": "ok"},
    )
    assert env["tool_response"] == {"stdout": "ok"}


def test_envelope_user_prompt_submit_guarantees_prompt_model_permission_mode():
    env = build_envelope(
        hook_event_name="UserPromptSubmit",
        session_id="s",
        cwd="/",
        prompt="hi there",
        model="claude-sonnet",
    )
    assert env["prompt"] == "hi there"
    assert env["model"] == "claude-sonnet"
    # No PermissionPolicy attached at this call site -> "default", per the ADR's
    # explicit fallback rule (not an omission).
    assert env["permission_mode"] == "default"


def test_envelope_post_tool_use_failure_guarantees_tool_response_as_error():
    env = build_envelope(
        hook_event_name="PostToolUseFailure",
        session_id="s",
        cwd="/",
        tool_name="bash",
        tool_input=None,
        tool_response={"error": "boom"},
    )
    assert env["tool_response"] == {"error": "boom"}


# ---------------------------------------------------------------------------
# match_hook: harness matcher semantics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("matcher", [None, "", "*"])
def test_match_hook_empty_or_star_matches_everything(matcher):
    assert match_hook(matcher, "bash") is True
    assert match_hook(matcher, "anything") is True


def test_match_hook_exact_or_list():
    assert match_hook("bash", "bash") is True
    assert match_hook("bash", "reader") is False
    assert match_hook("bash,reader", "reader") is True
    assert match_hook("bash|reader", "editor") is False


def test_match_hook_unanchored_regex_fallback():
    assert match_hook("^bash$", "bash") is True
    assert match_hook("bash.*", "bash_tool") is True


# ---------------------------------------------------------------------------
# Trust gate (D7)
# ---------------------------------------------------------------------------


def test_is_command_trusted_no_source_is_project_authored():
    assert is_command_trusted(["guard"], source=None) is True


def test_is_command_trusted_imported_without_record_is_untrusted(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert is_command_trusted(["guard"], source="imported:claude") is False


def test_is_command_trusted_imported_with_matching_hash(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    from lionagi.plugins._user_settings import write_user_settings

    command = ["guard", "--check"]
    write_user_settings({"trusted_hook_commands": [compute_command_hash(command)]})
    assert is_command_trusted(command, source="imported:claude") is True


def test_command_hash_changes_with_argv():
    assert compute_command_hash(["a", "b"]) != compute_command_hash(["a", "c"])


# ---------------------------------------------------------------------------
# external_hook_adapter: unsupported event fails config load
# ---------------------------------------------------------------------------


def test_adapter_rejects_unmappable_event():
    with pytest.raises(ExternalHookConfigError, match="no seam"):
        external_hook_adapter(event="Stop", command=["guard"])


def test_adapter_rejects_invalid_argv():
    with pytest.raises(ExternalHookConfigError):
        external_hook_adapter(event="PreToolUse", command="echo unsafe")


# ---------------------------------------------------------------------------
# PreToolUse: exit-code contract + stdout decision parsing
# ---------------------------------------------------------------------------


def _mock_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.pid = 4242
    return proc


async def test_pre_tool_use_exit_zero_no_stdout_allows(monkeypatch):
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=_mock_proc(0)))
    hook = external_hook_adapter(event="PreToolUse", command=["guard"])
    result = await hook("bash", {"command": ["ls"]})
    assert result == ToolPreDecision(decision="allow", updated_input=None)


async def test_pre_tool_use_exit_zero_with_updated_input(monkeypatch):
    stdout = json.dumps(
        {
            "hookSpecificOutput": {
                "permissionDecision": "allow",
                "updatedInput": {"command": ["ls", "-la"]},
            }
        }
    ).encode()
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=_mock_proc(0, stdout=stdout))
    )
    hook = external_hook_adapter(event="PreToolUse", command=["guard"])
    result = await hook("bash", {"command": ["ls"]})
    assert result.decision == "allow"
    assert result.updated_input == {"command": ["ls", "-la"]}


async def test_pre_tool_use_exit_two_is_deny_with_stderr_reason(monkeypatch):
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=_mock_proc(2, stderr=b"blocked: destructive command")),
    )
    hook = external_hook_adapter(event="PreToolUse", command=["guard"])
    result = await hook("bash", {"command": ["rm", "-rf", "/"]})
    assert result.decision == "deny"
    assert "blocked" in result.reason


async def test_pre_tool_use_other_nonzero_exit_is_deny_with_diagnostic(monkeypatch):
    """Distinguishes exit 2 (block) from any other nonzero exit (hook failure) --
    PreToolUse is a blocking seam, so a hook failure still fails closed."""
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=_mock_proc(1, stderr=b"crashed")),
    )
    hook = external_hook_adapter(event="PreToolUse", command=["guard"])
    result = await hook("bash", {"command": ["ls"]})
    assert result.decision == "deny"
    assert "crashed" in result.reason


async def test_pre_tool_use_stdout_deny_decision(monkeypatch):
    stdout = json.dumps(
        {
            "hookSpecificOutput": {
                "permissionDecision": "deny",
                "permissionDecisionReason": "not allowed",
            }
        }
    ).encode()
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=_mock_proc(0, stdout=stdout))
    )
    hook = external_hook_adapter(event="PreToolUse", command=["guard"])
    result = await hook("bash", {"command": ["ls"]})
    assert result.decision == "deny"
    assert result.reason == "not allowed"


async def test_pre_tool_use_ask_fails_closed(monkeypatch):
    stdout = json.dumps({"hookSpecificOutput": {"permissionDecision": "ask"}}).encode()
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=_mock_proc(0, stdout=stdout))
    )
    hook = external_hook_adapter(event="PreToolUse", command=["guard"])
    result = await hook("bash", {"command": ["ls"]})
    assert result.decision == "deny"
    assert "ask" in result.reason


async def test_pre_tool_use_unrecognized_decision_fails_closed(monkeypatch):
    stdout = json.dumps({"hookSpecificOutput": {"permissionDecision": "maybe"}}).encode()
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=_mock_proc(0, stdout=stdout))
    )
    hook = external_hook_adapter(event="PreToolUse", command=["guard"])
    result = await hook("bash", {"command": ["ls"]})
    assert result.decision == "deny"
    assert "maybe" in result.reason


async def test_pre_tool_use_top_level_unrecognized_decision_fails_closed(monkeypatch):
    """The top-level `decision` shape must fail closed the same way the nested
    `hookSpecificOutput.permissionDecision` shape does: an explicit but
    unrecognized value (e.g. "maybe") must never fall through to allow."""
    stdout = json.dumps({"decision": "maybe", "reason": "unexpected"}).encode()
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=_mock_proc(0, stdout=stdout))
    )
    hook = external_hook_adapter(event="PreToolUse", command=["guard"])
    result = await hook("bash", {"command": ["ls"]})
    assert result.decision == "deny"
    assert "maybe" in result.reason


@pytest.mark.parametrize("value", ["allow", "approve"])
async def test_pre_tool_use_top_level_allow_synonyms_allow(monkeypatch, value):
    stdout = json.dumps({"decision": value}).encode()
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=_mock_proc(0, stdout=stdout))
    )
    hook = external_hook_adapter(event="PreToolUse", command=["guard"])
    result = await hook("bash", {"command": ["ls"]})
    assert result.decision == "allow"


async def test_pre_tool_use_nonjson_stdout_is_treated_as_no_decision(monkeypatch):
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=_mock_proc(0, stdout=b"not json at all")),
    )
    hook = external_hook_adapter(event="PreToolUse", command=["guard"])
    result = await hook("bash", {"command": ["ls"]})
    assert result.decision == "allow"


async def test_pre_tool_use_matcher_skips_non_matching_tool(monkeypatch):
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    hook = external_hook_adapter(event="PreToolUse", command=["guard"], matcher="reader")
    result = await hook("bash", {"command": ["ls"]})
    assert result is None
    spawn.assert_not_called()


# ---------------------------------------------------------------------------
# PostToolUse: advisory only -- a deny/error becomes a surfaced note, never
# a raised exception (the action already happened).
# ---------------------------------------------------------------------------


async def test_post_tool_use_allow_returns_none(monkeypatch):
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=_mock_proc(0)))
    hook = external_hook_adapter(event="PostToolUse", command=["notify"])
    result = await hook("bash", {"command": ["ls"]}, {"stdout": "ok"}, None)
    assert result is None


async def test_post_tool_use_exit_two_surfaces_reason_not_raise(monkeypatch):
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=_mock_proc(2, stderr=b"flagged after the fact")),
    )
    hook = external_hook_adapter(event="PostToolUse", command=["notify"])
    result = await hook("bash", {"command": ["ls"]}, {"stdout": "ok"}, None)
    assert isinstance(result, ToolPostDecision)
    assert "flagged" in result.reason


async def test_post_tool_use_error_result_maps_tool_response_to_error_dict(monkeypatch):
    captured = {}

    async def fake_communicate(data):
        captured["envelope"] = json.loads(data.decode())
        return b"", b""

    proc = _mock_proc(0)
    proc.communicate = AsyncMock(side_effect=fake_communicate)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))

    hook = external_hook_adapter(event="PostToolUse", command=["notify"])
    err = ValueError("kaboom")
    await hook("bash", {"command": ["ls"]}, None, err)
    assert captured["envelope"]["tool_response"] == {"error": "kaboom"}


# ---------------------------------------------------------------------------
# Timeout: process group is killed; blocking events fail closed, advisory
# events are logged and continue.
# ---------------------------------------------------------------------------


async def test_pre_tool_use_timeout_kills_process_group_and_denies(monkeypatch):
    proc = MagicMock()
    proc.pid = 9999
    # asyncio.wait_for raises asyncio.TimeoutError, which is NOT the builtin
    # TimeoutError before 3.11 -- inject the real raised type so this exercises
    # the pre-3.11 teardown path rather than passing by luck on 3.11+.
    proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    proc.wait = AsyncMock(return_value=None)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))

    killed: list[int] = []
    monkeypatch.setattr("lionagi.ln._proc.os.killpg", lambda pgid, sig: killed.append(pgid))

    hook = external_hook_adapter(event="PreToolUse", command=["slow_guard"], timeout=0.01)
    result = await hook("bash", {"command": ["ls"]})
    assert result.decision == "deny"
    assert "timed out" in result.reason
    assert killed, "expected the hook's process group to be killed on timeout"


async def test_post_tool_use_timeout_surfaces_reason_not_raise(monkeypatch):
    proc = MagicMock()
    proc.pid = 8888
    proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    proc.wait = AsyncMock(return_value=None)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    monkeypatch.setattr("lionagi.ln._proc.os.killpg", lambda pgid, sig: None)

    hook = external_hook_adapter(event="PostToolUse", command=["slow_notify"], timeout=0.01)
    result = await hook("bash", {"command": ["ls"]}, {"ok": True}, None)
    assert isinstance(result, ToolPostDecision)
    assert "timed out" in result.reason


# ---------------------------------------------------------------------------
# UserPromptSubmit: blocking HookBus seam -- a deny verdict raises.
# ---------------------------------------------------------------------------


async def test_user_prompt_submit_deny_raises_permission_error(monkeypatch):
    stdout = json.dumps({"decision": "block", "reason": "hygiene check failed"}).encode()
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=_mock_proc(0, stdout=stdout))
    )
    hook = external_hook_adapter(event="UserPromptSubmit", command=["hygiene"])
    with pytest.raises(PermissionError, match="hygiene check failed"):
        await hook(session_id="s-1", branch_id="b-1", prompt="do a thing")


async def test_user_prompt_submit_allow_does_not_raise(monkeypatch):
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=_mock_proc(0)))
    hook = external_hook_adapter(event="UserPromptSubmit", command=["hygiene"])
    await hook(session_id="s-1", branch_id="b-1", prompt="do a thing")  # must not raise


# ---------------------------------------------------------------------------
# SessionStart/SessionEnd/PostToolUseFailure: advisory -- deny is logged, not raised.
# ---------------------------------------------------------------------------


async def test_session_start_deny_is_logged_not_raised(monkeypatch):
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=_mock_proc(2, stderr=b"nope"))
    )
    hook = external_hook_adapter(event="SessionStart", command=["notify"])
    await hook(session_id="s-1", model="sonnet")  # must not raise


async def test_post_tool_use_failure_stringifies_error_into_tool_response(monkeypatch):
    captured = {}

    async def fake_communicate(data):
        captured["envelope"] = json.loads(data.decode())
        return b"", b""

    proc = _mock_proc(0)
    proc.communicate = AsyncMock(side_effect=fake_communicate)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))

    hook = external_hook_adapter(event="PostToolUseFailure", command=["notify"])
    await hook(session_id="s-1", tool_name="bash", error=RuntimeError("disk full"))

    assert captured["envelope"]["tool_response"] == {"error": "disk full"}
    assert captured["envelope"]["tool_name"] == "bash"


# ---------------------------------------------------------------------------
# Untrusted command: imported entries never execute without a trust record.
# ---------------------------------------------------------------------------


async def test_untrusted_imported_pre_tool_use_denies_without_spawning(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    hook = external_hook_adapter(event="PreToolUse", command=["guard"], source="imported:claude")
    result = await hook("bash", {"command": ["ls"]})
    assert result.decision == "deny"
    assert "untrusted" in result.reason
    spawn.assert_not_called()


async def test_trusted_imported_pre_tool_use_executes(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    from lionagi.plugins._user_settings import write_user_settings

    command = ["guard"]
    write_user_settings({"trusted_hook_commands": [compute_command_hash(command)]})
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=_mock_proc(0)))
    hook = external_hook_adapter(event="PreToolUse", command=command, source="imported:claude")
    result = await hook("bash", {"command": ["ls"]})
    assert result.decision == "allow"


async def test_untrusted_imported_advisory_event_is_error_not_raise(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    hook = external_hook_adapter(event="SessionStart", command=["notify"], source="imported:codex")
    await hook(session_id="s-1")  # advisory: must not raise even though untrusted
    spawn.assert_not_called()
