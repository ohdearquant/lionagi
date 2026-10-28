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
from uuid import uuid4

import pytest

from lionagi.cli._providers import AgentProfile, _parse_profile

# Unit tests: profile field parsing/validation


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


# Integration: _run_agent auto-resume wiring


def _make_branch_json(tmp_path: Path) -> tuple[str, Path]:
    from lionagi import Branch

    tmp_path.mkdir(parents=True, exist_ok=True)
    b = Branch()
    branch_id = str(b.id)
    p = tmp_path / f"{branch_id}.json"
    p.write_text(json.dumps(b.to_dict()))
    return branch_id, p


def _wire_run_directories(monkeypatch, agent_mod, tmp_path):
    """Allocate one diagnostic directory per leg and resume its actual snapshot."""
    runs = []

    def allocate():
        root = tmp_path / f"run-{len(runs)}"
        for name in ("artifacts", "stream", "branches"):
            (root / name).mkdir(parents=True, exist_ok=True)
        run = SimpleNamespace(
            run_id=f"r-{len(runs)}",
            state_root=root,
            artifact_root=root / "artifacts",
            stream_dir=root / "stream",
            branches_dir=root / "branches",
        )
        runs.append(run)
        return run

    def find_branch(branch_id):
        for run in reversed(runs):
            path = run.branches_dir / f"{branch_id}.json"
            if path.is_file():
                return run.run_id, path
        raise LookupError("the test branch has no saved snapshot")

    monkeypatch.setattr(agent_mod, "allocate_run", allocate)
    monkeypatch.setattr(agent_mod, "find_branch", find_branch)
    return runs


def _wire_owned_persistence(monkeypatch, agent_mod, *, session_id):
    """Keep unit wiring isolated while using the real owner and continuation API."""
    from lionagi.state.cli_workers import (
        _consume_worker_spawn_continuation,
        _WorkerSpawnOwner,
    )

    generation = str(uuid4())
    row = {"id": session_id, "status": "running", "cli_worker_generation": generation}
    contexts = []

    class UnitStore:
        mode = "native"

        def __init__(self):
            self.closed = False

        async def get_session(self, requested):
            if self.closed:
                raise RuntimeError("the unit store is closed")
            return dict(row) if requested == session_id else None

        async def list_pending_session_controls(self, requested):
            assert requested == session_id
            return []

        async def close(self):
            self.closed = True

    async def setup(branch, *, run_id=None, _worker_spawn_continuation=None, **kwargs):
        db = UnitStore()
        if _worker_spawn_continuation is None:
            owner = _WorkerSpawnOwner(
                db,
                session_id=session_id,
                generation_id=generation,
                run_id=run_id,
                branch_id=str(branch.id),
            )
        else:
            owner = await _consume_worker_spawn_continuation(
                _worker_spawn_continuation, db, run_id=run_id, branch_id=str(branch.id)
            )
        ctx = {
            "session_id": session_id,
            "db": db,
            "worker_spawn_owner": owner,
            "worker_spawn_db_closed": None,
            "worker_spawn_teardown_status": None,
            "shared_db_registered": False,
        }
        contexts.append(ctx)
        return ctx

    async def teardown(ctx, *, status="completed", defer_terminal=False, **kwargs):
        ctx["worker_spawn_db_closed"] = None
        await ctx["worker_spawn_owner"].aclose()
        if not defer_terminal:
            row["status"] = status
        await ctx["db"].close()
        ctx["worker_spawn_db_closed"] = ctx["db"]
        ctx["worker_spawn_teardown_status"] = status
        return status

    monkeypatch.setattr(agent_mod, "setup_agent_persist", setup)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", teardown)
    return contexts


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
        snapshot_dir = kw.get("snapshot_dir")
        if snapshot_dir is not None:
            Path(snapshot_dir).mkdir(parents=True, exist_ok=True)
            (Path(snapshot_dir) / f"{self.id}.json").write_text(json.dumps(self.to_dict()))
        return operate_side_effect(idx)

    monkeypatch.setattr(Branch, "operate", fake_operate)
    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)
    monkeypatch.setattr(agent_mod, "build_chat_model", lambda *a, **kw: "claude_code/sonnet")

    _sids = session_ids or ["sess-0"]
    call_count["contexts"] = _wire_owned_persistence(monkeypatch, agent_mod, session_id=_sids[0])
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
    call_count["runs"] = _wire_run_directories(monkeypatch, agent_mod, tmp_path)

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
        session_ids=["sess-timeout"],
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
    assert session_id == "sess-timeout"
    parent, child = call_count["contexts"]
    assert child["session_id"] == parent["session_id"] == session_id
    assert child["worker_spawn_owner"].generation_id == parent["worker_spawn_owner"].generation_id
    assert parent["worker_spawn_db_closed"] is parent["db"]
    assert child["worker_spawn_db_closed"] is child["db"]
    assert parent["db"].closed and child["db"].closed
    assert len({run.run_id for run in call_count["runs"]}) == 2
    assert len({run.state_root for run in call_count["runs"]}) == 2
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

    _sids = session_ids or ["sess-0"]
    call_count["contexts"] = _wire_owned_persistence(monkeypatch, agent_mod, session_id=_sids[0])
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
    call_count["runs"] = _wire_run_directories(monkeypatch, agent_mod, tmp_path)

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


