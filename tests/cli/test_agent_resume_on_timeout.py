# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for opt-in auto-resume-once on a TIMEOUT terminal status.

Covers:
  * an invalid profile 'resume_on_timeout' is warned-and-ignored, not raised
  * 'resume_on_timeout' opt-in (CLI flag or profile field) fires exactly one
    auto-resume on a TIMEOUT terminal status, never on a failure or
    cancellation
  * auto-resume is off by default (no opt-in -> no resume)
  * a timeout on the resumed leg does not re-fire (bounded to once)
  * the final result surfaces the session id
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


def test_parse_profile_resume_on_timeout_once():
    text = "---\nresume_on_timeout: once\n---\nbody"
    profile = _parse_profile("reviewer", text)
    assert profile.resume_on_timeout is True


def test_parse_profile_resume_on_timeout_absent_is_false():
    profile = _parse_profile("reviewer", "---\nmodel: claude\n---\nbody")
    assert profile.resume_on_timeout is False


def test_parse_profile_resume_on_timeout_unrecognized_is_ignored(caplog):
    text = "---\nresume_on_timeout: sometimes\n---\nbody"
    with caplog.at_level(logging.WARNING):
        profile = _parse_profile("reviewer", text)
    assert profile.resume_on_timeout is False


@pytest.mark.parametrize("raw_yaml", ["true", "false", "yes", "sometimes"])
def test_parse_profile_resume_on_timeout_rejects_non_once_values(raw_yaml, caplog):
    """Only the literal string 'once' opts in; boolean aliases and other
    strings must warn-and-ignore (parse as False), not opt in."""
    text = f"---\nresume_on_timeout: {raw_yaml}\n---\nbody"
    with caplog.at_level(logging.WARNING):
        profile = _parse_profile("reviewer", text)
    assert profile.resume_on_timeout is False


# ---------------------------------------------------------------------------
# Integration: _run_agent auto-resume wiring
# ---------------------------------------------------------------------------


def _make_branch_json(tmp_path: Path) -> tuple[str, Path]:
    from lionagi import Branch

    tmp_path.mkdir(parents=True, exist_ok=True)
    b = Branch()
    branch_id = str(b.id)
    p = tmp_path / f"{branch_id}.json"
    p.write_text(json.dumps(b.to_dict()))
    return branch_id, p


def _wire_agent_stubs(
    monkeypatch,
    tmp_path: Path,
    *,
    operate_side_effect,
    profile: AgentProfile | None = None,
    session_ids: list[str] | None = None,
):
    """Wire all external I/O in _run_agent; operate_side_effect(call_index) -> result-or-raises."""
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.service.manager import iModelManager

    call_count = {"n": 0}
    captured_instructions: list[str] = []
    captured_timeouts: list[int | None] = []

    async def fake_operate(self, instruction=None, **kw):
        idx = call_count["n"]
        call_count["n"] += 1
        captured_instructions.append(instruction or "")
        captured_timeouts.append(kw.get("timeout"))
        return operate_side_effect(idx)

    monkeypatch.setattr(Branch, "operate", fake_operate)
    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)
    monkeypatch.setattr(agent_mod, "build_chat_model", lambda *a, **kw: "claude_code/sonnet")

    _sids = session_ids or ["sess-0", "sess-1"]

    async def fake_setup(*a, **kw):
        n = min(call_count["n"], len(_sids) - 1)
        return {"session_id": _sids[n]}

    async def fake_teardown(
        ctx,
        *,
        status="completed",
        exception=None,
        cwd=None,
        engine_session_uid=None,
        defer_terminal=False,
    ):
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

    # Any -r lookup (the auto-resume path) resolves to a valid serialized
    # branch, regardless of the requested id.
    _, resumable_path = _make_branch_json(tmp_path / "resumable")
    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", resumable_path))

    if profile is not None:
        monkeypatch.setattr(agent_mod, "load_agent_profile", lambda name: profile)

    return call_count, captured_instructions, captured_timeouts


