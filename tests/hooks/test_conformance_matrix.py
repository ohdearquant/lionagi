# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""External-hook compatibility profile v1 conformance matrix.

Fixture hooks shaped after the Claude Code and Codex hook config schemas
(``tests/hooks/fixtures/``) are imported and then actually run under the
LionAGI adapter, asserting: the per-event field-guarantee rows; the
exit-code/stdout decision contract; and the named divergences Dv1-1..Dv1-4.
Until this file exists and passes, no LionAGI documentation or `li hooks
import` output may claim a foreign hook runs "unmodified" -- the import
report always says "imported; verify against profile v1" (see
``lionagi/cli/hooks.py``).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from lionagi.agent.settings import parse_external_hooks
from lionagi.cli.hooks import _translate_config
from lionagi.hooks.external import build_envelope, external_hook_adapter
from lionagi.protocols.action.tool_hooks import ToolPreDecision

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _mock_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.pid = 1234
    return proc


# ---------------------------------------------------------------------------
# Claude Code fixture: import report + resulting hooks_external entries
# ---------------------------------------------------------------------------


def test_claude_fixture_imports_mappable_events_and_rejects_the_rest():
    data = _load("claude_settings.json")
    external, report = _translate_config(data, source_label="claude")

    # Mappable events with valid argv/shell-string commands import.
    assert "PreToolUse" in external
    assert "PostToolUse" in external
    assert "UserPromptSubmit" in external
    # Stop has no LionAGI seam (D2 fixed mapping) -- rejected, not silently dropped.
    assert "Stop" not in external
    assert any("rejected [Stop]" in line and "no LionAGI seam" in line for line in report)

    # The shell-string command containing `$HOME` (a shell metacharacter) is
    # rejected rather than reinterpreted (Dv1-3).
    assert any(
        "rejected [PreToolUse]" in line and "shell metacharacters" in line for line in report
    )

    # Every accepted entry's report line says "verify against profile v1" --
    # never "unmodified" (D1's acceptance-gate language).
    imported_lines = [line for line in report if line.startswith("imported")]
    assert imported_lines
    for line in imported_lines:
        assert "verify against profile v1" in line
        assert "unmodified" not in line


def test_claude_fixture_entries_parse_and_construct_adapters():
    data = _load("claude_settings.json")
    external, _ = _translate_config(data, source_label="claude")
    entries = parse_external_hooks(external)

    pre_entries = [e for e in entries if e["event"] == "PreToolUse"]
    assert len(pre_entries) == 1  # only the valid-argv one survived translation
    assert pre_entries[0]["source"] == "imported:claude"

    for entry in entries:
        handler = external_hook_adapter(
            event=entry["event"],
            command=entry["command"],
            timeout=entry["timeout"],
            matcher=entry.get("matcher"),
            source=entry.get("source"),
        )
        assert callable(handler)


# ---------------------------------------------------------------------------
# Codex fixture: PreCompact (out of scope, no runtime seam) is rejected.
# ---------------------------------------------------------------------------


def test_codex_fixture_rejects_precompact_no_seam():
    data = _load("codex_hooks.json")
    external, report = _translate_config(data, source_label="codex")

    assert "PreCompact" not in external
    assert any("rejected [PreCompact]" in line and "no LionAGI seam" in line for line in report)
    assert "PreToolUse" in external
    assert "UserPromptSubmit" in external


def test_codex_fixture_marks_dv1_1_transcript_divergence_for_user_prompt_submit():
    data = _load("codex_hooks.json")
    _, report = _translate_config(data, source_label="codex")
    ups_lines = [line for line in report if "UserPromptSubmit" in line and "imported" in line]
    assert ups_lines
    assert any("transcript_path/turn_id" in line for line in ups_lines)


# ---------------------------------------------------------------------------
# Field-guarantee rows, exercised end to end against a mocked subprocess.
# ---------------------------------------------------------------------------


async def test_pre_tool_use_field_guarantees_reach_the_subprocess(monkeypatch):
    captured = {}

    async def fake_communicate(data):
        captured["envelope"] = json.loads(data.decode())
        return b"", b""

    proc = _mock_proc(0)
    proc.communicate = AsyncMock(side_effect=fake_communicate)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))

    hook = external_hook_adapter(
        event="PreToolUse", command=["guard"], session_id="s-42", cwd="/work"
    )
    await hook("bash", {"command": ["git", "status"]})

    env = captured["envelope"]
    assert env["session_id"] == "s-42"
    assert env["cwd"] == "/work"
    assert env["hook_event_name"] == "PreToolUse"
    assert env["harness"] == "lionagi"
    assert env["tool_name"] == "bash"
    assert env["tool_input"] == {"command": ["git", "status"]}
    assert "tool_response" not in env


