# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for per-profile default --timeout.

Covers:
  * profile 'timeout' used as the --timeout default when the flag is absent
  * an explicit --timeout still beats the profile value
  * an invalid profile 'timeout' is warned-and-ignored, not raised
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lionagi.cli._providers import AgentProfile, _parse_profile

# ---------------------------------------------------------------------------
# Unit tests: profile field parsing/validation
# ---------------------------------------------------------------------------


def test_parse_profile_timeout_valid():
    text = "---\ntimeout: 1800\n---\nbody"
    profile = _parse_profile("reviewer", text)
    assert profile.timeout == 1800


def test_parse_profile_timeout_invalid_non_numeric_is_ignored(caplog):
    text = "---\ntimeout: not-a-number\n---\nbody"
    with caplog.at_level(logging.WARNING):
        profile = _parse_profile("reviewer", text)
    assert profile.timeout is None


def test_parse_profile_timeout_non_positive_is_ignored(caplog):
    text = "---\ntimeout: -5\n---\nbody"
    with caplog.at_level(logging.WARNING):
        profile = _parse_profile("reviewer", text)
    assert profile.timeout is None


def test_parse_profile_timeout_absent_is_none():
    profile = _parse_profile("reviewer", "---\nmodel: claude\n---\nbody")
    assert profile.timeout is None


@pytest.mark.parametrize("raw_yaml", ["true", "false", "1.9", "3.0"])
def test_parse_profile_timeout_rejects_bool_and_float(raw_yaml, caplog):
    """YAML booleans and floats must warn-and-ignore, not coerce (bool is an
    int subclass in Python; int(1.9) would silently truncate to 1)."""
    text = f"---\ntimeout: {raw_yaml}\n---\nbody"
    with caplog.at_level(logging.WARNING):
        profile = _parse_profile("reviewer", text)
    assert profile.timeout is None


# ---------------------------------------------------------------------------
# Integration: _run_agent precedence (explicit flag > profile > built-in default)
# ---------------------------------------------------------------------------


def _wire_agent_stubs(
    monkeypatch,
    tmp_path: Path,
    *,
    profile: AgentProfile | None = None,
):
    """Wire all external I/O in _run_agent; captures the timeout passed to operate()."""
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.service.manager import iModelManager

    captured_timeouts: list[int | None] = []

    async def fake_operate(self, instruction=None, **kw):
        captured_timeouts.append(kw.get("timeout"))
        return "done"

    monkeypatch.setattr(Branch, "operate", fake_operate)
    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)
    monkeypatch.setattr(agent_mod, "build_chat_model", lambda *a, **kw: "claude_code/sonnet")

    async def fake_setup(*a, **kw):
        return {"session_id": "sess-0"}

    async def fake_teardown(ctx, *, status="completed", exception=None):
        return status

    monkeypatch.setattr(agent_mod, "setup_agent_persist", fake_setup)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", fake_teardown)
    monkeypatch.setattr(agent_mod, "save_last_branch_pointer", lambda *a, **kw: None)
    monkeypatch.setattr(
        agent_mod,
        "_provenance",
        SimpleNamespace(
            resolve_model_spec=lambda p, m: f"{p}/{m}",
            agent_definition_hash=lambda n: "abc",
        ),
    )
    monkeypatch.setattr(agent_mod, "resolve_artifact_contract", lambda **_: None)
    monkeypatch.setattr(
        agent_mod,
        "allocate_run",
        lambda: SimpleNamespace(
            run_id="r",
            artifact_root=tmp_path / "artifacts",
            stream_dir=tmp_path / "stream",
            branches_dir=tmp_path / "branches",
        ),
    )

    if profile is not None:
        monkeypatch.setattr(agent_mod, "load_agent_profile", lambda name: profile)

    return captured_timeouts


@pytest.mark.asyncio
async def test_profile_timeout_used_when_flag_absent(monkeypatch, tmp_path):
    profile = AgentProfile(name="reviewer", model="claude_code/sonnet", timeout=999)
    timeouts = _wire_agent_stubs(monkeypatch, tmp_path, profile=profile)

    from lionagi.cli.agent import _run_agent

    await _run_agent(None, "hello", agent_name="reviewer", timeout=None)

    assert timeouts == [999]


@pytest.mark.asyncio
async def test_explicit_timeout_beats_profile(monkeypatch, tmp_path):
    profile = AgentProfile(name="reviewer", model="claude_code/sonnet", timeout=999)
    timeouts = _wire_agent_stubs(monkeypatch, tmp_path, profile=profile)

    from lionagi.cli.agent import _run_agent

    await _run_agent(None, "hello", agent_name="reviewer", timeout=42)

    assert timeouts == [42]


@pytest.mark.asyncio
async def test_no_profile_no_flag_timeout_stays_none(monkeypatch, tmp_path):
    timeouts = _wire_agent_stubs(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    await _run_agent("claude_code/sonnet", "hello", timeout=None)

    assert timeouts == [None]