def _reset_channel(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.propagate = True
    return logger


@pytest.mark.asyncio
async def test_auto_resume_disabled_by_default_no_opt_in(monkeypatch, tmp_path):
    """No resume_on_timeout opt-in anywhere -> a timeout stays a single leg."""

    def side_effect(i):
        raise TimeoutError("boom")

    call_count, _insts, _timeouts = _wire_agent_stubs(
        monkeypatch, tmp_path, operate_side_effect=side_effect
    )

    from lionagi.cli.agent import _run_agent

    _result, _provider, _bid, status, _sid = await _run_agent(
        "claude_code/sonnet", "hello", timeout=30
    )

    assert call_count["n"] == 1
    assert status == "timed_out"


@pytest.mark.asyncio
async def test_auto_resume_fires_once_on_timeout(monkeypatch, tmp_path, caplog):
    """resume_on_timeout=True: first leg times out, resumed leg completes."""

    def side_effect(i):
        if i == 0:
            raise TimeoutError("boom")
        return "concluded"

    call_count, insts, timeouts = _wire_agent_stubs(
        monkeypatch,
        tmp_path,
        operate_side_effect=side_effect,
        session_ids=["sess-timeout", "sess-resumed"],
    )

    from lionagi.cli.agent import _run_agent

    _reset_channel("lionagi.cli.warn")
    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        result, _provider, _bid, status, session_id = await _run_agent(
            "claude_code/sonnet",
            "hello",
            timeout=30,
            resume_on_timeout=True,
        )

    assert call_count["n"] == 2
    assert status == "completed"
    assert result == "concluded"
    assert session_id == "sess-resumed"
    assert insts[1].endswith("continue and conclude the task")
    assert timeouts == [30, 30]  # resumed leg gets the same timeout budget
    warn_text = " ".join(rec.message for rec in caplog.records)
    assert "auto-resume" in warn_text
    assert "sess-timeout" in warn_text


@pytest.mark.asyncio
async def test_auto_resume_does_not_fire_on_failure(monkeypatch, tmp_path):
    """A plain failure (not a timeout) never triggers the auto-resume path."""

    def side_effect(i):
        raise RuntimeError("boom")

    call_count, _insts, _timeouts = _wire_agent_stubs(
        monkeypatch, tmp_path, operate_side_effect=side_effect
    )

    from lionagi.cli.agent import _run_agent

    with pytest.raises(RuntimeError):
        await _run_agent(
            "claude_code/sonnet",
            "hello",
            timeout=30,
            resume_on_timeout=True,
        )

    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_auto_resume_does_not_fire_on_cancellation(monkeypatch, tmp_path):
    """A cancelled run never triggers the auto-resume path."""
    import asyncio

    def side_effect(i):
        raise asyncio.CancelledError()

    call_count, _insts, _timeouts = _wire_agent_stubs(
        monkeypatch, tmp_path, operate_side_effect=side_effect
    )

    from lionagi.cli.agent import _run_agent

    with pytest.raises(asyncio.CancelledError):
        await _run_agent(
            "claude_code/sonnet",
            "hello",
            timeout=30,
            resume_on_timeout=True,
        )

    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_resumed_leg_timeout_does_not_refire(monkeypatch, tmp_path, caplog):
    """The resumed leg also times out -> terminates normally, no third attempt."""

    def side_effect(i):
        raise TimeoutError("boom")

    call_count, _insts, _timeouts = _wire_agent_stubs(
        monkeypatch, tmp_path, operate_side_effect=side_effect
    )

    from lionagi.cli.agent import _run_agent

    _result, _provider, _bid, status, _sid = await _run_agent(
        "claude_code/sonnet",
        "hello",
        timeout=30,
        resume_on_timeout=True,
    )

    assert call_count["n"] == 2  # original + exactly one auto-resume, never a third
    assert status == "timed_out"


def _wire_agent_stubs_real_chat_model(
    monkeypatch,
    tmp_path: Path,
    *,
    operate_side_effect,
    profile: AgentProfile | None = None,
    session_ids: list[str] | None = None,
):
    """Like _wire_agent_stubs, but build_chat_model returns a *real* iModel
    (not a placeholder string) so branch.chat_model.endpoint.config reflects
    the model actually resolved for each leg. Needed to pin the model an
    auto-resumed leg actually runs with, not just what operate() was told."""
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch, iModel
    from lionagi.service.manager import iModelManager

    call_count = {"n": 0}
    captured_models: list[str | None] = []

    async def fake_operate(self, instruction=None, **kw):
        idx = call_count["n"]
        call_count["n"] += 1
        captured_models.append(self.chat_model.endpoint.config.kwargs.get("model"))
        snapshot_dir = kw.get("snapshot_dir")
        if snapshot_dir is not None:
            Path(snapshot_dir).mkdir(parents=True, exist_ok=True)
            (Path(snapshot_dir) / f"{self.id}.json").write_text(json.dumps(self.to_dict()))
        return operate_side_effect(idx)

    monkeypatch.setattr(Branch, "operate", fake_operate)
    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)
    monkeypatch.setattr(
        agent_mod,
        "build_chat_model",
        lambda provider, model, *a, **kw: iModel(provider=provider, model=model, api_key="dummy"),
    )

    _sids = session_ids or ["sess-0", "sess-1"]

    async def fake_setup(*a, **kw):
        n = min(call_count["n"], len(_sids) - 1)
        return {"session_id": _sids[n]}

    async def fake_teardown(
        ctx,
        *,
        status="completed",
        exception=None,
        cwd=None,
        engine_session_uid=None,
        defer_terminal=False,
    ):
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

    # The auto-resume path snapshots the branch itself (real Branch.to_dict())
    # into snapshot_dir; find_branch must resolve to that real snapshot so
    # Branch.from_dict on resume carries the actual chat_model config forward,
    # not a fixture stand-in.
    def fake_find_branch(bid):
        snapshot_path = tmp_path / "branches" / f"{bid}.json"
        return "run-x", snapshot_path

    monkeypatch.setattr(agent_mod, "find_branch", fake_find_branch)

    if profile is not None:
        monkeypatch.setattr(agent_mod, "load_agent_profile", lambda name: profile)

    return call_count, captured_models