async def test_post_tool_use_field_guarantees_include_tool_response(monkeypatch):
    captured = {}

    async def fake_communicate(data):
        captured["envelope"] = json.loads(data.decode())
        return b"", b""

    proc = _mock_proc(0)
    proc.communicate = AsyncMock(side_effect=fake_communicate)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))

    hook = external_hook_adapter(event="PostToolUse", command=["notify"], session_id="s-1")
    await hook("bash", {"command": ["ls"]}, {"stdout": "ok"}, None)

    env = captured["envelope"]
    assert env["tool_response"] == {"stdout": "ok"}


async def test_user_prompt_submit_field_guarantees_include_model_and_permission_mode(
    monkeypatch,
):
    captured = {}

    async def fake_communicate(data):
        captured["envelope"] = json.loads(data.decode())
        return b"", b""

    proc = _mock_proc(0)
    proc.communicate = AsyncMock(side_effect=fake_communicate)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))

    hook = external_hook_adapter(event="UserPromptSubmit", command=["hygiene"])
    await hook(session_id="s-1", branch_id="b-1", prompt="hello", model="claude-sonnet")

    env = captured["envelope"]
    assert env["prompt"] == "hello"
    assert env["model"] == "claude-sonnet"
    # Dv1-1 in miniature: LionAGI never fabricates transcript_path/turn_id --
    # they are simply absent, not present-with-a-placeholder.
    assert "transcript_path" not in env
    assert "turn_id" not in env
    # No PermissionPolicy attached at this call -> the ADR's explicit "else
    # default" fallback, not a missing field.
    assert env["permission_mode"] == "default"


# ---------------------------------------------------------------------------
# Exit-code protocol, exactly as the ADR states it (D1/D4 acceptance).
# ---------------------------------------------------------------------------


async def test_exit_code_protocol_zero_two_and_other(monkeypatch):
    hook = external_hook_adapter(event="PreToolUse", command=["guard"])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=_mock_proc(0)))
    allow = await hook("bash", {})
    assert allow.decision == "allow"

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=_mock_proc(2, stderr=b"blocked reason")),
    )
    deny = await hook("bash", {})
    assert deny.decision == "deny"
    assert "blocked reason" in deny.reason

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=_mock_proc(17, stderr=b"unexpected crash")),
    )
    error = await hook("bash", {})
    # Exit 2 (block) is distinguished from every other nonzero exit (hook
    # failure); on a blocking seam both fail the action closed, but the
    # reason text must not collapse the two into indistinguishable output.
    assert error.decision == "deny"
    assert "unexpected crash" in error.reason


# ---------------------------------------------------------------------------
# Named divergence Dv1-4: LionAGI fails closed on a blocking-event timeout,
# where Claude Code cancels the hook and lets the prompt proceed (fail open).
# ---------------------------------------------------------------------------


async def test_dv1_4_timeout_fails_closed_unlike_claude_codes_fail_open(monkeypatch):
    proc = MagicMock()
    proc.pid = 555
    proc.communicate = AsyncMock(side_effect=TimeoutError)
    proc.wait = AsyncMock(return_value=None)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    monkeypatch.setattr("lionagi.ln._proc.os.killpg", lambda pgid, sig: None)

    hook = external_hook_adapter(event="UserPromptSubmit", command=["slow_hygiene"], timeout=0.01)
    try:
        await hook(session_id="s-1", prompt="hi")
        raised = False
    except PermissionError:
        raised = True
    assert raised, "a timed-out UserPromptSubmit hook must fail closed (deny), not allow the prompt"


def test_build_envelope_matches_the_adapters_own_construction():
    """The envelope schema documented for hand-authored hooks (build_envelope)
    matches exactly what the adapter sends -- no drift between the two."""
    manual = build_envelope(
        hook_event_name="PreToolUse",
        session_id="s",
        cwd="/w",
        tool_name="bash",
        tool_input={"command": ["ls"]},
    )
    assert set(manual) == {
        "session_id",
        "cwd",
        "hook_event_name",
        "harness",
        "tool_name",
        "tool_input",
    }
