# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for --timeout deadline preamble: format, content, and injection into _run_agent."""

from __future__ import annotations

import re
import time

import pytest

from lionagi.cli._providers import build_deadline_preamble

# ── build_deadline_preamble unit tests ────────────────────────────────────────


def test_preamble_contains_deadline_tags():
    """Output is wrapped in [DEADLINE] … [/DEADLINE] markers."""
    preamble = build_deadline_preamble(300)
    assert preamble.startswith("[DEADLINE]\n")
    assert "[/DEADLINE]\n" in preamble


def test_preamble_contains_minutes():
    """300 seconds → 5 minutes in preamble."""
    preamble = build_deadline_preamble(300)
    assert "5 minutes" in preamble


def test_preamble_singular_minute():
    """60 seconds → '1 minute' (not '1 minutes')."""
    preamble = build_deadline_preamble(60)
    assert "1 minute " in preamble
    assert "1 minutes" not in preamble


def test_preamble_deadline_iso_format():
    """Preamble contains an ISO-8601 timestamp matching the expected pattern."""
    preamble = build_deadline_preamble(300)
    # Pattern: YYYY-MM-DDTHH:MM:SSZ
    assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", preamble), (
        f"No ISO timestamp found in preamble: {preamble!r}"
    )


def test_preamble_deadline_approximately_correct():
    """Deadline timestamp is within a few seconds of now + timeout."""
    before = time.time()
    preamble = build_deadline_preamble(300)
    after = time.time()

    # Extract the ISO timestamp
    m = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", preamble)
    assert m, f"No timestamp in preamble: {preamble!r}"

    from datetime import datetime, timezone

    deadline_ts = (
        datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    )

    # Allow 1 s below because strftime truncates sub-second fractions;
    # allow 5 s above for slow test machines.
    assert before + 300 - 1 <= deadline_ts <= after + 300 + 5, (
        f"Deadline {deadline_ts} not in expected range [{before + 299}, {after + 305}]"
    )


def test_preamble_contains_date_command_hint():
    """Preamble tells the agent how to check the current time."""
    preamble = build_deadline_preamble(300)
    assert "date -Iseconds" in preamble


def test_preamble_no_timeout_not_called():
    """When timeout is None, build_deadline_preamble is not called at all
    (guard test — importing the function is idempotent)."""
    # This is a trivial guard: the real test is in test_run_agent_* below.
    # We just verify the function is importable and callable.
    result = build_deadline_preamble(120)
    assert result  # non-empty string


def test_preamble_sub_minute_timeout_clamps_to_1():
    """Timeouts < 60 s still produce '1 minute' (not '0 minutes')."""
    preamble = build_deadline_preamble(30)
    assert "1 minute " in preamble


# ── Integration: preamble prepended to prompt in _run_agent ──────────────────
#
# Strategy: mock branch.operate at the *class* level using monkeypatch so we
# avoid Pydantic's __setattr__ guard.  The spy records what instruction was
# passed in before returning "done".


def _make_agent_mocks(monkeypatch, tmp_path, captured_instruction):
    """Wire all external-service stubs needed by _run_agent."""
    from types import SimpleNamespace

    import lionagi.cli.agent as agent_mod
    from lionagi import Branch

    # Spy on Branch.operate at the class level (avoids Pydantic __setattr__).
    async def spy_operate(self, instruction=None, **kw):
        captured_instruction.append(instruction or "")
        return "done"

    monkeypatch.setattr(Branch, "operate", spy_operate)

    # Stub iModel shutdown (called in finally block).
    from lionagi.service.manager import iModelManager

    async def fake_shutdown(self):
        pass

    monkeypatch.setattr(iModelManager, "shutdown", fake_shutdown)

    monkeypatch.setattr(
        agent_mod,
        "build_chat_model",
        lambda *a, **kw: "claude_code/sonnet",
    )
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)

    # _setup_live_persist returns None → _teardown_live_persist is a no-op.
    async def fake_setup(*a, **kw):
        return None

    async def fake_teardown(
        ctx, *, status="completed", exception=None, cwd=None, engine_session_uid=None
    ):
        return status

    monkeypatch.setattr(agent_mod, "setup_agent_persist", fake_setup)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", fake_teardown)
    monkeypatch.setattr(agent_mod, "save_last_branch_pointer", lambda *a, **kw: None)

    fake_run = SimpleNamespace(
        run_id="test-run",
        artifact_root=tmp_path / "artifacts",
        stream_dir=tmp_path / "stream",
        branches_dir=tmp_path / "branches",
    )
    monkeypatch.setattr(agent_mod, "allocate_run", lambda: fake_run)
    monkeypatch.setattr(
        agent_mod,
        "_provenance",
        SimpleNamespace(
            resolve_model_spec=lambda p, m: f"{p}/{m}",
            agent_definition_hash=lambda n: "abc123",
        ),
    )
    monkeypatch.setattr(
        agent_mod,
        "resolve_artifact_contract",
        lambda **_: None,
    )


@pytest.mark.asyncio
async def test_run_agent_prepends_preamble_when_timeout_set(monkeypatch, tmp_path):
    """_run_agent prepends the [DEADLINE] block to the user prompt."""
    captured_instruction: list[str] = []
    _make_agent_mocks(monkeypatch, tmp_path, captured_instruction)

    from lionagi.cli.agent import _run_agent

    await _run_agent(
        "claude_code/sonnet",
        "Write a function that adds two numbers.",
        timeout=300,
    )

    assert captured_instruction, "operate() was never called"
    instruction_text = captured_instruction[0]
    assert instruction_text.startswith("[DEADLINE]\n"), (
        f"Expected preamble prefix, got: {instruction_text[:120]!r}"
    )
    assert "Write a function that adds two numbers." in instruction_text


@pytest.mark.asyncio
async def test_run_agent_no_preamble_when_timeout_none(monkeypatch, tmp_path):
    """_run_agent does NOT inject a preamble when timeout is None."""
    captured_instruction: list[str] = []
    _make_agent_mocks(monkeypatch, tmp_path, captured_instruction)

    from lionagi.cli.agent import _run_agent

    await _run_agent(
        "claude_code/sonnet",
        "Write a function.",
        timeout=None,
    )

    assert captured_instruction, "operate() was never called"
    instruction_text = captured_instruction[0]
    assert not instruction_text.startswith("[DEADLINE]"), (
        f"Unexpected preamble injected when timeout=None: {instruction_text[:120]!r}"
    )