@pytest.mark.asyncio
async def test_auto_resume_preserves_explicit_model_over_profile_default(monkeypatch, tmp_path):
    """Explicit --model + a profile with a *different* default model: the
    resumed leg must keep the explicit model, not silently fall back to the
    profile's model (the auto-resume call must not pass model_str=None while
    still forwarding agent_name)."""
    profile = AgentProfile(name="profile-with-different-model", model="claude_code/haiku")

    def side_effect(i):
        if i == 0:
            raise TimeoutError("first leg timed out")
        return "concluded"

    call_count, captured_models = _wire_agent_stubs_real_chat_model(
        monkeypatch, tmp_path, operate_side_effect=side_effect, profile=profile
    )

    from lionagi.cli.agent import _run_agent

    _result, _provider, _bid, status, _sid = await _run_agent(
        "claude_code/opus",
        "hello",
        agent_name="profile-with-different-model",
        timeout=30,
        resume_on_timeout=True,
    )

    assert call_count["n"] == 2
    assert status == "completed"
    assert captured_models == ["opus", "opus"], (
        f"resumed leg must keep the explicit CLI model, got {captured_models!r}"
    )


@pytest.mark.asyncio
async def test_profile_resume_on_timeout_opts_in(monkeypatch, tmp_path):
    """profile 'resume_on_timeout: once' opts in without any CLI flag."""
    profile = AgentProfile(name="reviewer", model="claude_code/sonnet", resume_on_timeout=True)

    def side_effect(i):
        if i == 0:
            raise TimeoutError("boom")
        return "concluded"

    call_count, _insts, _timeouts = _wire_agent_stubs(
        monkeypatch, tmp_path, operate_side_effect=side_effect, profile=profile
    )

    from lionagi.cli.agent import _run_agent

    _result, _provider, _bid, status, _sid = await _run_agent(
        None, "hello", agent_name="reviewer", timeout=30
    )

    assert call_count["n"] == 2
    assert status == "completed"


# ---------------------------------------------------------------------------
# Real-persistence regression: the ADR-0035 terminal-guard race between an
# auto-resume leg's premature terminal stamp and the resumed leg's own
# teardown. Unlike _wire_agent_stubs (which stubs teardown_agent_persist
# entirely), these tests leave setup_agent_persist/teardown_agent_persist
# wired to the real StateDB-backed implementation, so a wiring regression in
# the defer_terminal plumbing shows up as a real TransitionRejectedError.
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


