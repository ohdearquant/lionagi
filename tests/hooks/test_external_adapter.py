# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the external-hook exec adapter: envelope construction, the
exit-code/stdout contract, timeout handling, and the trust gate."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from lionagi.hooks.external import (
    ExternalHookConfigError,
    build_envelope,
    compute_command_hash,
    compute_executable_digest,
    compute_trust_record,
    external_hook_adapter,
    is_command_trusted,
    match_hook,
    resolve_hook_executable,
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
# Trust gate (D7): content-pinned to the resolved executable, not argv alone
# ---------------------------------------------------------------------------


def test_is_command_trusted_no_source_is_project_authored():
    assert is_command_trusted(["guard"], source=None) is True


def test_is_command_trusted_imported_without_record_is_untrusted(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert is_command_trusted(["guard"], source="imported:claude", cwd=str(tmp_path)) is False


def test_is_command_trusted_imported_with_matching_record(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    from lionagi.plugins._user_settings import write_user_settings

    command = [sys.executable, "-c", "pass"]
    record = compute_trust_record(command, cwd=str(tmp_path))
    write_user_settings({"trusted_hook_commands": [record]})
    assert is_command_trusted(command, source="imported:claude", cwd=str(tmp_path)) is True


def test_command_hash_changes_with_argv():
    assert compute_command_hash(["a", "b"]) != compute_command_hash(["a", "c"])


# ---------------------------------------------------------------------------
# Content-pinned trust (Issue 3 fix): resolution, digesting, and the exact
# attack this closes -- an argv-only-hashed approval carrying over to a
# different resolved executable.
# ---------------------------------------------------------------------------


def test_resolve_hook_executable_relative_path(tmp_path):
    script = tmp_path / "guard"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    resolved = resolve_hook_executable(["./guard"], str(tmp_path))
    assert resolved == script.resolve()


def test_resolve_hook_executable_relative_path_missing_raises(tmp_path):
    with pytest.raises(ExternalHookConfigError, match="not found or not executable"):
        resolve_hook_executable(["./guard"], str(tmp_path))


def test_resolve_hook_executable_path_resolved_bare_name(tmp_path, monkeypatch):
    script = tmp_path / "myguard"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    resolved = resolve_hook_executable(["myguard"], str(tmp_path))
    assert resolved == script.resolve()


def test_resolve_hook_executable_bare_name_not_on_path_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))  # empty dir, nothing on PATH
    with pytest.raises(ExternalHookConfigError, match="not found on PATH"):
        resolve_hook_executable(["myguard"], str(tmp_path))


def test_compute_trust_record_content_pinning_detects_digest_change(tmp_path):
    script = tmp_path / "guard"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    before = compute_trust_record(["./guard"], str(tmp_path))

    script.write_text("#!/bin/sh\necho attacker-controlled\nexit 0\n")
    script.chmod(0o755)
    after = compute_trust_record(["./guard"], str(tmp_path))

    # argv identity (the old, insufficient trust key) is unchanged...
    assert before["argv_hash"] == after["argv_hash"]
    assert before["resolved_path"] == after["resolved_path"]
    # ...but the content digest of the resolved executable is not -- this is
    # the exact gap the content-pinning fix closes.
    assert before["content_digest"] != after["content_digest"]
    assert compute_executable_digest(script) == after["content_digest"]


