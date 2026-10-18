# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Characterization: an imported external hook re-resolves its trusted
executable and re-reads its trust record on every invocation -- never once at
registration -- so a change of trust or of the executable's content refuses the
next call. Pins what the caller sees on each seam when that refusal fires."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from lionagi.hooks.external import compute_trust_record, external_hook_adapter
from lionagi.plugins._user_settings import write_user_settings
from lionagi.protocols.action.tool_hooks import ToolPreDecision


def _write_guard(directory: Path, marker: Path, *, exit_code: int = 0) -> Path:
    """A minimal hook: drain the envelope, record that it ran, then exit."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "guard"
    script.write_text(f'#!/bin/sh\ncat > /dev/null\nprintf ran >> "{marker}"\nexit {exit_code}\n')
    script.chmod(0o755)
    return script


def _invocations(marker: Path) -> int:
    return marker.read_text().count("ran") if marker.is_file() else 0


class _NoOutputStream:
    """Async stream already at EOF: the fake hook prints nothing."""

    async def read(self, n: int = -1) -> bytes:
        return b""


class _DiscardingStdin:
    """Accepts the envelope write without keeping it."""

    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


def _mock_proc(returncode: int = 0) -> MagicMock:
    """A fake hook process that exits *returncode* with no output, so a call
    that wrongly reaches execution completes instead of hanging."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.pid = None  # no host pid, so the process-group probe reads nothing
    proc.stdin = _DiscardingStdin()
    proc.stdout = _NoOutputStream()
    proc.stderr = _NoOutputStream()
    proc.wait = AsyncMock(return_value=returncode)
    return proc


async def test_imported_hook_rechecks_trust_on_every_invocation(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    approved = tmp_path / "approved"
    marker = tmp_path / "marker"
    _write_guard(approved, marker)
    record = compute_trust_record(["./guard"], str(approved))
    write_user_settings({"trusted_hook_commands": [record]})

    hook = external_hook_adapter(
        event="PreToolUse", command=["./guard"], source="imported:claude", cwd=str(approved)
    )

    first = await hook("bash", {"command": ["ls"]})
    assert first == ToolPreDecision(decision="allow", updated_input=None)
    assert _invocations(marker) == 1

    write_user_settings({"trusted_hook_commands": []})
    second = await hook("bash", {"command": ["ls"]})
    assert second.decision == "deny"
    assert "untrusted" in second.reason
    assert _invocations(marker) == 1

    write_user_settings({"trusted_hook_commands": [record]})
    third = await hook("bash", {"command": ["ls"]})
    assert third == ToolPreDecision(decision="allow", updated_input=None)
    assert _invocations(marker) == 2


async def test_content_change_between_invocations_refuses_the_next_call(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    approved = tmp_path / "approved"
    marker = tmp_path / "marker"
    _write_guard(approved, marker)
    record = compute_trust_record(["./guard"], str(approved))
    write_user_settings({"trusted_hook_commands": [record]})

    hook = external_hook_adapter(
        event="PreToolUse", command=["./guard"], source="imported:claude", cwd=str(approved)
    )
    assert (await hook("bash", {"command": ["ls"]})).decision == "allow"
    assert _invocations(marker) == 1

    _write_guard(approved, marker, exit_code=2)

    second = await hook("bash", {"command": ["ls"]})
    assert second.decision == "deny"
    assert "different executable" in second.reason
    assert _invocations(marker) == 1


async def test_executable_removed_after_approval_refuses_the_next_call(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    approved = tmp_path / "approved"
    marker = tmp_path / "marker"
    script = _write_guard(approved, marker)
    record = compute_trust_record(["./guard"], str(approved))
    write_user_settings({"trusted_hook_commands": [record]})

    hook = external_hook_adapter(
        event="PreToolUse", command=["./guard"], source="imported:claude", cwd=str(approved)
    )
    assert (await hook("bash", {"command": ["ls"]})).decision == "allow"

    script.unlink()

    second = await hook("bash", {"command": ["ls"]})
    assert second.decision == "deny"
    assert "not found or not executable" in second.reason
    assert _invocations(marker) == 1


async def test_sourceless_hook_is_not_trust_gated_on_repeated_invocations(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / "project"
    marker = tmp_path / "marker"
    _write_guard(project, marker)

    hook = external_hook_adapter(
        event="PreToolUse", command=["./guard"], source=None, cwd=str(project)
    )
    assert (await hook("bash", {"command": ["ls"]})).decision == "allow"
    assert (await hook("bash", {"command": ["ls"]})).decision == "allow"
    assert _invocations(marker) == 2


async def test_unapproved_imported_pre_tool_use_denies_without_spawning_on_every_call(
    monkeypatch, tmp_path
):
    """The configured executable is present and resolvable, but carries no
    approval record: the refusal must come from the trust match, not from a
    resolution failure, on both calls, with no spawn."""
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / "project"
    _write_guard(project, tmp_path / "marker")

    spawn = AsyncMock(return_value=_mock_proc())
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    hook = external_hook_adapter(
        event="PreToolUse", command=["./guard"], source="imported:claude", cwd=str(project)
    )

    for result in (
        await hook("bash", {"command": ["ls"]}),
        await hook("bash", {"command": ["ls"]}),
    ):
        assert isinstance(result, ToolPreDecision)
        assert result.decision == "deny"
        assert "untrusted" in result.reason
        assert "not found" not in result.reason
        assert "li hooks trust" in result.reason
    spawn.assert_not_called()


async def test_unapproved_imported_user_prompt_submit_denial_raises_permission_error(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / "project"
    _write_guard(project, tmp_path / "marker")

    spawn = AsyncMock(return_value=_mock_proc())
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    hook = external_hook_adapter(
        event="UserPromptSubmit", command=["./guard"], source="imported:claude", cwd=str(project)
    )

    with pytest.raises(PermissionError) as raised:
        await hook(session_id="s-1", prompt="do a thing")
    assert type(raised.value) is PermissionError
    assert "untrusted" in str(raised.value)
    assert "not found" not in str(raised.value)
    spawn.assert_not_called()


async def test_unapproved_imported_advisory_seam_logs_and_returns_without_raising(
    monkeypatch, tmp_path, caplog
):
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / "project"
    _write_guard(project, tmp_path / "marker")

    spawn = AsyncMock(return_value=_mock_proc())
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    hook = external_hook_adapter(
        event="SessionStart", command=["./guard"], source="imported:codex", cwd=str(project)
    )

    with caplog.at_level(logging.WARNING, logger="lionagi.hooks.external"):
        assert await hook(session_id="s-1") is None
    assert "untrusted" in caplog.text
    assert "not found" not in caplog.text
    spawn.assert_not_called()