def _wire_agent_stubs_real_persist(
    monkeypatch,
    tmp_path: Path,
    *,
    operate_side_effect,
):
    """Like _wire_agent_stubs_real_chat_model, but setup_agent_persist /
    teardown_agent_persist are left untouched (the real StateDB-backed
    implementation), so the auto-resume terminal-status race is exercised
    for real instead of through a stub that echoes status unconditionally."""
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch, iModel
    from lionagi.service.manager import iModelManager

    call_count = {"n": 0}

    async def fake_operate(self, instruction=None, **kw):
        idx = call_count["n"]
        call_count["n"] += 1
        snapshot_dir = kw.get("snapshot_dir")
        if snapshot_dir is not None:
            Path(snapshot_dir).mkdir(parents=True, exist_ok=True)
            (Path(snapshot_dir) / f"{self.id}.json").write_text(json.dumps(self.to_dict()))
        return operate_side_effect(idx)

    monkeypatch.setattr(Branch, "operate", fake_operate)
    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)
    monkeypatch.setattr(
        agent_mod,
        "build_chat_model",
        lambda provider, model, *a, **kw: iModel(provider=provider, model=model, api_key="dummy"),
    )
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

    def fake_find_branch(bid):
        snapshot_path = tmp_path / "branches" / f"{bid}.json"
        return "run-x", snapshot_path

    monkeypatch.setattr(agent_mod, "find_branch", fake_find_branch)
    return call_count


@pytest.mark.asyncio
async def test_auto_resume_real_teardown_no_terminal_guard_rejection(
    monkeypatch, tmp_path, temp_db_path, caplog
):
    """Regression: leg-1 times out (resume_on_timeout=True), leg-2 concludes
    with a durable response. Exercises the REAL teardown/StateDB path
    end-to-end: without the defer_terminal fix, leg-1's teardown stamps the
    session terminal ('timed_out') before the auto-resume decision, and
    leg-2's own teardown then hits the ADR-0035 guard, losing the durable
    'concluded' response behind a TransitionRejectedError logged at warning
    level."""

    def side_effect(i):
        if i == 0:
            raise TimeoutError("first leg timed out")
        return "concluded"

    call_count = _wire_agent_stubs_real_persist(
        monkeypatch, tmp_path, operate_side_effect=side_effect
    )

    from lionagi.cli.agent import _run_agent
    from lionagi.state.db import StateDB

    with caplog.at_level(logging.WARNING):
        result, _provider, _bid, status, session_id = await _run_agent(
            "claude_code/sonnet",
            "hello",
            timeout=30,
            resume_on_timeout=True,
        )

    assert call_count["n"] == 2
    assert status == "completed"
    assert result == "concluded"
    assert not any(
        "TransitionRejectedError" in rec.message or "teardown failed" in rec.message
        for rec in caplog.records
    )

    async with StateDB() as db:
        s = await db.get_session(session_id)
    assert s is not None
    assert s["status"] == "completed"
    assert s["ended_at"] is not None


@pytest.mark.asyncio
async def test_no_auto_resume_opt_in_real_teardown_stamps_timed_out(
    monkeypatch, tmp_path, temp_db_path
):
    """resume_on_timeout=False (default): defer_terminal must NOT fire, and
    the real teardown stamps a genuine 'timed_out' terminal status exactly
    as before this fix."""

    def side_effect(i):
        raise TimeoutError("boom")

    call_count = _wire_agent_stubs_real_persist(
        monkeypatch, tmp_path, operate_side_effect=side_effect
    )

    from lionagi.cli.agent import _run_agent
    from lionagi.state.db import StateDB

    _result, _provider, _bid, status, session_id = await _run_agent(
        "claude_code/sonnet", "hello", timeout=30
    )

    assert call_count["n"] == 1
    assert status == "timed_out"

    async with StateDB() as db:
        s = await db.get_session(session_id)
    assert s is not None
    assert s["status"] == "timed_out"
    assert s["ended_at"] is not None