def test_trust_does_not_carry_over_when_relative_command_resolves_elsewhere(monkeypatch, tmp_path):
    """The exact attack in the verdict: a prior approval of `["./guard"]` in
    one directory must not authorize a DIFFERENT `./guard` that the same argv
    resolves to in a different directory (or after the file at that path
    changed) -- content pinning, not argv-only hashing, must gate this."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from lionagi.plugins._user_settings import write_user_settings

    approved_dir = tmp_path / "approved"
    approved_dir.mkdir()
    approved_script = approved_dir / "guard"
    approved_script.write_text("#!/bin/sh\nexit 0\n")
    approved_script.chmod(0o755)

    record = compute_trust_record(["./guard"], str(approved_dir))
    write_user_settings({"trusted_hook_commands": [record]})
    assert is_command_trusted(["./guard"], source="imported:claude", cwd=str(approved_dir)) is True

    attacker_dir = tmp_path / "attacker"
    attacker_dir.mkdir()
    attacker_script = attacker_dir / "guard"
    attacker_script.write_text("#!/bin/sh\necho pwned\nexit 0\n")
    attacker_script.chmod(0o755)

    # Same argv (["./guard"]), a DIFFERENT resolved+content-digested executable.
    assert is_command_trusted(["./guard"], source="imported:claude", cwd=str(attacker_dir)) is False


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


class _FakeStream:
    """Minimal async-read stand-in for a StreamReader: returns *data* on the
    first ``read()`` call, then EOF (``b""``) forever after -- matches how
    ``_read_capped``'s read-until-empty loop drains a real pipe. Set
    *raises* to make ``read()`` raise instead (timeout simulation)."""

    def __init__(self, data: bytes = b"", *, raises: BaseException | None = None):
        self._data = data
        self._sent = False
        self._raises = raises

    async def read(self, n: int = -1) -> bytes:
        if self._raises is not None:
            raise self._raises
        if self._sent:
            return b""
        self._sent = True
        return self._data


class _FakeStdin:
    """Captures what ``_write_stdin`` writes so tests can assert on the
    envelope actually sent, without going through a real pipe."""

    def __init__(self):
        self.written = b""

    def write(self, data: bytes) -> None:
        self.written += data

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
    proc = _mock_proc(0)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))

    hook = external_hook_adapter(event="PostToolUse", command=["notify"])
    err = ValueError("kaboom")
    await hook("bash", {"command": ["ls"]}, None, err)
    envelope = json.loads(proc.stdin.written.decode())
    assert envelope["tool_response"] == {"error": "kaboom"}


# ---------------------------------------------------------------------------
# Timeout: process group is killed; blocking events fail closed, advisory
# events are logged and continue.
# ---------------------------------------------------------------------------


async def test_pre_tool_use_timeout_kills_process_group_and_denies(monkeypatch):
    proc = MagicMock()
    proc.pid = 9999
    proc.stdin = _FakeStdin()
    # asyncio.wait_for raises asyncio.TimeoutError, which is NOT the builtin
    # TimeoutError before 3.11 -- inject the real raised type so this exercises
    # the pre-3.11 teardown path rather than passing by luck on 3.11+.
    proc.stdout = _FakeStream(raises=asyncio.TimeoutError())
    proc.stderr = _FakeStream()
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
    proc.stdin = _FakeStdin()
    proc.stdout = _FakeStream(raises=asyncio.TimeoutError())
    proc.stderr = _FakeStream()
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
    proc = _mock_proc(0)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))

    hook = external_hook_adapter(event="PostToolUseFailure", command=["notify"])
    await hook(session_id="s-1", tool_name="bash", error=RuntimeError("disk full"))

    envelope = json.loads(proc.stdin.written.decode())
    assert envelope["tool_response"] == {"error": "disk full"}
    assert envelope["tool_name"] == "bash"


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

    command = [sys.executable, "-c", "pass"]
    record = compute_trust_record(command, cwd=str(tmp_path))
    write_user_settings({"trusted_hook_commands": [record]})
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=_mock_proc(0)))
    hook = external_hook_adapter(
        event="PreToolUse", command=command, source="imported:claude", cwd=str(tmp_path)
    )
    result = await hook("bash", {"command": ["ls"]})
    assert result.decision == "allow"


async def test_untrusted_imported_advisory_event_is_error_not_raise(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    hook = external_hook_adapter(event="SessionStart", command=["notify"], source="imported:codex")
    await hook(session_id="s-1")  # advisory: must not raise even though untrusted
    spawn.assert_not_called()


# ---------------------------------------------------------------------------
# Bounded subprocess output (Issue 2 fix): stdout AND stderr are capped while
# streaming, never after an unbounded `communicate()`-style full buffer.
# ---------------------------------------------------------------------------


async def test_read_capped_truncates_and_logs(caplog):
    from lionagi.hooks.external import _MAX_STDOUT_BYTES, _read_capped

    oversized = b"x" * (_MAX_STDOUT_BYTES + 1000)
    with caplog.at_level(logging.WARNING, logger="lionagi.hooks.external"):
        result = await _read_capped(_FakeStream(oversized), _MAX_STDOUT_BYTES, "stdout")
    assert len(result) == _MAX_STDOUT_BYTES
    assert result == oversized[:_MAX_STDOUT_BYTES]
    assert any("exceeded" in rec.message for rec in caplog.records)


async def test_read_capped_under_cap_returns_everything_untruncated():
    from lionagi.hooks.external import _read_capped

    data = b"hello hook output"
    result = await _read_capped(_FakeStream(data), 1_048_576, "stdout")
    assert result == data


async def test_read_capped_none_stream_returns_empty():
    from lionagi.hooks.external import _read_capped

    assert await _read_capped(None, 1_048_576, "stdout") == b""


async def test_stderr_is_capped_independently_of_stdout(monkeypatch):
    """Before the fix, the advertised cap applied to stdout only -- stderr
    was read via the same `communicate()` call but never capped at all."""
    from lionagi.hooks.external import _MAX_STDERR_BYTES

    oversized_stderr = b"e" * (_MAX_STDERR_BYTES + 500)
    proc = _mock_proc(2, stdout=b"", stderr=oversized_stderr)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))

    hook = external_hook_adapter(event="PreToolUse", command=["guard"])
    result = await hook("bash", {"command": ["ls"]})
    assert result.decision == "deny"
    # Exit-2's reason comes straight from decoded stderr -- unbounded, this
    # would be exactly len(oversized_stderr); capped, it is bounded by
    # _MAX_STDERR_BYTES (plus decode overhead is impossible since 'e' is 1 byte).
    assert len(result.reason.encode()) <= _MAX_STDERR_BYTES


async def test_real_subprocess_large_dual_pipe_output_does_not_hang():
    """End-to-end regression against a REAL subprocess (no mocking of
    create_subprocess_exec): a hook that writes well more than the per-pipe
    cap to BOTH stdout and stderr before ever reading stdin -- the classic
    write-fills-the-pipe-before-read deadlock shape -- must still complete
    promptly, proving the stdin write and the stdout/stderr drains run
    concurrently rather than sequentially."""
    script = (
        "import sys\n"
        "sys.stdout.buffer.write(b'o' * (2 * 1024 * 1024))\n"
        "sys.stdout.buffer.flush()\n"
        "sys.stderr.buffer.write(b'e' * (2 * 1024 * 1024))\n"
        "sys.stderr.buffer.flush()\n"
        "sys.stdin.buffer.read()\n"
        "sys.exit(0)\n"
    )
    hook = external_hook_adapter(
        event="PreToolUse", command=[sys.executable, "-c", script], timeout=15
    )
    started = time.monotonic()
    result = await asyncio.wait_for(hook("bash", {"command": ["ls"]}), timeout=20)
    elapsed = time.monotonic() - started
    assert result.decision == "allow"
    assert elapsed < 15, "hook should complete well within its own timeout, not hang"


# ---------------------------------------------------------------------------
# Non-serializable tool results (Issue 9 fix): serialize the envelope BEFORE
# spawning; ADR-0048 D1's tool_response string fallback so PostToolUse hooks
# still fire; no path leaves a spawned process handle unterminated.
# ---------------------------------------------------------------------------


class _Unserializable:
    """A plain object with no __dict__ shape json.dumps can handle."""

    def __repr__(self) -> str:
        return "<Unserializable obj>"


def test_json_safe_passes_through_serializable_values():
    from lionagi.hooks.external import _json_safe

    assert _json_safe({"a": 1}) == {"a": 1}
    assert _json_safe([1, 2, 3]) == [1, 2, 3]
    assert _json_safe("plain string") == "plain string"


def test_json_safe_stringifies_non_serializable_value():
    from lionagi.hooks.external import _json_safe

    obj = _Unserializable()
    assert _json_safe(obj) == str(obj)


def test_build_envelope_applies_string_fallback_to_non_serializable_tool_response():
    env = build_envelope(
        hook_event_name="PostToolUse",
        session_id="s",
        cwd="/",
        tool_name="bash",
        tool_input={"command": ["ls"]},
        tool_response=_Unserializable(),
    )
    assert env["tool_response"] == str(_Unserializable())
    json.dumps(env)  # must not raise -- the whole envelope is JSON-safe now


async def test_post_tool_use_with_non_serializable_result_still_spawns_and_fires(monkeypatch):
    """The exact Issue 9 shape: a non-JSON-serializable tool result must not
    prevent the PostToolUse hook from running -- it gets the documented
    string fallback and the subprocess still spawns normally."""
    proc = _mock_proc(0)
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    hook = external_hook_adapter(event="PostToolUse", command=["notify"])
    result = await hook("bash", {"command": ["ls"]}, _Unserializable(), None)

    spawn.assert_called_once()  # the hook subprocess DID run -- never skipped
    assert result is None  # exit 0, no reason -> advisory no-op, same as any allow
    envelope = json.loads(proc.stdin.written.decode())
    assert envelope["tool_response"] == str(_Unserializable())


async def test_execute_hook_serializes_before_spawning_never_orphans_a_process(monkeypatch):
    """If the envelope is somehow still not JSON-serializable by the time it
    reaches `_execute_hook` (defense in depth beyond build_envelope's own
    tool_response fallback), the subprocess must never be spawned at all.
    The old bug spawned first and lost the process handle when json.dumps
    raised afterward, orphaning it."""
    from lionagi.hooks.external import _execute_hook

    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    verdict = await _execute_hook(
        argv=["guard"],
        envelope={"session_id": "s", "cwd": "/", "bad": _Unserializable()},
        timeout=5.0,
        blocking=False,
    )
    assert verdict.outcome == "error"
    assert "not JSON-serializable" in verdict.reason
    spawn.assert_not_called()  # no process was ever spawned to orphan


async def test_execute_hook_serialization_failure_fails_closed_on_blocking_seam(monkeypatch):
    from lionagi.hooks.external import _execute_hook

    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    verdict = await _execute_hook(
        argv=["guard"],
        envelope={"bad": _Unserializable()},
        timeout=5.0,
        blocking=True,
    )
    assert verdict.outcome == "deny"
    spawn.assert_not_called()