# Real-persistence regression: the ADR-0035 terminal-guard race between an
# auto-resume leg's premature terminal stamp and the resumed leg's own
# teardown. Unlike _wire_agent_stubs (which stubs teardown_agent_persist
# entirely), these tests leave setup_agent_persist/teardown_agent_persist
# wired to the real StateDB-backed implementation, so a wiring regression in
# the defer_terminal plumbing shows up as a real TransitionRejectedError.


@pytest.fixture(autouse=True)
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


def _wire_agent_stubs_real_persist(
    monkeypatch,
    tmp_path: Path,
    *,
    operate_side_effect,
    injection_stats_by_leg: list[dict[str, int]] | None = None,
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
        if injection_stats_by_leg is not None:
            self.providers.stats.update(injection_stats_by_leg[idx])
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
    call_count["runs"] = _wire_run_directories(monkeypatch, agent_mod, tmp_path)

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
        monkeypatch,
        tmp_path,
        operate_side_effect=side_effect,
        injection_stats_by_leg=[
            {
                "recall_turns": 2,
                "blocks_injected": 3,
                "failed": 1,
                "writeback_records": 0,
                "writeback_failed": 0,
            },
            {
                "recall_turns": 4,
                "blocks_injected": 5,
                "failed": 0,
                "writeback_records": 6,
                "writeback_failed": 1,
            },
        ],
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
    node_metadata = s["node_metadata"]
    if isinstance(node_metadata, str):
        node_metadata = json.loads(node_metadata)
    assert {"lion_class", "pid", "pid_create_time"} <= node_metadata.keys()
    assert node_metadata["khive_injection"] == {
        "recall_turns": 6,
        "blocks_injected": 8,
        "failed": 1,
        "degraded_turns": 0,
        "writeback_records": 6,
        "writeback_failed": 1,
    }


@pytest.mark.asyncio
async def test_manual_resume_accumulates_persisted_injection_stats(
    monkeypatch,
    tmp_path,
    temp_db_path,
):
    """A later top-level resume seeds its totals from the durable session.

    The resumed teardown must add new counters instead of replacing history.
    """

    def side_effect(i):
        return f"leg-{i}"

    call_count = _wire_agent_stubs_real_persist(
        monkeypatch,
        tmp_path,
        operate_side_effect=side_effect,
        injection_stats_by_leg=[
            {
                "recall_turns": 2,
                "blocks_injected": 3,
                "failed": 1,
                "writeback_records": 5,
                "writeback_failed": 0,
            },
            {
                "recall_turns": 7,
                "blocks_injected": 11,
                "failed": 0,
                "writeback_records": 13,
                "writeback_failed": 1,
            },
        ],
    )

    from lionagi.cli.agent import _run_agent
    from lionagi.state.db import StateDB

    _result, _provider, branch_id, _status, first_session_id = await _run_agent(
        "claude_code/sonnet",
        "first",
    )
    _result, _provider, _branch_id, _status, resumed_session_id = await _run_agent(
        "claude_code/sonnet",
        "second",
        resume=branch_id,
    )

    assert call_count["n"] == 2
    assert resumed_session_id == first_session_id
    async with StateDB() as db:
        session = await db.get_session(resumed_session_id)
    node_metadata = session["node_metadata"]
    if isinstance(node_metadata, str):
        node_metadata = json.loads(node_metadata)
    assert node_metadata["khive_injection"] == {
        "recall_turns": 9,
        "blocks_injected": 14,
        "failed": 1,
        "degraded_turns": 0,
        "writeback_records": 18,
        "writeback_failed": 1,
    }


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


@pytest.mark.asyncio
async def test_auto_resume_keeps_the_legs_workload(monkeypatch, tmp_path):
    """The resumed leg runs in the workload class of the leg that timed out, not
    the profile's default: a light leg resumed as heavy would lose its light
    containment and count against the heavy cap."""
    import lionagi.cli.agent as agent_mod

    def side_effect(i):
        if i == 0:
            raise TimeoutError("boom")
        return "concluded"

    call_count, _insts, _timeouts = _wire_agent_stubs(
        monkeypatch, tmp_path, operate_side_effect=side_effect
    )
    built: list[str | None] = []

    def build_chat_model(*a, **kw):
        built.append(kw.get("workload"))
        return "claude_code/sonnet"

    monkeypatch.setattr(agent_mod, "build_chat_model", build_chat_model)
    resumed: list[str] = []
    add_workload_kwarg = agent_mod._add_workload_kwarg

    def record(extra, endpoint, workload):
        resumed.append(workload)
        add_workload_kwarg(extra, endpoint, workload)

    monkeypatch.setattr(agent_mod, "_add_workload_kwarg", record)

    from lionagi.cli.agent import _run_agent

    _result, _provider, _bid, status, _sid = await _run_agent(
        "claude_code/sonnet", "hello", timeout=30, resume_on_timeout=True, workload="light"
    )

    assert call_count["n"] == 2
    assert status == "completed"
    assert built == ["light"]
    assert resumed == ["light"]
